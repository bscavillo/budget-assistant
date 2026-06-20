"""AI helpers backed by a local Ollama model.

The model classifies imported bank transactions into standard spending
categories. It never raises on a missing Ollama server; callers receive a
graceful fallback instead so the rest of the app keeps working offline.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import ollama

MODEL = os.environ.get("BUDGET_OLLAMA_MODEL", "gemma4:latest")

# How many classification batches to send to Ollama at once. Batches are
# independent, so overlapping them hides per-request latency; the local model
# still serialises actual inference, so very high values give little extra.
_MAX_CONCURRENCY = max(1, int(os.environ.get("BUDGET_OLLAMA_CONCURRENCY", "4")))


def _chat_json(messages):
    """Send a chat request forcing a JSON response; return a parsed dict.

    Returns ``{"_error": "..."}`` if Ollama is unavailable or the response is
    not valid JSON, so callers can degrade gracefully.
    """
    try:
        response = ollama.chat(
            model=MODEL,
            messages=messages,
            format="json",
            options={"num_ctx": 4096},
        )
        return json.loads(response["message"]["content"])
    except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
        return {"_error": str(exc)}


# Standard personal-finance categories the AI maps transactions into.
STANDARD_CATEGORIES = [
    "Groceries", "Rent", "Utilities", "Transport", "Dining", "Shopping",
    "Health", "Entertainment", "Subscriptions", "Education", "Cash",
    "Fees", "Other",
]


# Transactions are classified in small batches so the model's JSON response
# never gets truncated by the context/output window.
_BATCH_SIZE = 20


# Hints that map common German banking/merchant vocabulary to categories. The
# model often defaults everything to "Other" without concrete examples, so we
# spell out the kind of German terms it will actually see.
_CATEGORY_HINTS = (
    "- Groceries: supermarkets & food shops such as REWE, EDEKA, ALDI, LIDL, "
    "PENNY, NETTO, Kaufland, DM, Rossmann, Bäckerei, Metzgerei.\n"
    "- Rent: Miete, Mietzahlung, Kaltmiete, Warmmiete, payments to a Vermieter/"
    "Hausverwaltung.\n"
    "- Utilities: Strom, Gas, Wasser, Stadtwerke, Telekom, Vodafone, O2, "
    "1&1, GEZ/Rundfunkbeitrag, Internet, Handy/Mobilfunk.\n"
    "- Transport: Deutsche Bahn (DB), BVG, MVG, VVS, Tankstelle, Aral, Shell, "
    "Esso, Total, Uber, FREENOW, Flixbus, Parkhaus, Deutschlandticket.\n"
    "- Dining: Restaurant, Café, Bar, Imbiss, McDonald's, Burger King, "
    "Lieferando, Wolt, Uber Eats, Starbucks.\n"
    "- Shopping: Amazon, Zalando, MediaMarkt, Saturn, IKEA, H&M, Zara, "
    "Otto, clothing/electronics/home stores.\n"
    "- Health: Apotheke, Arzt, Praxis, Zahnarzt, Krankenkasse (AOK, TK, "
    "Barmer), Fitnessstudio, McFit.\n"
    "- Entertainment: Kino, Konzert, Steam, PlayStation, Eventim, museums, games.\n"
    "- Subscriptions: Netflix, Spotify, Disney+, Amazon Prime, YouTube "
    "Premium, Audible, recurring monthly memberships.\n"
    "- Education: Uni, Universität, Hochschule, Studienbeitrag, Semesterbeitrag, "
    "books, courses, Udemy.\n"
    "- Cash: Bargeld, Geldautomat, ATM, Auszahlung, Barabhebung.\n"
    "- Fees: Gebühr, Kontoführung, Entgelt, Zinsen, Bankgebühr.\n"
    "- Other: only when nothing above plausibly fits."
)


def _classify_batch(batch):
    """Return a list of (transaction, category) for one batch, or None on failure."""
    listing = [
        {"i": i, "text": (t["description"] or t["category"])[:140]}
        for i, t in enumerate(batch)
    ]
    prompt = (
        "These are German bank transactions; the descriptions are in German and "
        "contain German merchant names, banking terms and abbreviations.\n\n"
        "Assign each transaction to exactly one category from this list:\n"
        f"{', '.join(STANDARD_CATEGORIES)}.\n\n"
        "Use these hints to recognise common German merchants and terms:\n"
        f"{_CATEGORY_HINTS}\n\n"
        "Transactions (JSON):\n"
        f"{json.dumps(listing, ensure_ascii=False)}\n\n"
        'Respond with JSON of the form {"assignments": [{"i": 0, '
        '"category": "Groceries"}, ...]} covering every transaction index. '
        "Pick the single best fit and only use 'Other' as a last resort when no "
        "other category is plausible."
    )
    result = _chat_json(
        [
            {"role": "system", "content": "You categorize German bank "
             "transactions into spending categories. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ]
    )
    assignments = result.get("assignments") if isinstance(result, dict) else None
    if not assignments:
        return None

    valid = set(STANDARD_CATEGORIES)
    pairs = []
    for a in assignments:
        try:
            idx = int(a["i"])
            category = a["category"]
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(batch):
            pairs.append((batch[idx], category if category in valid else "Other"))
    return pairs


def categorize_spending(expenses):
    """Group expense transactions into standard categories using the model.

    The model only assigns each transaction to a category; the amounts are
    summed in Python so totals are always accurate. Transactions are processed
    in small batches; any batch the model can't classify falls back to its
    original imported category. Returns a sorted ``categories`` list and an
    optional ``warning``.
    """
    if not expenses:
        return {"categories": [], "warning": None}

    # Cap total work; classify the largest expenses first.
    items = sorted(expenses, key=lambda t: t["amount"], reverse=True)[:200]

    totals = {}
    transactions = {}
    failed_batches = 0

    batches = [items[start:start + _BATCH_SIZE]
               for start in range(0, len(items), _BATCH_SIZE)]
    total_batches = len(batches)

    # Run the independent batches concurrently; each Ollama call is blocking
    # I/O, so a small thread pool overlaps the per-request round-trips.
    with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENCY, total_batches)) as pool:
        results = pool.map(_classify_batch, batches)

    for batch, pairs in zip(batches, results):
        if pairs is None:
            failed_batches += 1
            pairs = [(t, t["category"] or "Other") for t in batch]
        for tx, category in pairs:
            totals[category] = totals.get(category, 0.0) + tx["amount"]
            transactions.setdefault(category, []).append(
                {
                    "id": tx["id"],
                    "date": tx["date"],
                    "description": tx["description"],
                    "amount": round(tx["amount"], 2),
                }
            )

    if failed_batches == total_batches:
        warning = "AI categorization unavailable; showing original categories."
    elif failed_batches:
        warning = (f"{failed_batches} of {total_batches} batches used original "
                   "categories (AI response incomplete).")
    else:
        warning = None

    categories = sorted(
        (
            {
                "category": c,
                "amount": round(totals[c], 2),
                "transactions": sorted(
                    transactions[c], key=lambda x: x["amount"], reverse=True
                ),
            }
            for c in totals
        ),
        key=lambda x: x["amount"],
        reverse=True,
    )
    return {"categories": categories, "warning": warning}

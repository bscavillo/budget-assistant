"""AI helpers backed by a local Ollama model.

The model classifies imported bank transactions into standard spending
categories. It never raises on a missing Ollama server; callers receive a
graceful fallback instead so the rest of the app keeps working offline.
"""

import json
import os

import ollama

MODEL = os.environ.get("BUDGET_OLLAMA_MODEL", "gemma4:latest")


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


def _classify_batch(batch):
    """Return a list of (transaction, category) for one batch, or None on failure."""
    listing = [
        {"i": i, "text": (t["description"] or t["category"])[:90]}
        for i, t in enumerate(batch)
    ]
    prompt = (
        "Assign each transaction below to exactly one category from this list:\n"
        f"{', '.join(STANDARD_CATEGORIES)}.\n\n"
        "Transactions (JSON):\n"
        f"{json.dumps(listing, ensure_ascii=False)}\n\n"
        'Respond with JSON of the form {"assignments": [{"i": 0, '
        '"category": "Groceries"}, ...]} covering every transaction index. '
        "Use 'Other' if nothing fits."
    )
    result = _chat_json(
        [
            {"role": "system", "content": "You categorize bank transactions. "
             "Reply with JSON only."},
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
    failed_batches = 0
    total_batches = 0

    for start in range(0, len(items), _BATCH_SIZE):
        batch = items[start:start + _BATCH_SIZE]
        total_batches += 1
        pairs = _classify_batch(batch)
        if pairs is None:
            failed_batches += 1
            pairs = [(t, t["category"] or "Other") for t in batch]
        for tx, category in pairs:
            totals[category] = totals.get(category, 0.0) + tx["amount"]

    if failed_batches == total_batches:
        warning = "AI categorization unavailable; showing original categories."
    elif failed_batches:
        warning = (f"{failed_batches} of {total_batches} batches used original "
                   "categories (AI response incomplete).")
    else:
        warning = None

    categories = sorted(
        ({"category": c, "amount": round(v, 2)} for c, v in totals.items()),
        key=lambda x: x["amount"],
        reverse=True,
    )
    return {"categories": categories, "warning": warning}

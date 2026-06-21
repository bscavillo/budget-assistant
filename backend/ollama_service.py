"""AI helpers backed by a local Ollama model.

Two jobs run through here:

* **Classification** — turn a messy German bank transaction (full of SEPA/IBAN
  boilerplate) into a clean merchant name and one real spending category, which
  the caller persists so the work is never repeated.
* **Advice** — look at a period's category spending against its budgets and
  suggest concrete things to trim or stop.

Neither ever raises on a missing Ollama server; callers get a graceful fallback
instead so the rest of the app keeps working offline.
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import ollama

import database

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
            # Keep the model resident between batches; reloading it per call
            # dominates the runtime on slower local setups.
            keep_alive="10m",
        )
        return json.loads(response["message"]["content"])
    except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
        return {"_error": str(exc)}


# Curated, canonical spending categories. This is the single source of truth:
# the model must classify into exactly these, off-list answers are rejected,
# and the budget UI offers exactly this list. Tuned for a German account
# (Insurance/Versicherung and Cash/Bargeld are split out because they are
# common and distinct on German statements).
STANDARD_CATEGORIES = [
    "Groceries", "Rent", "Utilities", "Transport", "Dining", "Shopping",
    "Health", "Insurance", "Entertainment", "Subscriptions", "Education",
    "Cash", "Fees", "Other",
]

# The bucket used for anything the model cannot place; kept off the list above
# so it is only ever assigned as a deliberate last resort.
_FALLBACK_CATEGORY = "Other"


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
    "- Insurance: Versicherung, Allianz, HUK, AXA, Haftpflicht, Hausrat, "
    "Kfz-Versicherung, Lebensversicherung.\n"
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
    """Classify one batch of transactions.

    Returns a list of ``(transaction, std_category, merchant)`` tuples, or
    ``None`` if the model was unavailable / gave no usable answer. The model
    does the "deep analysis": it strips the SEPA/IBAN/Mandatsreferenz noise to
    recover the real merchant, then picks the category from it.
    """
    listing = [
        {"i": i, "text": (t["description"] or t["category"])[:160]}
        for i, t in enumerate(batch)
    ]
    prompt = (
        "These are German bank transactions. The text is raw and noisy: it "
        "mixes SEPA/IBAN/BIC codes, Mandatsreferenz numbers and booking "
        "boilerplate with the actual merchant or payee.\n\n"
        "For each transaction:\n"
        "1. Extract the real merchant/payee as a short, clean name (e.g. "
        "'REWE', 'Deutsche Bahn', 'Netflix'). Ignore IBANs, reference numbers "
        "and terms like 'SEPA Lastschrift' or 'Überweisung'.\n"
        "2. Assign exactly one category from this list:\n"
        f"{', '.join(STANDARD_CATEGORIES)}.\n\n"
        "Use these hints to recognise common German merchants and terms:\n"
        f"{_CATEGORY_HINTS}\n\n"
        "Transactions (JSON):\n"
        f"{json.dumps(listing, ensure_ascii=False)}\n\n"
        'Respond with JSON of the form {"assignments": [{"i": 0, '
        '"merchant": "REWE", "category": "Groceries"}, ...]} covering every '
        "transaction index. Pick the single best category and only use 'Other' "
        "as a last resort when no other category is plausible."
    )
    result = _chat_json(
        [
            {"role": "system", "content": "You clean up and categorize German "
             "bank transactions. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ]
    )
    assignments = result.get("assignments") if isinstance(result, dict) else None
    if not assignments:
        return None

    valid = set(STANDARD_CATEGORIES)
    triples = []
    for a in assignments:
        try:
            idx = int(a["i"])
            category = a["category"]
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= idx < len(batch):
            continue
        merchant = (a.get("merchant") or "").strip()[:80] or None
        triples.append((
            batch[idx],
            category if category in valid else _FALLBACK_CATEGORY,
            merchant,
        ))
    return triples


# Only one classification pass runs at a time. The endpoint fires this as a
# background task on every view, and the UI polls while work remains, so
# without this guard repeated polls would pile up concurrent passes that all
# fight over the single local model. A non-blocking acquire means extra calls
# simply return instead of queueing.
_classify_lock = threading.Lock()

# Shared, pollable state so the UI can tell "still working" (a slow batch can
# take minutes) apart from "Ollama is unreachable", without guessing from
# timing. ``failed`` is set when a whole pass classified nothing.
_state_lock = threading.Lock()
_running = False
_failed = False


def classifier_status():
    """Return whether a classification pass is running and if the last failed."""
    with _state_lock:
        return {"running": _running, "failed": _failed}


def _set_state(running=None, failed=None):
    global _running, _failed
    with _state_lock:
        if running is not None:
            _running = running
        if failed is not None:
            _failed = failed


def ensure_classified(period=None):
    """Classify and persist any not-yet-classified expenses in the period.

    This is the heart of the "it just happens" flow: it is fired as a
    background task whenever a period is viewed (and after an import). It must
    never be awaited on the request path — it can take a while — so callers
    return the current summary immediately and let the UI poll as categories
    fill in. Results are persisted per batch so that progress is visible.
    Already-classified periods make no Ollama call, so the work is never
    redone. Returns a small status dict.
    """
    # If a pass is already running, let it finish rather than starting another.
    if not _classify_lock.acquire(blocking=False):
        return {"classified": 0, "unavailable": False, "running": True}
    try:
        pending = database.unclassified_expenses(period)
        if not pending:
            return {"classified": 0, "unavailable": False}

        _set_state(running=True)
        batches = [pending[start:start + _BATCH_SIZE]
                   for start in range(0, len(pending), _BATCH_SIZE)]

        classified = 0
        failed_batches = 0
        # Run the independent batches concurrently; each Ollama call is blocking
        # I/O, so a small thread pool overlaps the per-request round-trips. Each
        # batch is persisted as soon as it returns so a polling UI sees steady
        # progress instead of nothing until the whole period is done.
        with ThreadPoolExecutor(
                max_workers=min(_MAX_CONCURRENCY, len(batches))) as pool:
            futures = [pool.submit(_classify_batch, batch) for batch in batches]
            for future in as_completed(futures):
                triples = future.result()
                if triples is None:
                    failed_batches += 1
                    continue
                updates = [(tx["id"], category, merchant)
                           for tx, category, merchant in triples]
                if updates:
                    database.set_classifications(updates)
                    classified += len(updates)

        # A pass that touched batches but saved nothing means Ollama is down /
        # erroring; a pass that saved at least one clears the failed flag.
        _set_state(failed=(classified == 0 and failed_batches > 0))
        return {
            "classified": classified,
            # True only when nothing could be classified (Ollama down); a
            # partial failure just retries on the next view.
            "unavailable": failed_batches == len(batches),
        }
    finally:
        _set_state(running=False)
        _classify_lock.release()


def generate_advice(summary):
    """Suggest concrete things to trim or stop for one period's spending.

    ``summary`` is a ``database.period_summary`` result. Returns
    ``{"suggestions": [...], "warning": str | None}``; on a missing model the
    suggestions list is empty and ``warning`` explains why.
    """
    categories = [c for c in summary.get("categories", [])
                  if c["category"] != database.UNCLASSIFIED]
    if not categories:
        return {"suggestions": [], "warning": None}

    lines = []
    for c in categories:
        limit = (f", budget {c['limit']:.0f} €" if c["limit"] is not None
                 else ", no budget set")
        over = (" (OVER budget)" if c["limit"] is not None
                and c["spent"] > c["limit"] else "")
        lines.append(f"- {c['category']}: spent {c['spent']:.0f} €{limit}{over}")

    prompt = (
        "You are a personal budgeting assistant. Here is one period's spending "
        f"by category (income {summary['income']:.0f} €, expenses "
        f"{summary['expense']:.0f} €):\n"
        + "\n".join(lines)
        + "\n\nSuggest 3 to 5 concrete, specific things the user could trim or "
        "stop to save money. Prioritise categories that are over budget or "
        "unusually large. Each suggestion must be one short sentence.\n"
        'Respond with JSON of the form {"suggestions": ["...", "..."]}.'
    )
    result = _chat_json(
        [
            {"role": "system", "content": "You give short, practical budgeting "
             "advice. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ]
    )
    suggestions = result.get("suggestions") if isinstance(result, dict) else None
    if not suggestions:
        return {"suggestions": [],
                "warning": "AI advice unavailable (is Ollama running?)."}

    cleaned = [str(s).strip() for s in suggestions if str(s).strip()][:5]
    return {"suggestions": cleaned, "warning": None}

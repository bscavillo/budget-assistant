"""AI helpers backed by a local Ollama model.

The one job here is **classification** — turning a messy German bank
transaction (full of SEPA/IBAN boilerplate) into a clean merchant name and one
real spending category, which the caller persists so the work is never
repeated.

It never raises on a missing Ollama server; callers get a graceful fallback
instead so the rest of the app keeps working offline.
"""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import ollama

import database

MODEL = os.environ.get("BUDGET_OLLAMA_MODEL", "gemma4:latest")

# How many classification batches to send to Ollama at once. Batches are
# independent, so overlapping them hides per-request latency; the local model
# still serialises actual inference, so very high values give little extra.
_MAX_CONCURRENCY = max(1, int(os.environ.get("BUDGET_OLLAMA_CONCURRENCY", "4")))


def _chat_json(messages, *, temperature=0.0, num_ctx=8192):
    """Send a chat request forcing a JSON response; return a parsed dict.

    ``temperature`` defaults to 0 so classification is deterministic: the same
    transaction (and the same merchant seen twice) always gets the same answer,
    which matters most over a whole year where any wobble shows up as the same
    payee landing in different categories. ``num_ctx`` is large enough that a
    full batch of JSON assignments is never truncated mid-response (a truncated
    response is invalid JSON and loses the entire batch).

    Returns ``{"_error": "..."}`` if Ollama is unavailable or the response is
    not valid JSON, so callers can degrade gracefully.
    """
    try:
        response = ollama.chat(
            model=MODEL,
            messages=messages,
            format="json",
            options={"num_ctx": num_ctx, "temperature": temperature},
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
    "Cash", "Fees", "Transfers", "Other",
]

# The bucket used for anything the model cannot place; kept off the list above
# so it is only ever assigned as a deliberate last resort.
_FALLBACK_CATEGORY = "Other"


# Transactions are classified in small batches so the model's JSON response
# never gets truncated by the context/output window.
_BATCH_SIZE = 20


def _dedup_key(tx):
    """A coarse key that collapses repeat payees so each is classified once.

    A transaction's raw text carries per-booking noise — amounts, IBANs,
    Mandatsreferenz and terminal numbers — that differs between two payments to
    the same merchant. Lowercasing and dropping digits/punctuation leaves the
    stable merchant words ("rewe sagt danke berlin"), so a year full of REWE,
    Netflix or rent bookings collapses to a handful of representatives. Each is
    classified once and the answer fans out to every matching row, which both
    cuts the number of model calls sharply and guarantees the same merchant
    gets the same category everywhere.

    A key with no letters left (e.g. an all-numeric description) is too generic
    to safely merge unrelated rows, so the caller keeps those transactions
    separate.
    """
    text = (tx["description"] or tx["category"] or "").lower()
    text = re.sub(r"[^a-zäöüß ]+", " ", text)
    return " ".join(text.split())[:80]


# Hints that map common German banking/merchant vocabulary to categories. The
# model often defaults everything to "Other" without concrete examples, so we
# spell out the kind of German terms it will actually see.
_CATEGORY_HINTS = (
    "- Groceries: supermarkets & food shops in ANY country — REWE, EDEKA, ALDI, "
    "LIDL, PENNY, NETTO, Kaufland, Tegut, Norma, DM, Rossmann, Metzgerei "
    "(butcher); abroad e.g. Albert ('Albert vám děkuje'), Billa, Studenac, "
    "Ribola, Konzum, Mercadona, Pingo Doce, Continente, Carrefour, Spar.\n"
    "- Rent: Miete, Mietzahlung, Kaltmiete, Warmmiete, and any payment "
    "(including a SEPA-Dauerauftrag) to a Vermieter, Hausverwaltung, property "
    "manager or real-estate company — names containing 'Immobilien', "
    "'Hausverwaltung', 'HV GmbH' or 'Verwaltung' are Rent, not transfers.\n"
    "- Utilities: Strom, Gas, Wasser, Stadtwerke, Telekom, Vodafone, O2, "
    "1&1, GEZ/Rundfunkbeitrag, Internet, Handy/Mobilfunk.\n"
    "- Transport: Deutsche Bahn (DB), BVG, MVG, VVS, Tankstelle, Aral, Shell, "
    "Esso, Total, Uber, FREENOW, Flixbus, Parkhaus, Deutschlandticket.\n"
    "- Dining: eating & drinking out in ANY language — Restaurant, Café, Bar, "
    "Imbiss, Bistro, Pub, Coffee, Burger, plus any bakery or pastry shop: "
    "Bäckerei, Feinbäckerei, Konditorei, Backwaren, Heberer, McDonald's, Burger "
    "King, Lieferando, Wolt, Uber Eats, Starbucks; abroad e.g. 'The Good "
    "Bourger', 'HiBreeze Coffee', 'Aduela', 'Meia Nau', 'Pregar Baixa'.\n"
    "- Shopping: Amazon, Zalando, MediaMarkt, Saturn, IKEA, H&M, Zara, "
    "Otto, clothing/electronics/home stores.\n"
    "- Health: Apotheke, Arzt, Praxis, Zahnarzt, Krankenkasse (AOK, TK, "
    "Barmer), Fitnessstudio, McFit.\n"
    "- Insurance: Versicherung, Allianz, HUK, AXA, Haftpflicht, Hausrat, "
    "Kfz-Versicherung, Lebensversicherung.\n"
    "- Entertainment: museums in ANY language (Museum/Muzeum/Museu/Museo, e.g. "
    "'Narodni Muzeum'), castles & landmarks (Hrad, Castelo, Castillo), Kino, "
    "Konzert, Steam, PlayStation, Eventim, games.\n"
    "- Subscriptions: Netflix, Spotify, Disney+, Amazon Prime, YouTube "
    "Premium, Audible, recurring monthly memberships.\n"
    "- Education: Uni, Universität, Hochschule, Studienbeitrag, Semesterbeitrag, "
    "books, courses, Udemy.\n"
    "- Cash: Bargeld, Geldautomat, ATM, Auszahlung, Barabhebung.\n"
    "- Fees: Gebühr, Kontoführung, Entgelt, Zinsen, Bankgebühr.\n"
    "- Transfers: a payment to a named PRIVATE PERSON (peer-to-peer). Recognise "
    "real human names in any format or order, including the 'Lastname, "
    "Firstname' form: 'Max Mustermann', 'Anna Schmidt', 'John Smith', "
    "'Jane Doe', 'Smith, John'. A standalone human name with no business "
    "words is almost always a Transfer — strongly prefer Transfers over Other "
    "for these. It is NOT a Transfer if the name carries a company form (GmbH, "
    "AG, UG, mbH, KG, e.V., S.R.O., N.V., Lda, d.o.o., S.L.) or a business word "
    "(Immobilien, Hausverwaltung, Versicherung, Bank, Shop, Café, Restaurant).\n"
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
        "The user travels, so merchant names appear in many languages — German, "
        "English, Portuguese, Czech, Croatian, Spanish, Italian and more. Use "
        "your world knowledge of real merchants and decode meaningful words in "
        "ANY language before ever falling back to 'Other'. You already know what "
        "places like 'Tegut', 'Narodni Muzeum' (a museum) or 'Albert vám děkuje' "
        "(a Czech supermarket) are — classify them like a well-travelled local "
        "would, not as 'Other'.\n\n"
        "For each transaction:\n"
        "1. Extract the real merchant/payee as a short, clean name (e.g. "
        "'REWE', 'Deutsche Bahn', 'Netflix'). The text often LEADS with a bank "
        "or payment processor that is NOT the merchant — skip it and take the "
        "real name that follows: Postbank AG, Sparkasse, Deutsche Bank, ING, "
        "SumUp, Adyen N.V., Stripe, PayPal, Klarna, Mollie, iZettle, Concardis. "
        "So 'Postbank AG - Mata - Cafe.Bar' is the café 'Mata' (Dining), and "
        "'SumUp - HiBreeze' is 'HiBreeze' (Dining). Ignore IBANs, BIC, reference "
        "numbers and booking terms like 'SEPA Lastschrift' or 'Überweisung'.\n"
        "2. Assign exactly one category from this list:\n"
        f"{', '.join(STANDARD_CATEGORIES)}.\n\n"
        "Read the merchant name itself for meaning — the words in it are the "
        "strongest clue and usually settle the category on their own. For "
        "example a name with 'Bäckerei', 'Feinbäckerei' or 'Konditorei' is a "
        "bakery, so Dining (e.g. 'Wiener Feinbäckerei Heberer', 'Bäckerei "
        "Konditorei Voigt'); a name with 'Immobilien' or 'Hausverwaltung' is a "
        "landlord, so Rent; '... Apotheke' is Health; '... Restaurant' or "
        "'... Café' is Dining; '... Versicherung' is Insurance. A name ending "
        "in GmbH/AG is a company, never a personal Transfer. Always decode such "
        "German/English words in the name before considering 'Other'.\n\n"
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

        # Collapse repeat payees down to one representative each (see
        # ``_dedup_key``); we classify representatives and fan the answer back
        # out to every row that shares the key. Rows with a too-generic key are
        # kept on their own so unrelated payments never share a category.
        groups = {}
        for tx in pending:
            key = _dedup_key(tx) or f"\0{tx['id']}"
            groups.setdefault(key, []).append(tx)
        reps = [members[0] for members in groups.values()]
        rep_members = {members[0]["id"]: members for members in groups.values()}

        batches = [reps[start:start + _BATCH_SIZE]
                   for start in range(0, len(reps), _BATCH_SIZE)]

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
                updates = [(member["id"], category, merchant)
                           for rep, category, merchant in triples
                           for member in rep_members[rep["id"]]]
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


# --- Budget suggestions ----------------------------------------------------

# Suggested category budgets come from a single local-model call that, like
# classification, never runs on the request path. The result is cached and keyed
# on a signature of the spending picture, so an unchanged history makes no model
# call and the model is never asked the same question twice.
_suggest_lock = threading.Lock()
_suggest_state_lock = threading.Lock()
_suggestions = None        # last good list of {category, monthly_limit}
_suggest_signature = None  # signature of the stats those suggestions came from
_suggest_running = False
_suggest_failed = False


def _stats_signature(stats):
    """A stable string identifying a spending picture, to cache suggestions by."""
    return json.dumps(
        sorted((s["category"], s["avg_monthly"], s["months"]) for s in stats),
        ensure_ascii=False,
    )


def _set_suggest_state(running=None, failed=None):
    global _suggest_running, _suggest_failed
    with _suggest_state_lock:
        if running is not None:
            _suggest_running = running
        if failed is not None:
            _suggest_failed = failed


def budget_suggestions_snapshot():
    """Return the cached suggestions plus whether a fresh pass is needed.

    ``stale`` is True when there is spending to budget for but the cached
    suggestions no longer match the current numbers (or none exist yet) — the
    signal for the caller to schedule a background pass and for the UI to keep
    polling. Mirrors the classifier's pollable status so the frontend can drive
    both the same way.
    """
    stats = database.category_spending_stats()
    signature = _stats_signature(stats)
    with _suggest_state_lock:
        fresh = signature == _suggest_signature and _suggestions is not None
        return {
            "suggestions": list(_suggestions or []),
            "running": _suggest_running,
            "failed": _suggest_failed,
            "stale": bool(stats) and not fresh,
        }


def _suggest_from_model(stats):
    """Ask the model for one monthly budget per category; None if unusable.

    The model is given each category's average monthly spend and turns it into a
    clean, livable budget (average plus a little headroom, rounded to a human
    number), which is exactly the judgement a flat formula does poorly.
    """
    listing = [
        {
            "category": s["category"],
            "avg_monthly": s["avg_monthly"],
            "months_observed": s["months"],
        }
        for s in stats
    ]
    prompt = (
        "You are a personal budgeting assistant for a German user. Below is the "
        "user's own spending history, one row per category, with a "
        "recency-weighted average monthly spend (in euros) — recent months are "
        "weighted more heavily, so it already reflects current prices and income "
        "(inflation, raises) — and how many distinct months it is based on.\n\n"
        "Suggest a sensible monthly budget for each category:\n"
        "- Start from this recency-weighted average; do not adjust again for "
        "inflation, it is already baked in.\n"
        "- Add a small amount of headroom (about 5-15%) so a normal month does "
        "not immediately blow the budget.\n"
        "- Round to a clean, human number (nearest 5 or 10 euros; larger "
        "budgets can round to the nearest 25 or 50).\n"
        "- Never suggest a budget below the average monthly spend.\n"
        "- Give a budget for every category in the input and invent no new ones.\n\n"
        "Spending history (JSON):\n"
        f"{json.dumps(listing, ensure_ascii=False)}\n\n"
        'Respond with JSON of the form {"suggestions": [{"category": '
        '"Groceries", "monthly_limit": 350}, ...]} covering every category.'
    )
    result = _chat_json(
        [
            {"role": "system", "content": "You suggest realistic monthly budgets "
             "from spending history. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ]
    )
    suggestions = result.get("suggestions") if isinstance(result, dict) else None
    if not suggestions:
        return None

    known = {s["category"] for s in stats}
    cleaned = []
    seen = set()
    for item in suggestions:
        try:
            category = item["category"]
            limit = float(item["monthly_limit"])
        except (KeyError, TypeError, ValueError):
            continue
        # Keep only known categories, sane amounts, and the first take on each.
        if category in known and category not in seen and limit >= 0:
            seen.add(category)
            cleaned.append({"category": category, "monthly_limit": round(limit, 2)})
    return cleaned or None


def ensure_budget_suggestions():
    """Recompute budget suggestions in the background when they are stale.

    Fired as a background task whenever the suggestions are viewed and the
    spending picture has changed (e.g. after an import classifies new rows).
    Caches the result keyed on the spending signature, so an unchanged picture
    makes no model call. Returns a small status dict mirroring
    ``ensure_classified``; never raises on a missing Ollama server.
    """
    global _suggestions, _suggest_signature
    # If a pass is already running, let it finish rather than starting another.
    if not _suggest_lock.acquire(blocking=False):
        return {"running": True}
    try:
        stats = database.category_spending_stats()
        if not stats:
            return {"suggested": 0}
        signature = _stats_signature(stats)
        with _suggest_state_lock:
            if signature == _suggest_signature and _suggestions is not None:
                return {"suggested": len(_suggestions)}

        _set_suggest_state(running=True)
        cleaned = _suggest_from_model(stats)
        if cleaned is None:
            # Nothing usable came back: flag it so the UI can warn, and leave the
            # signature unset so the next view retries.
            _set_suggest_state(failed=True)
            return {"suggested": 0, "unavailable": True}

        with _suggest_state_lock:
            _suggestions = cleaned
            _suggest_signature = signature
            _suggest_failed = False
        return {"suggested": len(cleaned)}
    finally:
        _set_suggest_state(running=False)
        _suggest_lock.release()

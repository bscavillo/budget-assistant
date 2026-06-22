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


# Payment processors / banks that lead a description but are NOT the merchant.
# A booking like "Adyen N.V. - Fritz Mitte GmbH/.../Jena/DE" is the café "Fritz
# Mitte", not Adyen; we drop these heads so the real payee leads the text the
# model reads. Matched case-insensitively as a prefix of a " - " segment.
_PROCESSOR_HEADS = (
    "adyen", "sumup", "paypal", "stripe", "klarna", "mollie", "izettle",
    "concardis", "sg-vr payment", "vr payment", "payone", "unzer",
    "postbank", "sparkasse", "deutsche bank", "ing-diba", "ing",
    "lastschrift aus kartenzahlung", "kartenzahlung", "visa", "mastercard",
    "nexi germany gmbh", "nexi", "payu",
)

# Matches a leading processor token plus its glue punctuation ("SumUp .",
# "SumUp.", "Nexi Germany GmbH - ") so it can be stripped even without a clean
# " - " separator. Longest names first so the alternation prefers full matches.
_PROCESSOR_PREFIX_RE = re.compile(
    r"^(?:%s)\b[\s.\-]*" % "|".join(
        re.escape(p) for p in sorted(_PROCESSOR_HEADS, key=len, reverse=True)),
    re.IGNORECASE,
)

# Bank booking types (Umsatzart) that, on their own, pin the category with
# certainty. Surfaced to the model as a strong hint rather than hard-coded, but
# also used to recognise cash/fees deterministically in the cleanup struct.
_CASH_TYPES = ("auszahlung", "bargeld", "geldautomat")
_FEE_TYPES = ("entgelt", "gebuhr", "gebühr", "kontoabrechnung", "kontofuhrung")

# An ATM withdrawal looks like "GA NR07095930 BLZ820700240125.04/12.53UHR JENA"
# — a terminal/BLZ code and a time, with the city trailing. No merchant exists.
_ATM_RE = re.compile(r"^GA\s*NR", re.IGNORECASE)


def _clean_transaction(tx):
    """Strip SEPA/booking noise so the model reads a clean payee, not garbage.

    Returns a dict ``{text, city, country, type, hint}`` where ``text`` is the
    de-noised description (processor head dropped, timestamps / IBAN / BIC /
    Mandatsreferenz / terminal numbers removed), ``city``/``country`` are pulled
    from the trailing ``/City/Country`` block when present, ``type`` is the raw
    Umsatzart, and ``hint`` is a deterministic category when the booking type
    alone settles it (ATM cash, account fees) — empty otherwise.

    This is pure text normalisation: it never decides a *merchant's* category,
    it just hands the model the cleanest possible input.
    """
    raw = (tx["description"] or "").strip()
    raw_type = (tx["category"] or "").strip()
    type_l = raw_type.lower()

    # Cash withdrawals carry no merchant; recognise the ATM pattern and the
    # cash booking types so they never get guessed at.
    if _ATM_RE.match(raw) or any(t in type_l for t in _CASH_TYPES):
        city = raw.split()[-1] if raw.split() else ""
        return {"text": "Geldautomat (ATM cash withdrawal)", "city": city,
                "country": "", "type": raw_type, "hint": "Cash"}
    if any(t in type_l for t in _FEE_TYPES):
        return {"text": raw[:160] or raw_type, "city": "", "country": "",
                "type": raw_type, "hint": "Fees"}

    text = raw
    # Drop everything from the first booking timestamp / trailer onward.
    text = re.split(r"\s+\d{2}-\d{2}-\d{4}T", text)[0]
    text = re.split(r"\s+(?:Folgenr\.|Verfalld\.|Original\b)", text)[0]
    # Remove IBAN/BIC, BLZ codes, mandate refs and long digit runs.
    text = re.sub(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", " ", text)
    text = re.sub(r"\bBLZ\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:Mandatsref|Glaubiger|Gläubiger|End-?to-?End)\w*.*$",
                  " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{5,}\b", " ", text)

    # Drop leading payment-processor segments so the real payee leads.
    segments = [s.strip() for s in text.split(" - ") if s.strip()]
    while len(segments) > 1 and _is_processor(segments[0]):
        segments.pop(0)
    text = " - ".join(segments)

    # Some processors prefix the payee without a " - " separator, e.g.
    # "SumUp .Saray Doner" or "SumUp.Mata"; strip a leading processor token and
    # any glue punctuation so the real merchant still leads.
    text = re.sub(_PROCESSOR_PREFIX_RE, "", text).strip()

    # PayPal hides the real shop after "Ihr Einkauf bei" and pads the rest with
    # "PP.####.PP" boilerplate; pull the shop out (it is the only useful part).
    text = re.sub(r"\bPP\.\d+\.PP\b", " ", text)
    bei = re.search(r"Ihr Einkauf bei\s*(.*)$", text, flags=re.IGNORECASE)
    if bei:
        text = bei.group(1).strip(" .,") or "Online-Einkauf (PayPal)"

    # Pull city/country from a trailing "Merchant/Street/City/Country" block.
    city = country = ""
    if "/" in text:
        parts = [p.strip(" .") for p in text.split("/") if p.strip(" .")]
        if len(parts) >= 2:
            last = parts[-1]
            if 2 <= len(last) <= 3 and last.isalpha():
                country = last.upper()
                parts = parts[:-1]
            if len(parts) >= 2:
                city = parts[-1]

    text = " ".join(text.replace("/", " ").split())[:160]
    if not text:
        text = raw[:160] or raw_type
    return {"text": text, "city": city, "country": country,
            "type": raw_type, "hint": ""}


def _is_processor(segment):
    """True if a " - " segment is just a payment processor / bank, not a payee."""
    head = segment.lower().lstrip()
    return any(head.startswith(p) for p in _PROCESSOR_HEADS)


def _dedup_key(clean):
    """Collapse repeat payees so each distinct merchant is classified once.

    Built from the *cleaned* text (see ``_clean_transaction``) rather than the
    raw description, so per-booking noise (amounts, IBANs, timestamps, terminal
    numbers) is already gone and the leading boilerplate can no longer push the
    real merchant out of the key. Lowercased, letters-only, plus the city so two
    different shops that happen to share a name in different towns stay apart.
    A year of REWE/Netflix/rent bookings collapses to one representative each,
    whose single classification fans out to every matching row.

    Returns ``""`` when nothing stable is left, signalling the caller to keep
    that row on its own rather than merge it with unrelated payments.
    """
    base = f"{clean['text']} {clean['city']}".lower()
    base = re.sub(r"[^a-zäöüß ]+", " ", base)
    return " ".join(base.split())[:80]


# One tight line per category: what belongs there and the German/English/travel
# vocabulary that signals it. Kept short and scannable so a local model can hold
# the whole list while judging a single transaction.
_CATEGORY_GUIDE = (
    "- Groceries: supermarkets, food shops, butchers, drugstores. REWE, EDEKA, "
    "ALDI, LIDL, PENNY, NETTO, Kaufland, Tegut, Norma, DM, Rossmann, Müller, "
    "Metzgerei, 'nah & gut'; abroad Albert, Billa, Konzum, Studenac, Mercadona, "
    "Continente, Carrefour, Spar.\n"
    "- Rent: Miete, Kaltmiete, Warmmiete, or any payee with 'Immobilien', "
    "'Hausverwaltung', 'Verwaltung', 'HV GmbH' — a landlord, not a transfer.\n"
    "- Utilities: Strom, Gas, Wasser, Stadtwerke, Telekom, Vodafone, O2, 1&1, "
    "GEZ/Rundfunkbeitrag, Internet, Mobilfunk.\n"
    "- Transport: Deutsche Bahn/DB, BVG, MVG, VVS, Tankstelle, Aral, Shell, "
    "Esso, Total, Uber, FREENOW, Flixbus, Parkhaus, Deutschlandticket, LogPay.\n"
    "- Dining: eating & drinking out, in any language — Restaurant, Café, Bar, "
    "Imbiss, Bistro, Pub, Coffee, Roaster, Burger, Pizza; bakeries too "
    "(Bäckerei, Konditorei, Backwaren). McDonald's, Burger King, Starbucks, "
    "Lieferando, Wolt. A 'GmbH'/'OHG' whose name sounds like a café/bar/eatery "
    "and is paid at a point of sale is Dining (e.g. 'Fritz Mitte', 'Kuss', "
    "'Bunca Human Roaster', 'Barbarino'). Bakeries are often abbreviated — "
    "'Heberer' / 'WF Heberer' is Wiener Feinbäckerei Heberer (Dining).\n"
    "- Shopping: retail goods — clothing, electronics, home, gifts, books. "
    "Amazon, Zalando, MediaMarkt, Saturn, IKEA, H&M, Zara, Otto, NANU-NANA, "
    "GALERIA (Kaufhof department store), DEUTSCHE POST.\n"
    "- Health: Apotheke, Arzt, Praxis, Zahnarzt, Krankenkasse (AOK, TK, Barmer), "
    "Fitnessstudio, McFit.\n"
    "- Insurance: Versicherung, Allianz, HUK, AXA, Haftpflicht, Hausrat, Kfz-.\n"
    "- Entertainment: museums (Museum/Muzeum/Museu/Museo), landmarks & churches "
    "(Münster, Dom, Hrad, Castelo), Kino, Konzert, Theater, Steam, PlayStation, "
    "Eventim, games, fairground/Schausteller.\n"
    "- Subscriptions: Netflix, Spotify, Disney+, Amazon Prime, YouTube Premium, "
    "Audible — recurring digital memberships.\n"
    "- Education: Uni, Universität, Hochschule, Studienbeitrag, Semesterbeitrag, "
    "Udemy, courses.\n"
    "- Cash: cash withdrawals — Geldautomat, ATM, Bargeld, Auszahlung.\n"
    "- Fees: Gebühr, Kontoführung, Entgelt, Zinsen, Bankgebühr.\n"
    "- Transfers: a payment to a named PRIVATE PERSON, e.g. 'Max Mustermann' or "
    "'Mustermann, Max'. A standalone human name with no company form (GmbH, AG, "
    "UG, KG, OHG, e.V., N.V., S.R.O.) and no business word is a Transfer — "
    "strongly prefer Transfers over Other for such names.\n"
    "- Other: TRUE LAST RESORT only. A card payment at a named shop is essentially "
    "never Other — pick the most likely real category instead."
)

_SYSTEM_PROMPT = (
    "You categorize a single German bank transaction. The user travels, so "
    "merchant names appear in German, English, Portuguese, Czech, Croatian, "
    "Spanish, Italian and more — use your world knowledge of real merchants and "
    "the words in the name to decide. Reply with JSON only."
)


def _classify_one(clean):
    """Classify one cleaned transaction; return ``(category, merchant)`` or None.

    ``clean`` is the struct from ``_clean_transaction``. A deterministic ``hint``
    (ATM cash, account fees) short-circuits the model entirely. Otherwise the
    model gets the de-noised payee, its city/country and the bank booking type,
    and returns a merchant + one on-list category. Returns ``None`` only when the
    model is unavailable or gives nothing usable, so the row stays unclassified
    and is retried later.
    """
    if clean["hint"]:
        return clean["hint"], (clean["text"][:80] or None)

    context = [f"Payee text: {clean['text']}"]
    if clean["city"]:
        context.append(f"City: {clean['city']}")
    if clean["country"]:
        context.append(f"Country: {clean['country']}")
    if clean["type"]:
        context.append(f"Bank booking type: {clean['type']}")

    prompt = (
        "Below is ONE German bank transaction, already stripped of IBANs, "
        "reference numbers and booking boilerplate. Identify the real "
        "merchant/payee and assign exactly one category.\n\n"
        + "\n".join(context) + "\n\n"
        "Categories (choose exactly one):\n"
        f"{_CATEGORY_GUIDE}\n\n"
        "Booking-type cues: 'Kartenzahlung' is a card purchase at a physical "
        "shop, restaurant or service — it is a real merchant, so 'Other' is "
        "almost never correct. A 'Dauerauftrag' (standing order) is usually Rent "
        "or a recurring bill. Use the city/country to help place a foreign name "
        "(e.g. a shop in CZ/HR/PT/ES is likely Groceries or Dining).\n\n"
        "Important: if the payee is a PERSON'S NAME (one or two human first+last "
        "names, e.g. 'Max Mustermann' or 'Mustermann, Erika'), it is ALWAYS a "
        "Transfer — even when the trailing memo names a brand, product, app, "
        "game, ticket or shop (e.g. 'Nintendo', 'Apple Notes', 'Etsy', "
        "'Monoprix'). That memo is only what the money was for; it never "
        "overrides the fact that a person was paid. Classify on who was paid.\n\n"
        "Think briefly, then answer. Decode the words in the name first; only "
        "use 'Other' if no category is plausible at all.\n\n"
        'Respond with JSON: {"merchant": "<clean short name>", "reason": '
        '"<one short phrase>", "category": "<one category from the list>"}.'
    )
    result = _chat_json(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not isinstance(result, dict) or result.get("_error"):
        return None
    category = result.get("category")
    if not category:
        return None
    if category not in set(STANDARD_CATEGORIES):
        category = _FALLBACK_CATEGORY
    merchant = (result.get("merchant") or "").strip()[:80] or clean["text"][:80] or None
    return category, merchant


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

        # Clean each row's noisy text once, then collapse repeat payees down to
        # one representative each (see ``_dedup_key``, now keyed on the cleaned
        # text); we classify one representative and fan the answer out to every
        # row that shares the key. Rows whose cleaned text leaves no stable key
        # are kept on their own so unrelated payments never share a category.
        groups = {}
        for tx in pending:
            clean = _clean_transaction(tx)
            key = _dedup_key(clean) or f"\0{tx['id']}"
            groups.setdefault(key, (clean, []))[1].append(tx)
        reps = [(clean, members) for clean, members in groups.values()]

        classified = 0
        attempted = 0
        failed = 0
        # Classify one representative per distinct payee. Each Ollama call is
        # blocking I/O, so a small thread pool overlaps the round-trips; the
        # local model still serialises actual inference. Each result is
        # persisted as soon as it returns so a polling UI sees steady progress.
        with ThreadPoolExecutor(
                max_workers=min(_MAX_CONCURRENCY, len(reps))) as pool:
            futures = {pool.submit(_classify_one, clean): members
                       for clean, members in reps}
            for future in as_completed(futures):
                attempted += 1
                result = future.result()
                if result is None:
                    failed += 1
                    continue
                category, merchant = result
                updates = [(member["id"], category, merchant)
                           for member in futures[future]]
                database.set_classifications(updates)
                classified += len(updates)

        # A pass that tried every representative but saved nothing means Ollama
        # is down / erroring; saving at least one clears the failed flag.
        _set_state(failed=(classified == 0 and failed > 0))
        return {
            "classified": classified,
            # True only when nothing could be classified (Ollama down); a
            # partial failure just retries on the next view.
            "unavailable": failed == attempted and attempted > 0,
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

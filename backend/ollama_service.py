"""AI helpers backed by a local Ollama model.

These functions turn the user's financial data into prompts and return the
model's natural-language response. They never raise on a missing Ollama
server; callers receive a friendly message instead so the rest of the app
keeps working offline.
"""

import json
import os

import ollama

MODEL = os.environ.get("BUDGET_OLLAMA_MODEL", "gemma4:latest")
CURRENCY = "EUR"


def _format_summary(summary):
    """Render a summary dict as compact text for the model prompt."""
    lines = [
        f"Month: {summary['month']}",
        f"Income: {summary['income']} {CURRENCY}",
        f"Expenses: {summary['expense']} {CURRENCY}",
        f"Balance: {summary['balance']} {CURRENCY}",
        "Spending by category:",
    ]
    if summary["categories"]:
        for cat in summary["categories"]:
            limit = f", budget {cat['limit']}" if cat["limit"] is not None else ""
            lines.append(f"  - {cat['category']}: {cat['spent']} {CURRENCY}{limit}")
    else:
        lines.append("  (no expenses recorded)")
    return "\n".join(lines)


def _chat(messages):
    """Send a chat request to Ollama, returning the assistant text."""
    try:
        response = ollama.chat(model=MODEL, messages=messages)
        return response["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - surface any Ollama failure to the UI
        return (
            "AI is unavailable right now. Make sure Ollama is running and the "
            f"model '{MODEL}' is installed (`ollama pull {MODEL}`).\n\n"
            f"Details: {exc}"
        )


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


SYSTEM_PROMPT = (
    "You are a concise, practical personal-finance assistant. "
    f"All amounts are in {CURRENCY}. Give specific, actionable advice based "
    "only on the data provided. Use short paragraphs or bullet points."
)


def generate_insights(summary):
    """Return AI-generated observations about the month's finances."""
    prompt = (
        "Here is my budget for the month:\n\n"
        f"{_format_summary(summary)}\n\n"
        "Give me 3-5 short insights: where I overspend, where I'm doing well, "
        "and one concrete suggestion to improve my balance next month."
    )
    return _chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )


def answer_question(question, summary, transactions):
    """Answer a free-form question grounded in the user's data."""
    recent = transactions[:40]
    context = {
        "summary": summary,
        "recent_transactions": recent,
    }
    prompt = (
        "My financial data (JSON):\n"
        f"{json.dumps(context, ensure_ascii=False)}\n\n"
        f"Question: {question}"
    )
    return _chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )


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

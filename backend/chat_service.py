"""Conversational finance assistant backed by the local Ollama model.

Two jobs:

* **Answer** natural-language questions about the user's imported transactions.
  This is retrieval-augmented — the rows relevant to the question (by keyword)
  plus the currently selected period are pulled from the database and handed to
  the model as context, so it answers from the user's real data rather than
  guessing. Everything stays on the machine: the same local Ollama server that
  classifies transactions also runs the chat.

* **Fix** records on request — correcting a wrong date, amount, label or
  category, or deleting a duplicate. The model never writes to the database
  directly: it *proposes* structured actions referencing a transaction's id,
  which the caller shows to the user and only applies (via ``apply_action``)
  after an explicit confirmation. This keeps a hallucinated id or a
  misunderstood instruction from silently corrupting the ledger.

Like the classifier, it degrades gracefully when Ollama is unreachable: callers
get an ``unavailable`` flag instead of an exception.
"""

import re

import database
# Reuse the single Ollama call site (JSON mode, keep-alive, graceful failure)
# and the canonical category list, so chat and classification never drift.
from ollama_service import STANDARD_CATEGORIES, _chat_json

# How many prior chat turns to replay for continuity. The financial context is
# rebuilt and re-injected every turn, so history only needs to carry the thread
# of the conversation, not the data — a short window keeps the prompt small.
_MAX_HISTORY = 8

# Common words that never help locate a specific transaction; dropped from the
# keyword search so "why is my rent so high" keys on "rent", not "why"/"high".
_STOPWORDS = {
    "the", "and", "for", "was", "why", "how", "what", "when", "which", "this",
    "that", "with", "from", "have", "has", "did", "are", "you", "your", "can",
    "could", "would", "should", "much", "many", "there", "their", "about",
    "into", "out", "not", "but", "all", "any", "who", "does", "please", "show",
    "tell", "give", "make", "change", "fix", "set", "move", "correct", "delete",
    "wrong", "right", "high", "low", "more", "less", "than", "then", "them",
    "und", "der", "die", "das", "den", "dem", "ein", "eine", "einen", "wie",
    "was", "wer", "wann", "warum", "wieso", "welche", "welcher", "mein",
    "meine", "meinen", "meiner", "ist", "sind", "war", "auf", "für", "von",
    "mit", "auch", "nicht", "aber", "oder", "bitte", "ändere", "aendere",
    "buche", "setze", "korrigiere", "lösche", "loesche", "falsch", "richtig",
    "hoch", "mehr", "weniger", "ausgabe", "ausgaben", "transaktion",
}


def _keywords(text):
    """Pull the searchable keywords out of a question.

    Alphanumeric tokens of three or more characters, lowercased, minus the
    stopwords above. Capped so a rambling question can't blow up the LIKE query.
    """
    tokens = re.findall(r"[a-z0-9äöüß]{3,}", (text or "").lower())
    return [tok for tok in tokens if tok not in _STOPWORDS][:12]


def _tx_snapshot(tx):
    """A small, display-ready view of a transaction for an action card."""
    return {
        "id": tx["id"],
        "date": tx["date"],
        "description": (tx.get("merchant") or tx.get("description") or "").strip(),
        "amount": round(tx["amount"], 2),
        "category": tx.get("std_category") or "",
    }


def _format_tx(tx):
    """One compact context line the model can read and reference by id."""
    label = (tx.get("merchant") or tx.get("description") or "").strip()
    category = tx.get("std_category") or tx.get("category") or "-"
    return (f"#{tx['id']} | {tx['date']} | {tx['type']} | {category} | "
            f"{label[:60] or '-'} | {tx['amount']:.2f} EUR")


def build_context(period, question):
    """Assemble the retrieval context the model answers from.

    Combines a headline summary of the selected period (income, expenses,
    balance, spending per category) with the specific transactions most likely
    to be relevant: keyword matches for the question drawn from all data, plus
    the period's own rows. Keyword matches come first so they always survive the
    cap; rows are de-duplicated by id.
    """
    summary = database.period_summary(period)
    lines = [
        f"Selected period: {summary['period']}",
        (f"Totals — income: {summary['income']:.2f} EUR, "
         f"expenses: {summary['expense']:.2f} EUR, "
         f"balance: {summary['balance']:.2f} EUR"),
    ]
    if summary["categories"]:
        lines.append("Spending by category this period:")
        for cat in summary["categories"]:
            lines.append(f"  - {cat['category']}: {cat['spent']:.2f} EUR")

    terms = _keywords(question)
    matched = database.search_transactions(terms) if terms else []
    in_period = database.period_transactions(period)
    by_id = {}
    for tx in [*matched, *in_period]:
        by_id.setdefault(tx["id"], tx)
    rows = list(by_id.values())[:60]
    if rows:
        lines.append("")
        lines.append("Relevant transactions "
                     "(id | date | type | category | payee | amount):")
        lines.extend("  " + _format_tx(tx) for tx in rows)
    return "\n".join(lines)


_SYSTEM_PROMPT = (
    "You are a personal finance assistant for a single user. You are given the "
    "user's real bank transactions (already imported from their bank) as "
    "context. Answer their questions about spending, income, categories and "
    "budgets using ONLY that context; if the answer is not in the data, say so "
    "plainly instead of guessing. All amounts are in Euros.\n\n"
    "The user can also ask you to FIX their records — correct a wrong date, "
    "amount, label or category, or delete a duplicate. You cannot edit the "
    "database yourself: you PROPOSE a change as a structured action, and it is "
    "applied only after the user confirms it. Rules for actions:\n"
    "- To change anything, you MUST emit an action. Never say you changed, "
    "updated, moved or deleted something without a matching action — the words "
    "alone do nothing.\n"
    "- Because a change is not yet applied, phrase the reply as an offer, e.g. "
    "\"I can move this to Groceries — confirm below\", not \"I have moved it\".\n"
    "- Only use a tx_id that appears in the context. Never invent an id.\n"
    "- If you cannot identify the exact transaction, do NOT propose an action; "
    "ask a clarifying question in the reply instead.\n"
    "- Include only the fields you are changing in an update.\n"
    "- A category must be exactly one of the valid categories.\n\n"
    f"Valid categories: {', '.join(STANDARD_CATEGORIES)}.\n\n"
    "Always respond with a SINGLE JSON object of the form:\n"
    '{"reply": "<your natural-language answer to the user>", '
    '"actions": [<zero or more action objects>]}\n'
    "An action is either:\n"
    '  {"type": "update", "tx_id": <id>, "date": "YYYY-MM-DD" (optional), '
    '"amount": <number> (optional), "description": "<text>" (optional), '
    '"category": "<one valid category>" (optional), "reason": "<short why>"}\n'
    'or {"type": "delete", "tx_id": <id>, "reason": "<short why>"}.\n'
    "Use an empty actions list for pure questions. Keep the reply concise and "
    "written in the same language the user wrote in."
)


def _valid_date(value):
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


def _validate_actions(raw):
    """Turn the model's proposed actions into safe, confirmable action cards.

    Every action is checked against reality: the id must resolve to a real
    transaction, dates must be well-formed, amounts positive, categories on the
    canonical list. Only fields that actually differ from the current row are
    kept as changes, so an update that would be a no-op is dropped. Each returned
    action carries a ``current`` snapshot for display and never touches the
    database — application is deferred to ``apply_action`` after user confirm.
    """
    if not isinstance(raw, list):
        return []
    actions = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        atype = item.get("type")
        tx_id = item.get("tx_id")
        if atype not in ("update", "delete") or not isinstance(tx_id, int):
            continue
        current = database.get_transaction(tx_id)
        if current is None:
            continue

        if atype == "delete":
            actions.append({
                "type": "delete",
                "tx_id": tx_id,
                "reason": str(item.get("reason") or "")[:200],
                "current": _tx_snapshot(current),
                "changes": {},
            })
            continue

        changes = {}
        if _valid_date(item.get("date")) and item["date"] != current["date"]:
            changes["date"] = item["date"]

        amount = item.get("amount")
        if (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and amount > 0
                and round(float(amount), 2) != round(current["amount"], 2)):
            changes["amount"] = round(float(amount), 2)

        if isinstance(item.get("description"), str):
            desc = item["description"].strip()[:200]
            current_label = (current["merchant"] or current["description"] or "").strip()
            if desc and desc != current_label:
                changes["description"] = desc

        if isinstance(item.get("category"), str):
            cat = item["category"].strip()
            if (cat == "" or cat in STANDARD_CATEGORIES) \
                    and cat != (current["std_category"] or ""):
                changes["category"] = cat

        if not changes:
            continue
        actions.append({
            "type": "update",
            "tx_id": tx_id,
            "reason": str(item.get("reason") or "")[:200],
            "current": _tx_snapshot(current),
            "changes": changes,
        })
    return actions


def answer(messages, period=None):
    """Answer one chat turn about the user's finances.

    ``messages`` is the conversation so far as ``{"role", "content"}`` dicts
    (oldest first, ending with the user's new question). Returns
    ``{"reply", "actions", "unavailable"}``: ``reply`` is the assistant's text,
    ``actions`` a list of validated, not-yet-applied fix proposals, and
    ``unavailable`` True when the local model could not be reached.
    """
    question = next((m.get("content", "") for m in reversed(messages)
                     if m.get("role") == "user"), "")
    context = build_context(period, question)

    convo = [{"role": m["role"], "content": m["content"]}
             for m in messages[-_MAX_HISTORY:]
             if m.get("role") in ("user", "assistant") and m.get("content")]
    full = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "system", "content": "User's financial data:\n" + context},
        *convo,
    ]

    result = _chat_json(full, temperature=0.3)
    if not isinstance(result, dict) or result.get("_error"):
        return {"reply": None, "actions": [], "unavailable": True}

    reply = (result.get("reply") or "").strip()
    actions = _validate_actions(result.get("actions"))
    return {"reply": reply, "actions": actions, "unavailable": False}


def apply_action(action):
    """Apply a single user-confirmed fix to the database.

    ``action`` is one of the objects returned by :func:`answer` (or an
    equivalent ``{"type", "tx_id", "changes"}`` dict). The transaction is
    re-fetched and re-validated at apply time — never trusting the client's
    snapshot — so a stale or tampered payload cannot write bad data. Returns a
    small result dict; raises ``ValueError`` on anything invalid.
    """
    atype = action.get("type")
    tx_id = action.get("tx_id")
    current = database.get_transaction(tx_id) if isinstance(tx_id, int) else None
    if current is None:
        raise ValueError("Transaction not found")

    if atype == "delete":
        database.delete_transaction(tx_id)
        return {"applied": "delete", "tx_id": tx_id}

    if atype == "update":
        changes = action.get("changes") or {}
        date = changes.get("date", current["date"])
        amount = changes.get("amount", current["amount"])
        description = changes.get(
            "description", current["merchant"] or current["description"] or "")
        # A missing "category" key leaves the current bucket untouched; an
        # explicit "" moves the row back to Unclassified.
        category = changes["category"] or None if "category" in changes \
            else current["std_category"]

        if not _valid_date(date):
            raise ValueError("Invalid date")
        if not (isinstance(amount, (int, float)) and amount > 0):
            raise ValueError("Invalid amount")
        if category is not None and category not in STANDARD_CATEGORIES:
            raise ValueError("Unknown category")

        database.update_transaction(
            tx_id, date, float(amount), str(description).strip(), category)
        return {"applied": "update", "tx_id": tx_id}

    raise ValueError("Unknown action type")

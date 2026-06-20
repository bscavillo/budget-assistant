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


def suggest_category(description, known_categories):
    """Suggest a spending category for a transaction description."""
    cats = ", ".join(known_categories) if known_categories else "none yet"
    prompt = (
        f"Existing categories: {cats}.\n"
        f'Transaction: "{description}".\n'
        "Reply with a single short category name only, no punctuation or "
        "explanation. Reuse an existing category if one fits."
    )
    result = _chat(
        [
            {"role": "system", "content": "You categorize expenses tersely."},
            {"role": "user", "content": prompt},
        ]
    )
    # Keep only the first line/word group as a safety net.
    return result.splitlines()[0].strip().strip(".").title() if result else "Other"

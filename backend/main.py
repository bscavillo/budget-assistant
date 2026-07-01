"""FastAPI application exposing the budget assistant API and frontend.

Run with:  uvicorn main:app --reload  (from the backend directory)
"""

from datetime import date
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chat_service
import csv_import
import database
import ollama_service

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Budget Assistant", version="0.1.0")


@app.on_event("startup")
def _startup():
    database.init_db()


# --- Request models -------------------------------------------------------

class TransactionIn(BaseModel):
    date: str = Field(default_factory=lambda: date.today().isoformat())
    type: str = Field(pattern="^(income|expense)$")
    category: str = Field(min_length=1, max_length=60)
    amount: float = Field(gt=0)
    description: str = Field(default="", max_length=200)


class TransactionUpdate(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    amount: float = Field(gt=0)
    description: str = Field(default="", max_length=200)
    # Empty string means "leave/move to Unclassified"; any other value must be
    # one of the canonical spending categories (validated in the endpoint).
    category: str = Field(default="", max_length=60)


class BudgetIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    monthly_limit: float = Field(ge=0)


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    # The period currently on screen, so the assistant answers about the same
    # data the user is looking at. Same formats as the summary endpoint.
    period: str | None = None


class ChatApplyIn(BaseModel):
    # A fix proposed by /api/chat and confirmed by the user. Kept loosely typed
    # (the shape is validated in chat_service.apply_action against the real row)
    # so the client can hand back the action object it received unchanged.
    action: dict


# --- Transactions ---------------------------------------------------------

@app.get("/api/transactions")
def get_transactions():
    return database.list_transactions()


@app.post("/api/transactions", status_code=201)
def create_transaction(tx: TransactionIn):
    new_id = database.add_transaction(
        tx.date, tx.type, tx.category.strip(), tx.amount, tx.description.strip()
    )
    return {"id": new_id}


@app.put("/api/transactions/{tx_id}")
def edit_transaction(tx_id: int, tx: TransactionUpdate):
    category = tx.category.strip()
    if category and category not in ollama_service.STANDARD_CATEGORIES:
        raise HTTPException(status_code=400, detail="Unknown category")
    if not database.update_transaction(
        tx_id, tx.date, tx.amount, tx.description.strip(), category or None
    ):
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"updated": tx_id}


@app.delete("/api/transactions/{tx_id}")
def remove_transaction(tx_id: int):
    if not database.delete_transaction(tx_id):
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"deleted": tx_id}


# --- Budgets --------------------------------------------------------------

@app.get("/api/budgets")
def get_budgets():
    return database.list_budgets()


@app.post("/api/budgets")
def upsert_budget(budget: BudgetIn):
    database.set_budget(budget.category.strip(), budget.monthly_limit)
    return {"category": budget.category.strip(), "monthly_limit": budget.monthly_limit}


@app.delete("/api/budgets/{category}")
def remove_budget(category: str):
    if not database.delete_budget(category):
        raise HTTPException(status_code=404, detail="Budget not found")
    return {"deleted": category}


# --- Summary & AI ---------------------------------------------------------

@app.get("/api/summary")
def get_summary(period: str | None = None, *, background: BackgroundTasks):
    """Return spending grouped by real category for the period.

    Classification of any not-yet-categorized expenses is kicked off in the
    background (never on the request path, since it can take a while) and the
    current summary is returned immediately. ``unclassified_count`` lets the UI
    poll and watch categories fill in. Already-classified periods queue no work,
    so repeat loads are instant.
    """
    summary = database.period_summary(period)
    if summary["unclassified_count"]:
        background.add_task(ollama_service.ensure_classified, period)
    # Surface classifier state so the UI can keep polling through slow batches
    # but stop (and warn) when Ollama is actually unreachable.
    summary["classifier"] = ollama_service.classifier_status()
    return summary


@app.get("/api/categories")
def get_categories():
    """The canonical spending categories used for classification and budgets."""
    return {"categories": ollama_service.STANDARD_CATEGORIES}


@app.post("/api/chat")
def chat(body: ChatIn):
    """Answer a finance question (RAG over the user's data) or propose a fix.

    The reply and any proposed fixes are returned; nothing is written to the
    database here — fixes are applied only via ``/api/chat/apply`` after the
    user confirms them. Returns 503 when the local Ollama model is unreachable.
    """
    result = chat_service.answer(
        [m.model_dump() for m in body.messages], body.period)
    if result.get("unavailable"):
        raise HTTPException(
            status_code=503,
            detail="Ollama is unavailable. Is the local server running?")
    return result


@app.post("/api/chat/apply")
def chat_apply(body: ChatApplyIn):
    """Apply a single fix that the user confirmed from the chat."""
    try:
        return chat_service.apply_action(body.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/trend")
def trend(period: str | None = None):
    """Monthly income/expense trend anchored on the selected period.

    The chart follows the date picker instead of always ending on today: a
    single month shows the six months up to it, a full year shows that year's
    twelve months, and a year-to-date selection shows January through the
    latest month in view.
    """
    anchor, months = _trend_window(period)
    return {"trend": database.monthly_totals(months, anchor)}


def _trend_window(period):
    """Map a reporting ``period`` to a ``(anchor_month, months)`` window.

    ``anchor_month`` is the inclusive ``YYYY-MM`` end of the trend; ``months``
    is how many months back from it to show. Mirrors the period formats in
    ``database._period_clause``.
    """
    today = date.today()
    if not period:
        return today.strftime("%Y-%m"), 6
    period = period.strip()
    if period.endswith("-ytd"):
        year = int(period[:4])
        last = today.month if year == today.year else 12
        return f"{year:04d}-{last:02d}", last
    if len(period) == 4:  # YYYY — the whole calendar year
        return f"{int(period):04d}-12", 12
    year, _, month = period.partition("-")
    return f"{int(year):04d}-{int(month):02d}", 6


@app.get("/api/latest-month")
def latest_month():
    return {"month": database.latest_transaction_month()}


@app.get("/api/months")
def months():
    """The distinct ``YYYY-MM`` months that have data, so the date picker can
    decide between a "full year" and a "year to date" whole-period option."""
    return {"months": database.months_with_data()}


def _ingest(parsed, background):
    """Store parsed transactions and return an import summary.

    Skips rows already present (so overlapping exports don't create
    duplicates), inserts the rest, and schedules background AI classification
    for the months that gained expenses so categories are usually ready by the
    time the user looks.
    """
    imported = 0
    skipped = 0
    months = set()
    for tx in parsed:
        if database.transaction_exists(tx["date"], tx["type"], tx["amount"],
                                       tx["description"]):
            skipped += 1
            continue
        database.add_transaction(tx["date"], tx["type"], tx["category"],
                                 tx["amount"], tx["description"])
        imported += 1
        if tx["type"] == "expense":
            months.add(tx["date"][:7])  # YYYY-MM

    for month in months:
        background.add_task(ollama_service.ensure_classified, month)

    return {"parsed": len(parsed), "imported": imported, "skipped": skipped}


@app.post("/api/import/csv")
async def import_csv(background: BackgroundTasks, file: UploadFile = File(...)):
    """Import transactions from a German bank CSV export."""
    raw = await file.read()
    try:
        parsed = csv_import.parse(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ingest(parsed, background)


# --- Frontend -------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# Serve static assets (app.js, style.css) under /static.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

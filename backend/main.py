"""FastAPI application exposing the budget assistant API and frontend.

Run with:  uvicorn main:app --reload  (from the backend directory)
"""

from datetime import date
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import database
import ollama_service
import postbank_import

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


class BudgetIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    monthly_limit: float = Field(ge=0)


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
def get_summary(period: str | None = None):
    """Return spending grouped by real category for the period.

    Any not-yet-classified expenses in the period are classified and persisted
    first, so categories "just appear" on view without a manual step. Already
    classified periods make no AI call, so repeat loads are instant.
    """
    ollama_service.ensure_classified(period)
    return database.period_summary(period)


@app.get("/api/advice")
def get_advice(period: str | None = None):
    """AI suggestions on what to trim or stop for the given period."""
    summary = database.period_summary(period)
    return ollama_service.generate_advice(summary)


@app.get("/api/categories")
def get_categories():
    """The canonical spending categories used for classification and budgets."""
    return {"categories": ollama_service.STANDARD_CATEGORIES}


@app.get("/api/trend")
def trend(months: int = 6):
    months = max(1, min(months, 24))
    return {"trend": database.monthly_totals(months)}


@app.get("/api/latest-month")
def latest_month():
    return {"month": database.latest_transaction_month()}


@app.post("/api/import/postbank")
async def import_postbank(background: BackgroundTasks, file: UploadFile = File(...)):
    """Import transactions from a Postbank CSV export.

    Parses the file, skips rows already present (so overlapping exports don't
    create duplicates), inserts the rest, and schedules background AI
    classification for the months that gained transactions so categories are
    usually ready by the time the user looks.
    """
    raw = await file.read()
    try:
        parsed = postbank_import.parse(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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


# --- Frontend -------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# Serve static assets (app.js, style.css) under /static.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

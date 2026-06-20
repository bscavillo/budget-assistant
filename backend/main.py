"""FastAPI application exposing the budget assistant API and frontend.

Run with:  uvicorn main:app --reload  (from the backend directory)
"""

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
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


class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    month: str | None = None


class CategorizeIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)


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
def get_summary(month: str | None = None):
    return database.monthly_summary(month)


@app.get("/api/insights")
def get_insights(month: str | None = None):
    summary = database.monthly_summary(month)
    return {"insights": ollama_service.generate_insights(summary)}


@app.post("/api/ask")
def ask(payload: QuestionIn):
    summary = database.monthly_summary(payload.month)
    transactions = database.list_transactions()
    answer = ollama_service.answer_question(payload.question, summary, transactions)
    return {"answer": answer}


@app.post("/api/import/postbank")
async def import_postbank(file: UploadFile = File(...)):
    """Import transactions from a Postbank CSV export.

    Parses the file, skips rows already present (so overlapping exports don't
    create duplicates), and inserts the rest.
    """
    raw = await file.read()
    try:
        parsed = postbank_import.parse(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    imported = 0
    skipped = 0
    for tx in parsed:
        if database.transaction_exists(tx["date"], tx["type"], tx["amount"],
                                       tx["description"]):
            skipped += 1
            continue
        database.add_transaction(tx["date"], tx["type"], tx["category"],
                                 tx["amount"], tx["description"])
        imported += 1

    return {"parsed": len(parsed), "imported": imported, "skipped": skipped}


@app.post("/api/categorize")
def categorize(payload: CategorizeIn):
    known = sorted({b["category"] for b in database.list_budgets()} |
                   {t["category"] for t in database.list_transactions()})
    suggestion = ollama_service.suggest_category(payload.description, known)
    return {"category": suggestion}


# --- Frontend -------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# Serve static assets (app.js, style.css) under /static.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

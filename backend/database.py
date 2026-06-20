"""SQLite persistence layer for the budget assistant.

A single local database file holds all transactions and category budgets.
The module deliberately uses only the standard library so the backend has no
database server dependency.
"""

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "budget.db"


@contextmanager
def get_connection():
    """Yield a SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they do not exist yet."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                type        TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                category    TEXT NOT NULL,
                amount      REAL NOT NULL CHECK (amount >= 0),
                description TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                category      TEXT PRIMARY KEY,
                monthly_limit REAL NOT NULL CHECK (monthly_limit >= 0)
            )
            """
        )


# --- Transactions ---------------------------------------------------------

def list_transactions():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY date DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def add_transaction(tx_date, tx_type, category, amount, description):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO transactions (date, type, category, amount, description)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tx_date, tx_type, category, amount, description),
        )
        return cursor.lastrowid


def delete_transaction(tx_id):
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        return cursor.rowcount > 0


def transaction_exists(tx_date, tx_type, amount, description):
    """Return True if an identical transaction is already stored.

    Used to skip duplicates when re-importing a bank CSV that overlaps a
    previous export. Category is ignored because it may be re-assigned later.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM transactions
            WHERE date = ? AND type = ? AND amount = ? AND description = ?
            LIMIT 1
            """,
            (tx_date, tx_type, amount, description),
        ).fetchone()
        return row is not None


# --- Budgets --------------------------------------------------------------

def list_budgets():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM budgets ORDER BY category").fetchall()
        return [dict(row) for row in rows]


def set_budget(category, monthly_limit):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO budgets (category, monthly_limit)
            VALUES (?, ?)
            ON CONFLICT(category) DO UPDATE SET monthly_limit = excluded.monthly_limit
            """,
            (category, monthly_limit),
        )


def delete_budget(category):
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM budgets WHERE category = ?", (category,))
        return cursor.rowcount > 0


# --- Aggregations ---------------------------------------------------------

def latest_transaction_month():
    """Return the most recent ISO ``YYYY-MM`` with a transaction, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT substr(date, 1, 7) AS month FROM transactions "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return row["month"] if row else None


def expenses_for_month(month=None):
    """Return all expense transactions for the given ISO ``YYYY-MM`` month."""
    if month is None:
        month = date.today().strftime("%Y-%m")
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE type = 'expense' AND substr(date, 1, 7) = ?
            ORDER BY date DESC, id DESC
            """,
            (month,),
        ).fetchall()
        return [dict(row) for row in rows]


def monthly_totals(months=6):
    """Return income/expense totals per month for the last ``months`` months.

    Months with no transactions are included with zero totals so the trend
    line is continuous.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT substr(date, 1, 7) AS month, type,
                   COALESCE(SUM(amount), 0) AS total
            FROM transactions
            GROUP BY month, type
            """
        ).fetchall()

    by_month = {}
    for row in rows:
        by_month.setdefault(row["month"], {})[row["type"]] = row["total"]

    today = date.today()
    sequence = []
    year, month = today.year, today.month
    for _ in range(months):
        key = f"{year:04d}-{month:02d}"
        entry = by_month.get(key, {})
        sequence.append(
            {
                "month": key,
                "income": round(entry.get("income", 0.0), 2),
                "expense": round(entry.get("expense", 0.0), 2),
            }
        )
        month -= 1
        if month == 0:
            month = 12
            year -= 1

    return list(reversed(sequence))


def monthly_summary(month=None):
    """Return income/expense totals and per-category spending for a month.

    ``month`` is an ISO ``YYYY-MM`` string; defaults to the current month.
    """
    if month is None:
        month = date.today().strftime("%Y-%m")

    with get_connection() as conn:
        totals = conn.execute(
            """
            SELECT type, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE substr(date, 1, 7) = ?
            GROUP BY type
            """,
            (month,),
        ).fetchall()

        per_category = conn.execute(
            """
            SELECT category, COALESCE(SUM(amount), 0) AS spent
            FROM transactions
            WHERE type = 'expense' AND substr(date, 1, 7) = ?
            GROUP BY category
            ORDER BY spent DESC
            """,
            (month,),
        ).fetchall()

        budgets = {row["category"]: row["monthly_limit"] for row in
                   conn.execute("SELECT * FROM budgets").fetchall()}

    income = 0.0
    expense = 0.0
    for row in totals:
        if row["type"] == "income":
            income = row["total"]
        elif row["type"] == "expense":
            expense = row["total"]

    categories = []
    for row in per_category:
        spent = row["spent"]
        limit = budgets.get(row["category"])
        categories.append(
            {
                "category": row["category"],
                "spent": round(spent, 2),
                "limit": limit,
                "remaining": round(limit - spent, 2) if limit is not None else None,
            }
        )

    return {
        "month": month,
        "income": round(income, 2),
        "expense": round(expense, 2),
        "balance": round(income - expense, 2),
        "categories": categories,
    }

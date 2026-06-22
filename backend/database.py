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
    """Create tables if they do not exist yet, then apply migrations."""
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
        _migrate(conn)


def _migrate(conn):
    """Add columns introduced after the initial schema, idempotently.

    ``category`` keeps the raw bank ``Umsatzart`` text from the import; the
    real, AI-assigned spending category lives in ``std_category`` (NULL until
    a transaction has been classified) and the cleaned-up merchant name in
    ``merchant``. Existing rows simply start unclassified and get filled in by
    the lazy classification pass.
    """
    existing = {row["name"] for row in
                conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "std_category" not in existing:
        conn.execute("ALTER TABLE transactions ADD COLUMN std_category TEXT")
    if "merchant" not in existing:
        conn.execute("ALTER TABLE transactions ADD COLUMN merchant TEXT")


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


# --- Classification -------------------------------------------------------

def unclassified_expenses(period=None):
    """Return expense transactions in the period that lack a real category.

    These are the rows the AI still needs to look at (``std_category IS NULL``).
    """
    condition, params = _period_clause(period)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM transactions
            WHERE type = 'expense' AND std_category IS NULL AND {condition}
            ORDER BY date DESC, id DESC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def set_classifications(results):
    """Persist AI classifications for many transactions in one transaction.

    ``results`` is an iterable of ``(id, std_category, merchant)`` tuples.
    """
    with get_connection() as conn:
        conn.executemany(
            "UPDATE transactions SET std_category = ?, merchant = ? WHERE id = ?",
            [(std_category, merchant, tx_id)
             for tx_id, std_category, merchant in results],
        )


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

def _period_clause(period=None):
    """Return an SQL ``(condition, params)`` pair for a reporting period.

    ``period`` may be:
      * ``"YYYY-MM"``  – a single month (the default when ``period`` is falsy)
      * ``"YYYY"``     – a whole calendar year
      * ``"YYYY-ytd"`` – the year up to and including today
    The condition is meant to be AND-ed into a ``WHERE`` clause on ``date``.
    """
    if not period:
        period = date.today().strftime("%Y-%m")
    period = period.strip()
    if period.endswith("-ytd"):
        year = period[:4]
        return "substr(date, 1, 4) = ? AND date <= ?", (year, date.today().isoformat())
    if len(period) == 4:  # YYYY
        return "substr(date, 1, 4) = ?", (period,)
    # YYYY-MM; normalise an unpadded month (e.g. "2026-3") so it matches the
    # zero-padded ISO dates stored in the database.
    year, _, month = period.partition("-")
    return "substr(date, 1, 7) = ?", (f"{year}-{month.zfill(2)}",)


def latest_transaction_month():
    """Return the most recent ISO ``YYYY-MM`` with a transaction, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT substr(date, 1, 7) AS month FROM transactions "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return row["month"] if row else None


def monthly_totals(months=6, anchor=None):
    """Return income/expense totals per month for ``months`` months.

    The window ends on ``anchor`` (an inclusive ``YYYY-MM``, defaulting to the
    current month) and runs back from there, so the trend can follow whichever
    period the UI has selected rather than always ending on today. Months with
    no transactions are included with zero totals so the trend line stays
    continuous.
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

    if anchor:
        year, month = int(anchor[:4]), int(anchor[5:7])
    else:
        today = date.today()
        year, month = today.year, today.month
    sequence = []
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


# How much weight a month of spending keeps per month into the past. Recent
# months dominate the average so suggested budgets track current prices and
# income (inflation, a raise, a move) instead of being dragged down by old data.
# 0.9/month is a half-life of ~6.6 months: the last half-year sets the tone while
# older months still nudge the number.
_RECENCY_DECAY = 0.9


def _months_between(earlier, later):
    """Whole months from ``earlier`` to ``later`` (both ``YYYY-MM`` strings)."""
    ey, em = int(earlier[:4]), int(earlier[5:7])
    ly, lm = int(later[:4]), int(later[5:7])
    return (ly - ey) * 12 + (lm - em)


def category_spending_stats():
    """Per-category, recency-weighted spending stats over the whole history.

    For every real (AI-assigned) ``std_category`` it returns the total spent,
    how many distinct months it spans, and a recency-weighted average monthly
    spend: each active month is weighted by ``_RECENCY_DECAY`` raised to its age
    in months, so recent months count for more and the figure reflects current
    spending levels rather than a flat lifetime mean. Unclassified rows are
    excluded — they have no category to budget for yet. Period-agnostic by
    design (all datapoints), which is the "prior behavior" the suggestion model
    reasons over.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT std_category AS category,
                   substr(date, 1, 7) AS month,
                   COALESCE(SUM(amount), 0) AS total,
                   COUNT(*) AS tx_count
            FROM transactions
            WHERE type = 'expense' AND std_category IS NOT NULL
            GROUP BY std_category, month
            """
        ).fetchall()

    if not rows:
        return []

    # Anchor recency on the latest month that has any classified spending, so
    # month ages are measured from "now" in the data rather than today's date.
    latest = max(row["month"] for row in rows)

    agg = {}
    for row in rows:
        bucket = agg.setdefault(
            row["category"],
            {"total": 0.0, "months": 0, "tx_count": 0,
             "weighted_sum": 0.0, "weight_total": 0.0},
        )
        weight = _RECENCY_DECAY ** _months_between(row["month"], latest)
        bucket["total"] += row["total"]
        bucket["months"] += 1
        bucket["tx_count"] += row["tx_count"]
        bucket["weighted_sum"] += weight * row["total"]
        bucket["weight_total"] += weight

    stats = []
    for category, b in agg.items():
        weighted_avg = (b["weighted_sum"] / b["weight_total"]
                        if b["weight_total"] else 0.0)
        stats.append(
            {
                "category": category,
                "total": round(b["total"], 2),
                "months": b["months"],
                "tx_count": b["tx_count"],
                "avg_monthly": round(weighted_avg, 2),
            }
        )
    return stats


UNCLASSIFIED = "Unclassified"


def period_summary(period=None):
    """Return income/expense totals and per-category spending for a period.

    Spending is grouped by the real, AI-assigned ``std_category`` (rows not yet
    classified fold into an ``Unclassified`` bucket). Each category carries its
    own transactions so the UI can drill in without a second request, and the
    relevant budget (matched on the same category) for over-budget flagging.

    ``period`` accepts the formats described in ``_period_clause`` (month, full
    year, or year-to-date); defaults to the current month.
    """
    condition, params = _period_clause(period)

    with get_connection() as conn:
        totals = conn.execute(
            f"""
            SELECT type, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE {condition}
            GROUP BY type
            """,
            params,
        ).fetchall()

        expenses = conn.execute(
            f"""
            SELECT id, date, description, merchant, amount, std_category
            FROM transactions
            WHERE type = 'expense' AND {condition}
            ORDER BY amount DESC
            """,
            params,
        ).fetchall()

        incomes = conn.execute(
            f"""
            SELECT id, date, description, merchant, amount
            FROM transactions
            WHERE type = 'income' AND {condition}
            ORDER BY amount DESC
            """,
            params,
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

    grouped = {}
    unclassified_count = 0
    for row in expenses:
        category = row["std_category"] or UNCLASSIFIED
        if row["std_category"] is None:
            unclassified_count += 1
        bucket = grouped.setdefault(category, {"spent": 0.0, "transactions": []})
        bucket["spent"] += row["amount"]
        bucket["transactions"].append(
            {
                "id": row["id"],
                "date": row["date"],
                # Prefer the cleaned-up merchant; fall back to the raw text.
                "description": row["merchant"] or row["description"],
                "amount": round(row["amount"], 2),
            }
        )

    categories = []
    for category, bucket in grouped.items():
        spent = bucket["spent"]
        limit = budgets.get(category)
        categories.append(
            {
                "category": category,
                "spent": round(spent, 2),
                "limit": limit,
                "remaining": round(limit - spent, 2) if limit is not None else None,
                "transactions": bucket["transactions"],
            }
        )
    categories.sort(key=lambda c: c["spent"], reverse=True)

    income_transactions = [
        {
            "id": row["id"],
            "date": row["date"],
            "description": row["merchant"] or row["description"],
            "amount": round(row["amount"], 2),
        }
        for row in incomes
    ]

    return {
        "period": period or date.today().strftime("%Y-%m"),
        "income": round(income, 2),
        "expense": round(expense, 2),
        "balance": round(income - expense, 2),
        "categories": categories,
        "income_transactions": income_transactions,
        "unclassified_count": unclassified_count,
    }

"""Parser for Wells Fargo categorized CSV exports.

This is the export that has a header row and per-transaction categories::

    Master Category,Subcategory,Date,Location,Payee,Description,Payment Method,Amount,
    "Food/Drink","Fast Food","04/07/2025"," ","SHAKE SHACK","PURCHASE ... CHARLOTTE","Debit Card ...9215","$13.83",

Columns are matched by header name (case-insensitive), so a missing, extra or
reordered column is tolerated. Dates are US ``MM/DD/YYYY``; amounts are
US-formatted, ``$``-prefixed magnitudes (``$1,234.56``) with **no sign** — every
row is positive, so income vs. expense is inferred from the category, not the
amount.

There is a real category column, but the canonical spending category is still
assigned later by the AI classification pass. The imported
``Master Category / Subcategory`` is kept as the raw ``category`` text, which
doubles as a strong hint for that pass (rows stay ``std_category IS NULL`` until
classified, exactly like every other import).
"""

import csv
import io
import re
from datetime import datetime

# Header keywords (lower-case) used to locate columns by name.
MASTER_KEYS = ("master category", "master")
SUB_KEYS = ("subcategory", "sub category")
DATE_KEYS = ("date",)
PAYEE_KEYS = ("payee",)
DESC_KEYS = ("description", "memo")
AMOUNT_KEYS = ("amount",)

# Category words that mark a row as money coming IN. The amount is always a
# positive magnitude, so direction can only come from the category; anything
# that doesn't look like income is treated as an expense.
INCOME_HINTS = (
    "incoming", "income", "deposit", "paycheck", "payroll", "salary",
    "dividend", "refund", "reimburs", "rebate", "cashback", "cash back",
)


def _decode(raw):
    """Decode bytes trying the encodings Wells Fargo exports actually use."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize(text):
    """Lower-case and collapse whitespace for robust header matching."""
    return " ".join((text or "").strip().strip('"').lower().split())


def _parse_amount(value):
    """Parse a US-formatted ``$`` amount into a non-negative magnitude, or None.

    The export never signs its amounts, so the sign is dropped here and the
    direction is decided from the category instead.
    """
    if value is None:
        return None
    s = re.sub(r"[$\s\xa0]", "", value.strip())
    if not s:
        return None
    # Strip any stray sign / parentheses defensively; magnitude is all we use.
    s = s.strip("()+-").replace(",", "")
    try:
        return abs(float(s))
    except ValueError:
        return None


def _parse_date(value):
    """Parse a US date string into ISO ``YYYY-MM-DD``, or None."""
    if not value:
        return None
    value = value.strip().strip('"')
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _match_column(headers, keys):
    """Return the index of the first header equal to / containing any key."""
    for i, h in enumerate(headers):
        if h in keys or any(k in h for k in keys):
            return i
    return None


def _cell(row, idx):
    """Return a stripped cell value, or '' if the column is missing."""
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def _find_header(rows):
    """Return the index of the header row (needs date + amount columns), or None."""
    for i, row in enumerate(rows):
        norm = [_normalize(c) for c in row]
        if (_match_column(norm, DATE_KEYS) is not None
                and _match_column(norm, AMOUNT_KEYS) is not None):
            return i
    return None


def parse(raw_bytes):
    """Parse a Wells Fargo categorized CSV into a list of transaction dicts.

    Each dict has keys: ``date``, ``type``, ``category``, ``amount``,
    ``description``. Rows that don't yield a valid date and amount are skipped.
    Raises ``ValueError`` if no header row (with at least date and amount
    columns) can be found.
    """
    text = _decode(raw_bytes)
    rows = list(csv.reader(io.StringIO(text)))

    header_idx = _find_header(rows)
    if header_idx is None:
        raise ValueError(
            "Could not find a Wells Fargo header row. Expected columns like "
            "'Date' and 'Amount' (e.g. Master Category, Subcategory, Date, "
            "Location, Payee, Description, Payment Method, Amount)."
        )

    headers = [_normalize(c) for c in rows[header_idx]]
    master_col = _match_column(headers, MASTER_KEYS)
    sub_col = _match_column(headers, SUB_KEYS)
    date_col = _match_column(headers, DATE_KEYS)
    payee_col = _match_column(headers, PAYEE_KEYS)
    desc_col = _match_column(headers, DESC_KEYS)
    amount_col = _match_column(headers, AMOUNT_KEYS)

    transactions = []
    for row in rows[header_idx + 1:]:
        tx_date = _parse_date(_cell(row, date_col))
        if tx_date is None:
            continue
        amount = _parse_amount(_cell(row, amount_col))
        if not amount:
            continue

        master = _cell(row, master_col)
        sub = _cell(row, sub_col)
        category = " / ".join(c for c in (master, sub) if c)[:60] or "Uncategorized"

        blob = f"{master} {sub}".lower()
        tx_type = "income" if any(h in blob for h in INCOME_HINTS) else "expense"

        # Lead with the clean Payee; append the noisy memo (whitespace
        # collapsed) so the merchant, city and reference still reach the
        # classifier. Either part may be empty (e.g. transfers have no payee).
        payee = _cell(row, payee_col)
        memo = " ".join(_cell(row, desc_col).split())
        description = " - ".join(p for p in (payee, memo) if p)[:200] or "Imported"

        transactions.append(
            {
                "date": tx_date,
                "type": tx_type,
                "category": category,
                "amount": round(amount, 2),
                "description": description,
            }
        )

    if not transactions:
        raise ValueError(
            "No Wells Fargo transactions found. Expected rows like "
            '"Food/Drink","Fast Food","04/07/2025"," ","SHAKE SHACK",'
            '"PURCHASE ...","Debit Card ...1234","$13.83".'
        )
    return transactions

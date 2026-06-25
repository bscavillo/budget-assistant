"""Parser for Wells Fargo account / credit-card CSV exports.

Wells Fargo exports a headerless CSV with five quoted columns —
``date, amount, "*", "", description`` — e.g.::

    "06/15/2024","-12.34","*","","STARBUCKS STORE 123 SEATTLE WA"
    "06/14/2024","2000.00","*","","DIRECT DEPOSIT PAYROLL"

Dates are US ``MM/DD/YYYY`` and amounts use the US convention (``1,234.56``)
with a leading minus for debits (expenses). There is no category column, so
imported rows are left ``Uncategorized`` for the AI classification pass.
"""

import csv
import io
import re
from datetime import datetime


def _decode(raw):
    """Decode bytes trying the encodings Wells Fargo exports actually use."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_amount(value):
    """Parse a US-formatted monetary string into a signed float, or None."""
    if value is None:
        return None
    s = value.strip().replace("\xa0", " ")
    s = re.sub(r"[$\s]", "", s)
    if not s:
        return None
    # Parentheses denote a negative on some exports: "(12.34)" -> -12.34.
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.startswith("-") or s.endswith("-"):
        negative = True
        s = s.strip("+-")
    s = s.lstrip("+")
    # US format: ',' = thousands, '.' = decimal.
    s = s.replace(",", "")
    try:
        amount = float(s)
    except ValueError:
        return None
    return -amount if negative else amount


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


def parse(raw_bytes):
    """Parse Wells Fargo CSV bytes into a list of transaction dicts.

    Each dict has keys: ``date``, ``type``, ``category``, ``amount``,
    ``description``. The file is headerless, so any line that doesn't yield a
    valid date and amount (including a stray header row) is skipped. Raises
    ``ValueError`` if no usable rows are found.
    """
    text = _decode(raw_bytes)
    reader = csv.reader(io.StringIO(text))

    transactions = []
    for row in reader:
        if len(row) < 2:
            continue
        tx_date = _parse_date(row[0])
        if tx_date is None:
            continue
        amount = _parse_amount(row[1])
        if not amount:
            continue

        # The description is the memo column; skip the "*" flag and the empty
        # padding columns Wells Fargo places between the amount and the memo.
        description = ""
        for cell in row[2:]:
            cell = cell.strip()
            if cell and cell != "*":
                description = cell
        description = description[:200] or "Imported"

        transactions.append(
            {
                "date": tx_date,
                "type": "income" if amount > 0 else "expense",
                "category": "Uncategorized",
                "amount": round(abs(amount), 2),
                "description": description,
            }
        )

    if not transactions:
        raise ValueError(
            'No Wells Fargo transactions found. Expected rows like '
            '"06/15/2024","-12.34","*","","DESCRIPTION".'
        )
    return transactions

"""Parser for Postbank account-statement CSV exports.

Postbank's CSV format has changed over the years (especially after the
Deutsche Bank migration), so this parser is deliberately tolerant: it
auto-detects the encoding, the delimiter, the header row, and maps columns by
matching German header keywords rather than fixed positions.

Amounts use the German convention (``1.234,56``) and dates ``DD.MM.YYYY``.
"""

import csv
import io
import re
from datetime import datetime

# Header keywords (lower-case, accent-insensitive) used to locate columns.
DATE_KEYS = ("buchungstag", "buchungsdatum", "datum", "wertstellung")
# "betrag" is the canonical Postbank amount header. "umsatz" is a fallback for
# other layouts, but must not collide with the "umsatzart" type column.
AMOUNT_KEYS = ("betrag",)
PURPOSE_KEYS = ("verwendungszweck", "buchungsdetails", "buchungstext", "vorgang")
PARTY_KEYS = ("auftraggeber", "empfanger", "empfaenger", "begunstigter",
              "beguenstigter", "name", "zahlungsbeteiligter")
TYPE_KEYS = ("umsatzart", "buchungsart")
# Some Postbank exports split direction into separate debit/credit columns.
DEBIT_KEYS = ("soll",)
CREDIT_KEYS = ("haben",)

# A header line must contain at least a date-ish and an amount-ish keyword.
HEADER_HINTS = DATE_KEYS + AMOUNT_KEYS


def _normalize(text):
    """Lower-case and strip German accents for robust header matching."""
    text = text.lower().strip().strip('"')
    return (text.replace("ä", "a").replace("ö", "o").replace("ü", "u")
                .replace("ß", "ss"))


def _decode(raw):
    """Decode bytes trying the encodings Postbank exports actually use."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _detect_delimiter(line):
    """Pick the most frequent delimiter among tab, semicolon, and comma."""
    counts = {d: line.count(d) for d in ("\t", ";", ",")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ";"


def _find_header(lines):
    """Return (index, delimiter) of the header row, or (None, ';')."""
    for i, line in enumerate(lines):
        norm = _normalize(line)
        has_date = any(k in norm for k in DATE_KEYS)
        has_amount = "betrag" in norm or "umsatz" in norm
        if has_date and has_amount:
            return i, _detect_delimiter(line)
    return None, ";"


def _match_column(headers, keys):
    """Return the index of the first header containing any of ``keys``."""
    for i, h in enumerate(headers):
        norm = _normalize(h)
        if any(k in norm for k in keys):
            return i
    return None


def _match_amount_column(headers):
    """Locate the amount column, preferring 'Betrag' over ambiguous 'Umsatz'."""
    idx = _match_column(headers, AMOUNT_KEYS)
    if idx is not None:
        return idx
    # Fallback: a column named 'Umsatz' but not the 'Umsatzart' type column.
    for i, h in enumerate(headers):
        norm = _normalize(h)
        if "umsatz" in norm and "umsatzart" not in norm:
            return i
    return None


def _parse_amount(value):
    """Parse a German-formatted monetary string into a float, or None."""
    if value is None:
        return None
    s = value.strip().replace("\xa0", " ")
    s = re.sub(r"[€\s]", "", s)
    if not s:
        return None
    trailing_minus = s.endswith("-")
    s = s.rstrip("+-").lstrip("+")
    # German format: '.' = thousands, ',' = decimal.
    s = s.replace(".", "").replace(",", ".")
    try:
        amount = float(s)
    except ValueError:
        return None
    return -amount if trailing_minus and amount > 0 else amount


def _parse_date(value):
    """Parse a date string into ISO ``YYYY-MM-DD``, or None."""
    if not value:
        return None
    value = value.strip().strip('"')
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse(raw_bytes):
    """Parse Postbank CSV bytes into a list of transaction dicts.

    Each dict has keys: ``date``, ``type``, ``category``, ``amount``,
    ``description``. Rows that can't be parsed (no valid date or amount) are
    skipped. Raises ``ValueError`` if no usable header/columns are found.
    """
    text = _decode(raw_bytes)
    lines = text.splitlines()
    header_idx, delimiter = _find_header(lines)
    if header_idx is None:
        raise ValueError(
            "Could not find a transaction header row. Expected German column "
            "names like 'Buchungstag' and 'Betrag'."
        )

    reader = csv.reader(lines[header_idx:], delimiter=delimiter)
    rows = list(reader)
    headers = rows[0]

    date_col = _match_column(headers, DATE_KEYS)
    amount_col = _match_amount_column(headers)
    purpose_col = _match_column(headers, PURPOSE_KEYS)
    party_col = _match_column(headers, PARTY_KEYS)
    type_col = _match_column(headers, TYPE_KEYS)
    debit_col = _match_column(headers, DEBIT_KEYS)
    credit_col = _match_column(headers, CREDIT_KEYS)

    if date_col is None or (amount_col is None and credit_col is None and
                            debit_col is None):
        raise ValueError("Could not locate the date and amount columns.")

    transactions = []
    for row in rows[1:]:
        if len(row) <= date_col:
            continue
        tx_date = _parse_date(row[date_col])
        if tx_date is None:
            continue

        resolved = _resolve_amount(row, amount_col, debit_col, credit_col)
        if resolved is None:
            continue
        tx_type, value = resolved

        parts = []
        party = _cell(row, party_col)
        if party:
            parts.append(party)
        purpose = _cell(row, purpose_col)
        if purpose:
            parts.append(purpose)
        description = " - ".join(parts)[:200] or "Imported"

        category = _cell(row, type_col) or "Uncategorized"

        transactions.append(
            {
                "date": tx_date,
                "type": tx_type,
                "category": category[:60],
                "amount": round(value, 2),
                "description": description,
            }
        )

    return transactions


def _cell(row, idx):
    """Return a stripped cell value, or '' if the column is missing."""
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def _resolve_amount(row, amount_col, debit_col, credit_col):
    """Determine ``(type, positive_value)`` for a row, or None to skip it.

    Prefers the unambiguous Soll (debit) / Haben (credit) columns when present
    and filled, otherwise falls back to the signed Betrag column.
    """
    credit = _parse_amount(_cell(row, credit_col)) if credit_col is not None else None
    debit = _parse_amount(_cell(row, debit_col)) if debit_col is not None else None

    if credit:
        return "income", abs(credit)
    if debit:
        return "expense", abs(debit)

    amount = _parse_amount(_cell(row, amount_col)) if amount_col is not None else None
    if amount:
        return ("income" if amount > 0 else "expense"), abs(amount)
    return None

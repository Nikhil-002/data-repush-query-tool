"""
parsing.py
==========
Pure input parsing, no database and no GUI:

  * parse_dt()           - turn a typed date/time string into a datetime
  * load_meter_numbers() - read a list of meter numbers from CSV / TXT / XLSX

This is the piece that lets a user UPLOAD a meter-number file instead of having
to create a table in the database first.
"""

import csv
import os
import datetime as dt

try:
    import openpyxl
except ImportError:
    openpyxl = None

# Accepted date/time formats, tried in order.
DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_dt(s):
    """Parse 'YYYY-MM-DD [HH:MM[:SS]]' into a datetime, else raise ValueError."""
    s = (s or "").strip()
    for fmt in DT_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date/time: '{s}'. Use YYYY-MM-DD HH:MM")


def _read_csv_or_txt(path, ext):
    """Read meter numbers from a .csv (first column) or .txt (whitespace split)."""
    values = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        if ext == ".csv":
            for row in csv.reader(f):
                if not row:
                    continue
                first = str(row[0]).strip()
                if first:
                    values.append(first)
        else:  # .txt - one or many numbers per line
            for line in f:
                line = line.strip()
                if not line:
                    continue
                values.extend(tok.strip() for tok in line.split() if tok.strip())
    return values


def _read_xlsx(path):
    """Read meter numbers from the first column of the active Excel sheet."""
    if openpyxl is None:
        raise ImportError(
            "Excel files require openpyxl. Install it with:\n"
            "    pip install openpyxl"
        )
    values = []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    for row in ws.iter_rows(values_only=True):
        if not row or row[0] is None:
            continue
        item = str(row[0]).strip()
        if item:
            values.append(item)
    return values


def _dedupe(values):
    """Keep order, drop blanks and duplicates."""
    seen = set()
    unique = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def load_meter_numbers(path):
    """
    Load meter numbers from a CSV / TXT / XLSX file and return a de-duplicated
    list. Raises ValueError / ImportError with a friendly message on problems.
    """
    if not path:
        raise ValueError("No meter file selected.")
    if not os.path.exists(path):
        raise ValueError(f"Meter file not found: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt"):
        values = _read_csv_or_txt(path, ext)
    elif ext in (".xlsx", ".xls"):
        values = _read_xlsx(path)
    else:
        raise ValueError(
            "Unsupported meter file format. Use .csv, .txt, .xlsx, or .xls."
        )

    unique = _dedupe(values)
    if not unique:
        raise ValueError("No meter numbers found in the selected file.")
    return unique

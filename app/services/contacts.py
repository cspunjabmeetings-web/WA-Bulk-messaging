"""Read CSV / Excel files and extract clean, de-duplicated WhatsApp numbers."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import csv
import phonenumbers

PHONE_HEADER_HINTS = (
    "phone", "mobile", "number", "whatsapp", "contact", "cell", "msisdn", "tel", "nmbr", "no",
    "نمبر", "فون", "موبائل",
)
NAME_HEADER_HINTS = ("name", "full name", "customer", "نام")


class ContactFileError(ValueError):
    pass


@dataclass
class ParsedContacts:
    columns: list[str]
    phone_column: str
    name_column: str | None
    total_rows: int
    valid: list[dict] = field(default_factory=list)       # {"phone", "raw", "vars"}
    invalid: list[dict] = field(default_factory=list)     # {"row", "raw", "reason"}
    duplicates: int = 0

    def summary(self, sample: int = 10) -> dict:
        return {
            "columns": self.columns,
            "phone_column": self.phone_column,
            "name_column": self.name_column,
            "total_rows": self.total_rows,
            "valid_count": len(self.valid),
            "invalid_count": len(self.invalid),
            "duplicate_count": self.duplicates,
            "sample_valid": self.valid[:sample],
            "sample_invalid": self.invalid[:sample],
        }


@dataclass
class Table:
    """Minimal pandas-free table: header names + rows as dicts of strings."""
    columns: list[str]
    rows: list[dict[str, str]]

    def column(self, name: str) -> list[str]:
        return [r.get(name, "") for r in self.rows]


def _cell_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # Excel stores phone numbers as floats
    return str(value).strip()


def _read_excel_rows(path: Path, suffix: str) -> list[list[str]]:
    if suffix == ".xls":
        import xlrd
        sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
        return [[_cell_to_str(v) for v in sheet.row_values(i)] for i in range(sheet.nrows)]
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [[_cell_to_str(v) for v in row] for row in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()


def _read_csv_rows(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    text = ""
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def read_table(path: Path) -> Table:
    suffix = path.suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm", ".xls"):
            rows = _read_excel_rows(path, suffix)
        elif suffix in (".csv", ".txt"):
            rows = _read_csv_rows(path)
        else:
            raise ContactFileError("Only .csv, .xlsx or .xls files are supported.")
    except ContactFileError:
        raise
    except Exception as exc:  # readers raise many types
        raise ContactFileError(f"Could not read file: {exc}") from exc

    if not rows:
        raise ContactFileError("The file has no data rows.")
    header, body = rows[0], rows[1:]
    width = max(len(header), *(len(r) for r in body)) if body else len(header)
    columns: list[str] = []
    for i in range(width):
        name = (header[i].strip() if i < len(header) else "") or f"column_{i+1}"
        base, n = name, 1
        while name in columns:  # de-duplicate like pandas: "Phone", "Phone.1"
            name, n = f"{base}.{n}", n + 1
        columns.append(name)

    records = []
    for r in body:
        if not any(c.strip() for c in r):
            continue  # skip blank rows
        r = r + [""] * (width - len(r))
        records.append(dict(zip(columns, r)))
    if not records:
        raise ContactFileError("The file has no data rows.")
    return Table(columns=columns, rows=records)


_PHONE_LIKE = re.compile(r"\d{9,15}(\.0)?|\d\.\d+E\+\d+", re.IGNORECASE)


def _digit_ratio(values: list[str]) -> float:
    sample = values[:50]
    if not sample:
        return 0.0
    hits = sum(1 for v in sample if _PHONE_LIKE.fullmatch(re.sub(r"[\s\-\+\(\)\.]", "", v)))
    return hits / len(sample)


def detect_phone_column(df: Table) -> str:
    best, best_score = None, -1.0
    for col in df.columns:
        score = _digit_ratio(df.column(col))
        low = col.lower()
        if any(h in low for h in PHONE_HEADER_HINTS):
            score += 0.5
        if score > best_score:
            best, best_score = col, score
    if best is None or best_score < 0.3:
        raise ContactFileError("Could not find a phone number column. Please choose it manually.")
    return best


def detect_name_column(df: Table, exclude: str) -> str | None:
    for col in df.columns:
        if col != exclude and any(h in col.lower() for h in NAME_HEADER_HINTS):
            return col
    return None


def normalize_phone(raw: str, default_region: str = "PK") -> str | None:
    """Return E.164 (+923001234567) or None if invalid."""
    s = str(raw).strip()
    if not s:
        return None
    # Excel sometimes turns numbers into 9.23001234567E+11 or "923001234567.0"
    if re.fullmatch(r"\d+(\.\d+)?[eE]\+\d+", s):
        s = f"{int(float(s))}"
    s = re.sub(r"\.0$", "", s)
    s = re.sub(r"[^\d+]", "", s)
    if s.startswith("00"):
        s = "+" + s[2:]
    candidates = [s] if s.startswith("+") else [s, "+" + s]
    for cand in candidates:
        try:
            num = phonenumbers.parse(cand, None if cand.startswith("+") else default_region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    return None


def parse_contacts(path: Path, phone_column: str | None = None, default_region: str = "PK") -> ParsedContacts:
    df = read_table(path)
    if phone_column and phone_column not in df.columns:
        raise ContactFileError(f"Column '{phone_column}' not found in file.")
    phone_column = phone_column or detect_phone_column(df)
    name_column = detect_name_column(df, phone_column)

    parsed = ParsedContacts(columns=list(df.columns), phone_column=phone_column,
                            name_column=name_column, total_rows=len(df.rows))
    seen: set[str] = set()
    for idx, row in enumerate(df.rows, start=2):  # row 1 = header
        raw = str(row.get(phone_column, "")).strip()
        phone = normalize_phone(raw, default_region)
        if not phone:
            parsed.invalid.append({"row": idx, "raw": raw, "reason": "Invalid / empty number"})
            continue
        if phone in seen:
            parsed.duplicates += 1
            continue
        seen.add(phone)
        variables = {str(k): str(v) for k, v in row.items()}
        variables.setdefault("name", str(row.get(name_column, "")).strip() if name_column else "")
        parsed.valid.append({"phone": phone, "raw": raw, "vars": variables})
    return parsed


_PLACEHOLDER = re.compile(r"\{\s*([^{}]+?)\s*\}")


def render_message(template: str, variables: dict) -> str:
    """Replace {name}, {City} etc. with row values (case-insensitive). Unknown keys are left as-is."""
    lookup = {k.lower(): v for k, v in variables.items()}
    return _PLACEHOLDER.sub(lambda m: str(lookup.get(m.group(1).lower(), m.group(0))), template).strip()

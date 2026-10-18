import re
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Invoice Structured Output Service")


# ---------- Request and response schemas ----------

class ExtractRequest(BaseModel):
    text: str


class InvoiceExtraction(BaseModel):
    vendor: str
    amount: float
    currency: str
    date: str


# ---------- Helper functions ----------

def extract_vendor(text: str) -> str:
    # Look for common invoice phrases, then take the words after them.
    patterns = [
        r"(?:vendor|seller|supplier|billed by|from)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9&.,\- ]{1,100}?)(?=\s*(?:\n|$|,|\.|Total|Invoice|Amount|Due|Date|Payment))",
        r"(?:invoice from|from)\s+([A-Za-z0-9][A-Za-z0-9&.,\- ]{1,100}?)(?=\s*(?:\n|$|,|\.|Total|Invoice|Amount|Due|Date|Payment))",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            vendor = match.group(1).strip(" .,:;-")
            if vendor:
                return vendor

    # Fallback: use the first reasonably long line that looks like a company name.
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 3 and re.search(r"[A-Za-z]", line):
            return line[:120]

    return "Unknown Vendor"


def extract_amount(text: str) -> float:
    # Find money-like values, including 1,234.56, 1234.56, and 1234.
    matches = re.findall(r"(?<![\w.])(?:[$€£]\s*)?(\d{1,3}(?:[,\d]{0,12})(?:\.\d{1,2})?)(?![\w])", text)

    candidates = []
    for raw in matches:
        cleaned = raw.replace(",", "")
        try:
            value = float(cleaned)
            if 0 <= value <= 1_000_000:
                candidates.append(value)
        except ValueError:
            pass

    if not candidates:
        return 0.0

    # Prefer the largest amount, because invoices often list subtotal, tax, and total.
    return max(candidates)


def extract_currency(text: str) -> str:
    # Direct currency codes.
    match = re.search(r"\b(USD|EUR|GBP)\b", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # Currency symbols.
    symbol_map = {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
    }
    for symbol, code in symbol_map.items():
        if symbol in text:
            return code

    return "USD"


def extract_date(text: str) -> str:
    # Already in YYYY-MM-DD format.
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if match:
        return match.group(0)

    # Formats such as 14 July 2026 or July 14, 2026.
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }

    month_names = "|".join(months.keys())

    match = re.search(
        rf"\b(\d{{1,2}})\s+({month_names})\s+(\d{{4}})\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        day, month_name, year = match.groups()
        return f"{year}-{months[month_name.lower()]:02d}-{int(day):02d}"

    match = re.search(
        rf"\b({month_names})\s+(\d{{1,2}}),?\s+(\d{{4}})\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        month_name, day, year = match.groups()
        return f"{year}-{months[month_name.lower()]:02d}-{int(day):02d}"

    # Formats such as 14/07/2026 or 07/14/2026.
    match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if match:
        first, second, year = map(int, match.groups())
        # Assume day/month first unless the first number cannot be a month.
        if first > 12:
            day, month = first, second
        else:
            day, month = first, second
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year}-{month:02d}-{day:02d}"

    return "1970-01-01"


# ---------- Endpoint ----------

@app.post("/extract", response_model=InvoiceExtraction)
def extract_invoice(payload: ExtractRequest):
    text = payload.text or ""

    return InvoiceExtraction(
        vendor=extract_vendor(text),
        amount=extract_amount(text),
        currency=extract_currency(text),
        date=extract_date(text),
    )


# ---------- Safety net for malformed JSON ----------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Malformed/empty input must not become HTTP 500.
    return JSONResponse(
        status_code=422,
        content={
            "vendor": "Unknown Vendor",
            "amount": 0.0,
            "currency": "USD",
            "date": "1970-01-01",
            "detail": "Invalid request body",
        },
    )


# Catch any unexpected error too, so garbage input never crashes the service.
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=200,
        content={
            "vendor": "Unknown Vendor",
            "amount": 0.0,
            "currency": "USD",
            "date": "1970-01-01",
        },
    )
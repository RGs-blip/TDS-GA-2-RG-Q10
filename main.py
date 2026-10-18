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
    def to_float(raw: str) -> float:
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            return -1.0

    money = r"(?:[$€£]\s*)?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"

    # Each rule has a priority. Lower number = more trustworthy.
    rules = [
        # "Total due: 5,235.53", "Amount due: USD 5,235.53"
        (1, r"(?:total\s+due|amount\s+due|balance\s+due|total\s+payable|amount\s+payable)\s*[:\-]?\s*" + money),
        # "The total amount due is 5,235.53"
        (1, r"(?:total\s+amount\s+due|total\s+due)\s+(?:is|of)\s+" + money),
        # "Grand total: 5,235.53"
        (2, r"grand\s+total\s*[:\-]?\s*" + money),
        # "Total: 5,235.53" or "Amount: 5,235.53"
        (3, r"(?:total|amount)\s*[:\-]?\s*" + money),
        # "5,235.53 USD due" / "due: 5,235.53"
        (4, r"(?:due|payable)\s*[:\-]?\s*" + money),
        (4, money + r"\s*(?:is\s+)?(?:due|payable)"),
    ]

    candidates = []

    for priority, pattern in rules:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            raw_value = match.group(1)
            value = to_float(raw_value)

            if 0 <= value <= 1_000_000:
                candidates.append((priority, match.start(), value))

    if not candidates:
        return 0.0

    # Prefer the strongest label. If tied, prefer the match that appears later,
    # because invoice totals are usually near the bottom.
    candidates.sort(key=lambda item: (item[0], -item[1]))
    return candidates[0][2]

    # Fallback: choose the largest money-like number,
    # but ignore obvious date-like numbers.
    matches = re.findall(money_pattern, text)
    candidates = []

    for raw in matches:
        value = to_float(raw)
        if 0 <= value <= 1_000_000:
            candidates.append(value)

    return max(candidates) if candidates else 0.0


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

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from dateutil import parser as date_parser

ISO_CURRENCIES = {
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
    "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY",
    "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP",
    "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD",
    "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR",
    "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF",
    "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL",
    "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR",
    "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR",
    "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR",
    "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD",
    "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB",
    "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX",
    "USD", "UYU", "UZS", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XOF",
    "XPF", "YER", "ZAR", "ZMW", "ZWL",
}

CURRENCY_SYMBOLS = {
    "$": "USD",
    "US$": "USD",
    "USD$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "CNY",
    "￥": "CNY",
    "CN¥": "CNY",
    "RMB": "CNY",
    "HKD$": "HKD",
    "S$": "SGD",
    "A$": "AUD",
}

_MONEY_RE = re.compile(
    r"(?<![\w.])(?:USD|EUR|GBP|CNY|RMB|HKD|SGD|AUD|JPY|[US€£¥￥$]+\s*)?"
    r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?![\w.])"
)

_NUMBER_RE = re.compile(r"-?\d[\d,\s]*(?:\.\d+)?")


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_charge_name(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^0-9A-Z一-鿿]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_party_name(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).lower()
    text = re.sub(r"[（(].*?[）)]", " ", text)
    text = re.sub(r"[^0-9a-z一-鿿]+", " ", text)
    text = re.sub(
        r"\b(co|company|corp|corporation|inc|incorporated|ltd|limited|llc|plc|gmbh|"
        r"co ltd|company limited|shipping|logistics|logistics co)\b",
        " ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def fuzzy_ratio(left: str | None, right: str | None) -> float:
    a = normalize_party_name(left)
    b = normalize_party_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    return SequenceMatcher(None, a, b).ratio()


def parse_decimal(value: Any, correct_suspicious: bool = False) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"(?i)\b(?:USD|EUR|GBP|CNY|RMB|HKD|SGD|AUD|JPY)\b", "", text)
    text = text.replace("$", "").replace("€", "").replace("£", "").replace("¥", "").replace("￥", "")
    text = text.replace(" ", "")
    if correct_suspicious:
        # Only ASCII lookalikes inside an otherwise numeric token.
        text = re.sub(r"[Oo]", "0", text)
        text = re.sub(r"[Il|]", "1", text)
    comma_pos = text.rfind(",")
    dot_pos = text.rfind(".")
    if comma_pos > -1 and dot_pos > -1:
        if comma_pos > dot_pos:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif comma_pos > -1:
        tail = text[comma_pos + 1 :]
        if len(tail) == 3 and text.count(",") >= 1:
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text.count(".") > 1:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return -number if negative else number


def parse_date(value: Any, day_first: bool = False) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text, flags=re.IGNORECASE)
    try:
        return date_parser.parse(text, dayfirst=day_first, fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None


def find_currencies(text: str) -> list[str]:
    if not text:
        return []
    upper = text.upper()
    found: list[str] = []
    for code in sorted(ISO_CURRENCIES, key=len, reverse=True):
        if re.search(rf"(?<![A-Z]){re.escape(code)}(?![A-Z])", upper):
            found.append(code)
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text and code not in found:
            found.append(code)
    return found


def money_tokens(text: str, limit: int = 200) -> list[str]:
    return _MONEY_RE.findall(text or "")[:limit]


def is_iso_currency(value: str | None) -> bool:
    return bool(value) and value.upper() in ISO_CURRENCIES


_CONTAINER_LETTER_VALUES: dict[str, int] = {}
_value = 10
for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    while _value % 11 == 0:
        _value += 1
    _CONTAINER_LETTER_VALUES[_letter] = _value
    _value += 1


def is_valid_container_number(value: str | None) -> bool:
    if not value:
        return False
    token = re.sub(r"[^A-Z0-9]", "", value.upper())
    if not re.fullmatch(r"[A-Z]{4}\d{7}", token):
        return False
    total = sum(
        _CONTAINER_LETTER_VALUES[token[index]] * (2**index) for index in range(4)
    )
    total += sum(int(token[index]) * (2**index) for index in range(4, 10))
    check = total % 11
    if check == 10:
        check = 0
    return check == int(token[-1])


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def write_json(path: str | Path, payload: Any, ensure_ascii: bool = False) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        to_jsonable(payload),
        ensure_ascii=ensure_ascii,
        indent=2,
        allow_nan=False,
    )
    destination.write_text(text + "\n", encoding="utf-8")


def write_jsonl(path: str | Path, rows: Iterable[Any], ensure_ascii: bool = False) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(to_jsonable(row), ensure_ascii=ensure_ascii, allow_nan=False))
            handle.write("\n")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


_ACCOUNT_RE = re.compile(
    r"(?i)\b((?:bank\s+)?(?:boa\s+)?account\s*#?|acct\s*#?|routing\s+number(?:\s+\w+)?"
    r"|aba(?:/routing)?(?:\s+\w+)?\s*#?)\s*[:：]?\s*([\d][\d\-\s]{4,})"
)
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


def redact_sensitive_text(text: str | None) -> str | None:
    if not text:
        return text

    def mask_account(match: re.Match[str]) -> str:
        label = match.group(1)
        digits = re.sub(r"\D", "", match.group(2))
        if len(digits) <= 4:
            return f"{label} ****"
        return f"{label} ****{digits[-4:]}"

    result = _ACCOUNT_RE.sub(mask_account, text)
    result = _EMAIL_RE.sub(lambda m: f"***@{m.group(2)}", result)
    return result


def truncate(text: str | None, limit: int = 500) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]

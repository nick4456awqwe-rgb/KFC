import re
from datetime import datetime

_NUM_RE = re.compile(r"\d[\d\s  ]*(?:[.,]\d+)?")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?")


def clean(text):
    """Схлопывает пробелы/переносы и неразрывные пробелы."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def parse_money(text):
    """'4 692 600,50 руб. НДС...' -> 4692600.5; None, если числа нет."""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"[\s  ]", "", m.group(0)).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_date(text):
    """'29.09.2026 09:00' -> datetime; None, если даты нет."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    fmt = "%d.%m.%Y %H:%M" if m.group(2) else "%d.%m.%Y"
    value = f"{m.group(1)} {m.group(2)}" if m.group(2) else m.group(1)
    return datetime.strptime(value, fmt)

"""Deterministic value formatting shared by every presenter and the text fallback."""

import datetime
import decimal

from ai_agent.presentation.i18n import t

# English sources; translated where they are shown (t()), never at import time.
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

NULL_MONEY = "Not recorded"
NULL_TEXT = "—"
NULL_UNSET = "Not set"


def is_blank(value):
    return value is None or value == "" or (isinstance(value, str) and value.strip().lower() in ("none", "null", "n/a"))


def to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, decimal.Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


def number(value, digits=None):
    n = to_number(value)
    if n is None:
        return NULL_TEXT
    if digits is None:
        digits = 0 if abs(n - round(n)) < 0.005 else 2
    text = f"{abs(n):,.{digits}f}"
    return f"-{text}" if n < 0 and round(abs(n), digits) != 0 else text


def compact(value):
    n = to_number(value)
    if n is None:
        return NULL_TEXT
    sign = "-" if n < 0 else ""
    n = abs(n)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= limit:
            scaled = n / limit
            return f"{sign}{scaled:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{sign}{number(n)}"


def money(value, currency=None, *, zero_as_unset=False, digits=None):
    n = to_number(value)
    if n is None or (zero_as_unset and n == 0):
        return t(NULL_MONEY)
    text = number(n, digits)
    return f"{text} {currency}" if currency else text


def percent(value, digits=1):
    n = to_number(value)
    if n is None:
        return NULL_TEXT
    text = f"{n * 100:.{digits}f}"
    if "." in text:                       # never strip the zeros of a whole number: 90 must stay 90
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


def percent_value(value, digits=2):
    """A value that is already a percentage (ERPNext Percent fields store 15 for 15%)."""
    n = to_number(value)
    if n is None:
        return NULL_TEXT
    return f"{n:.{digits}f}".rstrip("0").rstrip(".") + "%"


def time_text(value):
    text_value = str(value or "")
    return text_value[:5] if len(text_value) >= 5 and text_value[2] == ":" else text_value


def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def date(value):
    d = as_date(value)
    if not d:
        return NULL_TEXT if is_blank(value) else str(value)
    return f"{d.day} {t(MONTHS[d.month - 1])} {d.year}"


def datetime_text(value):
    if isinstance(value, datetime.datetime):
        return f"{date(value)} {value:%H:%M}"
    if isinstance(value, str) and len(value) >= 16 and as_date(value):
        return f"{date(value)} {value[11:16]}"
    return date(value)


def boolean(value):
    if is_blank(value):
        return NULL_TEXT
    return t("Yes") if str(value).lower() in ("1", "true", "yes") or value is True or value == 1 else t("No")


def comma():
    """The list separator of the viewer's language: "، " in Arabic, ", " otherwise."""
    from ai_agent.presentation.i18n import is_rtl
    from ai_agent.presentation.labels import label_language

    return "، " if is_rtl(label_language()) else ", "


def text(value, empty=NULL_TEXT):
    return empty if is_blank(value) else str(value)


def change(current, previous):
    """(delta ratio or None, tone). Tone is only semantic direction."""
    c, p = to_number(current), to_number(previous)
    if c is None or p is None or p == 0:
        return None, "neutral"
    ratio = (c - p) / abs(p)
    return ratio, "success" if ratio > 0 else "danger" if ratio < 0 else "neutral"

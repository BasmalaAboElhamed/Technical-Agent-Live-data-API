"""Small shared helpers: time, text and number normalisation."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_MISSING_TOKENS = {"", "-", "--", "null", "none", "n/a", "nan"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """ISO 8601, UTC, second precision (stable for lexicographic comparison)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def normalize_text(value: Any) -> Optional[str]:
    """NFC-normalise and collapse whitespace. Never case-folds or transliterates."""
    if value is None:
        return None
    s = unicodedata.normalize("NFC", str(value))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def normalize_symbol(value: Any) -> Optional[str]:
    """'COMI.CA' / ' comi ' -> 'COMI'. Only a trailing '.CA' (Reuters suffix) is removed."""
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).strip().upper()
    if s.endswith(".CA"):
        s = s[:-3]
    s = s.strip()
    return s or None


def parse_number(value: Any) -> Optional[float]:
    """
    Return a float, or None when the value is *missing*.
    A genuine 0 stays 0.0 (it is NOT treated as missing).
    Raises ValueError when a value is present but not a usable number.

    Strings: Arabic-Indic digits, '٫' decimal and '٬' thousands separators are accepted.
    '1,234' / '1,234.5' -> thousands separators; '12,5' -> decimal comma.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, (int, float)):
        f = float(value)
        if not math.isfinite(f):
            raise ValueError("non-finite number")
        return f
    if isinstance(value, str):
        s = value.strip().translate(_AR_DIGITS).replace("\u00a0", "").replace(" ", "")
        s = s.replace("٫", ".").replace("٬", ",")
        if s.lower() in _MISSING_TOKENS:
            return None
        if s.endswith("%"):
            s = s[:-1]
        if "," in s and "." in s:
            s = s.replace(",", "")
        elif "," in s:
            if re.fullmatch(r"-?\d{1,3}(,\d{3})+", s):
                s = s.replace(",", "")
            elif re.fullmatch(r"-?\d+,\d+", s):
                s = s.replace(",", ".")
            else:
                raise ValueError(f"ambiguous numeric string: {value!r}")
        try:
            f = float(s)
        except ValueError as exc:
            raise ValueError(f"not a number: {value!r}") from exc
        if not math.isfinite(f):
            raise ValueError("non-finite number")
        return f
    raise ValueError(f"unsupported type {type(value).__name__}")

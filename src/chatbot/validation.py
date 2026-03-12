"""Shared backend validation for guided-flow form submissions.

The frontend submits step payloads as dictionaries (`form_data`). These validators
ensure important fields are present and well-formed.

On validation failure, raise `FormValidationError` so the API can return HTTP 422
with structured `field_errors`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Optional, Tuple


@dataclass
class FormValidationError(Exception):
    """Exception raised for form validation failures.

    Attributes:
        field_errors: mapping of field name -> human-readable error message.
        message: optional top-level message.
    """

    field_errors: Dict[str, str]
    message: str = "Validation failed"

    def __str__(self) -> str:  # pragma: no cover
        return self.message


def _as_str(v: Any) -> str:
    return "" if v is None else str(v)


def _strip(v: Any) -> str:
    return _as_str(v).strip()


def add_error(errors: Dict[str, str], field: str, message: str) -> None:
    if field not in errors:
        errors[field] = message


def require_str(payload: Dict[str, Any], field: str, errors: Dict[str, str], *, label: Optional[str] = None) -> str:
    value = _strip(payload.get(field))
    if not value:
        add_error(errors, field, f"{label or field} is required")
    return value


def optional_str(payload: Dict[str, Any], field: str) -> str:
    return _strip(payload.get(field))


def require_bool(payload: Dict[str, Any], field: str, errors: Dict[str, str], *, label: Optional[str] = None) -> bool:
    if field not in payload:
        add_error(errors, field, f"{label or field} is required")
        return False
    v = payload.get(field)
    if isinstance(v, bool):
        return v
    s = _strip(v).lower()
    if s in ("true", "1", "yes", "y", "on"):
        return True
    if s in ("false", "0", "no", "n", "off"):
        return False
    add_error(errors, field, f"{label or field} must be true/false")
    return False


def parse_int(
    payload: Dict[str, Any],
    field: str,
    errors: Dict[str, str],
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
    required: bool = False,
) -> int:
    raw = payload.get(field)
    if raw is None or _strip(raw) == "":
        if required:
            add_error(errors, field, f"{field} is required")
        return 0
    try:
        val = int(str(raw))
    except Exception:
        add_error(errors, field, f"{field} must be a whole number")
        return 0
    if min_value is not None and val < min_value:
        add_error(errors, field, f"{field} must be at least {min_value}")
    if max_value is not None and val > max_value:
        add_error(errors, field, f"{field} must be at most {max_value}")
    return val


def parse_decimal_str(payload: Dict[str, Any], field: str, errors: Dict[str, str], *, min_value: Optional[float] = None, required: bool = False) -> str:
    """Validate numeric input but return it as a string (to avoid changing storage shape)."""
    raw = _strip(payload.get(field))
    if not raw:
        if required:
            add_error(errors, field, f"{field} is required")
        return ""
    try:
        val = float(raw)
    except Exception:
        add_error(errors, field, f"{field} must be a number")
        return raw
    if min_value is not None and val < min_value:
        add_error(errors, field, f"{field} must be at least {min_value}")
    return raw


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(value: str, errors: Dict[str, str], field: str = "email") -> str:
    value = _strip(value)
    if not value:
        add_error(errors, field, "Email is required")
        return value
    if not _EMAIL_RE.match(value):
        add_error(errors, field, "Email is not valid")
    return value


def normalize_phone_ug(value: str) -> str:
    """Normalize common Ugandan phone formats.

    Accepts:
    - 07XXXXXXXX (10 digits)
    - +2567XXXXXXXX
    - 2567XXXXXXXX

    Returns digits-only international form: 2567XXXXXXXX when possible.
    """
    s = _strip(value)
    if not s:
        return ""
    s = re.sub(r"[\s\-\(\)]", "", s)
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("0") and len(s) == 10:
        return "256" + s[1:]
    if s.startswith("256") and len(s) == 12:
        return s
    return s


def validate_phone_ug(value: str, errors: Dict[str, str], field: str = "phone_number") -> str:
    raw = _strip(value)
    if not raw:
        add_error(errors, field, "Phone number is required")
        return raw
    norm = normalize_phone_ug(raw)
    if not norm.isdigit():
        add_error(errors, field, "Phone number must contain digits only")
        return raw
    # Basic UG mobile check: 2567XXXXXXXX (12 digits)
    if not (len(norm) == 12 and norm.startswith("2567")):
        add_error(errors, field, "Phone number format is not valid")
    return raw


# Accept both modern and legacy NIN variants used by existing clients.
_NIN_RE = re.compile(r"^(?:[A-Z]{2}\d{12}|[A-Z]{2}\d{10}[A-Z]{2})$")


def normalize_nin(value: str) -> str:
    s = _strip(value).upper()
    s = re.sub(r"[\s\-]", "", s)
    return s


def validate_nin_ug(value: str, errors: Dict[str, str], field: str = "national_id_number") -> str:
    raw = _strip(value)
    if not raw:
        add_error(errors, field, "National ID Number (NIN) is required")
        return raw
    nin = normalize_nin(raw)
    if not _NIN_RE.match(nin):
        add_error(errors, field, "NIN format is not valid")
    return raw


def parse_iso_date(value: str) -> Optional[date]:
    s = _strip(value)
    if not s:
        return None
    return date.fromisoformat(s)


def validate_date_iso(value: str, errors: Dict[str, str], field: str, *, required: bool = True, not_future: bool = False) -> str:
    raw = _strip(value)
    if not raw:
        if required:
            add_error(errors, field, f"{field} is required")
        return raw

    d: Optional[date] = None
    try:
        if "T" in raw:
            d = datetime.fromisoformat(raw).date()
        else:
            d = date.fromisoformat(raw)
    except Exception:
        d = None

    if d is None and "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3:
            try:
                # MM/DD/YYYY
                month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
                d = date(year, month, day)
            except Exception:
                d = None

    if d is None:
        add_error(errors, field, f"{field} must be a valid date (YYYY-MM-DD or MM/DD/YYYY)")
        return raw

    if not_future and d > date.today():
        add_error(errors, field, f"{field} cannot be in the future")
    return raw


def validate_in(value: str, allowed: Iterable[str], errors: Dict[str, str], field: str, *, required: bool = True) -> str:
    raw = _strip(value)
    if not raw:
        if required:
            add_error(errors, field, f"{field} is required")
        return raw
    if raw not in set(allowed):
        add_error(errors, field, f"{field} has an invalid value")
    return raw


def validate_list_ids(value: Any, allowed_ids: Iterable[str], errors: Dict[str, str], field: str) -> list[str]:
    allowed = set(allowed_ids)
    items: list[str]
    if value is None:
        return []
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        add_error(errors, field, f"{field} must be a list")
        return []

    bad = [v for v in items if v not in allowed]
    if bad:
        add_error(errors, field, f"{field} contains invalid selection(s)")
    return items


def raise_if_errors(errors: Dict[str, str], message: str = "Please correct the highlighted fields") -> None:
    if errors:
        raise FormValidationError(field_errors=errors, message=message)


# --- Motor Insurance specific helpers ---


def validate_length_range(
    value: str,
    *,
    field: str,
    errors: Dict[str, str],
    label: Optional[str] = None,
    min_len: int = 0,
    max_len: Optional[int] = None,
    required: bool = False,
    message: Optional[str] = None,
) -> str:
    """Trim a string and validate its length bounds with a custom error message.

    This is used for motor insurance name fields where the frontend has specific
    2–50 character requirements.
    """

    trimmed = _strip(value)
    if not trimmed:
        if required:
            add_error(errors, field, message or f"{label or field} is required")
        return trimmed

    length = len(trimmed)
    if length < min_len or (max_len is not None and length > max_len):
        add_error(errors, field, message or f"{label or field} must be between {min_len} and {max_len} characters.")
    return trimmed


def validate_enum(
    value: str,
    *,
    field: str,
    errors: Dict[str, str],
    allowed: Iterable[str],
    required: bool,
    message: str,
) -> str:
    raw = _strip(value).lower()
    if not raw:
        if required:
            add_error(errors, field, message)
        return raw
    if raw not in {v.lower() for v in allowed}:
        add_error(errors, field, message)
    return raw


def validate_uganda_mobile_frontend(value: str, errors: Dict[str, str], field: str = "mobile") -> Tuple[str, str]:
    """Validate and normalize Uganda mobile formats as per frontend spec.

    Acceptable formats:
    - +2567XXXXXXXX
    - +256 7XXXXXXXX
    - 07XXXXXXXX

    Returns (original_trimmed, normalized_e164_without_plus).
    """

    raw = _strip(value)
    if not raw:
        add_error(errors, field, "Mobile number must be in +2567XXXXXXXX, +256 7XXXXXXXX, or 07XXXXXXXX format.")
        return raw, ""

    # Remove spaces for validation/normalization
    compact = re.sub(r"\s+", "", raw)

    pattern = re.compile(r"^(\+2567\d{8}|07\d{8})$")
    if not pattern.match(compact):
        add_error(errors, field, "Mobile number must be in +2567XXXXXXXX, +256 7XXXXXXXX, or 07XXXXXXXX format.")
        return raw, ""

    # Normalize to +2567XXXXXXXX then strip + for storage (2567XXXXXXXX)
    if compact.startswith("07"):
        normalized = "+2567" + compact[2:]
    else:
        normalized = compact

    normalized_digits = normalized.lstrip("+")
    return raw, normalized_digits


def validate_motor_email_frontend(value: str, errors: Dict[str, str], field: str = "email") -> str:
    """Trim, lowercase and validate email according to frontend rules."""

    raw = _strip(value).lower()
    if not raw:
        add_error(errors, field, "Please enter a valid email address.")
        return raw
    if len(raw) > 100 or not re.match(r"^\S+@\S+\.\S+$", raw):
        add_error(errors, field, "Please enter a valid email address.")
    return raw


def validate_cover_start_date_range(
    value: str,
    errors: Dict[str, str],
    field: str = "coverStartDate",
    *,
    days_ahead: int = 90,
) -> str:
    """Validate that cover start date is within [today, today + days_ahead]."""

    raw = _strip(value)
    if not raw:
        add_error(errors, field, "Cover start date must be within the next 90 days.")
        return raw
    try:
        d = date.fromisoformat(raw)
    except Exception:
        add_error(errors, field, "Cover start date must be within the next 90 days.")
        return raw

    today = date.today()
    if d < today or d > today + timedelta(days=days_ahead):
        add_error(errors, field, "Cover start date must be within the next 90 days.")
    return raw


def validate_positive_number_field(
    value: Any,
    *,
    field: str,
    errors: Dict[str, str],
    message: str,
) -> float:
    raw = _strip(value)
    if not raw:
        add_error(errors, field, message)
        return 0.0
    try:
        num = float(raw)
    except Exception:
        add_error(errors, field, message)
        return 0.0
    if num <= 0:
        add_error(errors, field, message)
    return num

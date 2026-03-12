"""
Unified field validation service — single source of truth for all products.

Replaces:
  - field_filter.py  (can be deleted after adopting this)

Keeps using (do NOT delete):
  - validation.py    (low-level primitives imported below)

Three public classes + one helper function:

  FieldValidator          — validates one field by name
  StepValidator           — validates all fields in a step for any product
  FieldDecorator          — enriches field dicts with errors + frontend hints
  filter_collected_fields — progressive disclosure (replaces filter_already_collected_fields)

Adding a new product:
  1. Add per-field rules to FieldValidator._run() if new field types are needed
  2. Register step handlers in StepValidator._REGISTRY
  3. Everything else (endpoint, decorator) works automatically


Usage in a flow step:
    from src.chatbot.field_validator import FieldValidator, StepValidator, FieldDecorator

    errors = StepValidator.validate("personal_accident", "quick_quote", payload)
    if not errors:
        ...proceed to next step

    decorated = FieldDecorator.decorate(all_fields, errors=errors)

Usage in the API endpoint:
    result = FieldValidator.validate(field="dob", value="2010-01-01", context={})
    # {"valid": False, "field": "dob", "error": "You must be at least 18 years old."}
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.chatbot.validation import (
    _strip,
    normalize_nin,
)


# ---------------------------------------------------------------------------
# Internal date parser
# ---------------------------------------------------------------------------

def _parse_date(value: Any) -> Optional[date]:
    """Parse a date from multiple common formats. Returns None if unparseable."""
    from datetime import datetime as _dt

    if isinstance(value, date):
        return value
    s = _strip(value)
    if not s:
        return None
    if "T" in s:
        try:
            return _dt.fromisoformat(s).date()
        except (ValueError, TypeError):
            pass
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                return date(int(parts[2]), int(parts[0]), int(parts[1]))
            except (ValueError, TypeError):
                try:
                    return date(int(parts[2]), int(parts[1]), int(parts[0]))
                except (ValueError, TypeError):
                    pass
    return None


def _age(dob: date) -> int:
    today = date.today()
    return today.year - dob.year - (
        1 if (today.month, today.day) < (dob.month, dob.day) else 0
    )


# ---------------------------------------------------------------------------
# FieldValidator — one field at a time
# ---------------------------------------------------------------------------

class FieldValidator:
    """
    Validates a single named field.

    Called by:
      - /flow/validate-field API endpoint (on-blur or per-keystroke)
      - StepValidator internally          (on full step submission)

    Returns:
      {"valid": True,  "field": "dob"}
      {"valid": False, "field": "dob", "error": "You must be at least 18 years old."}
    """

    # Fields requiring a backend call.
    # FieldDecorator stamps these with backendValidation: true.
    BACKEND_VALIDATED_FIELDS = {
        "dob", "date_of_birth",
        "mobile", "mobile_number", "nok_phone_number",
        "email",
        "national_id_number", "nok_id_number",
        "policyStartDate", "policy_start_date",
        "coverStartDate",  "cover_start_date",
        "coverLimitAmountUgx",
        "vehicleValue",    "vehicle_value",
        "departure_date",  "departureDate",
        "return_date",     "returnDate",
    }

    @classmethod
    def requires_backend(cls, field_name: str) -> bool:
        return field_name in cls.BACKEND_VALIDATED_FIELDS

    @classmethod
    def validate(
        cls,
        field: str,
        value: Any,
        context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        Validate one field.
        Context = full session data for cross-field rules
        (e.g. return_date must be after departure_date).
        """
        context = context or {}
        error = cls._run(field, value, context)
        if error:
            return {"valid": False, "field": field, "error": error}
        return {"valid": True, "field": field}

    # ------------------------------------------------------------------
    # Dispatch — add new field types here as products grow
    # ------------------------------------------------------------------

    @classmethod
    def _run(cls, field: str, value: Any, ctx: Dict) -> Optional[str]:

        # ---- names ----
        if field in (
            "firstName", "first_name", "lastName", "surname", "last_name",
            "nok_first_name", "nok_last_name",
            "insured_name", "policy_holder_name",
        ):
            return cls._name(value)

        if field in ("middleName", "middle_name", "nok_middle_name"):
            return cls._optional_max(value, 50)

        # ---- required text ----
        if field in ("nok_relationship", "relationship"):
            return cls._required(value, "Relationship")

        if field in ("nok_address", "physical_address", "address", "residential_address"):
            return cls._required(value, "Address")

        if field in ("nationality", "citizenship"):
            return cls._required(value, "Nationality")

        if field in ("occupation", "profession"):
            return cls._required(value, "Occupation")

        if field in ("country_of_residence", "country"):
            return cls._required(value, "Country of Residence")

        # ---- optional with format ----
        if field in ("tax_identification_number", "tin"):
            return cls._optional_pattern(value, r"^\d{10}$", "Tax ID must be 10 digits.")

        # ---- contact ----
        if field in ("mobile", "mobile_number", "nok_phone_number", "phone_number"):
            return cls._phone(value)

        if field in ("email", "email_address"):
            return cls._email(value)

        # ---- identity ----
        if field == "national_id_number":
            return cls._nin(value, required=True)

        if field == "nok_id_number":
            return cls._nin(value, required=False)

        # ---- dates ----
        if field in ("dob", "date_of_birth"):
            return cls._dob(value, min_age=18, max_age=65)

        if field in ("policyStartDate", "policy_start_date"):
            return cls._future_date(value, "Policy start date", max_days_ahead=365)

        if field in ("coverStartDate", "cover_start_date"):
            return cls._future_date(value, "Cover start date", max_days_ahead=90)

        if field in ("departure_date", "departureDate"):
            return cls._future_date(value, "Departure date", max_days_ahead=365)

        if field in ("return_date", "returnDate"):
            dep = ctx.get("departure_date") or ctx.get("departureDate")
            return cls._return_date(value, dep)

        # ---- selects / enums ----
        if field == "gender":
            return cls._enum(value, {"Male", "Female", "Other"}, "Gender")

        if field == "coverLimitAmountUgx":
            return cls._enum(value, {"5000000", "10000000", "20000000"}, "Cover limit")

        if field in ("vehicleUsage", "vehicle_usage"):
            return cls._enum(
                value,
                {"private", "commercial", "psv", "special_hire"},
                "Vehicle usage",
            )

        if field in ("cover_type", "coverType"):
            return cls._enum(value, {"comprehensive", "third_party"}, "Cover type")

        # ---- numbers ----
        if field in ("vehicleValue", "vehicle_value", "vehicle_value_ugx"):
            return cls._positive_number(value, "Vehicle value")

        if field in ("numberOfTravellers", "number_of_travellers"):
            return cls._int_range(value, 1, 20, "Number of travellers")

        # unknown field — no error (fail open so new products don't break)
        return None

    # ------------------------------------------------------------------
    # Atomic validators — each returns an error string or None
    # ------------------------------------------------------------------

    @staticmethod
    def _required(value: Any, label: str) -> Optional[str]:
        return None if _strip(value) else f"{label} is required."

    @staticmethod
    def _name(value: Any) -> Optional[str]:
        v = _strip(value)
        if not v:
            return "This field is required."
        if len(v) < 2:
            return "Must be at least 2 characters."
        if len(v) > 50:
            return "Must not exceed 50 characters."
        return None

    @staticmethod
    def _optional_max(value: Any, max_len: int) -> Optional[str]:
        v = _strip(value)
        return f"Must not exceed {max_len} characters." if v and len(v) > max_len else None

    @staticmethod
    def _optional_pattern(value: Any, pattern: str, message: str) -> Optional[str]:
        v = _strip(value)
        if not v:
            return None
        return None if re.match(pattern, v) else message

    @staticmethod
    def _phone(value: Any) -> Optional[str]:
        raw = _strip(value)
        if not raw:
            return "Phone number is required."
        compact = re.sub(r"[\s\-\(\)]", "", raw).lstrip("+")
        if compact.startswith("0") and len(compact) == 10:
            compact = "256" + compact[1:]
        if not (len(compact) == 12 and compact.startswith("2567") and compact.isdigit()):
            return "Phone number must be in format 07XXXXXXXX or +2567XXXXXXXX."
        return None

    @staticmethod
    def _email(value: Any) -> Optional[str]:
        v = _strip(value).lower()
        if not v:
            return "Email address is required."
        if len(v) > 100 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            return "Please enter a valid email address."
        return None

    @staticmethod
    def _dob(value: Any, min_age: int, max_age: int) -> Optional[str]:
        raw = _strip(value)
        if not raw:
            return "Date of birth is required."
        dob = _parse_date(raw)
        if not dob:
            return "Please enter a valid date of birth (YYYY-MM-DD)."
        age = _age(dob)
        if age < min_age:
            return f"You must be at least {min_age} years old to apply."
        if age > max_age:
            return f"Cover is only available for applicants aged {min_age}–{max_age}."
        return None

    @staticmethod
    def _future_date(value: Any, label: str, max_days_ahead: int = 365) -> Optional[str]:
        raw = _strip(value)
        if not raw:
            return f"{label} is required."
        d = _parse_date(raw)
        if not d:
            return f"{label} must be a valid date (YYYY-MM-DD)."
        today = date.today()
        if d <= today:
            return f"{label} must be after today ({today})."
        if d > today + timedelta(days=max_days_ahead):
            return f"{label} cannot be more than {max_days_ahead} days in the future."
        return None

    @staticmethod
    def _return_date(value: Any, departure_raw: Any) -> Optional[str]:
        raw = _strip(value)
        if not raw:
            return "Return date is required."
        ret = _parse_date(raw)
        if not ret:
            return "Return date must be a valid date (YYYY-MM-DD)."
        if ret <= date.today():
            return "Return date must be in the future."
        if departure_raw:
            dep = _parse_date(departure_raw)
            if dep and ret <= dep:
                return "Return date must be after the departure date."
        return None

    @staticmethod
    def _nin(value: Any, required: bool) -> Optional[str]:
        raw = _strip(value)
        if not raw:
            return "National ID Number is required." if required else None
        if not re.match(r"^(?:[A-Z]{2}\d{12}|[A-Z]{2}\d{10}[A-Z]{2})$", normalize_nin(raw)):
            return "NIN format is not valid."
        return None

    @staticmethod
    def _enum(value: Any, allowed: set, label: str) -> Optional[str]:
        v = _strip(value)
        if not v:
            return f"{label} is required."
        if v not in allowed:
            return f"{label} must be one of: {', '.join(sorted(allowed))}."
        return None

    @staticmethod
    def _positive_number(value: Any, label: str) -> Optional[str]:
        raw = _strip(value)
        if not raw:
            return f"{label} is required."
        try:
            n = float(raw)
        except (ValueError, TypeError):
            return f"{label} must be a valid number."
        return None if n > 0 else f"{label} must be greater than zero."

    @staticmethod
    def _int_range(value: Any, min_val: int, max_val: int, label: str) -> Optional[str]:
        raw = _strip(value)
        if not raw:
            return f"{label} is required."
        try:
            n = int(raw)
        except (ValueError, TypeError):
            return f"{label} must be a whole number."
        return None if min_val <= n <= max_val else f"{label} must be between {min_val} and {max_val}."


# ---------------------------------------------------------------------------
# StepValidator — all fields for a step, any product
# ---------------------------------------------------------------------------

class StepValidator:
    """
    Validates all fields for a given product + step in one call.

    Usage:
        errors = StepValidator.validate("personal_accident", "quick_quote", payload)
        errors = StepValidator.validate("motor", "vehicle_details", payload, context=data)
        errors = StepValidator.validate("travel", "trip_details", payload)

    Returns {} if all valid, {field: error_message} if not.

    To add a new product:
        1. Write a @classmethod _<product>_<step> below
        2. Register it in _REGISTRY at the bottom of the class
    """

    @classmethod
    def validate(
        cls,
        product_id: str,
        step: str,
        payload: Dict[str, Any],
        context: Dict[str, Any] = None,
    ) -> Dict[str, str]:
        context = context or {}
        handler = cls._REGISTRY.get((product_id, step))
        if not handler:
            return {}
        return handler(cls, payload, context)

    @classmethod
    def _validate_fields(
        cls,
        payload: Dict,
        context: Dict,
        field_names: List[str],
    ) -> Dict[str, str]:
        """Validate a list of fields, return {field: error} for failures."""
        errors: Dict[str, str] = {}
        for field in field_names:
            result = FieldValidator.validate(
                field,
                payload.get(field),
                context={**payload, **context},
            )
            if not result["valid"]:
                errors[field] = result["error"]
        return errors

    # ------------------------------------------------------------------
    # Personal Accident steps
    # ------------------------------------------------------------------

    @classmethod
    def _pa_quick_quote(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "firstName", "lastName", "mobile", "email",
            "dob", "policyStartDate", "coverLimitAmountUgx",
        ])

    @classmethod
    def _pa_personal_details(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        # Merge quick_quote so auto-filled fields are also validated
        merged = {**ctx.get("quick_quote", {}), **payload}
        return cls._validate_fields(merged, ctx, [
            "national_id_number", "nationality", "occupation",
            "gender", "country_of_residence", "physical_address",
        ])

    @classmethod
    def _pa_next_of_kin(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        errors = cls._validate_fields(payload, ctx, [
            "nok_first_name", "nok_last_name",
            "nok_phone_number", "nok_relationship", "nok_address",
        ])
        # NIN optional for NOK — only validate if provided
        if payload.get("nok_id_number"):
            result = FieldValidator.validate("nok_id_number", payload["nok_id_number"])
            if not result["valid"]:
                errors["nok_id_number"] = result["error"]
        return errors

    # ------------------------------------------------------------------
    # Motor steps
    # ------------------------------------------------------------------

    @classmethod
    def _motor_owner_details(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "firstName", "lastName", "mobile", "email",
            "dob", "national_id_number", "physical_address",
        ])

    @classmethod
    def _motor_vehicle_details(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "vehicleValue", "vehicleUsage", "coverStartDate",
        ])

    # ------------------------------------------------------------------
    # Travel steps
    # ------------------------------------------------------------------

    @classmethod
    def _travel_traveller_details(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "firstName", "lastName", "mobile", "email",
            "dob", "nationality", "national_id_number",
        ])

    @classmethod
    def _travel_trip_details(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "departure_date", "return_date", "numberOfTravellers",
        ])

    # ------------------------------------------------------------------
    # Motor Private steps
    # ------------------------------------------------------------------

    @classmethod
    def _motor_private_about_you(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "first_name", "middle_name", "surname", "phone_number", "email",
        ])

    @classmethod
    def _motor_private_vehicle_details(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "cover_type", "cover_start_date", "vehicle_value_ugx",
        ])

    # ------------------------------------------------------------------
    # Serenicare steps
    # ------------------------------------------------------------------

    @classmethod
    def _serenicare_about_you(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "first_name", "middle_name", "surname", "phone_number", "email",
        ])

    @classmethod
    def _serenicare_cover_personalization(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, ["date_of_birth"])

    # ------------------------------------------------------------------
    # Travel Insurance steps
    # ------------------------------------------------------------------

    @classmethod
    def _travel_insurance_about_you(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "first_name", "middle_name", "surname", "phone_number", "email",
        ])

    @classmethod
    def _travel_insurance_trip(cls, payload: Dict, ctx: Dict) -> Dict[str, str]:
        return cls._validate_fields(payload, ctx, [
            "departure_date", "return_date",
        ])

    # ------------------------------------------------------------------
    # Registry — (product_id, step_name) → handler
    # Add new products/steps here only, nothing else needs to change.
    # ------------------------------------------------------------------

    _REGISTRY: Dict[Tuple[str, str], Callable] = {
        # Personal Accident
        ("personal_accident", "quick_quote"):      _pa_quick_quote.__func__,
        ("personal_accident", "personal_details"): _pa_personal_details.__func__,
        ("personal_accident", "next_of_kin"):      _pa_next_of_kin.__func__,

        # Motor (generic)
        ("motor", "owner_details"):   _motor_owner_details.__func__,
        ("motor", "vehicle_details"): _motor_vehicle_details.__func__,

        # Motor Private
        ("motor_private", "about_you"):        _motor_private_about_you.__func__,
        ("motor_private", "vehicle_details"):  _motor_private_vehicle_details.__func__,

        # Serenicare
        ("serenicare", "about_you"):             _serenicare_about_you.__func__,
        ("serenicare", "cover_personalization"): _serenicare_cover_personalization.__func__,

        # Travel (generic)
        ("travel", "traveller_details"): _travel_traveller_details.__func__,
        ("travel", "trip_details"):      _travel_trip_details.__func__,

        # Travel Insurance
        ("travel_insurance", "about_you"):             _travel_insurance_about_you.__func__,
        ("travel_insurance", "travel_party_and_trip"): _travel_insurance_trip.__func__,
    }


# ---------------------------------------------------------------------------
# FieldDecorator — enriches field dicts for the frontend response
# ---------------------------------------------------------------------------

class FieldDecorator:
    """
    One call that does three things:
      1. Attaches inline errors from last submission onto affected fields
      2. Stamps backendValidation: true on fields that need /validate-field
      3. Adds frontend hint rules (min/max dates, patterns) — cosmetic only,
         backend remains the authority on all validation

    Replaces both add_validation_hints_to_fields and add_frontend_validation_rules
    from the old field_filter.py.

    Usage:
        decorated = FieldDecorator.decorate(all_fields, errors=errors)
    """

    @classmethod
    def decorate(
        cls,
        fields: List[Dict[str, Any]],
        errors: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        errors = errors or {}
        return [cls._enhance(f, errors) for f in fields]

    @classmethod
    def _enhance(cls, field: Dict[str, Any], errors: Dict[str, str]) -> Dict[str, Any]:
        f = dict(field)
        name = f.get("name", "")
        ftype = f.get("type", "text")

        # backend validation flag + UX hints for per-field progression control.
        backend_validation = FieldValidator.requires_backend(name)
        f["backendValidation"] = backend_validation
        if backend_validation:
            f.setdefault("validateOn", "blur")
            f.setdefault("blockNextUntilValid", True)

        # inline error
        if name in errors:
            f["error"] = errors[name]
            f["hasError"] = True

        hint: Dict[str, Any] = {}

        if name in ("national_id_number", "nok_id_number"):
            hint["pattern"] = r"^(?:[A-Z]{2}\d{12}|[A-Z]{2}\d{10}[A-Z]{2})$"
            hint["patternMessage"] = "Use a valid NIN format"
            f.setdefault("placeholder", "CM123456789012")
            f["maxLength"] = 14

        elif ftype == "tel" or any(k in name for k in ("phone", "mobile")):
            hint["pattern"] = r"^(\+256|0)?7\d{8}$"
            hint["patternMessage"] = "Format: 07XXXXXXXX or +2567XXXXXXXX"
            f.setdefault("placeholder", "07XX XXX XXX")

        elif ftype == "email" or "email" in name:
            hint["pattern"] = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            hint["patternMessage"] = "Please enter a valid email address"
            f.setdefault("placeholder", "example@email.com")

        elif name in ("dob", "date_of_birth") or "birth" in name:
            today = date.today()
            f["max"] = date(today.year - 18, today.month, today.day).isoformat()
            f["min"] = date(today.year - 65, today.month, today.day).isoformat()
            hint["maxDateMessage"] = "You must be at least 18 years old"
            hint["minDateMessage"] = "Age cannot be more than 65 years"

        elif any(k in name for k in ("policyStart", "policy_start", "coverStart", "cover_start")):
            f["min"] = date.today().isoformat()
            hint["minDateMessage"] = "Must be after today"

        elif any(k in name for k in ("departure", "travel_start")):
            f["min"] = date.today().isoformat()
            hint["minDateMessage"] = "Must be in the future"

        elif any(k in name for k in ("return_date", "returnDate", "travel_end")):
            f["min"] = date.today().isoformat()
            hint["afterField"] = "departure_date"
            hint["afterFieldMessage"] = "Must be after departure date"

        elif ftype == "text" and "name" in name:
            hint["minLength"] = f.get("minLength", 2)
            hint["maxLength"] = f.get("maxLength", 50)

        if hint:
            f["validation"] = hint

        return f


# ---------------------------------------------------------------------------
# Progressive disclosure helper
# Replaces filter_already_collected_fields from field_filter.py
# ---------------------------------------------------------------------------

_FIELD_VARIATIONS: Dict[str, List[str]] = {
    "first_name":         ["firstName"],
    "surname":            ["lastName", "last_name"],
    "middle_name":        ["middleName"],
    "mobile_number":      ["mobile", "phone", "phoneNumber"],
    "email":              ["email_address"],
    "national_id_number": ["national_id", "nin"],
}


def filter_collected_fields(
    all_fields: List[Dict[str, Any]],
    collected_data: Dict[str, Any],
    previous_step_keys: List[str],
) -> List[Dict[str, Any]]:
    """
    Return only fields not already collected in previous steps.
    Drop-in replacement for filter_already_collected_fields from field_filter.py.

    Usage:
        fields = filter_collected_fields(all_fields, data, ["quick_quote"])
    """
    already: set = set()
    for key in previous_step_keys:
        step_data = collected_data.get(key, {})
        if isinstance(step_data, dict):
            for k, v in step_data.items():
                if v and (not isinstance(v, str) or v.strip()):
                    already.add(k)

    def _collected(field_name: str) -> bool:
        if field_name in already:
            return True
        for variant in _FIELD_VARIATIONS.get(field_name, []):
            if variant in already:
                return True
        for canonical, variants in _FIELD_VARIATIONS.items():
            if field_name in variants and canonical in already:
                return True
        return False

    return [f for f in all_fields if not _collected(f.get("name", ""))]

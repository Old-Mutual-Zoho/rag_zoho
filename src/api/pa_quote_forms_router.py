"""
APIRouter for Personal Accident quote forms (step-based) with Redis drafts
and final submission to Postgres.

Endpoints:
- POST /quote-forms/personal-accident/start
- PUT  /quote-forms/personal-accident/{draft_id}/steps/{step_index}
- GET  /quote-forms/personal-accident/{draft_id}
- POST /quote-forms/personal-accident/{draft_id}/submit
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.main import get_db, get_redis
from src.api.endpoints.payments import run_underwrite_quote_policy_payment
from src.chatbot.validation import (
    FormValidationError,
    raise_if_errors,
    require_str,
    optional_str,
    validate_email,
    validate_phone_ug,
    validate_date_iso,
    add_error,
    validate_in,
)


api = APIRouter()


def _redis_session_key(draft_id: str) -> str:
    return f"pa_quote:{draft_id}"


def _redis_product_session_key(product: str, draft_id: str) -> str:
    safe = str(product or "").strip().lower().replace("-", "_")
    return f"{safe}_quote:{draft_id}"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _validation_http_exception(err: FormValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error": "validation_error",
            "message": err.message,
            "field_errors": err.field_errors,
        },
    )


def _validate_step_0(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Step 0: firstName, lastName, middleName?
    - firstName, lastName: required, length 2..50
    - middleName: optional, max 50
    """
    errors: Dict[str, str] = {}
    first_name = require_str(payload, "firstName", errors, label="First Name")
    last_name = require_str(payload, "lastName", errors, label="Last Name")
    middle_name = optional_str(payload, "middleName")

    if first_name and (len(first_name) < 2 or len(first_name) > 50):
        add_error(errors, "firstName", "First Name must be 2-50 characters")
    if last_name and (len(last_name) < 2 or len(last_name) > 50):
        add_error(errors, "lastName", "Last Name must be 2-50 characters")
    if middle_name and len(middle_name) > 50:
        add_error(errors, "middleName", "Middle Name must be at most 50 characters")

    raise_if_errors(errors)
    return {
        "firstName": first_name,
        "lastName": last_name,
        "middleName": middle_name,
    }


def _validate_step_1(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Step 1: mobile, email
    - mobile: required, UG format
    - email: required, valid, max 100
    """
    errors: Dict[str, str] = {}
    mobile = validate_phone_ug(payload.get("mobile", ""), errors, field="mobile")
    email = validate_email(payload.get("email", ""), errors, field="email")

    if email and len(email) > 100:
        add_error(errors, "email", "Email must be at most 100 characters")

    raise_if_errors(errors)
    return {
        "mobile": mobile,
        "email": email,
    }


def _validate_step_2(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Step 2: policyStartDate, dob
    - policyStartDate: required ISO date, must be >= (today + 1 day)
    - dob: required ISO date, must be <= today, age between 18 and 65 inclusive
    """
    errors: Dict[str, str] = {}
    policy_start = validate_date_iso(payload.get("policyStartDate", ""), errors, field="policyStartDate", required=True)
    dob = validate_date_iso(payload.get("dob", ""), errors, field="dob", required=True, not_future=True)

    # Additional date rules
    try:
        if policy_start:
            psd = date.fromisoformat(policy_start)
            min_start = date.today() + timedelta(days=1)
            if psd < min_start:
                add_error(errors, "policyStartDate", "Policy start date must be at least tomorrow")
    except Exception:
        # parse error already handled by validator
        pass

    try:
        if dob:
            d_dob = date.fromisoformat(dob)
            today = date.today()
            # Compute age
            age = today.year - d_dob.year - ((today.month, today.day) < (d_dob.month, d_dob.day))
            if age < 18 or age > 65:
                add_error(errors, "dob", "Age must be between 18 and 65")
    except Exception:
        pass

    raise_if_errors(errors)
    return {
        "policyStartDate": policy_start,
        "dob": dob,
    }


def _validate_step_3(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Step 3: coverLimitAmountUgx must be one of {"5000000","10000000","20000000"}
    """
    errors: Dict[str, str] = {}
    cover = validate_in(
        str(payload.get("coverLimitAmountUgx", "")),
        {"5000000", "10000000", "20000000"},
        errors,
        field="coverLimitAmountUgx",
        required=True,
    )
    raise_if_errors(errors)
    return {"coverLimitAmountUgx": cover}


def _load_draft(redis_cache, draft_id: str) -> Dict[str, Any]:
    session_id = _redis_session_key(draft_id)
    obj = redis_cache.get_session(session_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Draft not found")
    return obj


def _load_product_draft(redis_cache, product: str, draft_id: str) -> Dict[str, Any]:
    session_id = _redis_product_session_key(product, draft_id)
    obj = redis_cache.get_session(session_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Draft not found")
    return obj


def _start_product_draft(*, body: Dict[str, Any], db, redis_cache, product_id: str) -> Dict[str, Any]:
    user_id = str(body.get("user_id") or "").strip()
    if not user_id:
        raise FormValidationError(field_errors={"user_id": "user_id is required"})
    user = db.get_or_create_user(phone_number=user_id)
    internal_user_id = str(user.id)
    draft_id = str(uuid4())
    session_id = _redis_product_session_key(product_id, draft_id)
    draft = {
        "draft_id": draft_id,
        "product": product_id,
        "user_id": internal_user_id,
        "data": {},
        "current_step": 0,
        "updated_at": _now_iso(),
    }
    redis_cache.set_session(session_id, draft, ttl=86400)
    return {"draft_id": draft_id}


def _validate_bool_like(payload: Dict[str, Any], field: str, errors: Dict[str, str]) -> str:
    raw = str(payload.get(field) or "").strip().lower()
    if raw not in {"yes", "no", "true", "false"}:
        add_error(errors, field, f"{field} must be yes/no")
    return raw


def _to_int(v: Any, field: str, errors: Dict[str, str], *, min_value: int | None = None) -> int:
    try:
        val = int(str(v))
    except Exception:
        add_error(errors, field, f"{field} must be a whole number")
        return 0
    if min_value is not None and val < min_value:
        add_error(errors, field, f"{field} must be at least {min_value}")
    return val


def _to_float(v: Any, field: str, errors: Dict[str, str], *, min_value: float | None = None) -> float:
    try:
        val = float(str(v))
    except Exception:
        add_error(errors, field, f"{field} must be a number")
        return 0.0
    if min_value is not None and val < min_value:
        add_error(errors, field, f"{field} must be at least {min_value}")
    return val


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _member_dob_value(member: Dict[str, Any]) -> str:
    return str(
        member.get("dob")
        or member.get("D.O.B")
        or member.get("date_of_birth")
        or ""
    ).strip()


def _validate_sereni_members(members: Any, errors: Dict[str, str]) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    if not isinstance(members, list) or not members:
        add_error(errors, "mainMembers", "At least one main member is required")
        return normalized

    today = date.today()
    for idx, member in enumerate(members):
        if not isinstance(member, dict):
            add_error(errors, f"mainMembers[{idx}]", "Member must be an object")
            continue

        include_spouse = _to_bool(member.get("includeSpouse", False))
        include_children = _to_bool(member.get("includeChildren", False))
        if include_spouse and include_children:
            add_error(
                errors,
                f"mainMembers[{idx}]",
                "Only one of includeSpouse or includeChildren can be true",
            )

        dob_raw = _member_dob_value(member)
        if not dob_raw:
            add_error(errors, f"mainMembers[{idx}].dob", "D.O.B is required")
            continue

        d = None
        try:
            d = date.fromisoformat(dob_raw)
        except Exception:
            add_error(errors, f"mainMembers[{idx}].dob", "D.O.B must be a valid date (YYYY-MM-DD)")
            continue

        if d >= today:
            add_error(errors, f"mainMembers[{idx}].dob", "D.O.B must be in the past")
            continue

        age = _to_int(member.get("age"), f"mainMembers[{idx}].age", errors, min_value=0)
        calc_age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        if age != calc_age:
            add_error(errors, f"mainMembers[{idx}].age", f"Age must match D.O.B (expected {calc_age})")

        if include_spouse and calc_age < 19:
            add_error(errors, f"mainMembers[{idx}].dob", "Spouse must be at least 19 years old")

        normalized.append(
            {
                **member,
                "includeSpouse": include_spouse,
                "includeChildren": include_children,
                "dob": dob_raw,
                "age": age,
            }
        )
    return normalized


@api.post("/quote-forms/personal-accident/start")
def pa_start(
    body: Dict[str, Any],
    db=Depends(get_db),
    redis_cache=Depends(get_redis),
):
    """
    Create a new PA quote draft in Redis.
    Body: { "user_id": "..." }
    Returns: { "draft_id": "..." }
    """
    try:
        user_id = str(body.get("user_id") or "").strip()
        if not user_id:
            raise FormValidationError(field_errors={"user_id": "user_id is required"})

        # Ensure user exists (create if necessary) so submit won't break
        user = db.get_or_create_user(phone_number=user_id)
        internal_user_id = str(user.id)

        draft_id = str(uuid4())
        session_id = _redis_session_key(draft_id)
        draft = {
            "draft_id": draft_id,
            "product": "personal_accident",
            "user_id": internal_user_id,
            "data": {},
            "current_step": 0,
            "updated_at": _now_iso(),
        }
        # TTL: 24h (86400 seconds)
        redis_cache.set_session(session_id, draft, ttl=86400)
        return {"draft_id": draft_id}
    except FormValidationError as e:
        raise _validation_http_exception(e)


# ============================================================================
# Motor Private quote forms (PA-style endpoints)
# ============================================================================
def _validate_motor_step_0(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    first_name = require_str(payload, "firstName", errors, label="First Name")
    surname = require_str(payload, "surname", errors, label="Surname")
    mobile = validate_phone_ug(payload.get("mobile", ""), errors, field="mobile")
    email = validate_email(payload.get("email", ""), errors, field="email")
    raise_if_errors(errors)
    return {"firstName": first_name, "surname": surname, "mobile": mobile, "email": email}


def _validate_motor_step_1(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    cover_type = validate_in(str(payload.get("coverType", "")), {"comprehensive", "third_party"}, errors, "coverType", required=True)
    vehicle_make = require_str(payload, "vehicleMake", errors, label="Vehicle Make")
    year = _to_int(payload.get("yearOfManufacture"), "yearOfManufacture", errors, min_value=1980)
    if year > date.today().year + 1:
        add_error(errors, "yearOfManufacture", "yearOfManufacture is too far in the future")
    raise_if_errors(errors)
    return {"coverType": cover_type, "vehicleMake": vehicle_make, "yearOfManufacture": year}


def _validate_motor_step_2(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    start_date = validate_date_iso(payload.get("coverStartDate", ""), errors, "coverStartDate", required=True)
    d = None
    if start_date:
        try:
            d = date.fromisoformat(start_date)
        except Exception:
            pass
    if d and d < date.today():
        add_error(errors, "coverStartDate", "coverStartDate cannot be in the past")
    is_rare = _validate_bool_like(payload, "isRareModel", errors)
    valuation = _validate_bool_like(payload, "hasUndergoneValuation", errors)
    vehicle_value = _to_float(payload.get("vehicleValueUgx"), "vehicleValueUgx", errors, min_value=1.0)
    raise_if_errors(errors)
    return {
        "coverStartDate": start_date,
        "isRareModel": is_rare,
        "hasUndergoneValuation": valuation,
        "vehicleValueUgx": vehicle_value,
    }


def _validate_motor_step_3(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    alarm = _validate_bool_like(payload, "carAlarmInstalled", errors)
    tracking = _validate_bool_like(payload, "trackingSystemInstalled", errors)
    region = validate_in(
        str(payload.get("carUsageRegion", "")),
        {"Within Uganda", "Within East Africa", "Outside East Africa"},
        errors,
        "carUsageRegion",
        required=True,
    )
    raise_if_errors(errors)
    return {"carAlarmInstalled": alarm, "trackingSystemInstalled": tracking, "carUsageRegion": region}


@api.post("/quote-forms/motor-private/start")
def motor_start(body: Dict[str, Any], db=Depends(get_db), redis_cache=Depends(get_redis)):
    try:
        return _start_product_draft(body=body, db=db, redis_cache=redis_cache, product_id="motor_private")
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.put("/quote-forms/motor-private/{draft_id}/steps/{step_index}")
def motor_update_step(draft_id: str, step_index: int, body: Dict[str, Any], redis_cache=Depends(get_redis)):
    try:
        if step_index < 0 or step_index > 3:
            raise FormValidationError(
                field_errors={"step_index": "Invalid step index. Expected a value from 0 to 3."}
            )
        draft = _load_product_draft(redis_cache, "motor_private", draft_id)
        step_data = [_validate_motor_step_0, _validate_motor_step_1, _validate_motor_step_2, _validate_motor_step_3][step_index](body)
        data = dict(draft.get("data") or {})
        data.update(step_data)
        draft["data"] = data
        draft["current_step"] = step_index
        draft["updated_at"] = _now_iso()
        redis_cache.set_session(_redis_product_session_key("motor_private", draft_id), draft, ttl=86400)
        return draft
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.get("/quote-forms/motor-private/{draft_id}")
def motor_get_draft(draft_id: str, redis_cache=Depends(get_redis)):
    return _load_product_draft(redis_cache, "motor_private", draft_id)


@api.post("/quote-forms/motor-private/{draft_id}/submit")
async def motor_submit(draft_id: str, body: Dict[str, Any] | None = None, db=Depends(get_db), redis_cache=Depends(get_redis)):
    try:
        body = body or {}
        draft = _load_product_draft(redis_cache, "motor_private", draft_id)
        data = dict(draft.get("data") or {})
        _validate_motor_step_0(data)
        _validate_motor_step_1(data)
        s2 = _validate_motor_step_2(data)
        _validate_motor_step_3(data)

        workflow_result = await run_underwrite_quote_policy_payment(
            user_id=draft["user_id"],
            product_id="motor_private",
            underwriting_data={
                "policyStartDate": s2.get("coverStartDate"),
                "vehicleValueUgx": s2.get("vehicleValueUgx"),
                "coverType": data.get("coverType"),
            },
            provider=body.get("provider"),
            phone_number=body.get("phone_number") or data.get("mobile"),
            currency=str(body.get("currency") or "UGX"),
            payee_name=str(body.get("payee_name") or "Old Mutual"),
            payment_before_policy=bool(body.get("payment_before_policy", False)),
            metadata={"draft_id": draft_id},
        )
        if workflow_result.get("declined"):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "underwriting_declined",
                    "decision_status": workflow_result.get("decision_status"),
                    "underwriting": workflow_result.get("underwriting"),
                },
            )
        quotation = workflow_result.get("quotation") or {}
        payable_amount = float(quotation.get("payable_amount") or 0.0)
        quote_status = "payment_initiated" if workflow_result.get("payment") else "quoted"
        quote = db.create_quote(
            user_id=draft["user_id"],
            product_id="motor_private",
            product_name="Motor Private",
            premium_amount=payable_amount,
            sum_assured=float(data.get("vehicleValueUgx") or 0.0),
            underwriting_data={"form_data": data, "workflow": workflow_result},
            pricing_breakdown=quotation.get("raw"),
            status=quote_status,
        )
        redis_cache.delete_session(_redis_product_session_key("motor_private", draft_id))
        return {
            "quote_id": str(quote.id),
            "status": quote_status,
            "workflow": workflow_result.get("workflow"),
            "underwriting": workflow_result.get("underwriting"),
            "quotation": workflow_result.get("quotation"),
            "policy": workflow_result.get("policy"),
            "payment": workflow_result.get("payment"),
            "next_action": workflow_result.get("next_action"),
        }
    except FormValidationError as e:
        raise _validation_http_exception(e)


# ============================================================================
# Serenicare quote forms (PA-style endpoints)
# ============================================================================
def _validate_sereni_step_0(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    first_name = require_str(payload, "firstName", errors, label="First Name")
    last_name = require_str(payload, "lastName", errors, label="Last Name")
    middle_name = optional_str(payload, "middleName")
    if first_name and (len(first_name) < 2 or len(first_name) > 50):
        add_error(errors, "firstName", "First Name must be 2-50 characters")
    if last_name and (len(last_name) < 2 or len(last_name) > 50):
        add_error(errors, "lastName", "Last Name must be 2-50 characters")
    if middle_name and len(middle_name) > 50:
        add_error(errors, "middleName", "Middle Name must be at most 50 characters")
    mobile = validate_phone_ug(payload.get("mobile", ""), errors, field="mobile")
    email = validate_email(payload.get("email", ""), errors, field="email")
    if email and len(email) > 100:
        add_error(errors, "email", "Email must be at most 100 characters")
    raise_if_errors(errors)
    return {
        "firstName": first_name,
        "lastName": last_name,
        "middleName": middle_name,
        "mobile": mobile,
        "email": email,
    }


def _validate_sereni_step_1(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    plan_type = validate_in(
        str(payload.get("planType", "")),
        {"essential", "classic", "comprehensive", "premium"},
        errors,
        "planType",
        required=True,
    )
    serious = validate_in(str(payload.get("seriousConditions", "")), {"yes", "no"}, errors, "seriousConditions", required=True)
    optional_benefits = payload.get("optionalBenefits", [])
    if isinstance(optional_benefits, str):
        optional_benefits = [v.strip() for v in optional_benefits.split(",") if v.strip()]
    if not isinstance(optional_benefits, list):
        add_error(errors, "optionalBenefits", "optionalBenefits must be a list")
        optional_benefits = []
    allowed = {"outpatient", "maternity", "dental", "optical", "covid"}
    for opt in optional_benefits:
        if str(opt) not in allowed:
            add_error(errors, "optionalBenefits", "optionalBenefits contains invalid value(s)")
            break
    raise_if_errors(errors)
    return {"planType": plan_type, "seriousConditions": serious, "optionalBenefits": optional_benefits}


def _validate_sereni_step_2(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    members = _validate_sereni_members(payload.get("mainMembers", []), errors)
    raise_if_errors(errors)
    return {"mainMembers": members}


def _validate_sereni_step_3(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Optional final/preferences step for frontend parity; no required fields.
    dob = str(payload.get("date_of_birth") or "").strip()
    include_spouse = _to_bool(payload.get("include_spouse", False))
    include_children = _to_bool(payload.get("include_children", False))
    add_member = _to_bool(payload.get("add_another_main_member", False))
    return {
        "date_of_birth": dob,
        "include_spouse": include_spouse,
        "include_children": include_children,
        "add_another_main_member": add_member,
    }


@api.post("/quote-forms/serenicare/start")
def serenicare_start(body: Dict[str, Any], db=Depends(get_db), redis_cache=Depends(get_redis)):
    try:
        return _start_product_draft(body=body, db=db, redis_cache=redis_cache, product_id="serenicare")
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.put("/quote-forms/serenicare/{draft_id}/steps/{step_index}")
def serenicare_update_step(draft_id: str, step_index: int, body: Dict[str, Any], redis_cache=Depends(get_redis)):
    try:
        if step_index < 0 or step_index > 3:
            raise FormValidationError(
                field_errors={"step_index": "Invalid step index. Expected a value from 0 to 3."}
            )
        draft = _load_product_draft(redis_cache, "serenicare", draft_id)
        step_data = [_validate_sereni_step_0, _validate_sereni_step_1, _validate_sereni_step_2, _validate_sereni_step_3][step_index](body)
        data = dict(draft.get("data") or {})
        data.update(step_data)
        draft["data"] = data
        draft["current_step"] = step_index
        draft["updated_at"] = _now_iso()
        redis_cache.set_session(_redis_product_session_key("serenicare", draft_id), draft, ttl=86400)
        return draft
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.get("/quote-forms/serenicare/{draft_id}")
def serenicare_get_draft(draft_id: str, redis_cache=Depends(get_redis)):
    return _load_product_draft(redis_cache, "serenicare", draft_id)


@api.post("/quote-forms/serenicare/{draft_id}/submit")
async def serenicare_submit(draft_id: str, body: Dict[str, Any] | None = None, db=Depends(get_db), redis_cache=Depends(get_redis)):
    try:
        body = body or {}
        draft = _load_product_draft(redis_cache, "serenicare", draft_id)
        data = dict(draft.get("data") or {})
        _validate_sereni_step_0(data)
        s1 = _validate_sereni_step_1(data)
        s2 = _validate_sereni_step_2(data)
        s3 = _validate_sereni_step_3(data)
        primary_member_dob = ""
        if s2.get("mainMembers"):
            primary_member_dob = str((s2["mainMembers"][0] or {}).get("dob") or "")
        workflow_result = await run_underwrite_quote_policy_payment(
            user_id=draft["user_id"],
            product_id="serenicare",
            underwriting_data={
                "plan_option": {"id": s1.get("planType")},
                "optional_benefits": s1.get("optionalBenefits", []),
                "medical_conditions": s1.get("seriousConditions") == "yes",
                "dob": s3.get("date_of_birth") or primary_member_dob,
                "policyStartDate": date.today().isoformat(),
            },
            provider=body.get("provider"),
            phone_number=body.get("phone_number") or data.get("mobile"),
            currency=str(body.get("currency") or "UGX"),
            payee_name=str(body.get("payee_name") or "Old Mutual"),
            payment_before_policy=bool(body.get("payment_before_policy", False)),
            metadata={"draft_id": draft_id},
        )
        if workflow_result.get("declined"):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "underwriting_declined",
                    "decision_status": workflow_result.get("decision_status"),
                    "underwriting": workflow_result.get("underwriting"),
                },
            )
        quotation = workflow_result.get("quotation") or {}
        payable_amount = float(quotation.get("payable_amount") or 0.0)
        quote_status = "payment_initiated" if workflow_result.get("payment") else "quoted"
        quote = db.create_quote(
            user_id=draft["user_id"],
            product_id="serenicare",
            product_name="Serenicare",
            premium_amount=payable_amount,
            sum_assured=None,
            underwriting_data={"form_data": data, "workflow": workflow_result},
            pricing_breakdown=quotation.get("raw"),
            status=quote_status,
        )
        redis_cache.delete_session(_redis_product_session_key("serenicare", draft_id))
        return {
            "quote_id": str(quote.id),
            "status": quote_status,
            "workflow": workflow_result.get("workflow"),
            "underwriting": workflow_result.get("underwriting"),
            "quotation": workflow_result.get("quotation"),
            "policy": workflow_result.get("policy"),
            "payment": workflow_result.get("payment"),
            "next_action": workflow_result.get("next_action"),
        }
    except FormValidationError as e:
        raise _validation_http_exception(e)


# ============================================================================
# Travel Insurance quote forms (PA-style endpoints)
# ============================================================================
def _validate_travel_step_0(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    first_name = require_str(payload, "first_name", errors, label="First Name")
    surname = require_str(payload, "surname", errors, label="Surname")
    phone = validate_phone_ug(payload.get("phone_number", ""), errors, field="phone_number")
    email = validate_email(payload.get("email", ""), errors, field="email")
    raise_if_errors(errors)
    return {"first_name": first_name, "surname": surname, "phone_number": phone, "email": email}


def _validate_travel_step_1(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    product_id = require_str(payload, "product_id", errors, label="Product")
    travel_party = validate_in(str(payload.get("travel_party", "")), {"myself_only", "myself_and_someone_else", "group"}, errors, "travel_party", required=True)
    n1 = _to_int(payload.get("num_travellers_18_69"), "num_travellers_18_69", errors, min_value=1)
    n2 = _to_int(payload.get("num_travellers_0_17", 0), "num_travellers_0_17", errors, min_value=0)
    departure_country = require_str(payload, "departure_country", errors, label="Departure Country")
    destination_country = require_str(payload, "destination_country", errors, label="Destination Country")
    departure_date = validate_date_iso(payload.get("departure_date", ""), errors, "departure_date", required=True)
    return_date = validate_date_iso(payload.get("return_date", ""), errors, "return_date", required=True)
    try:
        if departure_date and return_date and date.fromisoformat(return_date) < date.fromisoformat(departure_date):
            add_error(errors, "return_date", "Return date cannot be before departure date")
    except Exception:
        pass
    raise_if_errors(errors)
    return {
        "product_id": product_id,
        "travel_party": travel_party,
        "num_travellers_18_69": n1,
        "num_travellers_0_17": n2,
        "departure_country": departure_country,
        "destination_country": destination_country,
        "departure_date": departure_date,
        "return_date": return_date,
    }


def _validate_travel_step_2(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    agreed = payload.get("terms_and_conditions_agreed") in (True, "yes", "true", "1")
    if not agreed:
        add_error(errors, "terms_and_conditions_agreed", "You must agree to the Terms and Conditions")
    raise_if_errors(errors)
    return {"terms_and_conditions_agreed": agreed}


def _validate_travel_step_3(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    ec_surname = require_str(payload, "ec_surname", errors, label="Emergency contact surname")
    ec_relationship = require_str(payload, "ec_relationship", errors, label="Emergency contact relationship")
    ec_phone = validate_phone_ug(payload.get("ec_phone_number", ""), errors, field="ec_phone_number")
    ec_email = validate_email(payload.get("ec_email", ""), errors, field="ec_email")
    passport = require_str(payload, "passport_file_ref", errors, label="Passport file")
    raise_if_errors(errors)
    return {
        "ec_surname": ec_surname,
        "ec_relationship": ec_relationship,
        "ec_phone_number": ec_phone,
        "ec_email": ec_email,
        "passport_file_ref": passport,
    }


@api.post("/quote-forms/travel-insurance/start")
def travel_start(body: Dict[str, Any], db=Depends(get_db), redis_cache=Depends(get_redis)):
    try:
        return _start_product_draft(body=body, db=db, redis_cache=redis_cache, product_id="travel_insurance")
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.put("/quote-forms/travel-insurance/{draft_id}/steps/{step_index}")
def travel_update_step(draft_id: str, step_index: int, body: Dict[str, Any], redis_cache=Depends(get_redis)):
    try:
        if step_index < 0 or step_index > 3:
            raise FormValidationError(
                field_errors={"step_index": "Invalid step index. Expected a value from 0 to 3."}
            )
        draft = _load_product_draft(redis_cache, "travel_insurance", draft_id)
        step_data = [_validate_travel_step_0, _validate_travel_step_1, _validate_travel_step_2, _validate_travel_step_3][step_index](body)
        data = dict(draft.get("data") or {})
        data.update(step_data)
        draft["data"] = data
        draft["current_step"] = step_index
        draft["updated_at"] = _now_iso()
        redis_cache.set_session(_redis_product_session_key("travel_insurance", draft_id), draft, ttl=86400)
        return draft
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.get("/quote-forms/travel-insurance/{draft_id}")
def travel_get_draft(draft_id: str, redis_cache=Depends(get_redis)):
    return _load_product_draft(redis_cache, "travel_insurance", draft_id)


@api.post("/quote-forms/travel-insurance/{draft_id}/submit")
async def travel_submit(draft_id: str, body: Dict[str, Any] | None = None, db=Depends(get_db), redis_cache=Depends(get_redis)):
    try:
        body = body or {}
        draft = _load_product_draft(redis_cache, "travel_insurance", draft_id)
        data = dict(draft.get("data") or {})
        _validate_travel_step_0(data)
        s1 = _validate_travel_step_1(data)
        _validate_travel_step_2(data)
        _validate_travel_step_3(data)
        travellers_total = int(s1.get("num_travellers_18_69", 0)) + int(s1.get("num_travellers_0_17", 0))
        workflow_result = await run_underwrite_quote_policy_payment(
            user_id=draft["user_id"],
            product_id="travel_insurance",
            underwriting_data={
                "product_id": s1.get("product_id"),
                "departure_date": s1.get("departure_date"),
                "return_date": s1.get("return_date"),
                "travellers_total": travellers_total,
                "policyStartDate": s1.get("departure_date"),
            },
            provider=body.get("provider"),
            phone_number=body.get("phone_number") or data.get("phone_number"),
            currency=str(body.get("currency") or "UGX"),
            payee_name=str(body.get("payee_name") or "Old Mutual"),
            payment_before_policy=bool(body.get("payment_before_policy", False)),
            metadata={"draft_id": draft_id},
        )
        if workflow_result.get("declined"):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "underwriting_declined",
                    "decision_status": workflow_result.get("decision_status"),
                    "underwriting": workflow_result.get("underwriting"),
                },
            )
        quotation = workflow_result.get("quotation") or {}
        payable_amount = float(quotation.get("payable_amount") or 0.0)
        quote_status = "payment_initiated" if workflow_result.get("payment") else "quoted"
        quote = db.create_quote(
            user_id=draft["user_id"],
            product_id="travel_insurance",
            product_name="Travel Insurance",
            premium_amount=payable_amount,
            sum_assured=None,
            underwriting_data={"form_data": data, "workflow": workflow_result},
            pricing_breakdown=quotation.get("raw"),
            status=quote_status,
        )
        redis_cache.delete_session(_redis_product_session_key("travel_insurance", draft_id))
        return {
            "quote_id": str(quote.id),
            "status": quote_status,
            "workflow": workflow_result.get("workflow"),
            "underwriting": workflow_result.get("underwriting"),
            "quotation": workflow_result.get("quotation"),
            "policy": workflow_result.get("policy"),
            "payment": workflow_result.get("payment"),
            "next_action": workflow_result.get("next_action"),
        }
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.put("/quote-forms/personal-accident/{draft_id}/steps/{step_index}")
def pa_update_step(
    draft_id: str,
    step_index: int,
    body: Dict[str, Any],
    redis_cache=Depends(get_redis),
):
    """
    Update a specific step (0..3). Validates fields for that step only,
    merges into draft["data"], bumps current_step and timestamp, and stores in Redis.
    Returns the updated draft object.
    """
    try:
        if step_index < 0 or step_index > 3:
            raise FormValidationError(
                field_errors={"step_index": "Invalid step index. Expected a value from 0 to 3."}
            )

        draft = _load_draft(redis_cache, draft_id)

        # Validate step-specific payload
        if step_index == 0:
            step_data = _validate_step_0(body)
        elif step_index == 1:
            step_data = _validate_step_1(body)
        elif step_index == 2:
            step_data = _validate_step_2(body)
        else:
            step_data = _validate_step_3(body)

        # Merge and update metadata
        data = dict(draft.get("data") or {})
        data.update(step_data)
        draft["data"] = data
        draft["current_step"] = step_index
        draft["updated_at"] = _now_iso()

        # Persist back to Redis (keep TTL fresh by re-setting with 24h)
        session_id = _redis_session_key(draft_id)
        redis_cache.set_session(session_id, draft, ttl=86400)

        return draft
    except FormValidationError as e:
        raise _validation_http_exception(e)


@api.get("/quote-forms/personal-accident/{draft_id}")
def pa_get_draft(draft_id: str, redis_cache=Depends(get_redis)):
    """Return the full draft object or 404 if missing."""
    return _load_draft(redis_cache, draft_id)


@api.post("/quote-forms/personal-accident/{draft_id}/submit")
async def pa_submit(
    draft_id: str,
    body: Dict[str, Any] | None = None,
    db=Depends(get_db),
    redis_cache=Depends(get_redis),
):
    """
    Validate full draft, run underwriting -> quotation -> policy issuance -> payment,
    and persist the resulting quote in Postgres.
    """
    try:
        body = body or {}
        draft = _load_draft(redis_cache, draft_id)
        data = dict(draft.get("data") or {})

        # Validate all steps against aggregated data
        _validate_step_0(data)
        _validate_step_1(data)
        _validate_step_2(data)
        final = _validate_step_3(data)

        # Build underwriting payload aligned to the Personal Accident mock/service contract
        sum_assured_str = final["coverLimitAmountUgx"]
        try:
            sum_assured_val = float(sum_assured_str)
        except Exception:
            # Should not happen due to validate_in, but defend
            raise FormValidationError(field_errors={"coverLimitAmountUgx": "Invalid amount"})

        workflow_result = await run_underwrite_quote_policy_payment(
            user_id=draft["user_id"],
            product_id="personal_accident",
            underwriting_data={
                "dob": data.get("dob"),
                "coverLimitAmountUgx": sum_assured_str,
                "riskyActivities": data.get("riskyActivities", []),
                "policyStartDate": data.get("policyStartDate"),
            },
            provider=body.get("provider"),
            phone_number=body.get("phone_number") or data.get("mobile"),
            currency=str(body.get("currency") or "UGX"),
            payee_name=str(body.get("payee_name") or "Old Mutual"),
            payment_before_policy=bool(body.get("payment_before_policy", False)),
            metadata={"draft_id": draft_id},
        )

        if workflow_result.get("declined"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "underwriting_declined",
                    "decision_status": workflow_result.get("decision_status"),
                    "underwriting": workflow_result.get("underwriting"),
                },
            )

        quotation = workflow_result.get("quotation") or {}
        payable_amount = float(quotation.get("payable_amount") or 0.0)
        quote_status = "payment_initiated" if workflow_result.get("payment") else "quoted"

        # Persist quote
        quote = db.create_quote(
            user_id=draft["user_id"],
            product_id="personal_accident",
            product_name="Personal Accident",
            premium_amount=payable_amount,
            sum_assured=sum_assured_val,
            underwriting_data={
                "form_data": data,
                "workflow": workflow_result,
            },
            pricing_breakdown=quotation.get("raw"),
            status=quote_status,
        )

        # Clean up draft
        redis_cache.delete_session(_redis_session_key(draft_id))

        return {
            "quote_id": str(quote.id),
            "status": quote_status,
            "workflow": workflow_result.get("workflow"),
            "underwriting": workflow_result.get("underwriting"),
            "quotation": workflow_result.get("quotation"),
            "policy": workflow_result.get("policy"),
            "payment": workflow_result.get("payment"),
            "next_action": workflow_result.get("next_action"),
        }
    except FormValidationError as e:
        raise _validation_http_exception(e)

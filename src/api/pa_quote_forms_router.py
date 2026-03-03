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


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_error", "message": e.message, "field_errors": e.field_errors},
        )


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
            raise HTTPException(status_code=400, detail="Invalid step index")

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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_error", "message": e.message, "field_errors": e.field_errors},
        )


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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_error", "message": e.message, "field_errors": e.field_errors},
        )

"""
Validation endpoints for guided flow forms.

Two endpoints:
  POST /flow/validate-field  — validate a single field on blur
  POST /flow/validate-step   — validate all fields in a step on submission
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any, Dict, Optional

from src.chatbot.field_validator import FieldValidator, StepValidator

router = APIRouter()


# ---------------------------------------------------------------------------
# Per-field validation
# ---------------------------------------------------------------------------

class ValidateFieldRequest(BaseModel):
    product_id: str          # "personal_accident", "motor", "travel" etc.
    field: str               # "dob", "mobile", "national_id_number" etc.
    value: Any               # raw value submitted by the frontend
    session_data: Dict = {}  # current collected data for cross-field rules


@router.post("/flow/validate-field")
async def validate_field(request: ValidateFieldRequest):
    """
    Validate a single field value on blur.
    Only called for fields where backendValidation: true in the field definition.

    Returns:
        {"valid": true,  "field": "dob"}
        {"valid": false, "field": "dob", "error": "You must be at least 18 years old."}
    """
    return FieldValidator.validate(
        field=request.field,
        value=request.value,
        context=request.session_data,
    )


# ---------------------------------------------------------------------------
# Step validation
# ---------------------------------------------------------------------------

class ValidateStepRequest(BaseModel):
    product_id: str          # "personal_accident", "motor", "travel" etc.
    step: str                # "quick_quote", "personal_details", "next_of_kin" etc.
    payload: Dict[str, Any]  # all field values submitted for this step
    session_data: Dict = {}  # previously collected data for context-aware rules


@router.post("/flow/validate-step")
async def validate_step(request: ValidateStepRequest):
    """
    Validate all fields in a step at once.
    Called on step form submission before the frontend advances.

    Returns on success:
        {"valid": true, "product_id": "personal_accident", "step": "quick_quote"}

    Returns on failure:
        {
            "valid": false,
            "errors": {"dob": "You must be at least 18 years old.", ...},
            "error_summary": "Please fix 1 error(s) to continue."
        }
    """
    errors = StepValidator.validate(
        product_id=request.product_id,
        step=request.step,
        payload=request.payload,
        context=request.session_data,
    )

    if errors:
        return {
            "valid": False,
            "product_id": request.product_id,
            "step": request.step,
            "errors": errors,
            "error_summary": f"Please fix {len(errors)} error(s) to continue.",
        }

    return {
        "valid": True,
        "product_id": request.product_id,
        "step": request.step,
    }

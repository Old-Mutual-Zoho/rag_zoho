from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.integrations.policy.policy_service import PolicyService
from src.integrations.policy.response_wrappers import (
    IntegrationResponseError,
    normalize_policy_response,
)

api = APIRouter()
policies_api = api


class PolicyIssueRequest(BaseModel):
    user_id: str = Field(..., description="Internal/external user id")
    product_id: str = Field(..., description="Product id, e.g. personal_accident")
    quote_id: str = Field(..., description="Quote reference for policy issuance")
    currency: str = Field(default="UGX", description="Policy currency")
    premium_amount: float = Field(..., description="Approved payable premium")
    policy_start_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD")
    policy_end_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD")
    payment_status: Optional[str] = Field(default=None, description="Optional payment status")
    requires_payment_before_issuance: bool = Field(
        default=False,
        description="If true, policy may remain pending until payment succeeds.",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PolicyCancelRequest(BaseModel):
    reason: str = Field(default="", description="Cancellation reason")


@api.post("/issue", tags=["Policies"])
async def issue_policy(request: PolicyIssueRequest):
    try:
        payload: Dict[str, Any] = {
            "user_id": request.user_id,
            "product_id": request.product_id,
            "quote_id": request.quote_id,
            "currency": request.currency,
            "premium_amount": request.premium_amount,
            "policy_start_date": request.policy_start_date,
            "policy_end_date": request.policy_end_date,
            "payment_status": request.payment_status,
            "requires_payment_before_issuance": request.requires_payment_before_issuance,
            **request.metadata,
        }
        raw = await PolicyService().issue_policy(payload)
        policy = normalize_policy_response(
            raw,
            fallback_quote_id=request.quote_id,
            fallback_currency=request.currency,
        )
        return policy.model_dump()
    except IntegrationResponseError as e:
        raise HTTPException(
            status_code=502,
            detail={
                "message": str(e),
                "stage": "partner_response_validation",
                "payload": e.payload,
            },
        ) from e


@api.get("/{policy_id}", tags=["Policies"])
async def get_policy(policy_id: str):
    try:
        raw = await PolicyService().get_policy(policy_id)
        policy = normalize_policy_response(raw)
        return policy.model_dump()
    except KeyError:
        raise HTTPException(status_code=404, detail="Policy not found")
    except IntegrationResponseError as e:
        raise HTTPException(
            status_code=502,
            detail={
                "message": str(e),
                "stage": "partner_response_validation",
                "payload": e.payload,
            },
        ) from e


@api.post("/{policy_id}/cancel", tags=["Policies"])
async def cancel_policy(policy_id: str, request: PolicyCancelRequest):
    try:
        raw = await PolicyService().cancel_policy(policy_id, request.reason)
        policy = normalize_policy_response(raw)
        return policy.model_dump()
    except KeyError:
        raise HTTPException(status_code=404, detail="Policy not found")
    except IntegrationResponseError as e:
        raise HTTPException(
            status_code=502,
            detail={
                "message": str(e),
                "stage": "partner_response_validation",
                "payload": e.payload,
            },
        ) from e

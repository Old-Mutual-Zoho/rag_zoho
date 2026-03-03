import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.integrations.clients.mocks.mtn import MTNMockClient
from src.integrations.clients.mocks.airtel import AirtelMockClient
from src.integrations.clients.mocks.underwriting import mock_underwriting_client
from src.integrations.clients.real_http.payments import RealPaymentsClient
from src.integrations.contracts.payments import PaymentRequest, PaymentResponse
from src.integrations.policy.policy_service import PolicyService
from src.integrations.policy.quotation_service import QuotationService
from src.integrations.policy.response_wrappers import (
    IntegrationResponseError,
    normalize_policy_response,
    normalize_quotation_response,
    normalize_underwriting_response,
)
from src.integrations.policy.underwriting_service import UnderwritingService

api = APIRouter()
payments_api = api


class PaymentInitiateRequest(BaseModel):
    quote_id: str
    provider: str
    phone_number: str
    amount: float
    currency: str = "UGX"
    payee_name: str = "Old Mutual"


class UnderwriteQuotePayRequest(BaseModel):
    provider: str = Field(..., description="Payment provider: mtn or airtel")
    phone_number: str = Field(..., description="Customer phone number to receive payment prompt")
    user_id: str = Field(..., description="Your internal/external user id")
    product_id: str = Field(..., description="Product identifier used for underwriting/quotation")
    underwriting_data: Dict[str, Any] = Field(default_factory=dict, description="KYC + risk payload for underwriting")
    payment_before_policy: bool = Field(
        default=False,
        description="When true: payment is initiated before policy issuance.",
    )
    currency: str = "UGX"
    payee_name: str = Field(default="Old Mutual", description="Entity displayed in payment description/prompt")
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _should_use_real_integrations() -> bool:
    mode = os.getenv("INTEGRATIONS_MODE", "").strip().lower()
    if mode in {"real", "live"}:
        return True
    if mode in {"mock", "test"}:
        return False
    return bool(
        os.getenv("PARTNER_UNDERWRITING_API_URL")
        or os.getenv("PARTNER_QUOTATION_API_URL")
        or os.getenv("PARTNER_POLICY_API_URL")
        or os.getenv("PARTNER_PAYMENT_API_URL")
    )


def _select_payment_client(provider: str):
    provider_key = (provider or "").strip().lower()
    if provider_key not in {"mtn", "airtel"}:
        raise HTTPException(status_code=400, detail="Invalid provider. Expected 'mtn' or 'airtel'.")

    if _should_use_real_integrations() and os.getenv("PARTNER_PAYMENT_API_URL"):
        return RealPaymentsClient(provider=provider_key)

    return MTNMockClient() if provider_key == "mtn" else AirtelMockClient()


def _normalize_status(value: Optional[str]) -> str:
    raw = (value or "").strip().upper()
    return raw or "UNKNOWN"


@api.post("/initiate", tags=["Payments"])
@api.post("/payments/initiate", tags=["Payments"])
async def initiate_payment(request: PaymentInitiateRequest):
    client = _select_payment_client(request.provider)
    payment_request = PaymentRequest(
        reference=request.quote_id,
        phone_number=request.phone_number,
        amount=request.amount,
        currency=request.currency,
        description=f"Payment to {request.payee_name} for quote {request.quote_id}",
        metadata={"payee_name": request.payee_name},
    )

    payment_response: PaymentResponse = await _initiate_payment(client, payment_request)
    return _payment_response_to_dict(payment_response)


@api.post("/underwrite-quote-pay", tags=["Payments"])
@api.post("/underwrite-quote-policy-pay", tags=["Payments"])
async def underwrite_quote_pay(request: UnderwriteQuotePayRequest):
    try:
        result = await run_underwrite_quote_policy_payment(
            user_id=request.user_id,
            product_id=request.product_id,
            underwriting_data=request.underwriting_data,
            provider=request.provider,
            phone_number=request.phone_number,
            currency=request.currency,
            payee_name=request.payee_name,
            metadata=request.metadata,
            payment_before_policy=request.payment_before_policy,
        )
        if result.get("declined"):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Underwriting decision declined. Payment not initiated.",
                    "decision_status": result.get("decision_status"),
                    "underwriting": result.get("underwriting"),
                },
            )
        return result
    except IntegrationResponseError as e:
        raise HTTPException(
            status_code=502,
            detail={
                "message": str(e),
                "stage": "partner_response_validation",
                "payload": e.payload,
            },
        ) from e


async def run_underwrite_quote_policy_payment(
    *,
    user_id: str,
    product_id: str,
    underwriting_data: Dict[str, Any],
    currency: str = "UGX",
    payee_name: str = "Old Mutual",
    metadata: Optional[Dict[str, Any]] = None,
    provider: Optional[str] = None,
    phone_number: Optional[str] = None,
    payment_before_policy: bool = False,
) -> Dict[str, Any]:
    metadata = metadata or {}
    workflow = ["underwriting", "quotation"]

    underwriting_payload = {
        "user_id": user_id,
        "product_id": product_id,
        "underwriting_data": underwriting_data,
        "currency": currency,
        **metadata,
    }

    if _should_use_real_integrations() and os.getenv("PARTNER_UNDERWRITING_API_URL"):
        underwriting_raw = await UnderwritingService().submit_underwriting(underwriting_payload)
    else:
        mock_payload = {
            **(underwriting_data or {}),
            "user_id": user_id,
            "product_id": product_id,
            "currency": currency,
            "underwriting_data": underwriting_data,
            **metadata,
        }
        underwriting_raw = await mock_underwriting_client.submit_underwriting(mock_payload)

    underwriting = normalize_underwriting_response(underwriting_raw)
    decision = _normalize_status(underwriting.decision_status)
    if decision in {"DECLINED", "REJECTED"}:
        return {
            "declined": True,
            "decision_status": decision,
            "underwriting": underwriting.model_dump(),
        }

    quotation_payload: Dict[str, Any] = {
        "user_id": user_id,
        "product_id": product_id,
        "underwriting": underwriting.model_dump(),
        "currency": currency,
        **metadata,
    }
    if _should_use_real_integrations() and os.getenv("PARTNER_QUOTATION_API_URL"):
        quotation_raw = await QuotationService(
            base_url=os.getenv("PARTNER_QUOTATION_API_URL", ""),
            api_key=os.getenv("PARTNER_QUOTATION_API_KEY"),
        ).get_quote(quotation_payload)
    else:
        quotation_raw = {
            "quote_id": underwriting.quote_id,
            "premium": underwriting.premium,
            "currency": underwriting.currency or currency,
            "status": "quoted",
        }

    quotation = normalize_quotation_response(
        quotation_raw,
        fallback_quote_id=underwriting.quote_id,
        fallback_currency=currency,
    )

    payment_response_dict: Optional[Dict[str, Any]] = None
    payment_status = "NOT_INITIATED"
    payment_enabled = bool((provider or "").strip() and (phone_number or "").strip())
    policy_service = PolicyService(
        base_url=os.getenv("PARTNER_POLICY_API_URL", ""),
        api_key=os.getenv("PARTNER_POLICY_API_KEY"),
    )

    async def _do_payment() -> Dict[str, Any]:
        client = _select_payment_client(str(provider))
        req = PaymentRequest(
            reference=quotation.quote_id,
            phone_number=str(phone_number),
            amount=quotation.amount,
            currency=quotation.currency,
            description=f"Payment to {payee_name} for quote {quotation.quote_id}",
            metadata={
                "payee_name": payee_name,
                "product_id": product_id,
                "user_id": user_id,
                "quotation_status": quotation.status,
                **metadata,
            },
        )
        payment_response = await _initiate_payment(client, req)
        return _payment_response_to_dict(payment_response)

    async def _do_policy_issue(current_payment_status: str) -> Dict[str, Any]:
        policy_payload: Dict[str, Any] = {
            "user_id": user_id,
            "product_id": product_id,
            "quote_id": quotation.quote_id,
            "currency": quotation.currency,
            "premium_amount": quotation.amount,
            "policy_start_date": underwriting_data.get("policyStartDate"),
            "payment_status": current_payment_status,
            "requires_payment_before_issuance": payment_before_policy,
            "underwriting": underwriting.model_dump(),
            "quotation": quotation.model_dump(),
            **metadata,
        }
        policy_raw = await policy_service.issue_policy(policy_payload)
        return normalize_policy_response(
            policy_raw,
            fallback_quote_id=quotation.quote_id,
            fallback_currency=quotation.currency,
        ).model_dump()

    if payment_enabled and payment_before_policy:
        workflow.extend(["payment", "policy_issuance"])
        payment_response_dict = await _do_payment()
        payment_status = _normalize_status(str(payment_response_dict.get("status")))
        policy = await _do_policy_issue(payment_status)
    elif payment_enabled:
        workflow.extend(["policy_issuance", "payment"])
        policy = await _do_policy_issue(payment_status)
        payment_response_dict = await _do_payment()
        payment_status = _normalize_status(str(payment_response_dict.get("status")))
    else:
        workflow.append("policy_issuance")
        policy = await _do_policy_issue(payment_status)

    result: Dict[str, Any] = {
        "message": "Workflow completed.",
        "workflow": workflow,
        "underwriting": underwriting.model_dump(),
        "quotation": {
            **quotation.model_dump(),
            "payable_amount": quotation.amount,
            "payable_currency": quotation.currency,
        },
        "policy": policy,
    }

    if payment_response_dict:
        result["payment_prompt"] = {
            "phone_number": phone_number,
            "amount": quotation.amount,
            "currency": quotation.currency,
            "payee_name": payee_name,
            "reference": quotation.quote_id,
        }
        result["payment"] = payment_response_dict
    else:
        result["next_action"] = "collect_payment_details_and_initiate_payment"

    return result


async def _initiate_payment(client, payment_request: PaymentRequest) -> PaymentResponse:
    payment = client.initiate_payment(payment_request)
    if hasattr(payment, "__await__"):
        return await payment
    return payment


def _payment_response_to_dict(payment_response: PaymentResponse) -> Dict[str, Any]:
    return {
        "reference": payment_response.reference,
        "status": str(getattr(payment_response.status, "value", payment_response.status)),
        "message": payment_response.message,
        "provider_reference": payment_response.provider_reference,
        "amount": payment_response.amount,
        "currency": payment_response.currency,
        "metadata": payment_response.metadata,
    }

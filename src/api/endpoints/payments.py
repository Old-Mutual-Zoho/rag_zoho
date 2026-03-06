import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.integrations.clients.mocks.underwriting import mock_underwriting_client
from src.integrations.contracts.payments import PaymentRequest, PaymentResponse
from src.chatbot.flows.payment import PaymentFlow
from src.integrations.payments.payment_service import PaymentService
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
payment_service = PaymentService()


class PaymentInitiateRequest(BaseModel):
    quote_id: str
    provider: str
    phone_number: str
    amount: float
    currency: str = "UGX"
    payee_name: str = "Old Mutual"
    metadata: Dict[str, Any] = Field(default_factory=dict)


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


class TriggerCallbackRequest(BaseModel):
    outcome: Optional[str] = Field(default=None, description="Optional: success or failed")


class BuyNowPaymentRequest(BaseModel):
    provider: str = Field(..., description="Payment provider: mtn, airtel, or flexipay")
    phone_number: str = Field(..., description="Customer phone number to receive payment prompt")
    quote_number: Optional[str] = Field(default=None, description="Quote identifier to validate and pay")
    policy_number: Optional[str] = Field(default=None, description="Policy identifier to validate and pay")
    amount: Optional[float] = Field(default=None, description="Optional override amount. Required when policy amount cannot be resolved.")
    currency: str = Field(default="UGX", description="Payment currency")
    payee_name: str = Field(default="Old Mutual", description="Entity displayed in payment description/prompt")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BuyNowFlowStartRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    initial_data: Dict[str, Any] = Field(default_factory=dict, description="Optional initial payload e.g. quote_id")


class BuyNowFlowStepRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    current_step: int = Field(..., ge=0, description="Current flow step index")
    user_input: Dict[str, Any] = Field(default_factory=dict, description="Client submission payload for current step")
    collected_data: Dict[str, Any] = Field(default_factory=dict, description="Flow state returned from previous step")


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


def _normalize_status(value: Optional[str]) -> str:
    raw = (value or "").strip().upper()
    return raw or "UNKNOWN"


def _extract_policy_amount(raw_policy: Dict[str, Any]) -> Optional[float]:
    candidates = [
        raw_policy.get("premium_amount"),
        raw_policy.get("premium"),
        raw_policy.get("amount"),
        raw_policy.get("payable_amount"),
    ]
    for value in candidates:
        if value is None:
            continue
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            return amount
    return None


@api.post("/initiate", tags=["Payments"])
async def initiate_payment(request: PaymentInitiateRequest):
    metadata = {
        "payee_name": request.payee_name,
        **(request.metadata or {}),
    }

    payment_request = PaymentRequest(
        reference=request.quote_id,
        phone_number=request.phone_number,
        amount=request.amount,
        currency=request.currency,
        description=f"Payment to {request.payee_name} for quote {request.quote_id}",
        metadata=metadata,
    )

    try:
        payment_response = await payment_service.initiate_payment(provider=request.provider, request=payment_request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _payment_response_to_dict(payment_response)


@api.post("/buy-now", tags=["Payments"])
async def buy_now(request: BuyNowPaymentRequest):
    quote_number = (request.quote_number or "").strip()
    policy_number = (request.policy_number or "").strip()

    if bool(quote_number) == bool(policy_number):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of quote_number or policy_number.",
        )

    reference_type = "quote" if quote_number else "policy"
    input_reference = quote_number or policy_number
    resolved_reference = input_reference
    resolved_currency = request.currency
    resolved_amount: Optional[float] = request.amount
    validation_payload: Dict[str, Any] = {
        "reference_type": reference_type,
        "input_reference": input_reference,
        "validated": False,
    }

    if reference_type == "quote":
        quote = payment_service.db.get_quote(input_reference)
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")

        resolved_reference = str(quote.id)
        resolved_amount = request.amount if request.amount is not None else float(getattr(quote, "premium_amount", 0.0))
        resolved_currency = request.currency or "UGX"
        validation_payload.update(
            {
                "validated": True,
                "quote_id": str(quote.id),
                "product_id": getattr(quote, "product_id", None),
                "quote_status": getattr(quote, "status", None),
            }
        )
    else:
        try:
            raw_policy = await PolicyService().get_policy(input_reference)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Policy not found") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch policy: {str(exc)}") from exc

        try:
            policy = normalize_policy_response(raw_policy)
        except IntegrationResponseError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": str(exc),
                    "stage": "partner_response_validation",
                    "payload": exc.payload,
                },
            ) from exc

        if policy.status in {"DECLINED", "REJECTED", "CANCELLED"}:
            raise HTTPException(status_code=422, detail=f"Policy status '{policy.status}' is not payable")

        resolved_reference = policy.quote_id or policy.policy_id
        resolved_currency = policy.currency or request.currency
        policy_amount = _extract_policy_amount(policy.raw)
        resolved_amount = request.amount if request.amount is not None else policy_amount

        validation_payload.update(
            {
                "validated": True,
                "policy_id": policy.policy_id,
                "quote_id": policy.quote_id,
                "policy_status": policy.status,
            }
        )

    if resolved_amount is None or resolved_amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="Could not determine payable amount. Provide a positive amount.",
        )

    metadata = {
        "payee_name": request.payee_name,
        "source": "buy_now",
        "reference_type": reference_type,
        "input_reference": input_reference,
        **(request.metadata or {}),
    }

    payment_request = PaymentRequest(
        reference=resolved_reference,
        phone_number=request.phone_number,
        amount=float(resolved_amount),
        currency=resolved_currency,
        description=f"Payment to {request.payee_name} for {reference_type} {input_reference}",
        metadata=metadata,
    )

    try:
        payment_response = await payment_service.initiate_payment(provider=request.provider, request=payment_request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "validation": validation_payload,
        "payment": _payment_response_to_dict(payment_response),
    }


@api.post("/buy-now/flow/start", tags=["Payments"])
async def buy_now_flow_start(request: BuyNowFlowStartRequest):
    """
    Start backend-driven Buy Now flow for non-chat clients.
    Returns a renderable step payload (e.g. input for policy_or_quote_id).
    """
    flow = PaymentFlow(payment_service.db)
    result = await flow.start(request.user_id, request.initial_data or {})

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return {
        "flow": "payment",
        "step": int(result.get("next_step", 0) if result.get("next_step") is not None else 0),
        "response": result.get("response"),
        "complete": bool(result.get("complete", False)),
        "collected_data": result.get("collected_data", request.initial_data or {}),
        "data": result.get("data"),
    }


@api.post("/buy-now/flow/step", tags=["Payments"])
async def buy_now_flow_step(request: BuyNowFlowStepRequest):
    """
    Continue backend-driven Buy Now flow for non-chat clients.
    Client submits current_step + user_input + previous collected_data.
    """
    flow = PaymentFlow(payment_service.db)
    result = await flow.process_step(
        user_input=request.user_input or {},
        current_step=request.current_step,
        collected_data=request.collected_data or {},
        user_id=request.user_id,
    )

    return {
        "flow": "payment",
        "step": result.get("next_step", request.current_step),
        "response": result.get("response"),
        "complete": bool(result.get("complete", False)),
        "collected_data": result.get("collected_data", request.collected_data),
        "data": result.get("data"),
    }


@api.get("/status/{quote_id}", tags=["Payments"])
async def get_payment_status(quote_id: str):
    try:
        return _payment_response_to_dict(payment_service.get_payment_status(quote_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Payment transaction not found") from exc


@api.get("/transactions/{quote_id}", tags=["Payments"])
async def get_payment_transaction(quote_id: str):
    try:
        return payment_service.get_payment_transaction(quote_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Payment transaction not found") from exc


@api.post("/webhook/callback", tags=["Payments"])
async def payment_webhook_callback(payload: Dict[str, Any], x_signature: str = Header(..., alias="X-Signature")):
    try:
        return payment_service.apply_webhook_callback(payload, x_signature)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook signature") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Payment transaction not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.post("/mock/trigger-callback/{quote_id}", tags=["Payments"])
async def trigger_mock_callback(quote_id: str, request: TriggerCallbackRequest):
    try:
        return payment_service.trigger_mock_callback(quote_id, outcome=request.outcome)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Payment transaction not found") from exc


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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
        payment_response = await payment_service.initiate_payment(provider=str(provider), request=req)
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

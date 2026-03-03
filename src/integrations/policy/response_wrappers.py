from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from src.integrations.contracts.interfaces import PaymentStatus


class IntegrationResponseError(ValueError):
    def __init__(self, message: str, *, payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


class UnderwritingResponseModel(BaseModel):
    quote_id: str
    premium: float
    currency: str = "UGX"
    decision_status: str
    requirements: List[Dict[str, Any]] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


class QuotationResponseModel(BaseModel):
    quote_id: str
    amount: float
    currency: str = "UGX"
    status: str = "QUOTED"
    raw: Dict[str, Any] = Field(default_factory=dict)


class PolicyResponseModel(BaseModel):
    policy_id: str
    quote_id: str
    status: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    currency: str = "UGX"
    raw: Dict[str, Any] = Field(default_factory=dict)


class PaymentGatewayResponseModel(BaseModel):
    reference: str
    provider_reference: str
    status: PaymentStatus
    amount: float
    currency: str
    message: str
    raw: Dict[str, Any] = Field(default_factory=dict)


def normalize_underwriting_response(raw: Dict[str, Any]) -> UnderwritingResponseModel:
    quote_id = _first_non_empty(raw, "quote_id", "quoteId", "id")
    premium = _coerce_positive_amount(_first_non_empty(raw, "premium", "amount", "premium_amount"), "underwriting premium")
    currency = str(_first_non_empty(raw, "currency", default="UGX")).upper()
    decision_status = str(_first_non_empty(raw, "decision_status", "decisionStatus", "status", "decision")).upper()
    requirements = raw.get("requirements") if isinstance(raw.get("requirements"), list) else []

    if decision_status not in {"APPROVED", "REFERRED", "PENDING", "DECLINED", "REJECTED", "QUOTED"}:
        raise IntegrationResponseError(
            f"Unsupported underwriting decision_status '{decision_status}'.",
            payload=raw,
        )

    return _build_model(
        UnderwritingResponseModel,
        {
            "quote_id": str(quote_id),
            "premium": premium,
            "currency": currency,
            "decision_status": decision_status,
            "requirements": requirements,
            "raw": raw,
        },
        raw,
    )


def normalize_quotation_response(
    raw: Dict[str, Any],
    *,
    fallback_quote_id: Optional[str] = None,
    fallback_currency: str = "UGX",
) -> QuotationResponseModel:
    quote_id = _first_non_empty(raw, "quote_id", "quoteId", "id", default=fallback_quote_id)
    amount = _coerce_positive_amount(
        _first_non_empty(raw, "premium", "amount", "payable_amount", "monthly_premium", "total_premium"),
        "quotation payable amount",
    )
    currency = str(_first_non_empty(raw, "currency", default=fallback_currency)).upper()
    status = str(_first_non_empty(raw, "status", "decision_status", "quote_status", default="QUOTED")).upper()

    return _build_model(
        QuotationResponseModel,
        {
            "quote_id": str(quote_id),
            "amount": amount,
            "currency": currency,
            "status": status,
            "raw": raw,
        },
        raw,
    )


def normalize_policy_response(
    raw: Dict[str, Any],
    *,
    fallback_quote_id: Optional[str] = None,
    fallback_currency: str = "UGX",
) -> PolicyResponseModel:
    policy_id = _first_non_empty(raw, "policy_id", "policyId", "id")
    quote_id = _first_non_empty(raw, "quote_id", "quoteId", default=fallback_quote_id)
    status = str(_first_non_empty(raw, "status", "policy_status", default="PENDING")).upper()
    currency = str(_first_non_empty(raw, "currency", default=fallback_currency)).upper()
    start_date = raw.get("start_date") or raw.get("policy_start_date")
    end_date = raw.get("end_date") or raw.get("policy_end_date")

    if status not in {"PENDING", "PENDING_PAYMENT", "ISSUED", "ACTIVE", "DECLINED", "REJECTED", "CANCELLED"}:
        raise IntegrationResponseError(f"Unsupported policy status '{status}'.", payload=raw)

    return _build_model(
        PolicyResponseModel,
        {
            "policy_id": str(policy_id),
            "quote_id": str(quote_id),
            "status": status,
            "start_date": str(start_date) if start_date else None,
            "end_date": str(end_date) if end_date else None,
            "currency": currency,
            "raw": raw,
        },
        raw,
    )


def normalize_payment_gateway_response(
    raw: Dict[str, Any],
    *,
    fallback_reference: str,
    fallback_amount: float,
    fallback_currency: str,
) -> PaymentGatewayResponseModel:
    reference = str(_first_non_empty(raw, "reference", "our_reference", "transaction_reference", default=fallback_reference))
    provider_reference = str(_first_non_empty(raw, "provider_reference", "providerRef", "transaction_id", default=""))
    status = _map_payment_status(_first_non_empty(raw, "status", "payment_status", default="PENDING"))
    amount = _coerce_positive_amount(_first_non_empty(raw, "amount", default=fallback_amount), "payment amount")
    currency = str(_first_non_empty(raw, "currency", default=fallback_currency)).upper()
    message = str(_first_non_empty(raw, "message", "detail", default="Payment request accepted by gateway"))

    return _build_model(
        PaymentGatewayResponseModel,
        {
            "reference": reference,
            "provider_reference": provider_reference,
            "status": status,
            "amount": amount,
            "currency": currency,
            "message": message,
            "raw": raw,
        },
        raw,
    )


def _first_non_empty(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    if default is not None:
        return default
    raise IntegrationResponseError(f"Missing required field. Checked keys: {', '.join(keys)}", payload=data)


def _coerce_positive_amount(value: Any, label: str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise IntegrationResponseError(f"Invalid {label}: {value!r}") from exc
    if amount <= 0:
        raise IntegrationResponseError(f"{label.capitalize()} must be > 0; got {amount}.")
    return amount


def _map_payment_status(raw_status: Any) -> PaymentStatus:
    value = str(raw_status or "").strip().upper()
    mapping = {
        "PENDING": PaymentStatus.PENDING,
        "PROCESSING": PaymentStatus.PENDING,
        "SUCCESS": PaymentStatus.SUCCESS,
        "COMPLETED": PaymentStatus.SUCCESS,
        "FAILED": PaymentStatus.FAILED,
        "ERROR": PaymentStatus.FAILED,
        "REVERSED": PaymentStatus.REVERSED,
        "CANCELLED": PaymentStatus.CANCELLED,
    }
    if value not in mapping:
        raise IntegrationResponseError(f"Unsupported payment status '{value}'.")
    return mapping[value]


def _build_model(model_type, payload: Dict[str, Any], raw: Dict[str, Any]):
    try:
        return model_type(**payload)
    except ValidationError as exc:
        raise IntegrationResponseError(f"Response validation failed: {exc}", payload=raw) from exc

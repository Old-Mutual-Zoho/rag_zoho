from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from src.integrations.clients.mocks.base_mobile_money import BaseMobileMoneyMock
from src.integrations.clients.real_http.payments import RealPaymentsClient
from src.integrations.contracts.interfaces import PaymentRequest, PaymentResponse, PaymentStatus

_DEFAULT_WEBHOOK_SECRET = "mock-payment-webhook-secret"
_DEFAULT_DB: Optional[Any] = None


@dataclass
class MockWebhookEnvelope:
    payload: Dict[str, Any]
    signature: str


class DeterministicMockPaymentProvider(BaseMobileMoneyMock):
    def __init__(self, provider: str, webhook_secret: Optional[str] = None) -> None:
        super().__init__(provider=provider, webhook_secret=webhook_secret)


def _normalize_mode() -> str:
    mode = os.getenv("INTEGRATIONS_MODE", "mock").strip().lower()
    return mode if mode in {"mock", "real"} else "mock"


def _select_default_db() -> Any:
    global _DEFAULT_DB
    if _DEFAULT_DB is not None:
        return _DEFAULT_DB

    use_real = bool(os.getenv("DATABASE_URL")) and os.getenv("USE_POSTGRES_CONVERSATIONS", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if use_real:
        from src.database.postgres_real import PostgresDB

        _DEFAULT_DB = PostgresDB(connection_string=os.environ["DATABASE_URL"])
    else:
        from src.database.postgres import PostgresDB

        _DEFAULT_DB = PostgresDB()

    _DEFAULT_DB.create_tables()
    return _DEFAULT_DB


class PaymentService:
    def __init__(self, db: Optional[Any] = None) -> None:
        self.db = db or _select_default_db()

    def _resolve_client(self, provider: str) -> Tuple[Any, str]:
        provider_key = (provider or "").strip().lower()
        if provider_key not in {"mtn", "airtel", "flexipay"}:
            raise ValueError("Invalid provider. Expected 'mtn', 'airtel', or 'flexipay'.")

        if _normalize_mode() == "real" and os.getenv("PARTNER_PAYMENT_API_URL"):
            return RealPaymentsClient(provider=provider_key), provider_key
        return DeterministicMockPaymentProvider(provider_key), provider_key

    @staticmethod
    def _normalize_outcome(metadata: Optional[Dict[str, Any]]) -> str:
        requested = str((metadata or {}).get("simulate_outcome", "success")).strip().lower()
        return "failed" if requested == "failed" else "success"

    @staticmethod
    def _signature_for_payload(payload: Dict[str, Any]) -> str:
        provider = DeterministicMockPaymentProvider(
            provider=str(payload.get("provider") or "mtn"),
            webhook_secret=os.getenv("MOCK_PAYMENT_WEBHOOK_SECRET", _DEFAULT_WEBHOOK_SECRET),
        )
        return provider.sign_payload(payload)

    @staticmethod
    def _verify_signature(payload: Dict[str, Any], signature: str) -> bool:
        expected = PaymentService._signature_for_payload(payload)
        return bool(signature) and hmac.compare_digest((signature or "").strip(), expected)

    @staticmethod
    def _extract_transaction_metadata(transaction: Any) -> Dict[str, Any]:
        raw = getattr(transaction, "transaction_metadata", None)
        if raw is None:
            maybe_raw = getattr(transaction, "metadata", None)
            if isinstance(maybe_raw, dict):
                raw = maybe_raw
        return dict(raw or {})

    async def initiate_payment(self, *, provider: str, request: PaymentRequest) -> PaymentResponse:
        client, provider_key = self._resolve_client(provider)
        metadata = dict(request.metadata or {})
        metadata["simulate_outcome"] = self._normalize_outcome(metadata)

        enriched_request = PaymentRequest(
            reference=request.reference,
            phone_number=request.phone_number,
            amount=request.amount,
            currency=request.currency,
            description=request.description,
            customer_id=request.customer_id,
            metadata=metadata,
        )

        payment = client.initiate_payment(enriched_request)
        if hasattr(payment, "__await__"):
            payment = await payment

        transaction = self.db.create_payment_transaction(
            reference=payment.reference,
            provider=provider_key,
            provider_reference=payment.provider_reference,
            phone_number=request.phone_number,
            amount=request.amount,
            currency=request.currency,
            status=PaymentStatus.PENDING.value,
            metadata=metadata,
        )
        self.db.add_payment_audit_event(
            payment_reference=transaction.reference,
            event_type="PAYMENT_INITIATED",
            status_from=None,
            status_to=PaymentStatus.PENDING.value,
            payload={
                "provider": provider_key,
                "provider_reference": payment.provider_reference,
                "description": request.description,
            },
        )

        payment.status = PaymentStatus.PENDING
        payment.metadata = metadata
        return payment

    def get_payment_status(self, reference: str) -> PaymentResponse:
        transaction = self.db.get_payment_transaction_by_reference(reference)
        if not transaction:
            raise KeyError(reference)

        return PaymentResponse(
            reference=str(transaction.reference),
            provider_reference=str(transaction.provider_reference),
            status=PaymentStatus(str(transaction.status).upper()),
            amount=float(transaction.amount),
            currency=str(transaction.currency),
            message="Payment status fetched successfully.",
            metadata=self._extract_transaction_metadata(transaction),
        )

    def get_payment_transaction(self, reference: str) -> Dict[str, Any]:
        transaction = self.db.get_payment_transaction_by_reference(reference)
        if not transaction:
            raise KeyError(reference)

        metadata = self._extract_transaction_metadata(transaction)
        events = self.db.list_payment_audit_events(reference)
        return {
            "reference": str(transaction.reference),
            "provider": str(transaction.provider),
            "provider_reference": str(transaction.provider_reference),
            "phone_number": str(transaction.phone_number),
            "amount": float(transaction.amount),
            "currency": str(transaction.currency),
            "status": str(transaction.status),
            "metadata": metadata,
            "created_at": transaction.created_at.isoformat() if getattr(transaction, "created_at", None) else None,
            "updated_at": transaction.updated_at.isoformat() if getattr(transaction, "updated_at", None) else None,
            "audit_events": [
                {
                    "id": str(event.id),
                    "payment_reference": str(event.payment_reference),
                    "event_type": str(event.event_type),
                    "status_from": event.status_from,
                    "status_to": event.status_to,
                    "payload": dict(event.payload or {}),
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                }
                for event in events
            ],
        }

    def build_mock_webhook_callback(self, reference: str, outcome: Optional[str] = None) -> MockWebhookEnvelope:
        transaction = self.db.get_payment_transaction_by_reference(reference)
        if not transaction:
            raise KeyError(reference)

        metadata = self._extract_transaction_metadata(transaction)
        if outcome is not None:
            metadata["simulate_outcome"] = "failed" if str(outcome).strip().lower() == "failed" else "success"
            transaction.metadata = metadata

        provider = DeterministicMockPaymentProvider(
            provider=str(transaction.provider),
            webhook_secret=os.getenv("MOCK_PAYMENT_WEBHOOK_SECRET", _DEFAULT_WEBHOOK_SECRET),
        )
        payload = provider.build_webhook_payload(transaction)
        signature = provider.sign_payload(payload)
        return MockWebhookEnvelope(payload=payload, signature=signature)

    def apply_webhook_callback(self, payload: Dict[str, Any], signature: str) -> Dict[str, Any]:
        if not self._verify_signature(payload, signature):
            raise PermissionError("Invalid webhook signature")

        reference = str(payload.get("reference") or payload.get("our_reference") or "").strip()
        if not reference:
            raise ValueError("Missing payment reference")

        transaction = self.db.get_payment_transaction_by_reference(reference)
        if not transaction:
            raise KeyError(reference)

        target_status = str(payload.get("status", "")).strip().upper()
        if target_status not in {PaymentStatus.SUCCESS.value, PaymentStatus.FAILED.value}:
            raise ValueError("Unsupported webhook status")

        current_status = str(transaction.status).strip().upper()
        self.db.add_payment_audit_event(
            payment_reference=reference,
            event_type="WEBHOOK_RECEIVED",
            status_from=current_status,
            status_to=target_status,
            payload=payload,
        )

        if current_status == PaymentStatus.PENDING.value and target_status in {
            PaymentStatus.SUCCESS.value,
            PaymentStatus.FAILED.value,
        }:
            updated = self.db.update_payment_transaction_status(
                reference,
                target_status,
                provider_reference=str(payload.get("provider_reference") or transaction.provider_reference),
                metadata={"webhook_payload": payload},
            )
            self.db.add_payment_audit_event(
                payment_reference=reference,
                event_type="PAYMENT_STATUS_UPDATED",
                status_from=current_status,
                status_to=target_status,
                payload={"provider_reference": payload.get("provider_reference")},
            )
            return {
                "updated": True,
                "reference": reference,
                "status": str(updated.status if updated else target_status),
            }

        self.db.add_payment_audit_event(
            payment_reference=reference,
            event_type="WEBHOOK_IGNORED",
            status_from=current_status,
            status_to=current_status,
            payload={"reason": "Invalid transition", "requested_status": target_status},
        )
        return {
            "updated": False,
            "reference": reference,
            "status": current_status,
        }

    def trigger_mock_callback(self, reference: str, outcome: Optional[str] = None) -> Dict[str, Any]:
        envelope = self.build_mock_webhook_callback(reference, outcome=outcome)
        callback_result = self.apply_webhook_callback(envelope.payload, envelope.signature)
        return {
            "reference": reference,
            "payload": envelope.payload,
            "signature": envelope.signature,
            "result": callback_result,
        }

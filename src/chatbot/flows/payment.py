"""
Payment flow - Handle premium payments
"""

import json
from typing import Any, Dict
from datetime import datetime
import uuid

from src.integrations.contracts.interfaces import PaymentRequest
from src.integrations.payments.payment_service import PaymentService


class PaymentFlow:
    # Payment threshold for auto-processing vs agent assistance
    AUTO_PAYMENT_THRESHOLD = 500000  # UGX

    def __init__(self, db):
        self.db = db
        self.payment_service = PaymentService(db=db)

    async def start(self, user_id: str, initial_data: Dict) -> Dict:
        """Start payment flow"""
        quote_id = initial_data.get("quote_id")

        if not quote_id:
            return {"error": "No quote ID provided"}

        # Get quote details
        quote = self.db.get_quote(quote_id)

        if not quote:
            return {"error": "Quote not found"}

        return await self.process_step("", 0, {"quote": quote, "user_id": user_id}, user_id)

    async def process_step(self, user_input: str, current_step: int, collected_data: Dict, user_id: str) -> Dict:
        """Process payment flow"""

        quote = collected_data.get("quote")
        if not quote and collected_data.get("quote_id"):
            quote = self.db.get_quote(collected_data["quote_id"])
        if quote:
            collected_data["quote"] = quote
        premium_amount = float(quote.premium_amount) if quote else 0

        if current_step == 0:  # Payment method selection
            if not quote:
                return {
                    "response": {"type": "error", "message": "Quote not found. Please start again from the product flow."},
                    "complete": True,
                }
            # Check if amount requires agent assistance
            requires_agent = premium_amount >= self.AUTO_PAYMENT_THRESHOLD

            if requires_agent:
                return {
                    "response": {
                        "type": "agent_required",
                        "message": (
                            f"💼 For premiums above UGX {self.AUTO_PAYMENT_THRESHOLD:,}, "
                            "we'll connect you with an agent to guide you through the payment process."
                        ),
                        "agent_info": {"name": "Old Mutual Support", "phone": "+256 753 888232", "email": "support@oldmutual.co.ug"},
                        "actions": [{"type": "call_agent", "label": "📞 Call Agent"}, {"type": "schedule_callback", "label": "📅 Schedule Callback"}],
                    },
                    "complete": True,
                    "data": {"requires_agent": True},
                }

            return {
                "response": {
                    "type": "payment_method",
                    "message": "💳 Choose your payment method",
                    "amount": premium_amount,
                    "options": [
                        {"id": "mobile_money", "label": "📱 Mobile Money", "providers": ["MTN", "Airtel"], "icon": "📲"},
                        {"id": "bank_transfer", "label": "🏦 Bank Transfer", "icon": "🏛️"},
                        {"id": "card", "label": "💳 Credit/Debit Card", "icon": "💳"},
                    ],
                },
                "next_step": 1,
                "collected_data": collected_data,
            }

        elif current_step == 1:  # Payment details
            payment_method = self._extract_payment_method(user_input)
            collected_data["payment_method"] = payment_method

            if payment_method == "mobile_money":
                default_phone = self._extract_default_phone(collected_data)
                return {
                    "response": {
                        "type": "form",
                        "message": "📱 Enter your mobile money details",
                        "fields": [
                            {
                                "name": "provider",
                                "label": "Provider",
                                "type": "select",
                                "options": ["mtn", "airtel", "flexipay"],
                                "required": True,
                                "defaultValue": "mtn",
                            },
                            {
                                "name": "phone_number",
                                "label": "Phone Number",
                                "type": "tel",
                                "placeholder": "07XX XXX XXX",
                                "required": True,
                                "defaultValue": default_phone,
                            },
                        ],
                    },
                    "next_step": 2,
                    "collected_data": collected_data,
                }

            elif payment_method == "bank_transfer":
                return {
                    "response": {
                        "type": "bank_details",
                        "message": "🏦 Bank Transfer Details",
                        "bank_info": {
                            "bank_name": "Stanbic Bank Uganda",
                            "account_name": "Old Mutual Uganda Limited",
                            "account_number": "9030008765432",
                            "swift_code": "SBICUGKX",
                            "branch": "Kampala Main Branch",
                        },
                        "instructions": [
                            "Transfer the exact amount shown",
                            "Use your policy/quote number as reference",
                            "Send proof of payment to payments@oldmutual.co.ug",
                        ],
                        "reference": f"QUOTE-{quote.id}",
                    },
                    "next_step": 3,
                    "collected_data": collected_data,
                }

            elif payment_method == "card":
                return {
                    "response": {
                        "type": "card_payment",
                        "message": "💳 You will be redirected to our secure payment gateway",
                        "payment_url": f"https://payments.oldmutual.co.ug/pay/{quote.id}",
                        "amount": premium_amount,
                        "currency": "UGX",
                    },
                    "next_step": 2,
                    "collected_data": collected_data,
                }

        elif current_step == 2:  # Process payment
            payment_details = self._normalize_payment_details(user_input, collected_data)
            collected_data["payment_details"] = payment_details

            # For mobile money, initiate payment
            if collected_data["payment_method"] == "mobile_money":
                provider = str(payment_details.get("provider") or "mtn").strip().lower()
                phone_number = str(payment_details.get("phone_number") or "").strip()

                if not phone_number:
                    return {
                        "response": {
                            "type": "validation_error",
                            "message": "Phone number is required to initiate mobile money payment.",
                            "errors": {"phone_number": "Phone number is required"},
                        },
                        "next_step": 2,
                        "collected_data": collected_data,
                    }

                request = PaymentRequest(
                    reference=str(quote.id),
                    phone_number=phone_number,
                    amount=float(premium_amount),
                    currency="UGX",
                    description=f"Payment for quote {quote.id}",
                    metadata={
                        "product_id": getattr(quote, "product_id", "personal_accident"),
                        "user_id": user_id,
                        "payment_method": "mobile_money",
                    },
                )

                try:
                    payment_result = await self.payment_service.initiate_payment(provider=provider, request=request)
                except ValueError as exc:
                    return {
                        "response": {
                            "type": "validation_error",
                            "message": str(exc),
                            "errors": {"provider": str(exc)},
                        },
                        "next_step": 2,
                        "collected_data": collected_data,
                    }

                collected_data["payment_reference"] = payment_result.reference
                collected_data["payment_id"] = payment_result.reference

                return {
                    "response": {
                        "type": "payment_initiated",
                        "message": "✅ Payment request sent to your phone",
                        "instructions": "Please enter your PIN to complete the payment",
                        "transaction_ref": payment_result.provider_reference,
                        "reference": payment_result.reference,
                        "status": str(getattr(payment_result.status, "value", payment_result.status)),
                    },
                    "next_step": 3,
                    "collected_data": collected_data,
                    "data": {
                        "payment_reference": payment_result.reference,
                        "provider_reference": payment_result.provider_reference,
                    },
                }

            return {
                "response": {
                    "type": "payment_pending",
                    "message": "Payment processing...",
                    "payment_method": collected_data.get("payment_method"),
                },
                "next_step": 3,
                "collected_data": collected_data,
            }

        elif current_step == 3:  # Payment confirmation
            # Check payment status
            payment_reference = collected_data.get("payment_reference") or collected_data.get("payment_id") or str(quote.id)
            payment_status = await self._check_payment_status(payment_reference)

            if payment_status == "completed":
                # Create application
                application = None
                create_application = getattr(self.db, "create_application", None)
                if callable(create_application):
                    application = create_application(
                        user_id=user_id,
                        quote_id=quote.id,
                        product_id=quote.product_id,
                        application_data=collected_data,
                        status="submitted",
                    )

                return {
                    "response": {
                        "type": "payment_success",
                        "message": "🎉 Payment successful! Your policy is being processed.",
                        "policy_number": self._generate_policy_number(),
                        "next_steps": [
                            "You will receive your policy document via email within 24 hours",
                            "Your coverage starts immediately",
                            "Welcome to the Old Mutual family! 🏦",
                        ],
                        "support": {"email": "support@oldmutual.co.ug", "phone": "+256 753 888232"},
                    },
                    "complete": True,
                    "data": {
                        "application_id": str(application.id) if application and getattr(application, "id", None) else None,
                        "payment_status": "completed",
                        "payment_reference": payment_reference,
                    },
                }
            elif payment_status == "failed":
                return {
                    "response": {
                        "type": "payment_failed",
                        "message": "❌ Payment failed. Please try again.",
                        "actions": [
                            {"type": "retry", "label": "Try Again"},
                            {"type": "change_method", "label": "Use Different Payment Method"},
                            {"type": "contact_support", "label": "Contact Support"},
                        ],
                    },
                    "next_step": 0,  # Back to payment method selection
                    "collected_data": collected_data,
                }
            else:
                return {
                    "response": {"type": "payment_pending", "message": "⏳ Payment is being processed. Please wait...", "status": payment_status},
                    "next_step": 3,  # Stay on this step
                    "collected_data": collected_data,
                }

    def _create_payment_record(self, quote, payment_method, payment_details, user_id):
        """Create payment record in database"""
        # This would create actual payment record
        # For now, return mock
        from types import SimpleNamespace

        return SimpleNamespace(id=uuid.uuid4(), amount=quote.premium_amount, status="pending")

    async def _initiate_mobile_money_payment(self, payment_details, amount):
        """Initiate mobile money payment"""
        # This would integrate with MTN/Airtel API
        # For now, return mock
        return {"transaction_ref": f"MM{uuid.uuid4().hex[:10].upper()}", "status": "pending"}

    async def _check_payment_status(self, payment_reference: str):
        """Check payment status from payment service."""
        if not payment_reference:
            return "pending"

        try:
            status = self.payment_service.get_payment_status(payment_reference).status.value
        except KeyError:
            return "pending"

        normalized = str(status).strip().upper()
        if normalized == "SUCCESS":
            return "completed"
        if normalized == "FAILED":
            return "failed"
        return "pending"

    def _extract_payment_method(self, user_input: Any) -> str:
        if isinstance(user_input, dict):
            method = user_input.get("payment_method") or user_input.get("method") or user_input.get("id")
        else:
            method = user_input

        value = str(method or "").strip().lower()
        aliases = {
            "mobile_money": "mobile_money",
            "mobile money": "mobile_money",
            "mobile": "mobile_money",
            "bank_transfer": "bank_transfer",
            "bank transfer": "bank_transfer",
            "bank": "bank_transfer",
            "card": "card",
        }
        return aliases.get(value, value)

    def _normalize_payment_details(self, user_input: Any, collected_data: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(user_input, dict):
            payload = dict(user_input)
        else:
            payload = {}
            text = str(user_input or "").strip()
            if text.startswith("{"):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"raw": text}
            elif text:
                payload = {"raw": text}

        if "provider" in payload:
            payload["provider"] = str(payload["provider"]).strip().lower().replace(" ", "_")

        phone = payload.get("phone_number") or payload.get("phone") or payload.get("msisdn")
        if not phone:
            fallback_phone = self._extract_default_phone(collected_data)
            if fallback_phone:
                payload["phone_number"] = fallback_phone
        else:
            payload["phone_number"] = str(phone).strip()

        return payload

    def _extract_default_phone(self, collected_data: Dict[str, Any]) -> str:
        quote = collected_data.get("quote")
        underwriting_data = getattr(quote, "underwriting_data", {}) or {}
        quick_quote = underwriting_data.get("quick_quote", {}) if isinstance(underwriting_data, dict) else {}

        return (
            quick_quote.get("mobile")
            or quick_quote.get("mobile_number")
            or underwriting_data.get("mobile")
            or underwriting_data.get("phone_number")
            or ""
        )

    def _generate_policy_number(self):
        """Generate policy number"""
        import random

        return f"POL{datetime.now().year}{random.randint(100000, 999999)}"

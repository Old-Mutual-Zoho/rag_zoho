"""
Payment flow - Handle premium payments
"""

import json
from typing import Any, Dict, Optional
from datetime import datetime
import uuid
from types import SimpleNamespace

from src.integrations.contracts.interfaces import PaymentRequest
from src.integrations.payments.payment_service import PaymentService
from src.integrations.policy.policy_service import PolicyService
from src.integrations.policy.response_wrappers import IntegrationResponseError, normalize_policy_response
from src.chatbot.validation import normalize_phone_ug


class PaymentFlow:
    # Payment threshold for auto-processing vs agent assistance
    AUTO_PAYMENT_THRESHOLD = 500000  # UGX

    def __init__(self, db):
        self.db = db
        self.payment_service = PaymentService(db=db)

    async def start(self, user_id: str, initial_data: Dict) -> Dict:
        """Start payment flow"""
        collected_data = dict(initial_data or {})
        collected_data["user_id"] = user_id

        identifier = (
            collected_data.get("policy_or_quote_id")
            or collected_data.get("quote_id")
            or collected_data.get("policy_number")
        )

        if identifier:
            return await self.process_step({"policy_or_quote_id": str(identifier)}, 0, collected_data, user_id)

        return {
            "response": {
                "type": "input",
                "field": "policy_or_quote_id",
                "label": "Enter your Policy Number or Quote ID",
                "required": True,
            },
            "next_step": 0,
            "collected_data": collected_data,
        }

    async def process_step(self, user_input: str, current_step: int, collected_data: Dict, user_id: str) -> Dict:
        """Process payment flow"""

        quote = collected_data.get("quote")
        if not quote and collected_data.get("quote_id"):
            quote = self.db.get_quote(collected_data["quote_id"])
        if quote:
            collected_data["quote"] = quote
        premium_amount = float(quote.premium_amount) if quote else 0

        if current_step == 0:  # Identifier collection + validation
            if not quote:
                identifier = self._extract_policy_or_quote_id(user_input)
                if not identifier:
                    return {
                        "response": {
                            "type": "input",
                            "field": "policy_or_quote_id",
                            "label": "Enter your Policy Number or Quote ID",
                            "required": True,
                        },
                        "next_step": 0,
                        "collected_data": collected_data,
                    }

                quote, validation = await self._resolve_quote_for_payment(identifier)
                if not quote:
                    return {
                        "response": {
                            "type": "validation_error",
                            "message": validation.get("message") or "Could not validate that policy/quote. Please try again.",
                            "errors": {"policy_or_quote_id": validation.get("message") or "Invalid policy or quote identifier"},
                        },
                        "next_step": 0,
                        "collected_data": collected_data,
                    }

                collected_data["policy_or_quote_id"] = identifier
                collected_data["quote"] = quote
                collected_data["quote_id"] = str(getattr(quote, "id", identifier))
                collected_data["validation"] = validation
                premium_amount = float(getattr(quote, "premium_amount", 0) or 0)

            return self._build_payment_method_step(premium_amount, collected_data)

        elif current_step == 1:  # Payment details
            payment_method = self._extract_payment_method(user_input)
            if not payment_method:
                return {
                    "response": {
                        "type": "validation_error",
                        "message": "Please choose a payment method to continue.",
                        "errors": {"payment_method": "Payment method is required"},
                    },
                    "next_step": 1,
                    "collected_data": collected_data,
                }
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
                                "placeholder": "07XXXXXXXX or +2567XXXXXXXX",
                                "required": True,
                                "defaultValue": default_phone,
                                "hint": "Format: 07XXXXXXXX (10 digits) or +2567XXXXXXXX (with country code)",
                            },
                        ],
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
                errors_dict: Dict[str, str] = {}

                if not phone_number:
                    errors_dict["phone_number"] = "Phone number is required"
                else:
                    normalized_phone = normalize_phone_ug(phone_number)
                    if not normalized_phone or not (len(normalized_phone) == 12 and normalized_phone.startswith("2567")):
                        errors_dict["phone_number"] = "Phone number format is invalid. Use 07XXXXXXXX (10 digits) or +2567XXXXXXXX (with country code)"

                if errors_dict:
                    return {
                        "response": {
                            "type": "validation_error",
                            "message": "Please correct the errors below before continuing.",
                            "errors": errors_dict,
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
                    "next_step": 1,
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
        return SimpleNamespace(id=uuid.uuid4(), amount=quote.premium_amount, status="pending")

    def _build_payment_method_step(self, premium_amount: float, collected_data: Dict[str, Any]) -> Dict[str, Any]:
        requires_agent = premium_amount >= self.AUTO_PAYMENT_THRESHOLD

        if requires_agent:
            return {
                "response": {
                    "type": "agent_required",
                    "message": (
                        f"💼 For premiums above UGX {self.AUTO_PAYMENT_THRESHOLD:,}, "
                        "we'll connect you with an agent to guide you through the payment process."
                    ),
                    "agent_info": {
                        "name": "Old Mutual Support",
                        "phone": "+256 753 888232",
                        "email": "support@oldmutual.co.ug",
                    },
                    "actions": [
                        {"type": "call_agent", "label": "📞 Call Agent"},
                        {"type": "schedule_callback", "label": "📅 Schedule Callback"},
                    ],
                },
                "complete": True,
                "data": {"requires_agent": True},
            }

        return {
            "response": {
                "type": "payment_method",
                "message": "💳 Choose your provider",
                "amount": premium_amount,
                "options": [
                    {"id": "mobile_money", "label": "📱 Mobile Money", "providers": ["MTN", "Airtel", "Flexipay"], "icon": "📲"},
                ],
            },
            "next_step": 1,
            "collected_data": collected_data,
        }

    def _extract_policy_or_quote_id(self, user_input: Any) -> str:
        if isinstance(user_input, dict):
            value = (
                user_input.get("policy_or_quote_id")
                or user_input.get("quote_id")
                or user_input.get("quote_number")
                or user_input.get("policy_id")
                or user_input.get("policy_number")
                or user_input.get("reference")
                or user_input.get("id")
            )
        else:
            value = user_input

        return str(value or "").strip()

    async def _resolve_quote_for_payment(self, identifier: str):
        quote = self.db.get_quote(identifier)
        if quote:
            return quote, {
                "validated": True,
                "reference_type": "quote",
                "input_reference": identifier,
                "quote_id": str(getattr(quote, "id", identifier)),
                "product_id": getattr(quote, "product_id", None),
            }

        policy_service = PolicyService()
        try:
            raw_policy = await policy_service.get_policy(identifier)
            policy = normalize_policy_response(raw_policy)
        except KeyError:
            return None, {"validated": False, "message": "Policy/Quote number not found."}
        except IntegrationResponseError as exc:
            return None, {"validated": False, "message": str(exc)}
        except Exception:
            return None, {"validated": False, "message": "Unable to validate policy right now. Please try again."}

        if policy.status in {"DECLINED", "REJECTED", "CANCELLED"}:
            return None, {"validated": False, "message": f"Policy status '{policy.status}' is not payable."}

        quote_id = str(policy.quote_id or "").strip()
        if quote_id:
            linked_quote = self.db.get_quote(quote_id)
            if linked_quote:
                return linked_quote, {
                    "validated": True,
                    "reference_type": "policy",
                    "input_reference": identifier,
                    "policy_id": policy.policy_id,
                    "quote_id": quote_id,
                    "policy_status": policy.status,
                }

        policy_amount = self._extract_policy_amount(policy.raw)
        if not policy_amount or policy_amount <= 0:
            return None, {"validated": False, "message": "Policy premium amount could not be resolved for payment."}

        synthetic_quote = SimpleNamespace(
            id=quote_id or policy.policy_id,
            premium_amount=policy_amount,
            product_id=(policy.raw or {}).get("product_id", "policy_payment"),
            underwriting_data={},
            status=(policy.raw or {}).get("status", "approved"),
        )
        return synthetic_quote, {
            "validated": True,
            "reference_type": "policy",
            "input_reference": identifier,
            "policy_id": policy.policy_id,
            "quote_id": quote_id or policy.policy_id,
            "policy_status": policy.status,
        }

    def _extract_policy_amount(self, raw_policy: Optional[Dict[str, Any]]) -> Optional[float]:
        data = raw_policy or {}
        candidates = [
            data.get("premium_amount"),
            data.get("premium"),
            data.get("amount"),
            data.get("payable_amount"),
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
            "mtn": "mobile_money",
            "airtel": "mobile_money",
            "flexipay": "mobile_money",
        }
        return aliases.get(value, "mobile_money")

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

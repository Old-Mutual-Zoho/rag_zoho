"""
Serenicare flow - Collect cover personalization, optional benefits, medical conditions,
plan selection, user details, then proceed to payment.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict

from src.chatbot.validation import (
    raise_if_errors,
    validate_date_iso,
    validate_length_range,
    validate_motor_email_frontend,
    validate_uganda_mobile_frontend,
)
from src.integrations.policy.premium import premium_service

SERENICARE_OPTIONAL_BENEFITS = [
    {
        "id": "outpatient",
        "label": "Outpatient",
        "description": (
            "Clinic visits, diagnostics, and treatments without a hospital stay "
            "(Up to UGX 3,000,000.00 per person)"
        ),
    },
    {
        "id": "maternity",
        "label": "Maternity Cover",
        "description": (
            "Maternity benefits for checkups, scans, delivery, and immediate newborn care "
            "(Up to UGX 3,000,000.00 per family)"
        ),
    },
    {
        "id": "dental",
        "label": "Dental Cover",
        "description": (
            "Dental treatment for checkups, X-rays, fillings, and extractions "
            "(Up to UGX 300,000.00 per person)"
        ),
    },
    {
        "id": "optical",
        "label": "Optical Cover",
        "description": (
            "Vision care including eye tests, prescription glasses or contact lenses "
            "(Up to UGX 350,000.00 per person)"
        ),
    },
    {
        "id": "covid19",
        "label": "COVID-19 Cover",
        "description": "Care for COVID-19 from diagnosis to recovery",
    },
]

SERENICARE_PLANS = [
    {
        "id": "essential",
        "label": "Essential",
        "description": "Reliable coverage with fundamental limits, offering value and security.",
        "benefits": {
            "Inpatient limit per family": "UGX 15,000,000",
            "Outpatient limit per person": "UGX 1,500,000",
            "Maternity cover per family": "UGX 1,500,000",
            "Optical limit per person": "UGX 200,000",
            "Dental limit per person": "UGX 150,000",
        },
    },
    {
        "id": "classic",
        "label": "Classic",
        "description": "A balanced choice, delivering broader coverage with standout benefits.",
        "benefits": {
            "Inpatient limit per family": "UGX 30,000,000",
            "Outpatient limit per person": "UGX 2,000,000",
            "Maternity cover per family": "UGX 2,500,000",
            "Optical limit per person": "UGX 300,000",
            "Dental limit per person": "UGX 200,000",
        },
    },
    {
        "id": "comprehensive",
        "label": "Comprehensive",
        "description": "Expansive coverage with high limits for extensive health security.",
        "benefits": {
            "Inpatient limit per family": "UGX 60,000,000",
            "Outpatient limit per person": "UGX 3,000,000",
            "Maternity cover per family": "UGX 3,000,000",
            "Optical limit per person": "UGX 350,000",
            "Dental limit per person": "UGX 300,000",
        },
    },
    {
        "id": "premium",
        "label": "Premium",
        "description": "Ultimate health protection for those demanding the best healthcare.",
        "benefits": {
            "Inpatient limit per family": "UGX 100,000,000",
            "Outpatient limit per person": "UGX 5,000,000",
            "Maternity cover per family": "UGX 4,000,000",
            "Optical limit per person": "UGX 400,000",
            "Dental limit per person": "UGX 400,000",
        },
    },
]


class SerenicareFlow:
    """
    Guided flow for Serenicare: cover personalization, optional benefits,
    medical conditions, plan selection, user details, then payment.
    """

    STEPS = [
        "cover_personalization",
        "optional_benefits",
        "medical_conditions",
        "plan_selection",
        "about_you",
        "premium_and_download",
        "choose_plan_and_pay",
    ]

    def __init__(self, product_catalog, db):
        self.catalog = product_catalog
        self.db = db
        try:
            from src.chatbot.controllers.serenicare_controller import SerenicareController

            self.controller = SerenicareController(db)
        except Exception:
            self.controller = None

    def process_serenicare_form(self, app_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process and validate the full Serenicare form using the controller's validation logic.
        """
        if not self.controller:
            raise Exception("Serenicare controller not initialized")
        return self.controller.update_serenicare_form(app_id, payload)

    # -------------------------------------------------------------------------
    # PREMIUM CALCULATION (Added to fix failing tests)
    # -------------------------------------------------------------------------
    def _calculate_serenicare_premium(self, data: Dict, plan: Dict) -> Dict:
        """
        Calculate Serenicare premium based on plan tier and optional benefits.

        Returns:
            {
                "monthly": float,
                "annual": float,
                "breakdown": dict
            }
        """
        return premium_service.calculate_sync(
            "serenicare",
            {"data": data, "plan": plan},
        )

    async def complete_flow(self, collected_data: Dict[str, Any], user_id: str) -> Dict[str, Any]:
        """Finalize the flow from already-collected data.

        Convenience helper for tests/integrations that want to skip the step-by-step UI.
        """
        data = dict(collected_data or {})
        data.setdefault("user_id", user_id)
        data.setdefault("product_id", "serenicare")

        result = await self._step_choose_plan_and_pay({"action": "proceed_to_pay"}, data, user_id)
        result.setdefault("status", "success")
        return result

    async def start(self, user_id: str, initial_data: Dict) -> Dict:
        data = dict(initial_data or {})
        data.setdefault("user_id", user_id)
        data.setdefault("product_id", "serenicare")
        if self.controller:
            app = self.controller.create_application(user_id, data)
            data["application_id"] = app.get("id")
        return await self.process_step("", 0, data, user_id)

    async def process_step(
        self,
        user_input: str,
        current_step: int,
        collected_data: Dict[str, Any],
        user_id: str,
    ) -> Dict:
        try:
            if user_input and isinstance(user_input, str) and user_input.strip().startswith("{"):
                payload = json.loads(user_input)
            elif user_input and isinstance(user_input, dict):
                payload = user_input
            else:
                payload = {"_raw": user_input} if user_input else {}
        except (json.JSONDecodeError, TypeError):
            payload = {"_raw": user_input} if user_input else {}

        if current_step == 0:
            return await self._step_cover_personalization(payload, collected_data, user_id)
        if current_step == 1:
            return await self._step_optional_benefits(payload, collected_data, user_id)
        if current_step == 2:
            return await self._step_medical_conditions(payload, collected_data, user_id)
        if current_step == 3:
            return await self._step_plan_selection(payload, collected_data, user_id)
        if current_step == 4:
            return await self._step_about_you(payload, collected_data, user_id)
        if current_step == 5:
            return await self._step_premium_and_download(payload, collected_data, user_id)
        if current_step == 6:
            return await self._step_choose_plan_and_pay(payload, collected_data, user_id)
        return {"error": "Invalid step"}

    async def _step_cover_personalization(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if payload and "_raw" not in payload:
            if not self.controller:
                errors: Dict[str, str] = {}
                validate_date_iso(payload.get("date_of_birth", ""), errors, "date_of_birth", required=True, not_future=True)
                raise_if_errors(errors)
            data["cover_personalization"] = {
                "date_of_birth": payload.get("date_of_birth", ""),
                "include_spouse": payload.get("include_spouse", False),
                "include_children": payload.get("include_children", False),
                "add_another_main_member": payload.get("add_another_main_member", False),
            }
            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_cover_personalization(app_id, payload)
        return {
            "response": {
                "type": "form",
                "message": "👤 Cover Personalization",
                "fields": [
                    {"name": "date_of_birth", "label": "Date of Birth", "type": "date", "required": True},
                    {
                        "name": "include_spouse",
                        "label": "Include Spouse/Partner",
                        "type": "checkbox",
                        "required": False,
                        "description": "Add your spouse or partner to your cover",
                    },
                    {
                        "name": "include_children",
                        "label": "Include Child/Children",
                        "type": "checkbox",
                        "required": False,
                        "description": "Add your child or children to your cover",
                    },
                    {
                        "name": "add_another_main_member",
                        "label": "Add another main member",
                        "type": "checkbox",
                        "required": False,
                    },
                ],
            },
            "next_step": 1,
            "collected_data": data,
        }

    async def _step_optional_benefits(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if payload and "_raw" not in payload:
            selected = payload.get("optional_benefits") or []
            if isinstance(selected, str):
                selected = [s.strip() for s in selected.split(",") if s.strip()]
            data["optional_benefits"] = selected
            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_optional_benefits(app_id, payload)
        return {
            "response": {
                "type": "checkbox",
                "message": "Select any optional benefits you want to add",
                "options": SERENICARE_OPTIONAL_BENEFITS,
            },
            "next_step": 2,
            "collected_data": data,
        }

    async def _step_medical_conditions(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if payload and "_raw" not in payload:
            data["medical_conditions"] = {
                "has_condition": payload.get("has_condition", False),
            }
            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_medical_conditions(app_id, payload)
        return {
            "response": {
                "type": "radio",
                "message": (
                    "Do you or any family members you wish to include have any of the following: "
                    "Sickle Cells, Cancer(s), Leukaemia, or liver-related conditions?"
                ),
                "question_id": "medical_conditions",
                "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
                "required": True,
            },
            "next_step": 3,
            "collected_data": data,
        }

    async def _step_plan_selection(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if payload and "_raw" not in payload:
            plan_id = payload.get("plan_option") or payload.get("_raw", "").strip()
            if not plan_id:
                raise_if_errors({"plan_option": "Plan selection is required"})
            if plan_id:
                plan = next((p for p in SERENICARE_PLANS if p["id"] == plan_id), None)
                if plan:
                    data["plan_option"] = plan
                    app_id = data.get("application_id")
                    if self.controller and app_id:
                        self.controller.update_plan_selection(app_id, payload)
                else:
                    raise_if_errors({"plan_option": "Please select a valid plan"})
        return {
            "response": {
                "type": "options",
                "message": "Choose your Serenicare plan",
                "options": [
                    {
                        "id": p["id"],
                        "label": p["label"],
                        "description": p["description"],
                        "benefits": p["benefits"],
                    }
                    for p in SERENICARE_PLANS
                ],
            },
            "next_step": 4,
            "collected_data": data,
        }

    async def _step_about_you(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if payload and "_raw" not in payload:
            if not self.controller:
                errors: Dict[str, str] = {}
                first_name = validate_length_range(
                    payload.get("first_name", ""),
                    field="first_name",
                    errors=errors,
                    label="First Name",
                    min_len=2,
                    max_len=50,
                    required=True,
                    message="First name must be 2–50 characters.",
                )
                middle_name_raw = payload.get("middle_name", "")
                middle_name = validate_length_range(
                    middle_name_raw,
                    field="middle_name",
                    errors=errors,
                    label="Middle Name",
                    min_len=0,
                    max_len=50,
                    required=False,
                    message="Middle name must be up to 50 characters.",
                ) if middle_name_raw else ""
                surname = validate_length_range(
                    payload.get("surname", ""),
                    field="surname",
                    errors=errors,
                    label="Surname",
                    min_len=2,
                    max_len=50,
                    required=True,
                    message="Surname must be 2–50 characters.",
                )
                phone_original, phone_normalized = validate_uganda_mobile_frontend(
                    payload.get("phone_number", ""), errors, field="phone_number"
                )
                email = validate_motor_email_frontend(payload.get("email", ""), errors, field="email")
                raise_if_errors(errors)
            data["about_you"] = {
                "first_name": first_name if not self.controller else payload.get("first_name", ""),
                "middle_name": middle_name if not self.controller else payload.get("middle_name", ""),
                "surname": surname if not self.controller else payload.get("surname", ""),
                "phone_number": phone_original if not self.controller else payload.get("phone_number", ""),
                "phone_number_normalized": phone_normalized if not self.controller else payload.get("phone_number", ""),
                "email": email if not self.controller else payload.get("email", ""),
            }
            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_about_you(app_id, payload)
        return {
            "response": {
                "type": "form",
                "message": "About You",
                "fields": [
                    {"name": "first_name", "label": "First Name", "type": "text", "required": True},
                    {"name": "middle_name", "label": "Middle Name (Optional)", "type": "text", "required": False},
                    {"name": "surname", "label": "Surname", "type": "text", "required": True},
                    {"name": "phone_number", "label": "Phone Number", "type": "text", "required": True},
                    {"name": "email", "label": "Email", "type": "email", "required": True},
                ],
            },
            "next_step": 5,
            "collected_data": data,
        }

    async def _step_premium_and_download(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        plan = data.get("plan_option") or SERENICARE_PLANS[0]
        premium = self._calculate_serenicare_premium(data, plan)
        return {
            "response": {
                "type": "premium_summary",
                "message": "💰 Your Serenicare premium",
                "product_name": "Serenicare",
                "plan": plan["label"],
                "monthly_premium": premium["monthly"],
                "annual_premium": premium["annual"],
                "breakdown": premium.get("breakdown", {}),
                "download_option": True,
                "download_label": "Download summary (PDF)",
                "actions": [
                    {"type": "view_all_plans", "label": "View all plans"},
                    {"type": "proceed_to_pay", "label": "Choose this plan and proceed to pay"},
                ],
            },
            "next_step": 6,
            "collected_data": data,
        }

    async def _step_choose_plan_and_pay(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        action = (payload.get("action") or payload.get("_raw") or "").strip().lower()
        if "view" in action or "plan" in action:
            out = await self._step_plan_selection(payload, data, user_id)
            out["next_step"] = 3
            return out
        plan = data.get("plan_option") or SERENICARE_PLANS[0]
        premium = self._calculate_serenicare_premium(data, plan)
        app_id = data.get("application_id")
        if self.controller and app_id:
            app = self.controller.finalize_and_create_quote(app_id, user_id, premium)
            data["quote_id"] = app.get("quote_id") if app else None
        else:
            quote = self.db.create_quote(
                user_id=user_id,
                product_id=data.get("product_id", "serenicare"),
                premium_amount=premium["monthly"],
                sum_assured=None,
                underwriting_data=data,
                pricing_breakdown=premium.get("breakdown"),
                product_name="Serenicare",
            )
            data["quote_id"] = str(quote.id)
        return {
            "response": {
                "type": "proceed_to_payment",
                "message": "Proceeding to payment. Choose your payment method.",
                "quote_id": str(data["quote_id"]),
            },
            "complete": True,
            "next_flow": "payment",
            "collected_data": data,
            "data": {"quote_id": str(data["quote_id"])},
        }

"""
Motor Private flow - Collect vehicle details, excess parameters, additional benefits,
premium calculation, user details, then proceed to payment.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from datetime import date

from src.chatbot.validation import (
    raise_if_errors,
    require_str,
    parse_int,
    parse_decimal_str,
    validate_email,
    validate_in,
    validate_phone_ug,
    validate_enum,
    validate_length_range,
    validate_uganda_mobile_frontend,
    validate_motor_email_frontend,
    validate_cover_start_date_range,
    validate_positive_number_field,
)
from src.integrations.policy.premium import premium_service

MOTOR_PRIVATE_EXCESS_PARAMETERS = [
    {
        "id": "excess_1",
        "label": "10% of claim, UGX 1,000,000 to UGX 3,000,000\n10% of total premium"
    },
    {
        "id": "excess_2",
        "label": "10% of claim, UGX 3,000,001 to UGX 4,000,000\n15% of total premium"
    },
    {
        "id": "excess_3",
        "label": "10% of claim, UGX 4,000,001 to UGX 5,000,000\n25% of total premium"
    }
]

MOTOR_PRIVATE_ADDITIONAL_BENEFITS = [
    {
        "id": "political_violence",
        "label": "Political violence and terrorism\n0.25% of Total Premium"
    },
    {
        "id": "alternative_accommodation",
        "label": "Alternative accommodation\nUGX 300,000 x days x 10%"
    },
    {
        "id": "car_hire",
        "label": "Car hire\nUGX 100,000 x days x 10%"
    }
]

MOTOR_PRIVATE_BENEFITS = [
    {"label": "Limit of liability: third party bodily injury per occurrence", "value": "UGX 20M"},
    {"label": "Limit of liability: third party bodily injury in aggregate", "value": "UGX 50M"},
    {"label": "Limit of liability: third party property damage per occurrence", "value": "UGX 20M"},
    {"label": "Limit of liability: third party property damage in aggregate", "value": "UGX 50M"},
    {"label": "Section 2: passenger liability per occurrence", "value": "UGX 20M"},
    {"label": "Section 2: passenger liability in aggregate per policy period", "value": "UGX 50M"},
    {"label": "Windscreen extension", "value": "UGX 2M"},
    {"label": "Authorized repair limit", "value": "UGX 2M"},
    {"label": "Towing/wreckage removal charges", "value": "UGX 2M"},
    {"label": "Locks and keys extension", "value": "UGX 2M"},
    {"label": "Fire extinguishing charges", "value": "UGX 2M"},
    {"label": "Protection and removal", "value": "UGX 2M"},
    {"label": "Claims preparation costs", "value": "UGX 1M"},
    {"label": "Personal effects excluding cash", "value": "UGX 500,000/="},
    {"label": "Personal accident to driver", "value": "UGX 1M"},
    {"label": "Unobtainable parts extension", "value": "UGX 2M"},
    {"label": "Limit of liability; section 111 – medical expenses", "value": "UGX 2M"},
    {"label": "Free Cleaning and Fumigation of Vehicles after Repair following an accident", "value": "UGX 1M"},
    {"label": "Modification of motor vehicle in case of Permanent Incapacitation of the driver following an accident", "value": "N/A"},
    {"label": "Rim Damage following a motor accident", "value": "UGX 1M"},
    {"label": "Alternative accommodation", "value": "N/A"},
    {"label": "Hire of replacement vehicle", "value": "N/A"},
]


class MotorPrivateFlow:
    """
    Guided flow for Motor Private.

    Step order:
        0 - about_you
        1 - vehicle_details
        2 - excess_parameters
        3 - additional_benefits
        4 - benefits_summary
        5 - premium_calculation
        6 - premium_and_download
        7 - choose_plan_and_pay
    """

    STEPS = [
        "about_you",           # 0
        "vehicle_details",     # 1
        "excess_parameters",   # 2
        "additional_benefits",  # 3
        "benefits_summary",    # 4
        "premium_calculation",  # 5
        "premium_and_download",  # 6
        "choose_plan_and_pay",  # 7
    ]

    def __init__(self, product_catalog, db):
        self.catalog = product_catalog
        self.db = db

    # ------------------------------------------------------------------
    # complete_flow – convenience helper (skips step-by-step UI)
    # ------------------------------------------------------------------

    async def complete_flow(self, collected_data: Dict[str, Any], user_id: str) -> Dict[str, Any]:
        """Finalize the flow from already-collected data."""
        data = dict(collected_data or {})
        payload = data.copy()
        errors: Dict[str, str] = {}

        # ── Personal Details (frontend field names) ────────────────────
        first_name = None
        surname = None
        middle_name = None
        mobile_original = None
        mobile_normalized = None
        email = None
        if "firstName" in payload or "surname" in payload or "mobile" in payload or "email" in payload:
            first_name = validate_length_range(
                payload.get("firstName", ""),
                field="firstName",
                errors=errors,
                label="First name",
                min_len=2,
                max_len=50,
                required=True,
                message="First name must be 2–50 characters.",
            )
            middle_name = validate_length_range(
                payload.get("middleName", ""),
                field="middleName",
                errors=errors,
                label="Middle name",
                min_len=0,
                max_len=50,
                required=False,
                message="Middle name must be up to 50 characters.",
            )
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
            mobile_original, mobile_normalized = validate_uganda_mobile_frontend(
                payload.get("mobile", ""), errors, field="mobile"
            )
            email = validate_motor_email_frontend(payload.get("email", ""), errors, field="email")

        # ── Cover type ─────────────────────────────────────────────────
        cover_type = None
        if "coverType" in payload:
            cover_type = validate_enum(
                payload.get("coverType", ""),
                field="coverType",
                errors=errors,
                allowed=["comprehensive", "third_party"],
                required=True,
                message="Please select a cover type.",
            )

        # ── Vehicle / premium fields (frontend field names) ────────────
        vehicle_make_frontend = None
        year_frontend = None
        cover_start_frontend = None
        rare_model_frontend = None
        valuation_frontend = None
        vehicle_value_frontend = None
        if "vehicleMake" in payload or "yearOfManufacture" in payload or "coverStartDate" in payload:
            vehicle_make_frontend = require_str(payload, "vehicleMake", errors, label="Vehicle make")
            current_year_plus_one = date.today().year + 1
            year_frontend = parse_int(
                {"yearOfManufacture": payload.get("yearOfManufacture")},
                "yearOfManufacture",
                errors,
                min_value=1980,
                max_value=current_year_plus_one,
                required=True,
            )
            cover_start_frontend = validate_cover_start_date_range(
                payload.get("coverStartDate", ""), errors, field="coverStartDate"
            )
            rare_model_frontend = validate_enum(
                payload.get("isRareModel", ""),
                field="isRareModel",
                errors=errors,
                allowed=["yes", "no"],
                required=True,
                message="Please select if the vehicle is a rare model.",
            )
            valuation_frontend = validate_enum(
                payload.get("hasUndergoneValuation", ""),
                field="hasUndergoneValuation",
                errors=errors,
                allowed=["yes", "no"],
                required=True,
                message="Please indicate if the vehicle has undergone valuation.",
            )
            vehicle_value_frontend = validate_positive_number_field(
                payload.get("vehicleValueUgx", ""),
                field="vehicleValueUgx",
                errors=errors,
                message="Vehicle value must be a positive number.",
            )

        raise_if_errors(errors)

        # ── Map into internal structure ────────────────────────────────
        internal = data.setdefault("motor_frontend", {})
        if cover_type is not None:
            internal["cover_type"] = cover_type
        if first_name is not None:
            internal["first_name"] = first_name
        if middle_name is not None:
            internal["middle_name"] = middle_name
        if surname is not None:
            internal["surname"] = surname
        if mobile_original is not None:
            internal["mobile"] = mobile_original
        if mobile_normalized:
            internal["mobile_normalized"] = mobile_normalized
        if email is not None:
            internal["email"] = email
        if vehicle_make_frontend is not None:
            internal["vehicle_make"] = vehicle_make_frontend
        if year_frontend is not None:
            internal["year_of_manufacture"] = year_frontend
        if cover_start_frontend is not None:
            internal["cover_start_date"] = cover_start_frontend
        if rare_model_frontend is not None:
            internal["rare_model"] = rare_model_frontend
        if valuation_frontend is not None:
            internal["valuation_done"] = valuation_frontend
        if vehicle_value_frontend is not None:
            internal["vehicle_value"] = vehicle_value_frontend

        data.setdefault("user_id", user_id)
        data.setdefault("product_id", "motor_private")

        # ── Run all steps in order ─────────────────────────────────────
        step_handlers = [
            self._step_about_you,
            self._step_vehicle_details,
            self._step_excess_parameters,
            self._step_additional_benefits,
            self._step_benefits_summary,
            self._step_premium_calculation,
            self._step_premium_and_download,
            self._step_choose_plan_and_pay,
        ]
        result = {}
        for i, handler in enumerate(step_handlers):
            step_payload = {} if i != len(step_handlers) - 1 else {"action": "proceed_to_pay"}
            result = await handler(step_payload, data, user_id)
            if "collected_data" in result:
                data = result["collected_data"]

        result.setdefault("status", "success")
        return result

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def start(self, user_id: str, initial_data: Dict) -> Dict:
        data = dict(initial_data or {})
        data.setdefault("user_id", user_id)
        data.setdefault("product_id", "motor_private")
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

        handlers = {
            0: self._step_about_you,
            1: self._step_vehicle_details,
            2: self._step_excess_parameters,
            3: self._step_additional_benefits,
            4: self._step_benefits_summary,
            5: self._step_premium_calculation,
            6: self._step_premium_and_download,
            7: self._step_choose_plan_and_pay,
        }
        handler = handlers.get(current_step)
        if handler is None:
            return {"error": "Invalid step"}
        return await handler(payload, collected_data, user_id)

    # ------------------------------------------------------------------
    # Step 0 – About You
    # ------------------------------------------------------------------

    async def _step_about_you(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        errors: Dict[str, str] = {}
        try:
            if payload and "_raw" not in payload:
                first_name = validate_length_range(
                    payload.get("first_name", ""),
                    field="first_name",
                    errors=errors,
                    label="First name",
                    min_len=2,
                    max_len=50,
                    required=True,
                    message="First name must be 2–50 characters.",
                )
                middle_name = validate_length_range(
                    payload.get("middle_name", ""),
                    field="middle_name",
                    errors=errors,
                    label="Middle name",
                    min_len=0,
                    max_len=50,
                    required=False,
                    message="Middle name must be up to 50 characters.",
                )
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
                phone_number = validate_phone_ug(payload.get("phone_number", ""), errors, field="phone_number")
                email = validate_email(payload.get("email", ""), errors, field="email")
                if errors:
                    return {"error": "Validation failed in about_you", "details": errors, "step": "about_you"}
                data["about_you"] = {
                    "first_name": first_name,
                    "middle_name": middle_name,
                    "surname": surname,
                    "phone_number": phone_number,
                    "email": email,
                }
                out = await self._step_vehicle_details({}, data, user_id)
                out["next_step"] = 1
                return out
        except Exception as e:
            return {"error": f"Exception in about_you: {str(e)}", "step": "about_you"}

        return {
            "response": {
                "type": "form",
                "message": "About You",
                "fields": [
                    {"name": "first_name", "label": "First Name", "type": "text", "required": True},
                    {"name": "middle_name", "label": "Middle Name (Optional)", "type": "text", "required": False},
                    {"name": "surname", "label": "Surname", "type": "text", "required": True},
                    {"name": "phone_number", "label": "Phone Number", "type": "text", "required": True, "maxLength": 12},
                    {"name": "email", "label": "Email", "type": "email", "required": True},
                ],
            },
            "next_step": 1,          # ✅ Fixed: was incorrectly 6
            "collected_data": data,
        }

    # ------------------------------------------------------------------
    # Step 1 – Vehicle Details
    # ------------------------------------------------------------------

    async def _step_vehicle_details(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        errors: Dict[str, str] = {}
        try:
            if payload and "_raw" not in payload:
                vehicle_make = require_str(payload, "vehicle_make", errors, label="Vehicle make")
                year = parse_int(
                    payload,
                    "year_of_manufacture",
                    errors,
                    min_value=1980,
                    max_value=date.today().year + 1,
                    required=True,
                )
                cover_start_date = validate_cover_start_date_range(
                    payload.get("cover_start_date", ""), errors, field="cover_start_date"
                )
                rare_model = validate_in(
                    payload.get("rare_model", ""),
                    {"Yes", "No"},
                    errors,
                    "rare_model",
                    required=True,
                )
                valuation_done = validate_in(
                    payload.get("valuation_done", ""),
                    {"Yes", "No"},
                    errors,
                    "valuation_done",
                    required=True,
                )
                vehicle_value = parse_decimal_str(payload, "vehicle_value", errors, min_value=1, required=True)
                first_time_registration = validate_in(
                    payload.get("first_time_registration", ""),
                    {"Yes", "No"},
                    errors,
                    "first_time_registration",
                    required=True,
                )
                car_alarm_installed = validate_in(
                    payload.get("car_alarm_installed", ""),
                    {"Yes", "No"},
                    errors,
                    "car_alarm_installed",
                    required=True,
                )
                tracking_system_installed = validate_in(
                    payload.get("tracking_system_installed", ""),
                    {"Yes", "No"},
                    errors,
                    "tracking_system_installed",
                    required=True,
                )
                car_usage_region = validate_in(
                    payload.get("car_usage_region", ""),
                    {"Within Uganda", "Within East Africa", "Outside East Africa"},
                    errors,
                    "car_usage_region",
                    required=True,
                )
                if errors:
                    return {"error": "Validation failed in vehicle_details", "details": errors, "step": "vehicle_details"}
                data["vehicle_details"] = {
                    "vehicle_make": vehicle_make,
                    "year_of_manufacture": str(year),
                    "cover_start_date": cover_start_date,
                    "rare_model": rare_model,
                    "valuation_done": valuation_done,
                    "vehicle_value": vehicle_value,
                    "first_time_registration": first_time_registration,
                    "car_alarm_installed": car_alarm_installed,
                    "tracking_system_installed": tracking_system_installed,
                    "car_usage_region": car_usage_region,
                }
                out = await self._step_excess_parameters({}, data, user_id)
                out["next_step"] = 2
                return out
        except Exception as e:
            return {"error": f"Exception in vehicle_details: {str(e)}", "step": "vehicle_details"}

        return {
            "response": {
                "type": "form",
                "message": "Premium Calculation - Vehicle Details",
                "fields": [
                    {
                        "name": "vehicle_make",
                        "label": "Choose vehicle make",
                        "type": "select",
                        "required": True,
                        "options": [
                            "Toyota", "Nissan", "Honda", "Subaru", "Suzuki",
                            "Mazda", "Mitsubishi", "Isuzu", "Ford", "Hyundai",
                            "Kia", "Volkswagen", "Mercedes-Benz", "BMW",
                            "Peugeot", "Renault", "Other"
                        ],
                    },
                    {"name": "year_of_manufacture", "label": "Year of manufacture", "type": "text", "required": True},
                    {"name": "cover_start_date", "label": "Cover start date", "type": "date", "required": True},
                    {"name": "rare_model", "label": "Is the car a rare model?", "type": "radio", "options": ["Yes", "No"], "required": True},
                    {"name": "valuation_done", "label": "Has the vehicle undergone valuation?", "type": "radio", "options": ["Yes", "No"], "required": True},
                    {"name": "vehicle_value", "label": "Value of Vehicle (UGX)", "type": "number", "required": True},
                    {
                        "name": "first_time_registration",
                        "label": "First time this vehicle is registered for this type of insurance?",
                        "type": "radio",
                        "options": ["Yes", "No"],
                        "required": True,
                    },
                    {"name": "car_alarm_installed", "label": "Do you have a car alarm installed?", "type": "radio", "options": ["Yes", "No"], "required": True},
                    {
                        "name": "tracking_system_installed",
                        "label": "Do you have a tracking system installed?",
                        "type": "radio",
                        "options": ["Yes", "No"],
                        "required": True,
                    },
                    {
                        "name": "car_usage_region",
                        "label": "Car usage: within Uganda, East Africa, or outside East Africa?",
                        "type": "radio",
                        "options": ["Within Uganda", "Within East Africa", "Outside East Africa"],
                        "required": True,
                    },
                ],
            },
            "next_step": 2,          # ✅ Fixed: was incorrectly 1
            "collected_data": data,
        }

    # ------------------------------------------------------------------
    # Step 2 – Excess Parameters
    # ------------------------------------------------------------------

    async def _step_excess_parameters(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        try:
            if payload and "_raw" not in payload:
                selected = payload.get("excess_parameters") or []
                if isinstance(selected, str):
                    selected = [s.strip() for s in selected.split(",") if s.strip()]
                if not selected:
                    return {"error": "No excess parameters selected", "step": "excess_parameters"}
                data["excess_parameters"] = selected
                out = await self._step_additional_benefits({}, data, user_id)
                out["next_step"] = 3
                return out
        except Exception as e:
            return {"error": f"Exception in excess_parameters: {str(e)}", "step": "excess_parameters"}

        return {
            "response": {
                "type": "checkbox",
                "message": "Excess Parameters",
                "options": MOTOR_PRIVATE_EXCESS_PARAMETERS,
            },
            "next_step": 3,          # ✅ Correct
            "collected_data": data,
        }

    # ------------------------------------------------------------------
    # Step 3 – Additional Benefits
    # ------------------------------------------------------------------

    async def _step_additional_benefits(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        try:
            if payload and "_raw" not in payload:
                selected = payload.get("additional_benefits") or []
                if isinstance(selected, str):
                    selected = [s.strip() for s in selected.split(",") if s.strip()]
                if not selected:
                    return {"error": "No additional benefits selected", "step": "additional_benefits"}
                data["additional_benefits"] = selected
                out = await self._step_benefits_summary({}, data, user_id)
                out["next_step"] = 4
                return out
        except Exception as e:
            return {"error": f"Exception in additional_benefits: {str(e)}", "step": "additional_benefits"}

        return {
            "response": {
                "type": "checkbox",
                "message": "Additional Benefits",
                "options": MOTOR_PRIVATE_ADDITIONAL_BENEFITS,
            },
            "next_step": 4,          # ✅ Correct
            "collected_data": data,
        }

    # ------------------------------------------------------------------
    # Step 4 – Benefits Summary
    # ------------------------------------------------------------------

    async def _step_benefits_summary(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        try:
            if payload and "_raw" not in payload:
                out = await self._step_premium_calculation({}, data, user_id)
                out["next_step"] = 5
                return out
        except Exception as e:
            return {"error": f"Exception in benefits_summary: {str(e)}", "step": "benefits_summary"}

        return {
            "response": {
                "type": "benefits_summary",
                "message": "Benefits",
                "benefits": MOTOR_PRIVATE_BENEFITS,
            },
            "next_step": 5,          # ✅ Correct
            "collected_data": data,
        }

    # ------------------------------------------------------------------
    # Step 5 – Premium Calculation
    # ------------------------------------------------------------------

    async def _step_premium_calculation(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        try:
            if payload and "_raw" not in payload:
                data["premium_calculation"] = {
                    "base_premium": payload.get("base_premium", ""),
                    "training_levy": payload.get("training_levy", ""),
                    "sticker_fees": payload.get("sticker_fees", ""),
                    "vat": payload.get("vat", ""),
                    "stamp_duty": payload.get("stamp_duty", ""),
                }
                out = await self._step_premium_and_download({}, data, user_id)
                out["next_step"] = 6
                return out
        except Exception as e:
            return {"error": f"Exception in premium_calculation: {str(e)}", "step": "premium_calculation"}

        premium = self._calculate_motor_private_premium(data)
        return {
            "response": {
                "type": "premium_summary",
                "message": "Premium Calculation",
                "quote_summary": premium,
                "actions": [
                    {"type": "edit", "label": "Edit"},
                    {"type": "download_quote", "label": "Download Quote"},
                ],
            },
            "next_step": 6,          # ✅ Correct
            "collected_data": data,
        }

    # ------------------------------------------------------------------
    # Step 6 – Premium & Download
    # ------------------------------------------------------------------

    async def _step_premium_and_download(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        try:
            premium = self._calculate_motor_private_premium(data)
            if payload and "_raw" not in payload:
                out = await self._step_choose_plan_and_pay({}, data, user_id)
                out["next_step"] = 7
                return out
        except Exception as e:
            return {"error": f"Exception in premium_and_download: {str(e)}", "step": "premium_and_download"}

        return {
            "response": {
                "type": "premium_summary",
                "message": "Premium Calculation",
                "quote_summary": premium,
                "actions": [
                    {"type": "edit", "label": "Edit"},
                    {"type": "download_quote", "label": "Download Quote"},
                    {"type": "proceed_to_pay", "label": "Proceed to Pay"},
                ],
            },
            "next_step": 7,          # ✅ Correct
            "collected_data": data,
        }

    # ------------------------------------------------------------------
    # Step 7 – Choose Plan & Pay
    # ------------------------------------------------------------------

    async def _step_choose_plan_and_pay(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        try:
            action = (payload.get("action") or payload.get("_raw") or "").strip().lower()
            if "edit" in action:
                out = await self._step_about_you({}, data, user_id)
                out["next_step"] = 0   # ✅ Send user back to beginning of flow
                return out

            premium = self._calculate_motor_private_premium(data)
            quote = self.db.create_quote(
                user_id=user_id,
                product_id=data.get("product_id", "motor_private"),
                premium_amount=premium["total"],
                sum_assured=None,
                underwriting_data=data,
                pricing_breakdown=premium,
                product_name="Motor Private",
            )
            data["quote_id"] = str(quote.id)
        except Exception as e:
            return {"error": f"Exception in choose_plan_and_pay: {str(e)}", "step": "choose_plan_and_pay"}

        return {
            "response": {
                "type": "proceed_to_payment",
                "message": "Proceeding to payment. Choose your payment method.",
                "quote_id": str(quote.id),
            },
            "complete": True,
            "next_flow": "payment",
            "collected_data": data,
            "data": {"quote_id": str(quote.id)},
        }

    # ------------------------------------------------------------------
    # Premium calculation helper
    # ------------------------------------------------------------------

    def _calculate_motor_private_premium(self, data: Dict) -> Dict:
        """Calculate Motor Private premium. Replace with actual business logic as needed."""
        return premium_service.calculate_sync("motor_private", {"data": data})

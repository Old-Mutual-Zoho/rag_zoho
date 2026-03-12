"""
Personal Accident flow - Collect personal details, next of kin, underwriting questions,
coverage selection, ID upload, premium calculation, then proceed to payment.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from datetime import datetime, date

from src.chatbot.field_validator import (
    FieldDecorator,
    StepValidator,
    filter_collected_fields,
)
from src.chatbot.validation import (
    FormValidationError,
    optional_str,
    parse_date_flexible,
    raise_if_errors,
    require_str,
    validate_date_iso,
    validate_email,
    validate_in,
    validate_nin_ug,
    validate_phone_ug,
)
from src.integrations.policy.premium import premium_service
from src.integrations.underwriting import run_quote_preview
from src.integrations.product_benefits import product_benefits_loader


# Risky activities for the checkbox step (per product requirements)
PERSONAL_ACCIDENT_RISKY_ACTIVITIES = [
    {"id": "manufacture_wire_works", "label": "Manufacture of wire works"},
    {"id": "mining", "label": "Mining / Quarrying"},
    {"id": "explosives", "label": "Handling explosives or flammable materials"},
    {"id": "construction_heights", "label": "Construction work at heights"},
    {"id": "diving", "label": "Underwater diving"},
    {"id": "racing", "label": "Motor or speed racing"},
    {"id": "other_risky", "label": "Other risky activity (please specify in next step)"},
]

# Placeholder coverage tiers for "choose coverage" step
PERSONAL_ACCIDENT_COVERAGE_PLANS = [
    {"id": "basic", "label": "Basic", "sum_assured": 10_000_000, "description": "Essential accident cover"},
    {"id": "standard", "label": "Standard", "sum_assured": 25_000_000, "description": "Broader benefits"},
    {"id": "premium", "label": "Premium", "sum_assured": 50_000_000, "description": "Highest cover and benefits"},
]


class PersonalAccidentFlow:
    """
    Updated guided flow for Personal Accident:
    1. Quick Quote (name, email, phone, DOB, cover limit, policy start) → instant premium
    2. Premium Summary (show benefits, download option)
    3. Remaining Details (next of kin, disability, risky activities, ID upload)
    4. Payment
    """

    STEPS = [
        "quick_quote",              # Step 0: Minimal info to get premium
        "premium_summary",          # Step 1: Show premium, benefits, download
        "personal_details",         # Step 2: Full personal details (surname, occupation, nationality, etc.)
        "next_of_kin",              # Step 3: Detailed next of kin (auto-pre-filled)
        "previous_pa_policy",       # Step 4: Underwriting question
        "physical_disability",      # Step 5: Underwriting question
        "risky_activities",         # Step 6: Underwriting question
        "upload_national_id",       # Step 7: ID upload
        "final_confirmation",       # Step 8: Review all data before payment
        "choose_plan_and_pay",      # Step 9: Proceed to payment
    ]

    def __init__(self, product_catalog, db):
        self.catalog = product_catalog
        self.db = db

    async def complete_flow(self, collected_data: Dict[str, Any], user_id: str) -> Dict[str, Any]:
        """Finalize the flow from already-collected data.

        This is primarily a convenience helper for tests and integrations that already
        have all required data and simply need a quote/payment handoff.
        """
        data = dict(collected_data or {})
        data.setdefault("user_id", user_id)
        data.setdefault("product_id", "personal_accident")

        result = await self._step_choose_plan_and_pay({"action": "proceed_to_pay"}, data, user_id)
        result.setdefault("status", "success")
        return result

    async def start(self, user_id: str, initial_data: Dict) -> Dict:
        """Start Personal Accident flow"""
        data = dict(initial_data or {})
        data.setdefault("user_id", user_id)
        data.setdefault("product_id", "personal_accident")
        return await self.process_step("", 0, data, user_id)

    async def process_step(
        self,
        user_input: str,
        current_step: int,
        collected_data: Dict[str, Any],
        user_id: str,
    ) -> Dict:
        """Process one step of the flow."""
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
            return await self._step_quick_quote(payload, collected_data, user_id)
        if current_step == 1:
            return await self._step_premium_summary(payload, collected_data, user_id)
        if current_step == 2:
            return await self._step_personal_details(payload, collected_data, user_id)
        if current_step == 3:
            return await self._step_next_of_kin(payload, collected_data, user_id)
        if current_step == 4:
            return await self._step_previous_pa_policy(payload, collected_data, user_id)
        if current_step == 5:
            return await self._step_physical_disability(payload, collected_data, user_id)
        if current_step == 6:
            return await self._step_risky_activities(payload, collected_data, user_id)
        if current_step == 7:
            return await self._step_upload_national_id(payload, collected_data, user_id)
        if current_step == 8:
            return await self._step_final_confirmation(payload, collected_data, user_id)
        if current_step == 9:
            return await self._step_choose_plan_and_pay(payload, collected_data, user_id)

        return {"error": "Invalid step"}

    async def _step_quick_quote(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        """
        Step 0: Quick Quote
        Collect minimal info: firstName, lastName, mobile, email, dob, policyStartDate, coverLimitAmountUgx.
        Calculate premium immediately and store in Postgres.
        """
        errors: Dict[str, str] = {}

        if payload and "_raw" not in payload:
            # Run shared step-level validation first so field rules (including DOB)
            # are enforced consistently across flows/endpoints.
            errors.update(
                StepValidator.validate(
                    "personal_accident",
                    "quick_quote",
                    payload,
                    context=data,
                )
            )

            # Frontend-style field names
            first_name = require_str(payload, "firstName", errors, label="First Name")
            last_name = require_str(payload, "lastName", errors, label="Last Name")
            middle_name = optional_str(payload, "middleName")
            middle_name = optional_str(payload, "middleName")
            mobile = validate_phone_ug(payload.get("mobile", ""), errors, field="mobile")
            email = validate_email(payload.get("email", ""), errors, field="email")
            dob_str = payload.get("dob", "")
            policy_start_date_str = payload.get("policyStartDate", "")
            cover_limit_str = payload.get("coverLimitAmountUgx", "")

            # Validate DOB (must be at least 18, max 65)
            dob_str_validated = validate_date_iso(dob_str, errors, "dob", required=True, not_future=True)
            dob = parse_date_flexible(dob_str_validated) if dob_str_validated else None
            if dob:
                today = date.today()
                age = today.year - dob.year - (1 if (today.month, today.day) < (dob.month, dob.day) else 0)
                if age < 18:
                    errors["dob"] = "You must be at least 18 years old."
                elif age > 65:
                    errors["dob"] = "Age cannot be more than 65 years."

            # Validate policy start date (must be after today)
            if policy_start_date_str:
                try:
                    policy_start = parse_date_flexible(policy_start_date_str)
                    if not policy_start:
                        errors["policyStartDate"] = "Invalid date format (use YYYY-MM-DD or MM/DD/YYYY)."
                    elif policy_start <= date.today():
                        errors["policyStartDate"] = f"Cover start date must be after {date.today()}."
                except (ValueError, TypeError):
                    errors["policyStartDate"] = "Invalid date format (use YYYY-MM-DD or MM/DD/YYYY)."
            else:
                policy_start = None

            # Validate cover limit
            allowed_limits = ["5000000", "10000000", "20000000"]
            if cover_limit_str not in allowed_limits:
                errors["coverLimitAmountUgx"] = f"Cover limit must be one of: {', '.join(allowed_limits)}"

            if errors:
                raise FormValidationError(field_errors=errors)

            # If no errors, calculate premium and save data
            if not errors:
                # Calculate premium
                premium = self._calculate_pa_premium({"dob": dob}, int(cover_limit_str))

                # Create quote in Postgres (status="draft" for now)
                quote = self.db.create_quote(
                    user_id=user_id,
                    product_id="personal_accident",
                    premium_amount=premium["monthly"],
                    sum_assured=int(cover_limit_str),
                    underwriting_data={
                        "first_name": first_name,
                        "last_name": last_name,
                        "middle_name": middle_name,
                        "email": email,
                        "mobile": mobile,
                        "dob": dob.isoformat() if hasattr(dob, "isoformat") else None,
                        "policy_start_date": policy_start.isoformat() if policy_start else None,
                    },
                    pricing_breakdown=premium.get("breakdown"),
                    product_name="Personal Accident",
                )

                # Store quick quote data for later autofill
                data["quick_quote"] = {
                    "first_name": first_name,
                    "last_name": last_name,
                    "middle_name": middle_name,
                    "email": email,
                    "mobile": mobile,
                    "dob": dob.isoformat() if dob else None,
                    "policy_start_date": policy_start.isoformat() if policy_start else None,
                    "cover_limit_ugx": int(cover_limit_str),
                    "quote_id": str(quote.id),
                }

                data["quote_id"] = str(quote.id)

                # Proceed to premium summary
                return await self._step_premium_summary({}, data, user_id)

        # Pre-fill from existing data
        prefilled = data.get("quick_quote", {})

        # Define all fields
        all_fields = [
            {
                "name": "firstName",
                "label": "First Name",
                "type": "text",
                "required": True,
                "minLength": 2,
                "maxLength": 50,
                "defaultValue": prefilled.get("first_name", ""),
            },
            {
                "name": "middleName",
                "label": "Middle Name",
                "type": "text",
                "required": False,
                "maxLength": 50,
                "defaultValue": prefilled.get("middle_name", ""),
            },
            {
                "name": "lastName",
                "label": "Last Name",
                "type": "text",
                "required": True,
                "minLength": 2,
                "maxLength": 50,
                "defaultValue": prefilled.get("last_name", ""),
            },
            {
                "name": "mobile",
                "label": "Mobile Number",
                "type": "tel",
                "required": True,
                "placeholder": "07XX XXX XXX or +2567XX XXX XXX",
                "defaultValue": prefilled.get("mobile", ""),
            },
            {
                "name": "email",
                "label": "Email Address",
                "type": "email",
                "required": True,
                "maxLength": 100,
                "defaultValue": prefilled.get("email", ""),
            },
            {
                "name": "dob",
                "label": "Date of Birth",
                "type": "date",
                "required": True,
                "help": "Must be 18-65 years old",
                "defaultValue": prefilled.get("dob", ""),
            },
            {
                "name": "policyStartDate",
                "label": "Policy Start Date",
                "type": "date",
                "required": True,
                "help": "Must be after today",
                "defaultValue": prefilled.get("policy_start_date", ""),
            },
            {
                "name": "coverLimitAmountUgx",
                "label": "Cover Limit",
                "type": "select",
                "required": True,
                "defaultValue": str(prefilled.get("cover_limit_ugx", "")),
                "options": [
                    {"value": "5000000", "label": "UGX 5,000,000"},
                    {"value": "10000000", "label": "UGX 10,000,000"},
                    {"value": "20000000", "label": "UGX 20,000,000"},
                ],
            },
        ]

        # Show original quick quote form fields without filtering
        filtered_fields = all_fields

        fields_with_validation = FieldDecorator.decorate(filtered_fields, errors=errors)

        return {
            "response": {
                "type": "form",
                "message": "Get your Personal Accident quote in seconds" + (" - Please fix the errors below" if errors else ""),
                "fields": fields_with_validation,
            },
            "next_step": 0 if errors else 1,
            "collected_data": data,
        }

    async def _step_premium_summary(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        """
        Step 1: Premium Summary & Review
        Display premium, benefits per cover level, and download/proceed options.
        """
        action = (payload.get("action") or payload.get("_raw") or "").strip().lower()

        # If user clicks "Edit quote", go back to step 0
        if "edit" in action or "back" in action:
            return {
                "response": {"type": "message", "message": "Returning to quote form..."},
                "next_step": 0,
                "collected_data": data,
            }

        # If user clicks proceed, immediately return the next backend-defined form
        # (personal details) instead of requiring an extra request/round-trip.
        if any(token in action for token in ("proceed_to_details", "proceed_to_buy", "proceed_to_pay", "proceed", "continue")):
            return await self._step_personal_details({}, data, user_id)

        # Get cover limit from collected data
        cover_limit = data.get("quick_quote", {}).get("cover_limit_ugx") or 5000000

        # Get benefits for this level from product configuration
        benefits = product_benefits_loader.get_formatted_benefits("personal_accident", float(cover_limit))

        # Re-calculate premium (or fetch from data if already calculated)
        quick_quote = data.get("quick_quote", {})

        premium = self._calculate_pa_premium({"quick_quote": quick_quote}, cover_limit)

        # Attempt a non-destructive quotation preview from the underwriting pipeline.
        # This is used to display mocked quotation information to the user while
        # the flow continues to collect remaining details before payment.
        quotation_preview = None
        try:
            preview_result = await run_quote_preview(
                user_id=user_id,
                product_id="personal_accident",
                underwriting_data={
                    "dob": quick_quote.get("dob"),
                    "policyStartDate": quick_quote.get("policy_start_date"),
                    "coverLimitAmountUgx": str(cover_limit),
                },
                currency="UGX",
            )
            quotation_preview = preview_result.get("quotation") if preview_result else None
            if quotation_preview:
                data["preview_quotation"] = quotation_preview
        except Exception:
            quotation_preview = None

        resp = {
            "response": {
                "type": "premium_summary",
                "message": " Your Personal Accident Premium",
                "product_name": "Personal Accident",
                "cover_limit_ugx": cover_limit,
                "monthly_premium": premium["monthly"],
                "annual_premium": premium["annual"],
                "breakdown": premium.get("breakdown", {}),
                "benefits": benefits,
                "download_option": True,
                "download_label": "Download Quote (PDF)",
                "actions": [
                    {"type": "edit", "label": "Edit Quote"},
                    {"type": "proceed_to_details", "label": "Proceed with this quote to buy"},
                ],
            },
            "next_step": 2,
            "collected_data": data,
        }

        if quotation_preview:
            resp["response"]["quotation_preview"] = quotation_preview
            resp["response"]["payable_amount"] = quotation_preview.get("payable_amount")

        return resp

    async def _step_personal_details(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        """
        Step 2: Full Remaining Details
        Collects additional personal information: surname, occupation, nationality, gender, address, etc.
        Pre-fills from quick quote where applicable.
        Uses progressive disclosure - only shows NEW fields not collected in quick quote.
        """
        errors: Dict[str, str] = {}
        quick_quote = data.get("quick_quote", {})

        if payload and "_raw" not in payload:
            # Auto-use data from quick quote for fields already collected in Step 0
            # Only validate NEW fields that are being collected in this step

            surname = payload.get("surname") or quick_quote.get("last_name", "")
            if not surname:
                errors["surname"] = "Surname is required"

            first_name = payload.get("first_name") or quick_quote.get("first_name", "")
            if not first_name:
                errors["first_name"] = "First name is required"

            # Middle name - auto-use from quick quote if not in payload
            middle_name = payload.get("middle_name") or quick_quote.get("middle_name", "")

            # Email - auto-use from quick quote if not in payload
            email = payload.get("email") or quick_quote.get("email", "")
            if email:
                validate_email(email, errors, field="email")

            # Mobile - auto-use from quick quote if not in payload
            mobile_number = payload.get("mobile_number") or quick_quote.get("mobile", "")
            if mobile_number:
                validate_phone_ug(mobile_number, errors, field="mobile_number")

            # NEW fields being collected in this step (not in quick quote)
            national_id_number = require_str(payload, "national_id_number", errors, label="National ID Number")
            if national_id_number:
                validate_nin_ug(national_id_number, errors, field="national_id_number")

            nationality = require_str(payload, "nationality", errors, label="Nationality")
            occupation = require_str(payload, "occupation", errors, label="Occupation")
            gender = validate_in(payload.get("gender", ""), {"Male", "Female", "Other"}, errors, "gender", required=True)
            tax_identification_number = optional_str(payload, "tax_identification_number")
            country_of_residence = require_str(payload, "country_of_residence", errors, label="Country of Residence")
            physical_address = require_str(payload, "physical_address", errors, label="Physical Address")

            if errors:
                raise FormValidationError(field_errors=errors)

            # If no errors, save all data (including auto-filled from quick quote) and proceed
            if not errors:
                data["personal_details"] = {
                    "surname": surname,
                    "first_name": first_name,
                    "middle_name": middle_name,
                    "email": email,
                    "mobile_number": mobile_number,
                    "national_id_number": national_id_number,
                    "nationality": nationality,
                    "tax_identification_number": tax_identification_number,
                    "occupation": occupation,
                    "gender": gender,
                    "country_of_residence": country_of_residence,
                    "physical_address": physical_address,
                }
                # Return next step response
                return await self._step_next_of_kin({}, data, user_id)

        # Pre-fill from quick quote and previous personal_details submissions
        prefilled_personal = data.get("personal_details", {})

        # Define all fields
        all_fields = [
            {
                "name": "surname",
                "label": "Surname",
                "type": "text",
                "required": True,
                "defaultValue": prefilled_personal.get("surname", quick_quote.get("last_name", "")),
            },
            {
                "name": "first_name",
                "label": "First Name",
                "type": "text",
                "required": True,
                "defaultValue": prefilled_personal.get(
                    "first_name", quick_quote.get("first_name", "")
                ),
            },
            {
                "name": "middle_name",
                "label": "Middle Name",
                "type": "text",
                "required": False,
                "defaultValue": prefilled_personal.get("middle_name", quick_quote.get("middle_name", "")),
            },
            {
                "name": "email",
                "label": "Email Address",
                "type": "email",
                "required": True,
                "defaultValue": prefilled_personal.get("email", quick_quote.get("email", "")),
            },
            {
                "name": "mobile_number",
                "label": "Mobile Number",
                "type": "tel",
                "required": True,
                "defaultValue": prefilled_personal.get(
                    "mobile_number", quick_quote.get("mobile", "")
                ),
            },
            {
                "name": "national_id_number",
                "label": "National ID Number",
                "type": "text",
                "required": True,
                "defaultValue": prefilled_personal.get("national_id_number", ""),
            },
            {
                "name": "nationality",
                "label": "Nationality",
                "type": "text",
                "required": True,
                "defaultValue": prefilled_personal.get("nationality", ""),
            },
            {
                "name": "tax_identification_number",
                "label": "Tax Identification Number",
                "type": "text",
                "required": False,
                "defaultValue": prefilled_personal.get(
                    "tax_identification_number", ""
                ),
            },
            {
                "name": "occupation",
                "label": "Occupation",
                "type": "text",
                "required": True,
                "defaultValue": prefilled_personal.get("occupation", ""),
            },
            {
                "name": "gender",
                "label": "Gender",
                "type": "select",
                "options": ["Male", "Female", "Other"],
                "required": True,
                "defaultValue": prefilled_personal.get("gender", ""),
            },
            {
                "name": "country_of_residence",
                "label": "Country of Residence",
                "type": "text",
                "required": True,
                "defaultValue": prefilled_personal.get(
                    "country_of_residence", ""
                ),
            },
            {
                "name": "physical_address",
                "label": "Physical Address",
                "type": "text",
                "required": True,
                "defaultValue": prefilled_personal.get("physical_address", ""),
            },
        ]

        # Step 2 should show only remaining fields not already collected in quick quote
        filtered_fields = filter_collected_fields(
            all_fields=all_fields,
            collected_data=data,
            previous_step_keys=["quick_quote"],
        )

        fields_with_validation = FieldDecorator.decorate(filtered_fields, errors=errors)

        return {
            "response": {
                "type": "form",
                "message": "📋 Additional personal details" + (" - Please complete the missing fields" if errors else ""),
                "fields": fields_with_validation,
            },
            "next_step": 2,
            "collected_data": data,
        }

    async def _step_next_of_kin(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        """
        Step 3: Next of Kin
        Collect beneficiary details. Pre-fill name from quick quote if available.
        """
        errors: Dict[str, str] = {}

        if payload and "_raw" not in payload:
            first_name = require_str(payload, "nok_first_name", errors, label="First Name")
            last_name = require_str(payload, "nok_last_name", errors, label="Last Name")
            middle_name = optional_str(payload, "nok_middle_name")
            phone_number = validate_phone_ug(payload.get("nok_phone_number", ""), errors, field="nok_phone_number")
            relationship = require_str(payload, "nok_relationship", errors, label="Relationship")
            address = require_str(payload, "nok_address", errors, label="Address")
            id_number = optional_str(payload, "nok_id_number")
            id_number = optional_str(payload, "nok_id_number")
            if id_number:
                validate_nin_ug(id_number, errors, field="nok_id_number")

            # If no errors, save and proceed
            if not errors:
                data["next_of_kin"] = {
                    "nok_first_name": first_name,
                    "nok_last_name": last_name,
                    "nok_middle_name": middle_name,
                    "nok_phone_number": phone_number,
                    "nok_relationship": relationship,
                    "nok_address": address,
                    "nok_id_number": id_number,
                }
                return await self._step_previous_pa_policy({}, data, user_id)

        # Pre-fill from quick quote if available
        quick_quote = data.get("quick_quote", {})
        prefilled_nok = data.get("next_of_kin", {})
        autofill_first = prefilled_nok.get("nok_first_name", quick_quote.get("first_name", ""))
        autofill_last = prefilled_nok.get("nok_last_name", quick_quote.get("last_name", ""))

        # Define all fields
        all_fields = [
            {"name": "nok_first_name", "label": "First Name", "type": "text", "required": True, "defaultValue": autofill_first},
            {"name": "nok_last_name", "label": "Last Name", "type": "text", "required": True, "defaultValue": autofill_last},
            {"name": "nok_middle_name", "label": "Middle Name", "type": "text", "required": False, "defaultValue": prefilled_nok.get("nok_middle_name", "")},
            {"name": "nok_phone_number", "label": "Phone Number", "type": "tel", "required": True, "defaultValue": prefilled_nok.get("nok_phone_number", "")},
            {"name": "nok_relationship", "label": "Relationship", "type": "text", "required": True, "defaultValue": prefilled_nok.get("nok_relationship", "")},
            {"name": "nok_address", "label": "Address", "type": "text", "required": True, "defaultValue": prefilled_nok.get("nok_address", "")},
            {"name": "nok_id_number", "label": "ID Number", "type": "text", "required": False, "defaultValue": prefilled_nok.get("nok_id_number", "")},
        ]

        # Show original form fields without filtering
        filtered_fields = all_fields

        fields_with_validation = FieldDecorator.decorate(filtered_fields, errors=errors)

        return {
            "response": {
                "type": "form",
                "message": "👥 Next of kin details" + (" - Please fix the errors below" if errors else ""),
                "fields": fields_with_validation,
            },
            "next_step": 3,
            "collected_data": data,
        }

    async def _step_previous_pa_policy(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        """
        Step 4: Previous PA Policy
        Check if customer had a previous personal accident policy.
        """
        raw = (payload.get("_raw") or "").strip().lower() if payload else ""
        if payload:
            had = payload.get("had_previous_pa_policy") in ("yes", "Yes", True) or raw in ("yes", "y")
            data["previous_pa_policy"] = {
                "had_policy": had,
                "insurer_name": payload.get("previous_insurer_name", ""),
            }
            return await self._step_physical_disability({}, data, user_id)

        return {
            "response": {
                "type": "yes_no_details",
                "message": "Have you previously had a Personal Accident policy?",
                "question_id": "previous_pa_policy",
                "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
                "details_field": {
                    "name": "previous_insurer_name",
                    "label": "Name of insurer",
                    "show_when": "yes",
                },
            },
            "next_step": 4,
            "collected_data": data,
        }

    async def _step_physical_disability(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        raw = (payload.get("_raw") or "").strip().lower() if payload else ""
        if payload:
            free = payload.get("free_from_disability") in ("yes", "Yes", True) or raw in ("yes", "y")
            data["physical_disability"] = {
                "free_from_disability": free,
                "details": payload.get("disability_details", ""),
            }
            return await self._step_risky_activities({}, data, user_id)

        return {
            "response": {
                "type": "yes_no_details",
                "message": "Are you free from any physical disability?",
                "question_id": "physical_disability",
                "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
                "details_field": {
                    "name": "disability_details",
                    "label": "Please give details",
                    "show_when": "no",
                },
            },
            "next_step": 5,
            "collected_data": data,
        }

    async def _step_risky_activities(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if payload and "_raw" not in payload:
            activities = payload.get("risky_activities") or []
            if isinstance(activities, str):
                activities = [a.strip() for a in activities.split(",") if a.strip()]
            data["risky_activities"] = {
                "selected": activities,
                "other_description": payload.get("risky_activity_other", ""),
            }
            return await self._step_upload_national_id({}, data, user_id)

        return {
            "response": {
                "type": "checkbox",
                "message": "Are you engaged in any of these activities? (Select all that apply)",
                "options": PERSONAL_ACCIDENT_RISKY_ACTIVITIES,
                "allow_other": True,
                "other_field": {"name": "risky_activity_other", "label": "Other (please specify)"},
            },
            "next_step": 6,
            "collected_data": data,
        }

    async def _step_upload_national_id(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if payload and "_raw" not in payload:
            file_ref = (
                payload.get("file_ref")
                or payload.get("national_id_file_ref")
                or payload.get("ref_value")
                or payload.get("ref_vale")
                or ""
            )
            if not str(file_ref or "").strip():
                raise_if_errors({"national_id_file_ref": "National ID file is required"})
            data["national_id_upload"] = {
                "file_ref": file_ref,
                "uploaded_at": datetime.utcnow().isoformat(),
            }
            return await self._step_final_confirmation({}, data, user_id)

        return {
            "response": {
                "type": "file_upload",
                "message": "Upload your National ID (PDF)",
                "accept": "application/pdf",
                "field_name": "national_id_file_ref",
                "max_size_mb": 5,
                "help": "Upload a clear PDF of your National ID.",
            },
            "next_step": 7,
            "collected_data": data,
        }

    async def _step_final_confirmation(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        """
        Step 8: Final Confirmation & Review
        Show all collected data, calculated premium, and ask for confirmation before payment.
        """
        action = str(payload.get("action") or payload.get("_raw") or "").strip().lower() if payload else ""
        if any(token in action for token in ("confirm", "proceed_to_payment", "proceed", "pay")):
            return await self._step_choose_plan_and_pay(payload, data, user_id)

        quick_quote = data.get("quick_quote", {})
        cover_limit = quick_quote.get("cover_limit_ugx", 5000000)

        premium = self._calculate_pa_premium({"quick_quote": quick_quote}, cover_limit)

        # Summarize collected data
        summary = {
            "Applicant": {
                "Name": f"{quick_quote.get('first_name')} {quick_quote.get('last_name')}",
                "Email": quick_quote.get("email"),
                "Mobile": quick_quote.get("mobile"),
                "Date of Birth": quick_quote.get("dob"),
            },
            "Next of Kin": {
                "Name": f"{data.get('next_of_kin', {}).get('nok_first_name')} {data.get('next_of_kin', {}).get('nok_last_name')}",
                "Relationship": data.get('next_of_kin', {}).get('nok_relationship'),
                "Phone": data.get('next_of_kin', {}).get('nok_phone_number'),
            },
            "Coverage": {
                "Cover Limit": f"UGX {cover_limit:,}",
                "Policy Start": quick_quote.get('policy_start_date'),
                "Monthly Premium": f"UGX {premium['monthly']:,.2f}",
                "Annual Premium": f"UGX {premium['annual']:,.2f}",
            },
        }

        return {
            "response": {
                "type": "confirmation",
                "message": " Please review your details below",
                "summary": summary,
                "actions": [
                    {"type": "edit", "label": "Edit Details"},
                    {"type": "confirm", "label": "Confirm & Proceed to Payment"},
                ],
            },
            "next_step": 8,
            "collected_data": data,
        }

    async def _step_premium_and_download(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        # This method is deprecated in the new flow; using _step_premium_summary instead
        # Kept for backwards compatibility
        return {
            "response": {
                "type": "message",
                "message": "Redirecting to confirmation...",
            },
            "next_step": 7,
            "collected_data": data,
        }

    async def _step_choose_plan_and_pay(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        """
        Step 8: Final Submission & Payment
        Create/update quote and proceed to payment flow.
        """
        # Get quote ID from earlier quick quote step
        quote_id = data.get("quote_id")
        if not quote_id:
            # Fallback: create a new quote if not already created
            quick_quote = data.get("quick_quote", {})
            cover_limit = quick_quote.get("cover_limit_ugx", 5000000)

            premium = self._calculate_pa_premium({"quick_quote": quick_quote}, cover_limit)

            quote = self.db.create_quote(
                user_id=user_id,
                product_id="personal_accident",
                premium_amount=premium["monthly"],
                sum_assured=cover_limit,
                underwriting_data=data,
                pricing_breakdown=premium.get("breakdown"),
                product_name="Personal Accident",
            )
            quote_id = str(quote.id)
            data["quote_id"] = quote_id

        return {
            "response": {
                "type": "proceed_to_payment",
                "message": " Proceeding to payment. Choose your payment method.",
                "quote_id": quote_id,
            },
            "complete": True,
            "next_flow": "payment",
            "collected_data": data,
            "data": {"quote_id": quote_id},
        }

    def _calculate_pa_premium(
        self,
        data: Dict[str, Any],
        sum_assured: int,
    ) -> Dict:
        """
        Calculate premium for Personal Accident.

        - Base rate: 0.15% of sum assured per year (illustrative).
        - Age-based modifiers: lower risk for 25-45, higher for <25 or >60 (if DOB provided).
        - Risky activities: add loading when any risky activities are selected.
        """
        return premium_service.calculate_sync(
            "personal_accident",
            {"data": data, "sum_assured": sum_assured},
        )

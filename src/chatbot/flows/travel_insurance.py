"""
Travel Insurance flow - Customer buying journey for Old Mutual Travel products.

Flow: About you → Product selection → Travel party & trip details → Data consent →
Traveller details → Emergency contact → Bank details (optional) → Passport upload →
Premium calculation → Payment.
"""


from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from src.chatbot.travel_insurance_countries import DEPARTURE_COUNTRY, DESTINATION_COUNTRIES
from src.chatbot.validation import (
    add_error,
    parse_int,
    parse_iso_date,
    raise_if_errors,
    require_str,
    validate_date_iso,
    validate_email,
    validate_in,
    validate_phone_ug,
)
from src.integrations.policy.premium import premium_service

# Travel insurance product cards (from product selection screen)
TRAVEL_INSURANCE_PRODUCTS: List[Dict[str, str]] = [
    {
        "id": "worldwide_essential",
        "label": "Worldwide Essential",
        "description": "Simple insurance for worry-free international travel",
    },
    {
        "id": "worldwide_elite",
        "description": "Comprehensive cover for confident world travel",
    },
    {
        "id": "schengen_essential",
        "label": "Schengen Essential",
        "description": "Core cover for travel to the Schengen-area",
    },
    {
        "id": "schengen_elite",
        "label": "Schengen Elite",
        "description": "Enhanced benefits for travel to the Schengen-area",
    },
    {
        "id": "student_cover",
        "label": "Student Cover",
        "description": "Flexible travel cover designed for students abroad",
    },
    {
        "id": "africa_asia",
        "label": "Africa & Asia",
        "description": "Tailored protection for trips across Africa and Asia",
    },
    {
        "id": "inbound_karibu",
        "label": "Inbound Karibu",
        "description": "Travel insurance for visitors coming to Uganda",
    },
]

# Sample benefits for premium summary (Worldwide Essential tier)
TRAVEL_INSURANCE_BENEFITS: List[Dict[str, str]] = [
    {
        "benefit": "Emergency medical expenses (Including epidemics and pandemics)",
        "amount": "Up to $40,000",
    },
    {
        "benefit": "Compulsory quarantine expenses (epidemics/pandemics)",
        "amount": "$85 per night up to 14 nights",
    },
    {"benefit": "Emergency medical evacuation and repatriation", "amount": "Actual Expenses"},
    {"benefit": "Emergency dental care", "amount": "Up to $250"},
    {"benefit": "Optical expenses", "amount": "Up to $100"},
    {"benefit": "Baggage delay", "amount": "$50 per hour up to $250"},
    {"benefit": "Replacement of passport and driving license", "amount": "Up to $300"},
    {"benefit": "Personal Liability", "amount": "Up to $100,000"},
]

# Relationship options for emergency contact
EMERGENCY_CONTACT_RELATIONSHIPS: Tuple[str, ...] = (
    "Spouse",
    "Parent",
    "Child",
    "Sibling",
    "Sister-in-law",
    "Brother-in-law",
    "Friend",
    "Other",
)


class TravelInsuranceFlow:
    """
    Guided flow for Travel Insurance: about you, product selection, travel details,
    data consent, traveller details, emergency contact, bank (optional), passport upload,
    premium calculation, then payment.
    """

    STEPS = [
        "about_you",
        "product_selection",
        "travel_party_and_trip",
        "data_consent",
        "traveller_details",
        "emergency_contact",
        "bank_details_optional",
        "upload_passport",
        "premium_summary",
        "choose_plan_and_pay",
    ]

    def __init__(self, product_catalog: Any, db: Any) -> None:
        self.catalog = product_catalog
        self.db = db
        self.controller = None

        # Controller for persistence (optional)
        try:
            from src.chatbot.controllers.travel_insurance_controller import (  # noqa: WPS433
                TravelInsuranceController,
            )

            self.controller = TravelInsuranceController(db)
        except (ImportError, ModuleNotFoundError):
            self.controller = None

    async def start(self, user_id: str, initial_data: Dict[str, Any]) -> Dict[str, Any]:
        """Start Travel Insurance flow."""
        data: Dict[str, Any] = dict(initial_data or {})
        data.setdefault("user_id", user_id)
        data.setdefault("product_id", "travel_insurance")

        # Create persistent application record if controller available
        if self.controller:
            app = self.controller.create_application(user_id, data)
            data["application_id"] = (app or {}).get("id")

        return await self.process_step("", 0, data, user_id)

    async def process_step(
        self,
        user_input: Any,
        current_step: int,
        collected_data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        """Process one step of the flow."""
        payload = self._normalize_payload(user_input)

        step_handlers = [
            self._step_about_you,
            self._step_product_selection,
            self._step_travel_party_and_trip,
            self._step_data_consent,
            self._step_traveller_details,
            self._step_emergency_contact,
            self._step_bank_details_optional,
            self._step_upload_passport,
            self._step_premium_summary,
            self._step_choose_plan_and_pay,
        ]

        if 0 <= current_step < len(step_handlers):
            return await step_handlers[current_step](payload, collected_data, user_id)

        return {"error": "Invalid step"}

    async def _step_product_selection(self, payload: Dict, data: Dict, user_id: str) -> Dict:
        if not data.get("selected_product"):
            data["selected_product"] = TRAVEL_INSURANCE_PRODUCTS[0]
            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_product_selection(app_id, {"product_id": TRAVEL_INSURANCE_PRODUCTS[0]["id"]})

        if payload and "_raw" not in payload:
            errors: Dict[str, str] = {}
            product_id = (payload.get("product_id") or payload.get("coverage_product") or "").strip()

            if product_id:
                product = next(
                    (p for p in TRAVEL_INSURANCE_PRODUCTS if p["id"] == product_id),
                    None,
                )
                if not product:
                    add_error(errors, "product_id", "Invalid product selection")
                else:
                    data["selected_product"] = product

                    app_id = data.get("application_id")
                    if self.controller and app_id:
                        self.controller.update_product_selection(app_id, {"product_id": product_id})

            raise_if_errors(errors)

        return {
            "response": {
                "type": "product_cards",
                "message": "✈️ Select your travel insurance cover",
                "products": [
                    {
                        "id": p["id"],
                        "label": p["label"],
                        "description": p["description"],
                        "action": "select_cover",
                        "selected": p["id"] == data.get("selected_product", {}).get("id"),
                    }
                    for p in TRAVEL_INSURANCE_PRODUCTS
                ],
            },
            "next_step": 2,
            "collected_data": data,
        }

    @staticmethod
    def _normalize_payload(user_input: Any) -> Dict[str, Any]:
        """
        Normalize incoming step input into a dictionary payload.

        - None/empty -> {}
        - dict -> shallow copy
        - JSON string -> parsed dict (if valid JSON object)
        - other string -> {"_raw": "..."}
        - anything else -> {"_raw": str(...)}
        """
        if user_input is None:
            return {}

        if isinstance(user_input, dict):
            return dict(user_input)

        if isinstance(user_input, str):
            cleaned = user_input.strip()
            if not cleaned:
                return {}

            if cleaned.startswith("{") and cleaned.endswith("}"):
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass

            return {"_raw": cleaned}

        return {"_raw": str(user_input)}

    async def _step_about_you(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        if payload and "_raw" not in payload:
            errors: Dict[str, str] = {}
            require_str(payload, "first_name", errors, label="First Name")
            require_str(payload, "surname", errors, label="Surname")
            validate_phone_ug(payload.get("phone_number", ""), errors, field="phone_number")
            validate_email(payload.get("email", ""), errors, field="email")
            raise_if_errors(errors)

            data["about_you"] = {
                "first_name": payload.get("first_name", ""),
                "middle_name": payload.get("middle_name", ""),
                "surname": payload.get("surname", ""),
                "phone_number": payload.get("phone_number", ""),
                "email": payload.get("email", ""),
            }

            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_about_you(app_id, payload)

        return {
            "response": {
                "type": "form",
                "message": "👤 About you – Get your travel insurance quote in minutes",
                "fields": [
                    {"name": "first_name", "label": "First Name", "type": "text", "required": True},
                    {
                        "name": "middle_name",
                        "label": "Middle Name (Optional)",
                        "type": "text",
                        "required": False,
                    },
                    {"name": "surname", "label": "Surname", "type": "text", "required": True},
                    {
                        "name": "phone_number",
                        "label": "Phone Number",
                        "type": "tel",
                        "required": True,
                        "placeholder": "07XX XXX XXX",
                    },
                    {"name": "email", "label": "Email", "type": "email", "required": True},
                ],
            },
            "next_step": 1,
            "collected_data": data,
        }

    async def _step_travel_party_and_trip(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        travel_fields = {
            "travel_party",
            "total_travellers",
            "traveller_1_date_of_birth",
            "traveller_2_date_of_birth",
            "num_travellers_18_69",
            "num_travellers_0_17",
            "num_travellers_70_75",
            "num_travellers_76_80",
            "num_travellers_81_85",
            "departure_country",
            "destination_country",
            "departure_date",
            "return_date",
        }
        has_travel_submission = any(field in payload for field in travel_fields)

        if payload and "_raw" not in payload and has_travel_submission:
            errors: Dict[str, str] = {}

            travel_party = validate_in(
                payload.get("travel_party", ""),
                ("myself_only", "myself_and_someone_else", "group"),
                errors,
                "travel_party",
                required=True,
            )

            n18_69 = 0
            n0_17 = 0
            n70_75 = 0
            n76_80 = 0
            n81_85 = 0
            total_travellers = 0
            traveller_1_date_of_birth = ""
            traveller_2_date_of_birth = ""

            if travel_party in ("myself_only", "myself_and_someone_else"):
                traveller_1_date_of_birth = validate_date_iso(
                    payload.get("traveller_1_date_of_birth", ""),
                    errors,
                    "traveller_1_date_of_birth",
                    required=True,
                    not_future=True,
                )

                if travel_party == "myself_and_someone_else":
                    traveller_2_date_of_birth = validate_date_iso(
                        payload.get("traveller_2_date_of_birth", ""),
                        errors,
                        "traveller_2_date_of_birth",
                        required=True,
                        not_future=True,
                    )

                for field_name, dob_value in (
                    ("traveller_1_date_of_birth", traveller_1_date_of_birth),
                    ("traveller_2_date_of_birth", traveller_2_date_of_birth),
                ):
                    if not dob_value:
                        continue

                    parsed_dob = parse_iso_date(dob_value)
                    if not parsed_dob:
                        continue

                    age = self._calculate_age(parsed_dob)
                    if age > 85:
                        add_error(errors, field_name, "Traveller age must be 85 years or below")
                        continue

                    bucket = self._age_bucket(age)
                    if bucket == "0_17":
                        n0_17 += 1
                    elif bucket == "18_69":
                        n18_69 += 1
                    elif bucket == "70_75":
                        n70_75 += 1
                    elif bucket == "76_80":
                        n76_80 += 1
                    elif bucket == "81_85":
                        n81_85 += 1

                total_travellers = 1 if travel_party == "myself_only" else 2

            elif travel_party == "group":
                total_travellers = parse_int(
                    payload,
                    "total_travellers",
                    errors,
                    min_value=1,
                    required=True,
                )
                n18_69 = parse_int(payload, "num_travellers_18_69", errors, min_value=0, required=True)
                n0_17 = parse_int(payload, "num_travellers_0_17", errors, min_value=0, required=True)
                n70_75 = parse_int(payload, "num_travellers_70_75", errors, min_value=0, required=True)
                n76_80 = parse_int(payload, "num_travellers_76_80", errors, min_value=0, required=True)
                n81_85 = parse_int(payload, "num_travellers_81_85", errors, min_value=0, required=True)

                range_total = n18_69 + n0_17 + n70_75 + n76_80 + n81_85
                if total_travellers and range_total != total_travellers:
                    add_error(
                        errors,
                        "total_travellers",
                        "Total travellers must equal the sum of all age-range counts",
                    )

            departure_country = validate_in(
                payload.get("departure_country", DEPARTURE_COUNTRY),
                (DEPARTURE_COUNTRY,),
                errors,
                "departure_country",
                required=True,
            )
            destination_country = validate_in(
                payload.get("destination_country", ""),
                DESTINATION_COUNTRIES,
                errors,
                "destination_country",
                required=True,
            )

            departure_date = validate_date_iso(
                payload.get("departure_date", ""),
                errors,
                "departure_date",
                required=True,
            )
            return_date = validate_date_iso(
                payload.get("return_date", ""),
                errors,
                "return_date",
                required=True,
            )

            dd = parse_iso_date(departure_date)
            rd = parse_iso_date(return_date)
            if dd and rd and rd < dd:
                add_error(errors, "return_date", "Return date cannot be before departure date")

            raise_if_errors(errors)

            data["travel_party_and_trip"] = {
                "travel_party": travel_party,
                "total_travellers": total_travellers,
                "traveller_1_date_of_birth": traveller_1_date_of_birth,
                "traveller_2_date_of_birth": traveller_2_date_of_birth,
                "num_travellers_18_69": n18_69,
                "num_travellers_0_17": n0_17,
                "num_travellers_70_75": n70_75,
                "num_travellers_76_80": n76_80,
                "num_travellers_81_85": n81_85,
                "departure_country": departure_country,
                "destination_country": destination_country,
                "departure_date": departure_date,
                "return_date": return_date,
            }

            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_travel_party_and_trip(app_id, payload)

        return {
            "response": {
                "type": "form",
                "message": "✈️ Travel details",
                "fields": [
                    {
                        "name": "travel_party",
                        "label": "Travel party",
                        "type": "radio",
                        "options": [
                            {"id": "myself_only", "value": "myself_only", "label": "Myself only"},
                            {
                                "id": "myself_and_someone_else",
                                "value": "myself_and_someone_else",
                                "label": "Myself and someone else",
                            },
                            {"id": "group", "value": "group", "label": "Group"},
                        ],
                        "required": True,
                    },
                    {
                        "name": "traveller_1_date_of_birth",
                        "label": "Your Date of Birth",
                        "type": "date",
                        "required": False,
                        "required_when": {"travel_party": ["myself_only", "myself_and_someone_else"]},
                        "show_when": {"travel_party": ["myself_only", "myself_and_someone_else"]},
                    },
                    {
                        "name": "traveller_2_date_of_birth",
                        "label": "Second Traveller Date of Birth",
                        "type": "date",
                        "required": False,
                        "required_when": {"travel_party": ["myself_and_someone_else"]},
                        "show_when": {"travel_party": ["myself_and_someone_else"]},
                    },
                    {
                        "name": "total_travellers",
                        "label": "Total number of travellers",
                        "type": "number",
                        "min": 1,
                        "required": False,
                        "required_when": {"travel_party": ["group"]},
                        "show_when": {"travel_party": ["group"]},
                    },
                    {
                        "name": "num_travellers_18_69",
                        "label": "Number of travellers (18–69 years)",
                        "type": "number",
                        "min": 0,
                        "required": False,
                        "required_when": {"travel_party": ["group"]},
                        "show_when": {"travel_party": ["group"]},
                    },
                    {
                        "name": "num_travellers_0_17",
                        "label": "Number of travellers (0–17 years)",
                        "type": "number",
                        "min": 0,
                        "required": False,
                        "required_when": {"travel_party": ["group"]},
                        "show_when": {"travel_party": ["group"]},
                    },
                    {
                        "name": "num_travellers_70_75",
                        "label": "Number of travellers (70–75 years)",
                        "type": "number",
                        "min": 0,
                        "required": False,
                        "required_when": {"travel_party": ["group"]},
                        "show_when": {"travel_party": ["group"]},
                    },
                    {
                        "name": "num_travellers_76_80",
                        "label": "Number of travellers (76–80 years)",
                        "type": "number",
                        "min": 0,
                        "required": False,
                        "required_when": {"travel_party": ["group"]},
                        "show_when": {"travel_party": ["group"]},
                    },
                    {
                        "name": "num_travellers_81_85",
                        "label": "Number of travellers (81–85 years)",
                        "type": "number",
                        "min": 0,
                        "required": False,
                        "required_when": {"travel_party": ["group"]},
                        "show_when": {"travel_party": ["group"]},
                    },
                    {
                        "name": "departure_country",
                        "label": "Departure Country",
                        "type": "select",
                        "options": [
                            {
                                "value": DEPARTURE_COUNTRY,
                                "label": DEPARTURE_COUNTRY,
                            }
                        ],
                        "required": True,
                    },
                    {
                        "name": "destination_country",
                        "label": "Destination Country",
                        "type": "select",
                        "options": [
                            {
                                "value": country,
                                "label": country,
                            }
                            for country in DESTINATION_COUNTRIES
                        ],
                        "required": True,
                    },
                    {"name": "departure_date", "label": "Departure Date", "type": "date", "required": True},
                    {"name": "return_date", "label": "Return Date", "type": "date", "required": True},
                ],
                "info": "A change in number of travellers will result in a premium adjustment.",
            },
            "next_step": 3,
            "collected_data": data,
        }

    @staticmethod
    def _calculate_age(dob: date) -> int:
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    @staticmethod
    def _age_bucket(age: int) -> Optional[str]:
        if age < 0:
            return None
        if age <= 17:
            return "0_17"
        if age <= 69:
            return "18_69"
        if age <= 75:
            return "70_75"
        if age <= 80:
            return "76_80"
        if age <= 85:
            return "81_85"
        return None

    async def _step_data_consent(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        if payload and "_raw" not in payload:
            errors: Dict[str, str] = {}

            data["data_consent"] = {
                "terms_and_conditions_agreed": payload.get("terms_and_conditions_agreed")
                in (True, "yes", "true", "1"),
                "consent_data_outside_uganda": payload.get("consent_data_outside_uganda")
                in (True, "yes", "true", "1"),
                "consent_child_data": payload.get("consent_child_data") in (True, "yes", "true", "1"),
                "consent_marketing": payload.get("consent_marketing") in (True, "yes", "true", "1"),
            }

            if not data["data_consent"].get("terms_and_conditions_agreed"):
                add_error(errors, "terms_and_conditions_agreed", "You must accept the Terms and Conditions")

            raise_if_errors(errors)

            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_data_consent(app_id, payload)

        return {
            "response": {
                "type": "consent",
                "message": "📋 Before we begin – Data consent",
                "consents": [
                    {
                        "id": "terms_and_conditions_agreed",
                        "label": "I have read and understand the Terms and Conditions.",
                        "required": True,
                        "link": "https://www.oldmutual.co.ug/terms",
                    },
                    {
                        "id": "consent_data_outside_uganda",
                        "label": (
                            "I consent to processing of my personal data outside Uganda "
                            "(as per Privacy Notice and Privacy Policy)."
                        ),
                        "required": True,
                    },
                    {
                        "id": "consent_child_data",
                        "label": (
                            "I am the parent/legal guardian and consent to processing of my child's "
                            "personal data (if children are travelling)."
                        ),
                        "required": False,
                    },
                    {
                        "id": "consent_marketing",
                        "label": (
                            "I consent to receive information about insurance/financial products and "
                            "special offers. (You can opt-out anytime.)"
                        ),
                        "required": False,
                    },
                ],
            },
            "next_step": 4,
            "collected_data": data,
        }

    async def _step_traveller_details(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        if payload and "_raw" not in payload:
            errors: Dict[str, str] = {}

            require_str(payload, "first_name", errors, label="First Name")
            require_str(payload, "surname", errors, label="Surname")
            validate_in(
                payload.get("nationality_type", ""),
                ("ugandan", "non_ugandan"),
                errors,
                "nationality_type",
                required=True,
            )
            require_str(payload, "passport_number", errors, label="Passport Number")
            validate_date_iso(
                payload.get("date_of_birth", ""),
                errors,
                "date_of_birth",
                required=True,
                not_future=True,
            )
            require_str(payload, "occupation", errors, label="Profession/Occupation")
            validate_phone_ug(payload.get("phone_number", ""), errors, field="phone_number")
            validate_email(payload.get("email", ""), errors, field="email")
            require_str(payload, "postal_address", errors, label="Postal/Home Address")
            require_str(payload, "town_city", errors, label="Town/City")

            raise_if_errors(errors)

            travellers = data.get("travellers") or []
            primary = {
                "first_name": payload.get("first_name", ""),
                "middle_name": payload.get("middle_name", ""),
                "surname": payload.get("surname", ""),
                "nationality_type": payload.get("nationality_type", ""),
                "passport_number": payload.get("passport_number", ""),
                "date_of_birth": payload.get("date_of_birth", ""),
                "occupation": payload.get("occupation", ""),
                "phone_number": payload.get("phone_number", ""),
                "office_number": payload.get("office_number", ""),
                "email": payload.get("email", ""),
                "postal_address": payload.get("postal_address", ""),
                "town_city": payload.get("town_city", ""),
            }

            if not travellers:
                travellers.append(primary)
            else:
                travellers[0] = primary

            data["travellers"] = travellers

            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_traveller_details(app_id, payload)

        return {
            "response": {
                "type": "form",
                "message": (
                    "👤 Traveller details – Please provide your details and those of any "
                    "accompanying travelers"
                ),
                "fields": [
                    {"name": "first_name", "label": "First Name", "type": "text", "required": True},
                    {
                        "name": "middle_name",
                        "label": "Middle Name (Optional)",
                        "type": "text",
                        "required": False,
                    },
                    {"name": "surname", "label": "Surname", "type": "text", "required": True},
                    {
                        "name": "nationality_type",
                        "label": "Nationality Type",
                        "type": "radio",
                        "options": [
                            {"id": "ugandan", "label": "Ugandan"},
                            {"id": "non_ugandan", "label": "Non-Ugandan"},
                        ],
                        "required": True,
                    },
                    {"name": "passport_number", "label": "Passport Number", "type": "text", "required": True},
                    {"name": "date_of_birth", "label": "Date of Birth", "type": "date", "required": True},
                    {
                        "name": "occupation",
                        "label": "Profession/Occupation",
                        "type": "text",
                        "required": True,
                    },
                    {"name": "phone_number", "label": "Phone Number", "type": "tel", "required": True},
                    {
                        "name": "office_number",
                        "label": "Office Number (Optional)",
                        "type": "tel",
                        "required": False,
                    },
                    {"name": "email", "label": "Email Address", "type": "email", "required": True},
                    {
                        "name": "postal_address",
                        "label": "Postal/Home Address",
                        "type": "text",
                        "required": True,
                    },
                    {"name": "town_city", "label": "Town/City", "type": "text", "required": True},
                ],
                "add_another": {"label": "Add another traveller", "action": "add_traveller"},
            },
            "next_step": 5,
            "collected_data": data,
        }

    async def _step_emergency_contact(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        if payload and "_raw" not in payload:
            errors: Dict[str, str] = {}
            require_str(payload, "ec_surname", errors, label="Surname")
            validate_in(
                payload.get("ec_relationship", ""),
                EMERGENCY_CONTACT_RELATIONSHIPS,
                errors,
                "ec_relationship",
                required=True,
            )
            validate_phone_ug(payload.get("ec_phone_number", ""), errors, field="ec_phone_number")
            validate_email(payload.get("ec_email", ""), errors, field="ec_email")
            raise_if_errors(errors)

            data["emergency_contact"] = {
                "surname": payload.get("ec_surname", ""),
                "relationship": payload.get("ec_relationship", ""),
                "phone_number": payload.get("ec_phone_number", ""),
                "email": payload.get("ec_email", ""),
                "home_address": payload.get("ec_home_address", ""),
            }

            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_emergency_contact(app_id, payload)

        return {
            "response": {
                "type": "form",
                "message": "📞 Emergency contact / beneficiary",
                "fields": [
                    {"name": "ec_surname", "label": "Surname", "type": "text", "required": True},
                    {
                        "name": "ec_relationship",
                        "label": "Relationship",
                        "type": "select",
                        "options": list(EMERGENCY_CONTACT_RELATIONSHIPS),
                        "required": True,
                    },
                    {"name": "ec_phone_number", "label": "Phone Number", "type": "tel", "required": True},
                    {"name": "ec_email", "label": "Email Address", "type": "email", "required": True},
                    {
                        "name": "ec_home_address",
                        "label": "Home/Postal Address",
                        "type": "text",
                        "required": False,
                    },
                ],
            },
            "next_step": 6,
            "collected_data": data,
        }

    async def _step_bank_details_optional(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        if payload and "_raw" not in payload:
            errors: Dict[str, str] = {}

            bank_name = str(payload.get("bank_name") or "").strip()
            account_holder_name = str(payload.get("account_holder_name") or "").strip()
            account_number = str(payload.get("account_number") or "").strip()
            bank_branch = str(payload.get("bank_branch") or "").strip()
            account_currency = str(payload.get("account_currency") or "").strip()

            any_bank_field = any(
                [bank_name, account_holder_name, account_number, bank_branch, account_currency]
            )

            if any_bank_field:
                if not bank_name:
                    add_error(errors, "bank_name", "Bank Name is required")
                if not account_holder_name:
                    add_error(errors, "account_holder_name", "Bank Account Holder Name is required")
                if not account_number:
                    add_error(errors, "account_number", "Bank Account Number is required")
                elif not account_number.isdigit():
                    add_error(errors, "account_number", "Bank Account Number must be numeric")
                if not bank_branch:
                    add_error(errors, "bank_branch", "Bank Branch is required")

                validate_in(
                    account_currency,
                    ("UGX", "USD", "EUR"),
                    errors,
                    "account_currency",
                    required=True,
                )

            raise_if_errors(errors)

            data["bank_details"] = {
                "bank_name": payload.get("bank_name", ""),
                "account_holder_name": payload.get("account_holder_name", ""),
                "account_number": payload.get("account_number", ""),
                "bank_branch": payload.get("bank_branch", ""),
                "account_currency": payload.get("account_currency", ""),
            }

            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_bank_details(app_id, payload)

        return {
            "response": {
                "type": "form",
                "message": "🏦 Bank details (optional) – For refunds or payouts",
                "optional": True,
                "fields": [
                    {"name": "bank_name", "label": "Bank Name", "type": "text", "required": False},
                    {
                        "name": "account_holder_name",
                        "label": "Bank Account Holder Name",
                        "type": "text",
                        "required": False,
                    },
                    {
                        "name": "account_number",
                        "label": "Bank Account Number",
                        "type": "text",
                        "required": False,
                    },
                    {"name": "bank_branch", "label": "Bank Branch", "type": "text", "required": False},
                    {
                        "name": "account_currency",
                        "label": "Bank Account Currency",
                        "type": "select",
                        "options": ["UGX", "USD", "EUR"],
                        "required": False,
                    },
                ],
            },
            "next_step": 7,
            "collected_data": data,
        }

    async def _step_upload_passport(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        if payload and "_raw" not in payload:
            errors: Dict[str, str] = {}
            file_ref = require_str(payload, "passport_file_ref", errors, label="Passport file")
            raise_if_errors(errors)

            data["passport_upload"] = {"file_ref": file_ref, "uploaded_at": datetime.utcnow().isoformat()}

            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_passport_upload(app_id, payload)

        return {
            "response": {
                "type": "file_upload",
                "message": "📄 Upload copy of Passport Bio Data Page",
                "accept": "application/pdf,image/jpeg,image/jpg",
                "field_name": "passport_file_ref",
                "max_size_mb": 1,
                "help": "PDF, JPEG or JPG. Max 1 MB",
            },
            "next_step": 8,
            "collected_data": data,
        }

    async def _step_premium_summary(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        # Allow the UI to pass the upload ref again on this step without failing
        if payload.get("passport_file_ref") and not data.get("passport_upload"):
            data["passport_upload"] = {
                "file_ref": payload.get("passport_file_ref", ""),
                "uploaded_at": datetime.utcnow().isoformat(),
            }
            app_id = data.get("application_id")
            if self.controller and app_id:
                self.controller.update_passport_upload(app_id, payload)

        trip = data.get("travel_party_and_trip") or {}
        total_premium = self._calculate_travel_premium(data)

        # Persist pricing summary into application (keep it simple: store updated trip)
        app_id = data.get("application_id")
        if self.controller and app_id:
            self.controller.update_travel_party_and_trip(app_id, data.get("travel_party_and_trip", {}))

        return {
            "response": {
                "type": "premium_summary",
                "message": "💰 Premium calculation",
                "product_name": (data.get("selected_product") or {}).get("label", "Travel Insurance"),
                "total_premium_usd": total_premium["total_usd"],
                "total_premium_ugx": total_premium["total_ugx"],
                "covering": trip.get("travel_party", "Myself"),
                "period_of_coverage": self._get_period_text(trip),
                "departure_country": trip.get("departure_country", ""),
                "destination_country": trip.get("destination_country", ""),
                "departure_date": trip.get("departure_date", ""),
                "return_date": trip.get("return_date", ""),
                "benefits": TRAVEL_INSURANCE_BENEFITS,
                "breakdown": total_premium.get("breakdown", {}),
                "download_option": True,
                "download_label": "Download Quote",
                "actions": [
                    {"type": "edit", "label": "Edit"},
                    {"type": "call_me_back", "label": "Call Me Back"},
                    {"type": "proceed_to_pay", "label": "Proceed"},
                ],
            },
            "next_step": 9,
            "collected_data": data,
        }

    async def _step_choose_plan_and_pay(
        self,
        payload: Dict[str, Any],
        data: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        action = str(payload.get("action") or payload.get("_raw") or "").strip().lower()

        if "edit" in action:
            out = await self._step_travel_party_and_trip(payload, data, user_id)
            out["next_step"] = 2
            return out

        if "call" in action or "back" in action:
            return {
                "response": {
                    "type": "call_me_back",
                    "message": "We'll call you back shortly. Our team will reach out at your provided number.",
                },
                "next_step": 9,
                "collected_data": data,
            }

        total_premium = self._calculate_travel_premium(data)
        product = data.get("selected_product") or TRAVEL_INSURANCE_PRODUCTS[0]

        app_id = data.get("application_id")
        if self.controller and app_id:
            app = self.controller.finalize_and_create_quote(app_id, user_id, total_premium)
            data["quote_id"] = (app or {}).get("quote_id")
        else:
            quote = self.db.create_quote(
                user_id=user_id,
                product_id=data.get("product_id", "travel_insurance"),
                premium_amount=total_premium["total_ugx"],
                sum_assured=None,
                underwriting_data=data,
                pricing_breakdown=total_premium.get("breakdown"),
                product_name=product.get("label", "Travel Insurance"),
            )
            data["quote_id"] = str(quote.id)

        return {
            "response": {
                "type": "proceed_to_payment",
                "message": "Proceeding to payment. Choose Mobile Money (MTN/Airtel) or Bank Transfer.",
                "quote_id": str(data["quote_id"]),
                "total_due_ugx": total_premium["total_ugx"],
                "payment_options": [
                    {"id": "mobile_money", "label": "Mobile Money", "providers": ["MTN", "Airtel"]},
                    {"id": "bank_transfer", "label": "Bank Transfer"},
                ],
            },
            "complete": True,
            "next_flow": "payment",
            "collected_data": data,
            "data": {"quote_id": str(data["quote_id"])},
        }

    @staticmethod
    def _get_period_text(trip: Dict[str, Any]) -> str:
        """Human-readable coverage period."""
        dd = trip.get("departure_date")
        rd = trip.get("return_date")
        if dd and rd:
            return f"{dd} to {rd}"
        if dd:
            return f"From {dd}"
        return "Not provided"

    def _calculate_travel_premium(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate travel insurance premium.

        Test expectations:
        - returns total_usd, total_ugx, breakdown
        - breakdown includes "days"
        - for 2026-03-03 to 2026-03-08 => days == 6
        """
        return premium_service.calculate_sync("travel_insurance", {"data": data})

    @staticmethod
    def _calculate_trip_days(departure_date: Any, return_date: Any) -> int:
        """Inclusive trip days; defaults to 1 if invalid/missing."""
        d1 = TravelInsuranceFlow._safe_iso_date(departure_date)
        d2 = TravelInsuranceFlow._safe_iso_date(return_date)
        if not d1 or not d2:
            return 1
        return max(1, (d2 - d1).days + 1)

    @staticmethod
    def _safe_iso_date(value: Any) -> Optional[date]:
        """Parse YYYY-MM-DD safely into date."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).date()
        except (TypeError, ValueError):
            return None

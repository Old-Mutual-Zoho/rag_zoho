"""Controller for Serenicare flow persistence."""
from typing import Any, Dict, Optional
from src.chatbot.validation import (
    raise_if_errors,
    require_str,
    optional_str,
    validate_date_iso,
    validate_email,
    validate_phone_ug,
)


class SerenicareController:

    # ...existing code...

    def update_serenicare_form(self, app_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Update Serenicare application with full form payload and validate all fields.
        """
        from src.chatbot.validation import (
            validate_length_range,
            validate_enum,
            validate_list_ids,
            validate_motor_email_frontend,
            validate_uganda_mobile_frontend,
            raise_if_errors,
        )
        errors: Dict[str, str] = {}

        # Validate names
        first_name = validate_length_range(
            payload.get("firstName", ""),
            field="firstName",
            errors=errors,
            label="First Name",
            min_len=2,
            max_len=50,
            required=True
        )
        last_name = validate_length_range(
            payload.get("lastName", ""),
            field="lastName",
            errors=errors,
            label="Last Name",
            min_len=2,
            max_len=50,
            required=True
        )
        middle_name = validate_length_range(
            payload.get("middleName", ""),
            field="middleName",
            errors=errors,
            label="Middle Name",
            min_len=0,
            max_len=50,
            required=False
        )

        # Validate mobile
        _, mobile = validate_uganda_mobile_frontend(payload.get("mobile", ""), errors, field="mobile")

        # Validate email
        email = validate_motor_email_frontend(payload.get("email", ""), errors, field="email")
        if email and len(email) > 100:
            errors["email"] = "Email must be at most 100 characters."

        # Validate planType
        plan_type = validate_enum(
            payload.get("planType", ""),
            field="planType",
            errors=errors,
            allowed=["essential", "classic", "comprehensive", "premium"],
            required=True,
            message="Plan type is required and must be one of: essential, classic, comprehensive, premium."
        )

        # Validate optionalBenefits
        optional_benefits = validate_list_ids(
            payload.get("optionalBenefits", []),
            allowed_ids=["outpatient", "maternity", "dental", "optical", "covid"],
            errors=errors,
            field="optionalBenefits"
        )

        # Validate seriousConditions
        serious_conditions = validate_enum(
            payload.get("seriousConditions", ""),
            field="seriousConditions",
            errors=errors,
            allowed=["yes", "no"],
            required=True,
            message="Serious conditions must be 'yes' or 'no'."
        )

        # Validate mainMembers
        main_members = payload.get("mainMembers", [])
        if not isinstance(main_members, list) or not main_members:
            errors["mainMembers"] = "At least one main member is required."
        else:
            from datetime import date, datetime as dt
            for idx, member in enumerate(main_members):
                m_err = {}
                if not isinstance(member, dict):
                    errors[f"mainMembers[{idx}]"] = "Member must be an object."
                    continue
                # Mutual exclusion
                if member.get("includeSpouse") and member.get("includeChildren"):
                    m_err["includeSpouse"] = "Cannot select both spouse and children for the same member."
                # D.O.B
                dob_str = str(member.get("dob") or member.get("D.O.B") or member.get("date_of_birth") or "").strip()
                try:
                    dob = dt.fromisoformat(dob_str).date()
                except Exception:
                    m_err["dob"] = "D.O.B must be a valid date (YYYY-MM-DD)."
                    dob = None
                if dob:
                    if dob >= date.today():
                        m_err["dob"] = "D.O.B must be in the past."
                    if member.get("includeSpouse") and (date.today().year - dob.year < 19 or (date.today() - dob).days < 19*365):
                        m_err["dob"] = "Spouse must be at least 19 years old."
                # Age
                age = member.get("age")
                if age is None or str(age).strip() == "":
                    m_err["age"] = "Age is required."
                elif dob:
                    # Calculate age from dob
                    today = date.today()
                    calc_age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                    try:
                        age_i = int(age)
                    except Exception:
                        m_err["age"] = "Age must be a whole number."
                    else:
                        if age_i != calc_age:
                            m_err["age"] = f"Age must match D.O.B (expected {calc_age})."
                if m_err:
                    errors[f"mainMembers[{idx}]"] = ", ".join([f"{k}: {v}" for k, v in m_err.items()])

        raise_if_errors(errors)

        updates = {
            "first_name": first_name,
            "last_name": last_name,
            "middle_name": middle_name,
            "mobile": mobile,
            "email": email,
            "plan_type": plan_type,
            "optional_benefits": optional_benefits,
            "serious_conditions": serious_conditions,
            "main_members": main_members,
        }

        app = self.db.update_serenicare_application(app_id, updates)
        return self._to_dict(app) if app else None

    # ...existing code...
    def __init__(self, db):
        self.db = db

    def create_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        app = self.db.create_serenicare_application(user_id, initial_data or {})
        return self._to_dict(app)

    def get_application(self, app_id: str) -> Optional[Dict[str, Any]]:
        app = self.db.get_serenicare_application(app_id)
        return self._to_dict(app) if app else None

    def list_applications(
        self,
        user_id: Optional[str] = None,
        order_by: str = "created_at",
        descending: bool = True,
    ):
        apps = self.db.list_serenicare_applications(user_id=user_id, order_by=order_by, descending=descending)
        return [self._to_dict(a) for a in apps]

    def delete_application(self, app_id: str) -> bool:
        return self.db.delete_serenicare_application(app_id)

    # Step helpers

    def update_cover_personalization(self, app_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        errors: Dict[str, str] = {}
        dob = validate_date_iso(payload.get("date_of_birth", ""), errors, "date_of_birth", required=True, not_future=True)
        raise_if_errors(errors)
        updates = {"cover_personalization": {
            "date_of_birth": dob,
            "include_spouse": payload.get("include_spouse", False),
            "include_children": payload.get("include_children", False),
            "add_another_main_member": payload.get("add_another_main_member", False),
        }}
        app = self.db.update_serenicare_application(app_id, updates)
        return self._to_dict(app) if app else None

    def update_optional_benefits(self, app_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        selected = payload.get("optional_benefits") or []
        if isinstance(selected, str):
            selected = [s.strip() for s in selected.split(",") if s.strip()]
        updates = {"optional_benefits": selected}
        app = self.db.update_serenicare_application(app_id, updates)
        return self._to_dict(app) if app else None

    def update_medical_conditions(self, app_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        updates = {"medical_conditions": {"has_condition": payload.get("has_condition", False)}}
        app = self.db.update_serenicare_application(app_id, updates)
        return self._to_dict(app) if app else None

    def update_plan_selection(self, app_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        errors: Dict[str, str] = {}
        plan_id = (payload.get("plan_option") or payload.get("_raw") or "").strip()
        if not plan_id:
            errors["plan_option"] = "Plan selection is required"
        raise_if_errors(errors)
        updates = {"plan_option": {"id": plan_id}}
        app = self.db.update_serenicare_application(app_id, updates)
        return self._to_dict(app) if app else None

    def update_about_you(self, app_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        errors: Dict[str, str] = {}
        first_name = require_str(payload, "first_name", errors, label="First Name")
        middle_name = optional_str(payload, "middle_name")
        surname = require_str(payload, "surname", errors, label="Surname")
        phone_number = validate_phone_ug(payload.get("phone_number", ""), errors, field="phone_number")
        email = validate_email(payload.get("email", ""), errors, field="email")
        raise_if_errors(errors)
        updates = {"about_you": {
            "first_name": first_name,
            "middle_name": middle_name,
            "surname": surname,
            "phone_number": phone_number,
            "email": email,
        }}
        app = self.db.update_serenicare_application(app_id, updates)
        return self._to_dict(app) if app else None

    def finalize_and_create_quote(self, app_id: str, user_id: str, pricing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        app = self.db.get_serenicare_application(app_id)
        if not app:
            return None
        quote = self.db.create_quote(
            user_id=user_id,
            product_id=app.plan_option.get("id", "serenicare"),
            premium_amount=pricing.get("monthly"),
            sum_assured=None,
            underwriting_data={
                "cover_personalization": app.cover_personalization,
                "optional_benefits": app.optional_benefits,
                "medical_conditions": app.medical_conditions,
                "about_you": app.about_you,
            },
            pricing_breakdown=pricing.get("breakdown"),
            product_name="Serenicare",
        )
        updates = {"quote_id": str(quote.id), "status": "quoted"}
        self.db.update_serenicare_application(app_id, updates)
        app = self.db.get_serenicare_application(app_id)
        return self._to_dict(app) if app else None

    def _to_dict(self, app):
        if not app:
            return None
        return {
            "id": app.id,
            "user_id": app.user_id,
            "status": app.status,
            "cover_personalization": app.cover_personalization,
            "optional_benefits": app.optional_benefits,
            "medical_conditions": app.medical_conditions,
            "plan_option": app.plan_option,
            "about_you": app.about_you,
            "quote_id": app.quote_id,
            "created_at": app.created_at.isoformat(),
            "updated_at": app.updated_at.isoformat(),
        }

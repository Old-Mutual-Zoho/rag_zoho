"""Controller for Motor Private full-form submissions.

This controller wraps MotorPrivateFlow.complete_flow so that motor-specific
validations and quote creation are encapsulated outside the FastAPI layer.
"""

from typing import Any, Dict, Optional

from src.chatbot.validation import (
    validate_length_range,
    validate_enum,
    validate_motor_email_frontend,
    validate_uganda_mobile_frontend,
    raise_if_errors,
)

MOTOR_PRIVATE_VEHICLE_MAKE_OPTIONS = [
    "Toyota",
    "Nissan",
    "Honda",
    "Subaru",
    "Suzuki",
    "Mazda",
    "Mitsubishi",
    "Isuzu",
    "Ford",
    "Hyundai",
    "Kia",
    "Volkswagen",
    "Mercedes-Benz",
    "BMW",
    "Peugeot",
    "Renault",
    "Other",
]


class MotorPrivateController:

    def get_vehicle_make_options(self):
        """
        Return the hard-coded list of vehicle make options for Motor Private.
        """
        return MOTOR_PRIVATE_VEHICLE_MAKE_OPTIONS

    def __init__(self, db):
        self.db = db

    def create_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        app = self.db.create_motor_private_application(user_id, initial_data or {})
        return self._to_dict(app)

    def get_application(self, app_id: str) -> Optional[Dict[str, Any]]:
        app = self.db.get_motor_private_application(app_id)
        return self._to_dict(app) if app else None

    def list_applications(self, user_id: Optional[str] = None, order_by: str = "created_at", descending: bool = True):
        apps = self.db.list_motor_private_applications(user_id=user_id, order_by=order_by, descending=descending)
        return [self._to_dict(a) for a in apps]

    def delete_application(self, app_id: str) -> bool:
        return self.db.delete_motor_private_application(app_id)

    def _validate_motor_private_form(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and normalize the full Motor Private payload."""
        errors: Dict[str, str] = {}

        first_name = validate_length_range(
            payload.get("firstName", ""),
            field="firstName",
            errors=errors,
            label="First Name",
            min_len=2,
            max_len=50,
            required=True,
        )
        middle_name = validate_length_range(
            payload.get("middleName", ""),
            field="middleName",
            errors=errors,
            label="Middle Name",
            min_len=0,
            max_len=50,
            required=False,
        )
        surname = validate_length_range(
            payload.get("surname", ""),
            field="surname",
            errors=errors,
            label="Surname",
            min_len=2,
            max_len=50,
            required=True,
        )
        _, mobile = validate_uganda_mobile_frontend(payload.get("mobile", ""), errors, field="mobile")
        email = validate_motor_email_frontend(payload.get("email", ""), errors, field="email")

        cover_type = validate_enum(
            payload.get("coverType", ""),
            field="coverType",
            errors=errors,
            allowed=["comprehensive", "third_party"],
            required=True,
            message="Please select a cover type.",
        )
        vehicle_make = validate_enum(
            payload.get("vehicleMake", ""),
            field="vehicleMake",
            errors=errors,
            allowed=self.get_vehicle_make_options(),
            required=True,
            message="Please select a valid vehicle make.",
        )

        year_of_manufacture = payload.get("yearOfManufacture")
        try:
            year_of_manufacture = int(year_of_manufacture)
            from datetime import date

            current_year = date.today().year
            if not (1980 <= year_of_manufacture <= current_year + 1):
                errors["yearOfManufacture"] = "Year of manufacture must be between 1980 and next year."
        except Exception:
            errors["yearOfManufacture"] = "Year of manufacture must be a valid integer."

        cover_start_date = payload.get("coverStartDate", "")
        try:
            from datetime import datetime, timedelta

            cover_date = datetime.fromisoformat(cover_start_date)
            today = datetime.now().date()
            if not (today <= cover_date.date() <= today + timedelta(days=90)):
                errors["coverStartDate"] = "Cover start date must be within the next 90 days."
        except Exception:
            errors["coverStartDate"] = "Cover start date must be a valid date (YYYY-MM-DD)."

        is_rare_model = validate_enum(
            payload.get("isRareModel", ""),
            field="isRareModel",
            errors=errors,
            allowed=["yes", "no"],
            required=True,
            message="Please select if the vehicle is a rare model.",
        )
        has_undergone_valuation = validate_enum(
            payload.get("hasUndergoneValuation", ""),
            field="hasUndergoneValuation",
            errors=errors,
            allowed=["yes", "no"],
            required=True,
            message="Please indicate if the vehicle has undergone valuation.",
        )

        vehicle_value_ugx = payload.get("vehicleValueUgx")
        try:
            vehicle_value_ugx = float(vehicle_value_ugx)
            if vehicle_value_ugx <= 0:
                errors["vehicleValueUgx"] = "Vehicle value must be a positive number."
        except Exception:
            errors["vehicleValueUgx"] = "Vehicle value must be a positive number."

        raise_if_errors(errors)
        return {
            "cover_type": cover_type,
            "first_name": first_name,
            "middle_name": middle_name,
            "surname": surname,
            "mobile": mobile,
            "email": email,
            "vehicle_make": vehicle_make,
            "year_of_manufacture": year_of_manufacture,
            "cover_start_date": cover_start_date,
            "is_rare_model": is_rare_model,
            "has_undergone_valuation": has_undergone_valuation,
            "vehicle_value_ugx": vehicle_value_ugx,
        }

    def update_motor_private_form(self, app_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Update Motor Private application with full form payload and validate all fields.
        """
        updates = self._validate_motor_private_form(payload)
        app = self.db.update_motor_private_application(app_id, updates)
        return self._to_dict(app) if app else None

    async def submit_full_form(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate the full Motor Private form, calculate premium, and persist a quote."""
        from src.integrations.policy.premium import premium_service

        # Resolve external identifier to internal user
        user = self.db.get_or_create_user(phone_number=user_id)
        internal_user_id = str(user.id)

        # Validate the full form in a backend-agnostic way. Persist an application
        # record only when the active DB implementation supports it.
        validated = self._validate_motor_private_form(payload)

        app_id = None
        if hasattr(self.db, "create_motor_private_application") and hasattr(self.db, "update_motor_private_application"):
            app_data = self.create_application(internal_user_id, {})
            app_id = app_data["id"]
            self.db.update_motor_private_application(app_id, validated)

        # Build the data dict expected by the premium calculator
        data = {
            "cover_type": validated["cover_type"],
            "vehicle_make": validated["vehicle_make"],
            "year_of_manufacture": validated["year_of_manufacture"],
            "vehicle_value_ugx": validated["vehicle_value_ugx"],
            "is_rare_model": validated["is_rare_model"],
            "has_undergone_valuation": validated["has_undergone_valuation"],
            "first_name": validated["first_name"],
            "middle_name": validated["middle_name"],
            "surname": validated["surname"],
            "mobile": validated["mobile"],
            "email": validated["email"],
        }
        premium = premium_service.calculate_sync("motor_private", {"data": data})

        quote = self.db.create_quote(
            user_id=internal_user_id,
            product_id="motor_private",
            premium_amount=premium.get("total", 0),
            sum_assured=None,
            underwriting_data=data,
            pricing_breakdown=premium,
            product_name="Motor Private",
        )
        if app_id and hasattr(self.db, "update_motor_private_application"):
            self.db.update_motor_private_application(app_id, {"quote_id": str(quote.id), "status": "quoted"})

        return {
            "quote_id": str(quote.id),
            "product_name": "Motor Private",
            "total_premium": premium.get("total", 0),
            "breakdown": premium,
        }

    def _to_dict(self, app):
        if not app:
            return None
        return {
            "id": app.id,
            "user_id": app.user_id,
            "status": app.status,
            "cover_type": app.cover_type,
            "first_name": app.first_name,
            "middle_name": app.middle_name,
            "surname": app.surname,
            "mobile": app.mobile,
            "email": app.email,
            "vehicle_make": app.vehicle_make,
            "year_of_manufacture": app.year_of_manufacture,
            "cover_start_date": app.cover_start_date,
            "is_rare_model": app.is_rare_model,
            "has_undergone_valuation": app.has_undergone_valuation,
            "vehicle_value_ugx": app.vehicle_value_ugx,
            "quote_id": app.quote_id,
            "created_at": app.created_at.isoformat(),
            "updated_at": app.updated_at.isoformat(),
        }

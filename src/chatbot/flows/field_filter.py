"""
Helper utility for filtering form fields to show only missing/invalid fields.

This improves user experience by not re-asking for data that's already been provided.
Also provides frontend validation rules for real-time validation.
"""

from datetime import date
from typing import Any, Dict, List, Optional


def filter_missing_fields(
    all_fields: List[Dict[str, Any]],
    payload: Dict[str, Any],
    collected_data: Dict[str, Any],
    validation_errors: Optional[Dict[str, str]] = None,
    data_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Filter form fields to show only those that are missing (empty required fields).

    With real-time frontend validation, invalid fields are caught before submission,
    so we only need to filter for missing required fields.

    Args:
        all_fields: Complete list of field definitions
        payload: Current form submission data (empty on first display)
        collected_data: All previously collected data for this flow
        validation_errors: Dictionary of field_name -> error_message for failed validations
        data_key: Optional key in collected_data where field values are stored (e.g., "personal_details")

    Returns:
        Filtered list of fields to display

    Behavior:
        - First visit (empty payload): Show all fields
        - Re-submission: Show only required fields that are still empty
        - With frontend validation: Invalid fields are caught before submission,
          so validation_errors typically only contains missing field errors
    """
    # First visit - show all fields
    if not payload or "_raw" in payload:
        return all_fields

    validation_errors = validation_errors or {}

    # Get the data source for checking existing values
    existing_data = collected_data.get(data_key, {}) if data_key else collected_data

    # If there are no validation errors, return all fields (form will proceed anyway)
    if not validation_errors:
        return all_fields

    # Filter to show only fields that are missing (required but empty)
    filtered_fields = []

    for field in all_fields:
        field_name = field.get("name", "")

        # Include field if it's required and has no value yet
        if field.get("required", False):
            # Check payload first, then existing data, then defaultValue
            value = payload.get(field_name) or existing_data.get(field_name) or field.get("defaultValue", "")

            if not value or (isinstance(value, str) and not value.strip()):
                filtered_fields.append(field)
                continue

        # Also include fields that have validation errors (backup for server-side validation)
        # This handles edge cases where frontend validation was bypassed
        elif field_name in validation_errors:
            filtered_fields.append(field)
            continue

    # If no fields match the criteria, return all fields as fallback
    return filtered_fields if filtered_fields else all_fields


def add_validation_hints_to_fields(
    fields: List[Dict[str, Any]],
    validation_errors: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    """
    Add validation error messages to field definitions for frontend display.

    Args:
        fields: List of field definitions
        validation_errors: Dictionary of field_name -> error_message

    Returns:
        Fields with error hints added
    """
    if not validation_errors:
        return fields

    enhanced_fields = []
    for field in fields:
        field_copy = dict(field)
        field_name = field.get("name", "")

        if field_name in validation_errors:
            field_copy["error"] = validation_errors[field_name]
            field_copy["hasError"] = True

        enhanced_fields.append(field_copy)

    return enhanced_fields


def add_frontend_validation_rules(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Add real-time validation rules to field definitions for frontend validation.

    This enables the frontend to validate fields as users type or when they blur/leave a field,
    providing immediate feedback instead of waiting for form submission.

    Args:
        fields: List of field definitions

    Returns:
        Fields enhanced with validation rules
    """
    enhanced_fields = []

    for field in fields:
        field_copy = dict(field)
        field_name = field.get("name", "")
        field_type = field.get("type", "text")

        # Add validation rules based on field name patterns and types
        validation = {}

        # National ID Number (NIN) - Uganda format
        if "national_id" in field_name.lower() or "nin" in field_name.lower():
            validation["pattern"] = r"^[A-Z]{2}\d{12}$"
            validation["patternMessage"] = "NIN must be 2 letters followed by 12 digits (e.g., CM1234567890AB)"
            field_copy["placeholder"] = field_copy.get("placeholder", "CM1234567890AB")
            field_copy["maxLength"] = 14

        # Phone number - Uganda format
        elif field_type == "tel" or "phone" in field_name.lower() or "mobile" in field_name.lower():
            validation["pattern"] = r"^(\+256|0)?7\d{8}$"
            validation["patternMessage"] = "Phone number must be in format 07XX XXX XXX or +2567XX XXX XXX"
            field_copy["placeholder"] = field_copy.get("placeholder", "07XX XXX XXX")

        # Email validation
        elif field_type == "email" or "email" in field_name.lower():
            validation["pattern"] = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            validation["patternMessage"] = "Please enter a valid email address"
            field_copy["placeholder"] = field_copy.get("placeholder", "example@email.com")

        # Date of Birth - Age validation (18-65 years)
        elif field_name == "dob" or field_name == "date_of_birth" or "birth" in field_name.lower():
            today = date.today()
            max_date = date(today.year - 18, today.month, today.day)  # Must be at least 18
            min_date = date(today.year - 65, today.month, today.day)  # Max 65 years old

            validation["minDate"] = min_date.isoformat()
            validation["maxDate"] = max_date.isoformat()
            validation["minDateMessage"] = "Age cannot be more than 65 years"
            validation["maxDateMessage"] = "You must be at least 18 years old"

            field_copy["max"] = max_date.isoformat()
            field_copy["min"] = min_date.isoformat()

        # Policy/Cover start date - Must be in the future
        elif "policy_start" in field_name.lower() or "cover_start" in field_name.lower():
            tomorrow = date.today()
            validation["minDate"] = tomorrow.isoformat()
            validation["minDateMessage"] = "Start date must be in the future"
            field_copy["min"] = tomorrow.isoformat()

        # Travel dates - Must be in the future
        elif "departure" in field_name.lower() or "travel_start" in field_name.lower():
            tomorrow = date.today()
            validation["minDate"] = tomorrow.isoformat()
            validation["minDateMessage"] = "Travel date must be in the future"
            field_copy["min"] = tomorrow.isoformat()

        # Return date - Must be after departure
        elif "return" in field_name.lower() or "travel_end" in field_name.lower():
            tomorrow = date.today()
            validation["minDate"] = tomorrow.isoformat()
            validation["minDateMessage"] = "Return date must be in the future"
            # Frontend should also validate this is after departure date
            validation["afterField"] = "departure_date"
            validation["afterFieldMessage"] = "Return date must be after departure date"

        # Tax ID / TIN
        elif "tax" in field_name.lower() and ("id" in field_name.lower() or "tin" in field_name.lower()):
            validation["pattern"] = r"^\d{10}$"
            validation["patternMessage"] = "Tax ID must be 10 digits"
            field_copy["maxLength"] = 10

        # Add generic text length validations
        if field_type == "text":
            if "name" in field_name.lower():
                # Names typically 2-50 characters
                validation["minLength"] = field_copy.get("minLength", 2)
                validation["maxLength"] = field_copy.get("maxLength", 50)
                validation["minLengthMessage"] = f"{field_copy.get('label', 'This field')} must be at least 2 characters"
                validation["maxLengthMessage"] = f"{field_copy.get('label', 'This field')} must not exceed 50 characters"

        # Add the validation object if it has rules
        if validation:
            field_copy["validation"] = validation

        enhanced_fields.append(field_copy)

    return enhanced_fields


def filter_already_collected_fields(
    all_fields: List[Dict[str, Any]],
    collected_data: Dict[str, Any],
    previous_step_keys: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Filter out fields that were already collected in previous steps (progressive disclosure).

    This enables multi-step forms where each step only asks for NEW information,
    not re-asking for data already provided in earlier steps.

    Args:
        all_fields: Complete list of field definitions for this step
        collected_data: All data collected in the flow so far
        previous_step_keys: List of data keys from previous steps to check
                           (e.g., ["quick_quote"] to check if data exists in quick_quote)

    Returns:
        Filtered list containing only fields that haven't been collected yet

    Example:
        Step 0 collected: firstName, lastName, email, mobile
        Step 2 defines: first_name, surname, email, mobile_number, national_id, occupation
        Result: Show only national_id, occupation (new fields)
    """
    previous_step_keys = previous_step_keys or []

    # Build a set of field names that already have values from previous steps
    already_collected = set()

    for step_key in previous_step_keys:
        step_data = collected_data.get(step_key, {})
        if isinstance(step_data, dict):
            # Add all keys that have non-empty values
            for key, value in step_data.items():
                if value and (not isinstance(value, str) or value.strip()):
                    already_collected.add(key)

    # Filter fields: only show those that don't have values yet
    new_fields = []

    for field in all_fields:
        field_name = field.get("name", "")

        # Check if this field already has a value from a previous step
        # Look for exact match or common variations (e.g., firstName -> first_name)
        has_value = False

        # Direct match
        if field_name in already_collected:
            has_value = True

        # Check common name variations
        name_variations = _get_field_name_variations(field_name)
        if any(var in already_collected for var in name_variations):
            has_value = True

        # If field doesn't have a value yet, include it
        if not has_value:
            new_fields.append(field)

    return new_fields


def _get_field_name_variations(field_name: str) -> List[str]:
    """
    Generate common variations of a field name for matching.

    Examples:
        first_name -> [firstName, First_Name, FirstName]
        mobile_number -> [mobile, phoneNumber, phone]
        surname -> [lastName, last_name]
    """
    variations = [field_name]

    # Common field mappings
    mapping = {
        "first_name": ["firstName", "First_Name"],
        "surname": ["lastName", "last_name"],
        "middle_name": ["middleName", "Middle_Name"],
        "mobile_number": ["mobile", "phone", "phoneNumber", "phone_number"],
        "email": ["email_address", "Email"],
        "national_id_number": ["national_id", "nin", "NIN"],
    }

    if field_name in mapping:
        variations.extend(mapping[field_name])

    # Also check reverse mappings
    for key, values in mapping.items():
        if field_name in values:
            variations.append(key)
            variations.extend(values)

    return list(set(variations))  # Remove duplicates

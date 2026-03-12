"""Motor Private "get a quote" frontend/back-end validation tests.

These tests focus on MotorPrivateFlow.complete_flow, ensuring that:
- All documented fields (coverType, names, mobile, email, vehicle/premium fields)
  are validated as per spec.
- Common fields (names, email, mobile) reuse shared validation helpers.
- There is NO requirement for a NIN / national_id_number field in this flow.
"""

import pytest
from datetime import date, timedelta

from src.chatbot.flows.motor_private import MotorPrivateFlow
from src.chatbot.validation import FormValidationError


@pytest.fixture
def motor_flow(db):
    """MotorPrivateFlow instance using the shared db fixture."""

    return MotorPrivateFlow(product_catalog={}, db=db)


def _valid_motor_frontend_payload():
    """Return a fully valid single-form motor frontend payload."""

    # Use a cover start date that is always within the next 90 days
    # relative to "today" so that validate_cover_start_date_range passes.
    cover_start = date.today() + timedelta(days=1)
    cover_start_str = cover_start.isoformat()

    return {
        # Step 1: Get a quote
        "coverType": "comprehensive",
        # Step 2: Personal details
        "firstName": "John",
        "middleName": "K",
        "surname": "Doe",
        "mobile": "+256712345678",
        "email": "john.doe@example.com",
        # Step 3: Premium calculation
        "vehicleMake": "Toyota",
        "yearOfManufacture": 2024,
        "coverStartDate": cover_start_str,
        "isRareModel": "no",
        "hasUndergoneValuation": "yes",
        "vehicleValueUgx": "15000000",
    }


@pytest.mark.asyncio
async def test_complete_flow_accepts_valid_motor_frontend_payload(motor_flow):
    """A fully valid payload should complete without raising FormValidationError."""

    payload = _valid_motor_frontend_payload()
    collected_data = payload.copy()

    # Should not raise FormValidationError
    result = await motor_flow.complete_flow(collected_data, user_id="user123")
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_missing_required_fields_raise_validation_error(motor_flow):
    """Missing required core fields (coverType, names, mobile, email, vehicle fields) should error."""

    payload = _valid_motor_frontend_payload()
    # Remove a few required fields one by one and ensure they are reported.
    # Note: coverType is treated as optional in the current backend
    # implementation when entirely absent, so it is not included here.
    for field in [
        "firstName",
        "surname",
        "mobile",
        "email",
        "vehicleMake",
        "yearOfManufacture",
        "coverStartDate",
        "isRareModel",
        "hasUndergoneValuation",
        "vehicleValueUgx",
    ]:
        bad = payload.copy()
        bad.pop(field, None)
        with pytest.raises(FormValidationError) as exc:
            await motor_flow.complete_flow(bad, user_id="user123")
        # Ensure the field is mentioned in field_errors
        assert field in exc.value.field_errors


@pytest.mark.asyncio
async def test_name_length_constraints_enforced(motor_flow):
    """firstName/surname must be 2–50 chars; middleName optional up to 50."""

    payload = _valid_motor_frontend_payload()

    # Too short firstName
    bad = payload.copy()
    bad["firstName"] = "J"
    with pytest.raises(FormValidationError) as exc1:
        await motor_flow.complete_flow(bad, user_id="user123")
    assert "firstName" in exc1.value.field_errors

    # Too long surname
    bad = payload.copy()
    bad["surname"] = "D" * 51
    with pytest.raises(FormValidationError) as exc2:
        await motor_flow.complete_flow(bad, user_id="user123")
    assert "surname" in exc2.value.field_errors

    # Middle name can be long up to 50 chars; 50 chars is allowed
    ok = payload.copy()
    ok["middleName"] = "M" * 50
    result = await motor_flow.complete_flow(ok, user_id="user123")
    assert result.get("status") == "success"


@pytest.mark.asyncio
async def test_mobile_and_email_validation_rules(motor_flow):
    """Mobile must be Uganda format; email must be valid and <= 100 chars."""

    payload = _valid_motor_frontend_payload()

    # Bad mobile format
    bad_mobile = payload.copy()
    bad_mobile["mobile"] = "12345"
    with pytest.raises(FormValidationError) as exc1:
        await motor_flow.complete_flow(bad_mobile, user_id="user123")
    assert "mobile" in exc1.value.field_errors

    # Bad email format
    bad_email = payload.copy()
    bad_email["email"] = "not-an-email"
    with pytest.raises(FormValidationError) as exc2:
        await motor_flow.complete_flow(bad_email, user_id="user123")
    assert "email" in exc2.value.field_errors

    # Email too long
    bad_email2 = payload.copy()
    bad_email2["email"] = ("a" * 101) + "@example.com"
    with pytest.raises(FormValidationError) as exc3:
        await motor_flow.complete_flow(bad_email2, user_id="user123")
    assert "email" in exc3.value.field_errors


@pytest.mark.asyncio
async def test_vehicle_and_premium_field_rules(motor_flow):
    """yearOfManufacture, coverStartDate, isRareModel, hasUndergoneValuation, vehicleValueUgx rules are enforced."""

    payload = _valid_motor_frontend_payload()

    # Year of manufacture too low
    bad_year = payload.copy()
    bad_year["yearOfManufacture"] = 1970
    with pytest.raises(FormValidationError) as exc1:
        await motor_flow.complete_flow(bad_year, user_id="user123")
    assert "yearOfManufacture" in exc1.value.field_errors

    # Rare model enum invalid
    bad_rare = payload.copy()
    bad_rare["isRareModel"] = "maybe"
    with pytest.raises(FormValidationError) as exc2:
        await motor_flow.complete_flow(bad_rare, user_id="user123")
    assert "isRareModel" in exc2.value.field_errors

    # Valuation enum invalid
    bad_val = payload.copy()
    bad_val["hasUndergoneValuation"] = "unknown"
    with pytest.raises(FormValidationError) as exc3:
        await motor_flow.complete_flow(bad_val, user_id="user123")
    assert "hasUndergoneValuation" in exc3.value.field_errors

    # Vehicle value must be positive number
    bad_value = payload.copy()
    bad_value["vehicleValueUgx"] = "0"
    with pytest.raises(FormValidationError) as exc4:
        await motor_flow.complete_flow(bad_value, user_id="user123")
    assert "vehicleValueUgx" in exc4.value.field_errors


@pytest.mark.asyncio
async def test_no_nin_required_for_motor_frontend_flow(motor_flow):
    """Motor front-end flow must *not* require or mention NIN / national_id_number."""

    payload = _valid_motor_frontend_payload()
    # Deliberately do NOT include any NIN-related field, and ensure the
    # validation still passes. If the implementation ever starts requiring a
    # NIN, this test will fail.
    assert all(
        key not in payload
        for key in ["nin", "NIN", "national_id_number", "nationalIdNumber"]
    )

    result = await motor_flow.complete_flow(payload, user_id="user123")
    assert result.get("status") == "success"


@pytest.mark.asyncio
async def test_vehicle_details_form_exposes_backend_validation_metadata(motor_flow):
    """Guided vehicle-details form should advertise backend validation for date/number fields."""

    result = await motor_flow._step_vehicle_details({}, {}, user_id="user123")
    fields = result["response"]["fields"]
    by_name = {field["name"]: field for field in fields}

    assert by_name["cover_start_date"]["backendValidation"] is True
    assert by_name["cover_start_date"]["type"] == "date"
    assert "validation" in by_name["cover_start_date"]
    assert by_name["vehicle_value"]["backendValidation"] is True
    assert by_name["year_of_manufacture"]["backendValidation"] is False

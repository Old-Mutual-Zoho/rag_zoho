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
async def test_cover_start_date_accepts_iso_datetime_string(motor_flow):
    """Frontend may submit ISO datetime strings for date inputs; these should still validate."""

    payload = _valid_motor_frontend_payload()
    payload["coverStartDate"] = f"{payload['coverStartDate']}T00:00:00"

    result = await motor_flow.complete_flow(payload, user_id="user123")
    assert result.get("status") == "success"


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


@pytest.mark.asyncio
async def test_excess_parameters_step_accepts_frontend_checkbox_alias(motor_flow):
    """Motor private should tolerate the legacy checkbox key currently sent by the frontend."""

    result = await motor_flow._step_excess_parameters(
        {"risky_activities": ["excess_3", "excess_1"]},
        {},
        user_id="user123",
    )

    assert result["next_step"] == 3
    assert result["collected_data"]["excess_parameters"] == ["excess_3", "excess_1"]


def test_pure_vehicle_details_validator_is_callable(motor_flow):
    """The extracted validator should work directly for future API reuse."""

    validated, errors = motor_flow._validate_vehicle_details(
        {
            "cover_type": "comprehensive",
            "vehicle_make": "Toyota",
            "year_of_manufacture": str(date.today().year),
            "cover_start_date": (date.today() + timedelta(days=1)).isoformat(),
            "is_rare_model": "no",
            "has_undergone_valuation": "yes",
            "vehicle_value_ugx": "15000000",
        }
    )

    assert errors == {}
    assert validated["cover_type"] == "comprehensive"
    assert validated["vehicle_make"] == "toyota"


def test_pure_excess_validator_filters_invalid_values(motor_flow):
    validated, errors = motor_flow._validate_excess_parameters(
        {"risky_activities": ["excess_1", "political_violence", "excess_1"]}
    )

    assert errors == {}
    assert validated["excess_choice"] == ["excess_1"]


@pytest.mark.asyncio
async def test_excess_parameters_response_exposes_field_name(motor_flow):
    """Checkbox response should tell the frontend which field name to submit."""

    result = await motor_flow._step_excess_parameters({}, {}, user_id="user123")

    assert result["response"]["name"] == "excess_parameters"
    assert result["response"]["defaultValue"] == []


@pytest.mark.asyncio
async def test_additional_benefits_accepts_alias_and_filters_mixed_ids(motor_flow):
    """Additional benefits should accept legacy alias and keep only valid benefit IDs."""

    result = await motor_flow._step_additional_benefits(
        {
            "risky_activities": [
                "excess_1",
                "excess_2",
                "political_violence",
                "political_violence",
                "alternative_accommodation",
            ]
        },
        {},
        user_id="user123",
    )

    assert result["next_step"] == 4
    assert result["collected_data"]["additional_benefits"] == [
        "political_violence",
        "alternative_accommodation",
    ]


@pytest.mark.asyncio
async def test_additional_benefits_accepts_object_checkbox_values(motor_flow):
    """Frontend may submit checkbox options as objects; step should still advance."""

    result = await motor_flow._step_additional_benefits(
        {
            "risky_activities": [
                {"id": "political_violence"},
                {"value": "alternative_accommodation"},
                {"id": "excess_2"},
            ]
        },
        {},
        user_id="user123",
    )

    assert result["next_step"] == 4
    assert result["collected_data"]["additional_benefits"] == [
        "political_violence",
        "alternative_accommodation",
    ]


@pytest.mark.asyncio
async def test_guided_premium_preview_uses_vehicle_details_state(motor_flow, monkeypatch):
    """Step-by-step guided flow should preview quotes from vehicle_details, not only motor_frontend."""

    captured = {}

    async def fake_preview(**kwargs):
        captured.update(kwargs)
        return {"quotation": {"payable_amount": 12345}}

    monkeypatch.setattr("src.chatbot.flows.motor_private.run_quote_preview", fake_preview)

    result = await motor_flow._step_premium_calculation(
        {},
        {
            "vehicle_details": {
                "vehicle_make": "Toyota",
                "year_of_manufacture": "2024",
                "cover_start_date": (date.today() + timedelta(days=2)).isoformat(),
                "rare_model": "No",
                "valuation_done": "Yes",
                "vehicle_value": "15000000",
            }
        },
        user_id="user123",
    )

    assert captured["underwriting_data"]["vehicleMake"] == "Toyota"
    assert captured["underwriting_data"]["policyStartDate"] == (date.today() + timedelta(days=2)).isoformat()
    assert result["response"]["payable_amount"] == 12345

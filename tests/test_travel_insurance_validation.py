"""
Validation-centric tests for Travel Insurance flow using validators from validation.py.

Run:
    pytest tests/test_travel_insurance_validation.py -q
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import os
import sys
import pytest

# Ensure project root is on sys.path so `src` imports resolve
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.chatbot.validation import FormValidationError  # noqa: E402
from src.chatbot.flows.travel_insurance import TravelInsuranceFlow  # noqa: E402


def _make_mock_db():
    quotes = []

    def create_quote(**kwargs):
        q = SimpleNamespace(
            id="mock-quote-ti-1",
            premium_amount=kwargs.get("premium_amount", 0),
            product_id=kwargs.get("product_id", "travel_insurance"),
            product_name=kwargs.get("product_name", "Travel Insurance"),
        )
        quotes.append(q)
        return q

    def get_quote(quote_id):
        return next((q for q in quotes if str(q.id) == str(quote_id)), None)

    db = MagicMock()
    db.create_quote = create_quote
    db.get_quote = get_quote
    return db


@pytest.fixture
def flow():
    db = _make_mock_db()
    return TravelInsuranceFlow(product_catalog=MagicMock(), db=db)


@pytest.mark.asyncio
async def test_about_you_invalid_email_and_phone_raises(flow):
    payload = {
        "first_name": "Jane",
        "surname": "Doe",
        "email": "not-an-email",
        "phone_number": "abcd",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_about_you(payload, {}, "user-1")
    err = exc.value.field_errors
    assert "email" in err
    assert "phone_number" in err


@pytest.mark.asyncio
async def test_travel_party_return_before_departure_raises(flow):
    payload = {
        "travel_party": "myself_only",
        "num_travellers_18_69": 1,
        "departure_country": "Uganda",
        "destination_country": "Portugal",
        "departure_date": "2026-03-08",
        "return_date": "2026-03-03",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_travel_party_and_trip(payload, {}, "user-1")
    err = exc.value.field_errors
    assert "return_date" in err


@pytest.mark.asyncio
async def test_travel_party_ignores_non_travel_payload(flow):
    payload = {"product_id": "worldwide_essential", "action": "select_cover"}

    result = await flow._step_travel_party_and_trip(payload, {}, "user-1")

    assert result.get("response", {}).get("type") == "form"
    field_names = [field["name"] for field in result.get("response", {}).get("fields", [])]
    assert "travel_party" in field_names
    assert "departure_date" in field_names
    assert result.get("next_step") == 3


@pytest.mark.asyncio
async def test_travel_party_country_select_options_use_value_label(flow):
    result = await flow._step_travel_party_and_trip({}, {}, "user-1")
    fields = result.get("response", {}).get("fields", [])

    departure_field = next(field for field in fields if field.get("name") == "departure_country")
    destination_field = next(field for field in fields if field.get("name") == "destination_country")

    departure_options = departure_field.get("options", [])
    destination_options = destination_field.get("options", [])

    assert departure_options and isinstance(departure_options[0], dict)
    assert "value" in departure_options[0]
    assert "label" in departure_options[0]

    assert destination_options and isinstance(destination_options[0], dict)
    assert "value" in destination_options[0]
    assert "label" in destination_options[0]


@pytest.mark.asyncio
async def test_travel_party_selection_only_renders_myself_dob(flow):
    result = await flow._step_travel_party_and_trip({"travel_party": "myself_only"}, {}, "user-1")
    fields = {field["name"] for field in result.get("response", {}).get("fields", [])}
    assert "traveller_1_date_of_birth" in fields
    assert "traveller_2_date_of_birth" not in fields
    assert "total_travellers" not in fields


@pytest.mark.asyncio
async def test_travel_party_selection_only_renders_two_dobs(flow):
    result = await flow._step_travel_party_and_trip(
        {"travel_party": "myself_and_someone_else"}, {}, "user-1"
    )
    fields = {field["name"] for field in result.get("response", {}).get("fields", [])}
    assert "traveller_1_date_of_birth" in fields
    assert "traveller_2_date_of_birth" in fields
    assert "total_travellers" not in fields


@pytest.mark.asyncio
async def test_travel_party_selection_only_renders_group_counts(flow):
    result = await flow._step_travel_party_and_trip({"travel_party": "group"}, {}, "user-1")
    fields = {field["name"] for field in result.get("response", {}).get("fields", [])}
    assert "traveller_1_date_of_birth" not in fields
    assert "traveller_2_date_of_birth" not in fields
    assert "total_travellers" in fields
    assert "num_travellers_18_69" in fields


@pytest.mark.asyncio
async def test_travel_party_myself_only_requires_primary_dob(flow):
    payload = {
        "travel_party": "myself_only",
        "departure_country": "Uganda",
        "destination_country": "Portugal",
        "departure_date": "2026-03-08",
        "return_date": "2026-03-10",
    }

    with pytest.raises(FormValidationError) as exc:
        await flow._step_travel_party_and_trip(payload, {}, "user-1")

    err = exc.value.field_errors
    assert "traveller_1_date_of_birth" in err


@pytest.mark.asyncio
async def test_travel_party_myself_and_someone_else_requires_second_dob(flow):
    payload = {
        "travel_party": "myself_and_someone_else",
        "traveller_1_date_of_birth": "1990-01-01",
        "departure_country": "Uganda",
        "destination_country": "Portugal",
        "departure_date": "2026-03-08",
        "return_date": "2026-03-10",
    }

    with pytest.raises(FormValidationError) as exc:
        await flow._step_travel_party_and_trip(payload, {}, "user-1")

    err = exc.value.field_errors
    assert "traveller_2_date_of_birth" in err


@pytest.mark.asyncio
async def test_travel_party_group_total_must_match_age_ranges(flow):
    payload = {
        "travel_party": "group",
        "total_travellers": 4,
        "num_travellers_18_69": 1,
        "num_travellers_0_17": 1,
        "num_travellers_70_75": 0,
        "num_travellers_76_80": 0,
        "num_travellers_81_85": 0,
        "departure_country": "Uganda",
        "destination_country": "Portugal",
        "departure_date": "2026-03-08",
        "return_date": "2026-03-10",
    }

    with pytest.raises(FormValidationError) as exc:
        await flow._step_travel_party_and_trip(payload, {}, "user-1")

    err = exc.value.field_errors
    assert "total_travellers" in err


@pytest.mark.asyncio
async def test_passport_upload_missing_file_ref_raises(flow):
    # Provide an empty field to enter the validation branch
    payload = {"passport_file_ref": ""}
    with pytest.raises(FormValidationError) as exc:
        await flow._step_upload_passport(payload, {}, "user-1")
    err = exc.value.field_errors
    # Field expected by require_str in upload step
    assert "passport_file_ref" in err


# ---------------------------------------------------------------------------
# About You – valid and field-missing paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_you_valid_saves_data_and_returns_travel_party_form(flow):
    payload = {
        "first_name": "Alice",
        "surname": "Smith",
        "phone_number": "0771234567",
        "email": "alice@example.com",
    }
    result = await flow._step_about_you(payload, {}, "user-1")
    # Valid submission auto-advances to travel_party_and_trip form
    assert result["response"]["type"] == "form"
    assert result["next_step"] == 3
    data = result["collected_data"]
    assert data["about_you"]["first_name"] == "Alice"
    assert data["about_you"]["surname"] == "Smith"


@pytest.mark.asyncio
async def test_about_you_missing_first_name_raises(flow):
    payload = {
        "surname": "Smith",
        "phone_number": "0771234567",
        "email": "alice@example.com",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_about_you(payload, {}, "user-1")
    assert "first_name" in exc.value.field_errors


@pytest.mark.asyncio
async def test_about_you_missing_surname_raises(flow):
    payload = {
        "first_name": "Alice",
        "phone_number": "0771234567",
        "email": "alice@example.com",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_about_you(payload, {}, "user-1")
    assert "surname" in exc.value.field_errors


# ---------------------------------------------------------------------------
# Product selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_product_selection_valid_id_selects_product(flow):
    payload = {"product_id": "schengen_essential"}
    result = await flow._step_product_selection(payload, {}, "user-1")
    assert result["next_step"] == 2
    assert result["collected_data"]["selected_product"]["id"] == "schengen_essential"


@pytest.mark.asyncio
async def test_product_selection_invalid_id_raises(flow):
    payload = {"product_id": "nonexistent_product"}
    with pytest.raises(FormValidationError) as exc:
        await flow._step_product_selection(payload, {}, "user-1")
    assert "product_id" in exc.value.field_errors


@pytest.mark.asyncio
async def test_product_selection_no_payload_returns_product_cards(flow):
    result = await flow._step_product_selection({}, {}, "user-1")
    assert result["response"]["type"] == "product_cards"
    assert result["next_step"] == 2
    products = result["response"]["products"]
    assert any(p["id"] == "worldwide_essential" for p in products)


# ---------------------------------------------------------------------------
# Data consent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_consent_missing_terms_raises(flow):
    payload = {"terms_and_conditions_agreed": False, "consent_marketing": True}
    with pytest.raises(FormValidationError) as exc:
        await flow._step_data_consent(payload, {}, "user-1")
    assert "terms_and_conditions_agreed" in exc.value.field_errors


@pytest.mark.asyncio
async def test_data_consent_valid_saves_and_advances(flow):
    payload = {"terms_and_conditions_agreed": True}
    data = {}
    result = await flow._step_data_consent(payload, data, "user-1")
    assert result["next_step"] == 4
    assert data["data_consent"]["terms_and_conditions_agreed"] is True


# ---------------------------------------------------------------------------
# Traveller details
# ---------------------------------------------------------------------------

_VALID_TRAVELLER = {
    "first_name": "Bob",
    "surname": "Jones",
    "nationality_type": "ugandan",
    "passport_number": "A1234567",
    "date_of_birth": "1988-06-15",
    "occupation": "Engineer",
    "phone_number": "0781234567",
    "email": "bob@example.com",
    "postal_address": "P.O. Box 1",
    "town_city": "Kampala",
}


def _trip_data(total: int = 1) -> dict:
    return {"travel_party_and_trip": {"total_travellers": total}}


@pytest.mark.asyncio
async def test_traveller_details_valid_single_advances_to_emergency_contact(flow):
    data = _trip_data(1)
    result = await flow._step_traveller_details(_VALID_TRAVELLER, data, "user-1")
    # After one traveller, advances to emergency contact (next_step 6)
    assert result["next_step"] == 6
    assert len(data["travellers"]) == 1
    assert data["travellers"][0]["first_name"] == "Bob"


@pytest.mark.asyncio
async def test_traveller_details_missing_passport_raises(flow):
    bad = {**_VALID_TRAVELLER, "passport_number": ""}
    with pytest.raises(FormValidationError) as exc:
        await flow._step_traveller_details(bad, _trip_data(1), "user-1")
    assert "passport_number" in exc.value.field_errors


@pytest.mark.asyncio
async def test_traveller_details_missing_nationality_raises(flow):
    bad = {**_VALID_TRAVELLER, "nationality_type": ""}
    with pytest.raises(FormValidationError) as exc:
        await flow._step_traveller_details(bad, _trip_data(1), "user-1")
    assert "nationality_type" in exc.value.field_errors


@pytest.mark.asyncio
async def test_traveller_details_invalid_dob_raises(flow):
    bad = {**_VALID_TRAVELLER, "date_of_birth": "not-a-date"}
    with pytest.raises(FormValidationError) as exc:
        await flow._step_traveller_details(bad, _trip_data(1), "user-1")
    assert "date_of_birth" in exc.value.field_errors


@pytest.mark.asyncio
async def test_traveller_details_two_travellers_first_stays_on_step(flow):
    data = _trip_data(2)
    result = await flow._step_traveller_details(_VALID_TRAVELLER, data, "user-1")
    # One collected, one still needed – should stay on traveller form (step 4)
    assert result["next_step"] == 4
    assert len(data["travellers"]) == 1


@pytest.mark.asyncio
async def test_traveller_details_two_travellers_second_advances(flow):
    data = _trip_data(2)
    await flow._step_traveller_details(_VALID_TRAVELLER, data, "user-1")
    assert len(data["travellers"]) == 1
    second = {**_VALID_TRAVELLER, "first_name": "Carol", "email": "carol@example.com"}
    result = await flow._step_traveller_details(second, data, "user-1")
    assert result["next_step"] == 6
    assert len(data["travellers"]) == 2


@pytest.mark.asyncio
async def test_traveller_details_prefilled_from_about_you(flow):
    data = {
        **_trip_data(1),
        "about_you": {
            "first_name": "Pre",
            "surname": "Filled",
            "phone_number": "0771111111",
            "email": "pre@example.com",
        },
    }
    result = await flow._step_traveller_details({}, data, "user-1")
    fields = {f["name"]: f for f in result["response"]["fields"]}
    assert fields["first_name"].get("defaultValue") == "Pre"
    assert fields["surname"].get("defaultValue") == "Filled"


# ---------------------------------------------------------------------------
# Emergency contact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_contact_valid_saves_and_advances(flow):
    payload = {
        "ec_surname": "Doe",
        "ec_relationship": "Spouse",
        "ec_phone_number": "0771234568",
        "ec_email": "doe@example.com",
    }
    data = {}
    result = await flow._step_emergency_contact(payload, data, "user-1")
    assert result["next_step"] == 6
    assert data["emergency_contact"]["surname"] == "Doe"
    assert data["emergency_contact"]["relationship"] == "Spouse"


@pytest.mark.asyncio
async def test_emergency_contact_missing_surname_raises(flow):
    payload = {
        "ec_surname": "",
        "ec_relationship": "Parent",
        "ec_phone_number": "0771234568",
        "ec_email": "doe@example.com",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_emergency_contact(payload, {}, "user-1")
    assert "ec_surname" in exc.value.field_errors


@pytest.mark.asyncio
async def test_emergency_contact_invalid_relationship_raises(flow):
    payload = {
        "ec_surname": "Doe",
        "ec_relationship": "Stranger",
        "ec_phone_number": "0771234568",
        "ec_email": "doe@example.com",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_emergency_contact(payload, {}, "user-1")
    assert "ec_relationship" in exc.value.field_errors


# ---------------------------------------------------------------------------
# Bank details (optional)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bank_details_optional_empty_payload_skips(flow):
    result = await flow._step_bank_details_optional({}, {}, "user-1")
    assert result["next_step"] == 7
    assert result["response"]["type"] == "form"


@pytest.mark.asyncio
async def test_bank_details_optional_partial_raises(flow):
    payload = {
        "bank_name": "Stanbic",
        "account_holder_name": "",
        "account_number": "123",
        "bank_branch": "",
        "account_currency": "UGX",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_bank_details_optional(payload, {}, "user-1")
    err = exc.value.field_errors
    assert "account_holder_name" in err or "bank_branch" in err


@pytest.mark.asyncio
async def test_bank_details_optional_full_valid_advances(flow):
    payload = {
        "bank_name": "Stanbic",
        "account_holder_name": "Bob Jones",
        "account_number": "123456789",
        "bank_branch": "Kampala",
        "account_currency": "UGX",
    }
    data = {}
    result = await flow._step_bank_details_optional(payload, data, "user-1")
    assert result["next_step"] == 7
    assert data["bank_details"]["bank_name"] == "Stanbic"


# ---------------------------------------------------------------------------
# Passport upload – valid path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passport_upload_valid_file_ref_saves_and_advances(flow):
    payload = {"passport_file_ref": "upload://abc123"}
    data = {}
    result = await flow._step_upload_passport(payload, data, "user-1")
    assert result["next_step"] == 8
    assert data["passport_upload"]["file_ref"] == "upload://abc123"


# ---------------------------------------------------------------------------
# Premium summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_premium_summary_returns_premium_type_with_actions(flow, monkeypatch):
    monkeypatch.setattr(
        "src.chatbot.flows.travel_insurance.premium_service.calculate_sync",
        lambda *_a, **_kw: {"total_usd": 50, "total_ugx": 185000, "breakdown": {"days": 6}},
    )
    data = {
        "selected_product": {"id": "worldwide_essential", "label": "Worldwide Essential"},
        "travel_party_and_trip": {
            "travel_party": "myself_only",
            "departure_date": "2026-03-08",
            "return_date": "2026-03-14",
        },
    }
    result = await flow._step_premium_summary({}, data, "user-1")
    assert result["response"]["type"] == "premium_summary"
    assert result["next_step"] == 9
    actions = [a["type"] for a in result["response"]["actions"]]
    assert "proceed_to_pay" in actions
    assert result["response"]["total_premium_usd"] == 50

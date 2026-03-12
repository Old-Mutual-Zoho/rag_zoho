"""
Validation-centric tests for Personal Accident flow using validators from validation.py.

Run:
    pytest tests/test_personal_accident_validation.py -q
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import os
import sys
import pytest

# Ensure project root is on sys.path so `src` imports resolve
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.chatbot.validation import FormValidationError, parse_date_flexible  # noqa: E402
from src.chatbot.flows.personal_accident import PersonalAccidentFlow  # noqa: E402
from src.chatbot.flows.payment import PaymentFlow  # noqa: E402


def _make_mock_db():
    quotes = []

    def create_quote(**kwargs):
        q = SimpleNamespace(
            id="mock-quote-pa-1",
            premium_amount=kwargs.get("premium_amount", 0),
            product_id=kwargs.get("product_id", "personal_accident"),
            product_name=kwargs.get("product_name", "Personal Accident"),
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
    return PersonalAccidentFlow(product_catalog=MagicMock(), db=db)


@pytest.mark.asyncio
async def test_quick_quote_invalid_age_raises(flow):
    # DOB clearly under 18
    payload = {
        "firstName": "Jane",
        "lastName": "Doe",
        "mobile": "0772123456",
        "email": "jane@example.com",
        "dob": str(date.today().replace(year=date.today().year - 10)),
        "policyStartDate": str(date.today().replace(day=date.today().day + 1)),
        "coverLimitAmountUgx": "5000000",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_quick_quote(payload, {}, "user-1")
    err = exc.value.field_errors
    assert "dob" in err


@pytest.mark.asyncio
async def test_quick_quote_invalid_cover_limit_raises(flow):
    payload = {
        "firstName": "Jane",
        "lastName": "Doe",
        "mobile": "0772123456",
        "email": "jane@example.com",
        "dob": "1990-01-01",
        "policyStartDate": str(date.today().replace(day=date.today().day + 1)),
        "coverLimitAmountUgx": "3000000",  # not in allowed set
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_quick_quote(payload, {}, "user-1")
    err = exc.value.field_errors
    assert "coverLimitAmountUgx" in err


def test_parse_date_flexible_accepts_personal_accident_formats():
    assert parse_date_flexible("1990-01-15") == date(1990, 1, 15)
    assert parse_date_flexible("1990-01-15T00:00:00") == date(1990, 1, 15)
    assert parse_date_flexible("01/15/1990") == date(1990, 1, 15)


@pytest.mark.asyncio
async def test_quick_quote_invalid_mobile_and_email_raises(flow):
    payload = {
        "firstName": "Jane",
        "lastName": "Doe",
        "mobile": "123",
        "email": "no-at",
        "dob": "1995-01-01",
        "policyStartDate": str(date.today().replace(day=date.today().day + 1)),
        "coverLimitAmountUgx": "5000000",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_quick_quote(payload, {}, "user-1")
    err = exc.value.field_errors
    assert "mobile" in err
    assert "email" in err


@pytest.mark.asyncio
async def test_personal_details_invalid_nin_raises(flow):
    payload = {
        "surname": "Doe",
        "first_name": "Jane",
        "middle_name": "",
        "email": "jane@example.com",
        "mobile_number": "0772123456",
        "national_id_number": "AB12",  # invalid format
        "nationality": "Ugandan",
        "tax_identification_number": "",
        "occupation": "Engineer",
        "gender": "Female",
        "country_of_residence": "Uganda",
        "physical_address": "Kampala",
    }
    with pytest.raises(FormValidationError) as exc:
        await flow._step_personal_details(payload, {}, "user-1")
    err = exc.value.field_errors
    assert "national_id_number" in err


@pytest.mark.asyncio
async def test_personal_details_render_stays_on_current_step_and_has_gender_options(flow):
    data = {
        "quick_quote": {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@example.com",
            "mobile": "0772123456",
        }
    }

    result = await flow._step_personal_details({}, data, "user-1")

    assert result["next_step"] == 2
    gender_field = next(field for field in result["response"]["fields"] if field["name"] == "gender")
    assert gender_field["type"] == "select"
    assert gender_field["options"] == ["Male", "Female", "Other"]


@pytest.mark.asyncio
async def test_upload_national_id_accepts_ref_value_and_returns_confirmation(flow):
    data = {
        "quick_quote": {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@example.com",
            "mobile": "0772123456",
            "dob": "1990-01-01",
            "policy_start_date": str(date.today().replace(day=date.today().day + 1)),
            "cover_limit_ugx": 5000000,
        },
        "next_of_kin": {
            "nok_first_name": "John",
            "nok_last_name": "Doe",
            "nok_relationship": "Brother",
            "nok_phone_number": "0772123456",
        },
    }

    result = await flow._step_upload_national_id({"ref_value": "file-123"}, data, "user-1")

    assert result["response"]["type"] == "confirmation"
    assert result["next_step"] == 8
    assert result["collected_data"]["national_id_upload"]["file_ref"] == "file-123"


@pytest.mark.asyncio
async def test_payment_flow_rehydrates_quote_when_session_quote_is_string():
    quote = SimpleNamespace(
        id="quote-1",
        premium_amount=781.25,
        product_id="personal_accident",
        underwriting_data={"mobile": "0772123456"},
    )

    db = MagicMock()
    db.get_quote.return_value = quote

    flow = PaymentFlow(db)

    async def _fake_initiate_payment(*, provider, request):
        return SimpleNamespace(
            reference="pay-1",
            provider_reference="prov-1",
            status=SimpleNamespace(value="PENDING"),
        )

    flow.payment_service.initiate_payment = _fake_initiate_payment

    result = await flow.process_step(
        user_input={"provider": "mtn", "phone_number": "0772123456"},
        current_step=2,
        collected_data={
            "quote": "quote-1",
            "quote_id": "quote-1",
            "payment_method": "mobile_money",
        },
        user_id="user-1",
    )

    assert result["response"]["type"] == "payment_initiated"
    assert result["data"]["payment_reference"] == "pay-1"

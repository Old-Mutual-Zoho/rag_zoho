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

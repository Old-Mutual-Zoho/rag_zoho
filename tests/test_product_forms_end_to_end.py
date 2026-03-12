from fastapi.testclient import TestClient

from src.api.main import app
from src.chatbot.dependencies import api_key_protection


client = TestClient(app)


def _auth_bypass():
    return None


def test_personal_accident_full_form_creates_quote():
    app.dependency_overrides[api_key_protection] = _auth_bypass
    try:
        payload = {
            "user_id": "256771111111",
            "data": {
                "first_name": "Monica",
                "surname": "Auma",
                "email": "monica@example.com",
                "mobile_number": "0771111111",
                "national_id_number": "CF1234567890AB",
                "nationality": "Ugandan",
                "occupation": "Engineer",
                "gender": "Female",
                "country_of_residence": "Uganda",
                "physical_address": "Kampala",
                "nok_first_name": "Paul",
                "nok_last_name": "Auma",
                "nok_relationship": "Brother",
                "nok_address": "Kampala",
                "nok_phone_number": "0771222333",
                "cover_limit_amount_ugx": "10000000",
                "has_previous_pa_policy": "no",
                "has_physical_disability": "no",
                "engage_in_risky_activities": "no",
                "national_id_file_ref": "file-ref-1"
            }
        }

        response = client.post("/api/v1/forms/personal-accident/full", json=payload)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["quote_id"]
        assert body["product_name"] == "Personal Accident"
        assert body["monthly_premium"] > 0
        assert body["annual_premium"] > 0
        assert body["sum_assured"] > 0
    finally:
        app.dependency_overrides.pop(api_key_protection, None)


def test_motor_private_full_form_creates_quote():
    app.dependency_overrides[api_key_protection] = _auth_bypass
    try:
        payload = {
            "user_id": "256772222222",
            "data": {
                "firstName": "Moses",
                "middleName": "K",
                "surname": "Okello",
                "mobile": "0772222222",
                "email": "moses@example.com",
                "coverType": "comprehensive",
                "vehicleMake": "Toyota",
                "yearOfManufacture": 2020,
                "coverStartDate": "2026-04-15",
                "isRareModel": "no",
                "hasUndergoneValuation": "yes",
                "vehicleValueUgx": 45000000
            }
        }

        response = client.post("/api/v1/forms/motor-private/full", json=payload)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["quote_id"]
        assert body["product_name"] == "Motor Private"
        assert body["total_premium"] > 0
        assert body["breakdown"]["total"] == body["total_premium"]
    finally:
        app.dependency_overrides.pop(api_key_protection, None)


def test_travel_insurance_full_form_creates_quote():
    app.dependency_overrides[api_key_protection] = _auth_bypass
    try:
        payload = {
            "user_id": "256773333333",
            "data": {
                "product_id": "worldwide_essential",
                "first_name": "Ruth",
                "middle_name": "N",
                "surname": "Atim",
                "phone_number": "0773333333",
                "email": "ruth@example.com",
                "travel_party": "myself_only",
                "num_travellers_18_69": 1,
                "num_travellers_0_17": 0,
                "num_travellers_70_75": 0,
                "num_travellers_76_80": 0,
                "num_travellers_81_85": 0,
                "departure_country": "Uganda",
                "destination_country": "Kenya",
                "departure_date": "2026-04-10",
                "return_date": "2026-04-15",
                "terms_and_conditions_agreed": True,
                "consent_data_outside_uganda": True,
                "consent_child_data": False,
                "consent_marketing": False,
                "passport_number": "B1234567",
                "date_of_birth": "1992-05-10",
                "occupation": "Designer",
                "postal_address": "P.O. Box 123, Kampala",
                "town_city": "Kampala",
                "office_number": "0414000000",
                "ec_surname": "Akena",
                "ec_relationship": "Sister",
                "ec_phone_number": "0773444444",
                "ec_email": "family@example.com"
            }
        }

        response = client.post("/api/v1/forms/travel-insurance/full", json=payload)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["quote_id"]
        assert body["product_name"] == "Travel Insurance"
        assert body["total_premium_ugx"] > 0
        assert body["total_premium_usd"] > 0
        assert body["breakdown"]["days"] == 6
    finally:
        app.dependency_overrides.pop(api_key_protection, None)


def test_serenicare_full_form_creates_quote():
    app.dependency_overrides[api_key_protection] = _auth_bypass
    try:
        payload = {
            "user_id": "256774444444",
            "data": {
                "first_name": "Grace",
                "middle_name": "A",
                "surname": "Nabirye",
                "phone_number": "0774444444",
                "email": "grace@example.com",
                "plan_option": "classic",
                "optional_benefits": ["dental", "optical"],
                "has_condition": False,
                "date_of_birth": "1988-11-03",
                "include_spouse": False,
                "include_children": True,
                "add_another_main_member": False
            }
        }

        response = client.post("/api/v1/forms/serenicare/full", json=payload)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["quote_id"]
        assert body["product_name"] == "Serenicare"
        assert body["monthly_premium"] > 0
        assert body["annual_premium"] == body["monthly_premium"] * 12
        assert body["breakdown"]["plan_id"] == "classic"
    finally:
        app.dependency_overrides.pop(api_key_protection, None)

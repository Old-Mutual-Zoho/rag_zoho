"""Real premium client.

Current implementation is rule-based and mirrors existing flow logic exactly,
while preserving each product output structure.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional

from src.integrations.contracts.premium import PremiumContract


class RealPremiumClient(PremiumContract):
    """Real premium client (ready for future HTTP integration)."""

    async def calculate_premium(self, product_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.calculate_premium_sync(product_key, payload)

    def calculate_premium_sync(self, product_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_product_key(product_key)

        if normalized == "personal_accident":
            return self._calculate_personal_accident_premium(payload)
        if normalized == "serenicare":
            return self._calculate_serenicare_premium(payload)
        if normalized == "travel_insurance":
            return self._calculate_travel_premium(payload)
        if normalized == "motor_private":
            return self._calculate_motor_private_premium(payload)

        raise ValueError(f"Unsupported product_key for premium calculation: {product_key}")

    @staticmethod
    def _normalize_product_key(product_key: str) -> str:
        normalized = str(product_key or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "travel": "travel_insurance",
            "travel_insurance": "travel_insurance",
            "personal_accident": "personal_accident",
            "motor_private": "motor_private",
            "serenicare": "serenicare",
        }
        if normalized not in aliases:
            raise ValueError(f"Unsupported product_key for premium calculation: {product_key}")
        return aliases[normalized]

    @staticmethod
    def _calculate_personal_accident_premium(payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else (payload if isinstance(payload, dict) else {})
        sum_assured = int(payload.get("sum_assured") or 0)

        base_rate = Decimal("0.0015")
        annual = Decimal(sum_assured) * base_rate

        breakdown: Dict[str, Any] = {"base_annual": float(annual)}

        dob: Optional[date] = None
        try:
            dob_str = ""
            if isinstance(data, dict):
                dob_str = str(data.get("dob") or "")
                if not dob_str:
                    q = data.get("quick_quote") or {}
                    dob_str = str((q or {}).get("dob") or "")
            if dob_str:
                dob = date.fromisoformat(dob_str)
        except Exception:
            dob = None

        if dob:
            today = date.today()
            age = today.year - dob.year - (1 if (today.month, today.day) < (dob.month, dob.day) else 0)

            if age < 25:
                modifier = Decimal("1.25")
                loading = annual * (modifier - 1)
                annual += loading
                breakdown["age_loading"] = float(loading)
            elif age > 60:
                modifier = Decimal("1.20")
                loading = annual * (modifier - 1)
                annual += loading
                breakdown["age_loading"] = float(loading)

        risky_selected = []
        if isinstance(data, dict):
            risky = data.get("risky_activities") or {}
            risky_selected = risky.get("selected") or []
        if isinstance(risky_selected, list) and len(risky_selected) > 0:
            loading = annual * Decimal("0.10")
            annual += loading
            breakdown["risky_activities_loading"] = float(loading)

        monthly = annual / 12

        return {
            "annual": float(annual.quantize(Decimal("0.01"))),
            "monthly": float(monthly.quantize(Decimal("0.01"))),
            "breakdown": breakdown,
        }

    @staticmethod
    def _calculate_serenicare_premium(payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else (payload if isinstance(payload, dict) else {})
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}

        plan_id = (plan or {}).get("id", "essential")

        base_by_plan = {
            "essential": Decimal("50000"),
            "classic": Decimal("80000"),
            "comprehensive": Decimal("120000"),
            "premium": Decimal("180000"),
        }
        base = base_by_plan.get(plan_id, base_by_plan["essential"])

        optional_prices = {
            "outpatient": Decimal("15000"),
            "maternity": Decimal("20000"),
            "dental": Decimal("8000"),
            "optical": Decimal("7000"),
            "covid19": Decimal("5000"),
        }

        selected = data.get("optional_benefits") or []
        if isinstance(selected, str):
            selected = [s.strip() for s in selected.split(",") if s.strip()]

        breakdown: Dict[str, Any] = {
            "base": float(base),
            "plan_id": plan_id,
        }

        opts_total = Decimal("0")
        for opt in selected:
            if opt in optional_prices:
                breakdown[opt] = float(optional_prices[opt])
                opts_total += optional_prices[opt]

        monthly = base + opts_total
        annual = monthly * 12

        return {
            "monthly": float(monthly),
            "annual": float(annual),
            "breakdown": breakdown,
        }

    @staticmethod
    def _calculate_travel_premium(payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else (payload if isinstance(payload, dict) else {})
        trip = data.get("travel_party_and_trip") or {}
        days = RealPremiumClient._calculate_trip_days(trip.get("departure_date"), trip.get("return_date"))

        travellers_18_69 = int(trip.get("num_travellers_18_69") or 0)
        travellers_0_17 = int(trip.get("num_travellers_0_17") or 0)
        travellers_70_75 = int(trip.get("num_travellers_70_75") or 0)
        travellers_76_80 = int(trip.get("num_travellers_76_80") or 0)
        travellers_81_85 = int(trip.get("num_travellers_81_85") or 0)

        product = data.get("selected_product") or {}
        product_id = product.get("id", "worldwide_essential")

        product_multiplier = {
            "worldwide_essential": Decimal("1.0"),
            "worldwide_elite": Decimal("1.5"),
            "schengen_essential": Decimal("1.2"),
            "schengen_elite": Decimal("1.7"),
            "student_cover": Decimal("0.9"),
            "africa_asia": Decimal("0.8"),
            "inbound_karibu": Decimal("0.6"),
        }.get(product_id, Decimal("1.0"))

        rate_18_69 = Decimal("2.0")
        rate_0_17 = Decimal("1.0")
        rate_70_75 = Decimal("3.0")
        rate_76_80 = Decimal("4.0")
        rate_81_85 = Decimal("5.0")

        base_usd = Decimal(days) * (
            Decimal(travellers_18_69) * rate_18_69
            + Decimal(travellers_0_17) * rate_0_17
            + Decimal(travellers_70_75) * rate_70_75
            + Decimal(travellers_76_80) * rate_76_80
            + Decimal(travellers_81_85) * rate_81_85
        )

        total_usd = (base_usd * product_multiplier).quantize(Decimal("0.01"))

        usd_to_ugx = Decimal("3900")
        total_ugx = (total_usd * usd_to_ugx).quantize(Decimal("1."))

        return {
            "total_usd": float(total_usd),
            "total_ugx": float(total_ugx),
            "breakdown": {
                "days": days,
                "product_id": product_id,
                "product_multiplier": float(product_multiplier),
                "travellers": {
                    "18_69": travellers_18_69,
                    "0_17": travellers_0_17,
                    "70_75": travellers_70_75,
                    "76_80": travellers_76_80,
                    "81_85": travellers_81_85,
                },
                "base_usd": float(base_usd),
                "usd_to_ugx": float(usd_to_ugx),
            },
        }

    @staticmethod
    def _calculate_motor_private_premium(payload: Dict[str, Any]) -> Dict[str, Any]:
        base_premium = Decimal("1280000")
        training_levy = Decimal("6400")
        sticker_fees = Decimal("6000")
        vat = Decimal("232632")
        stamp_duty = Decimal("35000")
        total = base_premium + training_levy + sticker_fees + vat + stamp_duty
        return {
            "base_premium": float(base_premium),
            "training_levy": float(training_levy),
            "sticker_fees": float(sticker_fees),
            "vat": float(vat),
            "stamp_duty": float(stamp_duty),
            "total": float(total),
        }

    @staticmethod
    def _calculate_trip_days(departure_date: Any, return_date: Any) -> int:
        d1 = RealPremiumClient._safe_iso_date(departure_date)
        d2 = RealPremiumClient._safe_iso_date(return_date)
        if not d1 or not d2:
            return 1
        return max(1, (d2 - d1).days + 1)

    @staticmethod
    def _safe_iso_date(value: Any) -> Optional[date]:
        from datetime import datetime

        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).date()
        except (TypeError, ValueError):
            return None


real_premium_client = RealPremiumClient()

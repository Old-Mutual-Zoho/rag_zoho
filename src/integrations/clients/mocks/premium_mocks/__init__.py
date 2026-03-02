"""Registry for product-specific premium mock builders."""

from __future__ import annotations

from typing import Any, Callable, Dict

from .motor_private import build_motor_private_premium_mock
from .personal_accident import build_personal_accident_premium_mock
from .serenicare import build_serenicare_premium_mock
from .travel_insurance import build_travel_insurance_premium_mock

PremiumMockBuilder = Callable[[Dict[str, Any]], Dict[str, Any]]

_REGISTRY: Dict[str, PremiumMockBuilder] = {
    "personal_accident": build_personal_accident_premium_mock,
    "serenicare": build_serenicare_premium_mock,
    "travel_insurance": build_travel_insurance_premium_mock,
    "motor_private": build_motor_private_premium_mock,
}


def get_premium_mock_builder(product_key: str) -> PremiumMockBuilder:
    """Return product premium mock builder by normalized product key."""
    normalized = str(product_key or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "travel": "travel_insurance",
        "travel_insurance": "travel_insurance",
        "personal_accident": "personal_accident",
        "motor_private": "motor_private",
        "serenicare": "serenicare",
    }
    mapped = aliases.get(normalized)
    if not mapped:
        raise ValueError(f"Unsupported product_key for premium mock: {product_key}")
    return _REGISTRY[mapped]

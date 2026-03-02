"""Travel Insurance premium mock builder."""

from __future__ import annotations

from typing import Any, Dict

from src.integrations.clients.real_http.premium import RealPremiumClient


def build_travel_insurance_premium_mock(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build Travel Insurance premium payload with flow-compatible shape."""
    return RealPremiumClient._calculate_travel_premium(payload)

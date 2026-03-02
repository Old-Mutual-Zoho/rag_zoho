"""Serenicare premium mock builder."""

from __future__ import annotations

from typing import Any, Dict

from src.integrations.clients.real_http.premium import RealPremiumClient


def build_serenicare_premium_mock(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build Serenicare premium payload with flow-compatible shape."""
    return RealPremiumClient._calculate_serenicare_premium(payload)

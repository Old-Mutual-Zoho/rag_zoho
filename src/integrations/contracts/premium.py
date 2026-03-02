"""Premium calculation contracts."""

from __future__ import annotations

from typing import Any, Dict, Protocol


class PremiumContract(Protocol):
    """Contract for premium integrations."""

    async def calculate_premium(self, product_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate premium asynchronously."""

    def calculate_premium_sync(self, product_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate premium synchronously."""

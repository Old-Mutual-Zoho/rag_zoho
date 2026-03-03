"""
Policy Service for Partner API Integration.

Uses real partner policy APIs when configured, and falls back to a deterministic
mock response for development/test environments.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx


_MOCK_POLICIES: Dict[str, Dict[str, Any]] = {}


class PolicyService:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = base_url or os.getenv("PARTNER_POLICY_API_URL", "")
        self.api_key = api_key or os.getenv("PARTNER_POLICY_API_KEY", "")

    async def issue_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.base_url:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(f"{self.base_url}/policies/issue", json=payload, headers=headers)
                response.raise_for_status()
                return response.json()

        return self._mock_issue(payload)

    async def get_policy(self, policy_id: str) -> Dict[str, Any]:
        if self.base_url:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(f"{self.base_url}/policies/{policy_id}", headers=headers)
                response.raise_for_status()
                return response.json()

        policy = _MOCK_POLICIES.get(str(policy_id))
        if not policy:
            raise KeyError(f"Policy '{policy_id}' not found")
        return policy

    async def cancel_policy(self, policy_id: str, reason: str = "") -> Dict[str, Any]:
        if self.base_url:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            payload = {"reason": reason}
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self.base_url}/policies/{policy_id}/cancel",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                return response.json()

        policy = _MOCK_POLICIES.get(str(policy_id))
        if not policy:
            raise KeyError(f"Policy '{policy_id}' not found")
        updated = dict(policy)
        updated["status"] = "CANCELLED"
        md = dict(updated.get("metadata") or {})
        md["cancellation_reason"] = reason
        updated["metadata"] = md
        _MOCK_POLICIES[str(policy_id)] = updated
        return updated

    def _mock_issue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payment_status = str(payload.get("payment_status") or "").upper()
        requires_payment_first = bool(payload.get("requires_payment_before_issuance"))

        if requires_payment_first and payment_status not in {"SUCCESS", "COMPLETED"}:
            policy_status = "PENDING_PAYMENT"
        else:
            policy_status = "ISSUED"

        start_date = str(payload.get("policy_start_date") or date.today().isoformat())
        end_date = str(payload.get("policy_end_date") or (date.today() + timedelta(days=365)).isoformat())

        policy = {
            "policy_id": f"POL-MOCK-{uuid4().hex[:10].upper()}",
            "quote_id": str(payload.get("quote_id") or ""),
            "status": policy_status,
            "start_date": start_date,
            "end_date": end_date,
            "currency": str(payload.get("currency") or "UGX").upper(),
            "metadata": {
                "source": "mock_policy_service",
                "product_id": payload.get("product_id"),
                "user_id": payload.get("user_id"),
            },
        }
        _MOCK_POLICIES[str(policy["policy_id"])] = policy
        return policy

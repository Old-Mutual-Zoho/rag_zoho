"""Mock premium client with per-product handlers and persisted artifacts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from src.integrations.contracts.premium import PremiumContract

from . import get_premium_mock_builder

logger = logging.getLogger(__name__)


class MockPremiumClient(PremiumContract):
    """Mock premium client that routes by product key and persists outputs."""

    def __init__(self, output_root: Path | None = None) -> None:
        self.output_root = output_root or self._default_output_root()
        self.output_root.mkdir(parents=True, exist_ok=True)

    async def calculate_premium(self, product_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.calculate_premium_sync(product_key, payload)

    def calculate_premium_sync(self, product_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_product_key(product_key)
        builder = get_premium_mock_builder(normalized)
        response = builder(payload)

        output_path = self._write_mock_output(normalized, payload, response)
        response_with_artifact = dict(response)
        response_with_artifact["mock_output_path"] = str(output_path)
        return response_with_artifact

    def _write_mock_output(self, product_key: str, payload: Dict[str, Any], response: Dict[str, Any]) -> Path:
        product_dir = self.output_root / product_key
        product_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        file_path = product_dir / f"{timestamp}_{uuid4().hex}.json"

        output_document = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "product_id": product_key,
            "input": payload,
            "output": response,
        }

        try:
            file_path.write_text(json.dumps(output_document, indent=2, default=str), encoding="utf-8")
            logger.info("Wrote premium mock output file: %s", file_path)
        except Exception:
            logger.exception("Failed to write premium mock output file: %s", file_path)

        return file_path

    @staticmethod
    def _default_output_root() -> Path:
        return Path(__file__).resolve().parents[5] / "premium_mocks"

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
        mapped = aliases.get(normalized)
        if not mapped:
            raise ValueError(f"Unsupported product_key for premium mock: {product_key}")
        return mapped


mock_premium_client = MockPremiumClient()

import os

from src.integrations.clients.mocks.base_mobile_money import BaseMobileMoneyMock


class AirtelMockClient(BaseMobileMoneyMock):
    def __init__(self) -> None:
        super().__init__(
            provider="AIRTEL",
            webhook_secret=os.getenv("MOCK_PAYMENT_WEBHOOK_SECRET", "mock-secret"),
        )
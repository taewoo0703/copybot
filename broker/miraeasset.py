from __future__ import annotations

from core.schemas import PortfolioSnapshot, Quote, TargetOrder

from .base import BrokerCapabilities, BrokerClient, BrokerCredentials, BrokerFeatureUnavailable


class MiraeAssetBrokerClient(BrokerClient):
    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        self.last_message = "Mirae Asset API integration is pending"

    async def connect(self) -> bool:
        self.connected = False
        self.last_message = "Mirae Asset does not currently expose a supported trading API in this bot"
        return False

    async def refresh_token(self) -> bool:
        return await self.connect()

    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        raise BrokerFeatureUnavailable(self.last_message)

    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        raise BrokerFeatureUnavailable(self.last_message)

    async def place_order(self, order: TargetOrder):
        raise BrokerFeatureUnavailable(self.last_message)

    def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker="miraeasset",
            supports_domestic_stock=False,
            supports_global_stock=False,
            supports_market_order=False,
            supports_live_trading=False,
            notes="Shell only. Implementation is held until an official supported API is available.",
        )

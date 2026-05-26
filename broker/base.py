from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import os
import re
from typing import Any

from core.schemas import AccountConfig, OrderResult, PortfolioSnapshot, Quote, TargetOrder
from core.types import MarketScope, OrderType


class BrokerError(Exception):
    pass


class BrokerFeatureUnavailable(BrokerError):
    pass


@dataclass(frozen=True)
class BrokerCapabilities:
    broker: str
    supports_domestic_stock: bool = False
    supports_global_stock: bool = False
    supports_market_order: bool = False
    supports_limit_order: bool = False
    supports_live_trading: bool = False
    supports_fractional_quantity: bool = False
    notes: str = ""

    def supports_scope(self, market_scope: MarketScope) -> bool:
        if market_scope == MarketScope.DOMESTIC:
            return self.supports_domestic_stock
        if market_scope == MarketScope.GLOBAL:
            return self.supports_global_stock
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "supports_domestic_stock": self.supports_domestic_stock,
            "supports_global_stock": self.supports_global_stock,
            "supports_market_order": self.supports_market_order,
            "supports_limit_order": self.supports_limit_order,
            "supports_live_trading": self.supports_live_trading,
            "supports_fractional_quantity": self.supports_fractional_quantity,
            "notes": self.notes,
        }


@dataclass
class BrokerCredentials:
    ref: str
    app_key: str | None = None
    app_secret: str | None = None
    is_mock: bool = False

    @staticmethod
    def env_prefix(ref: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "_", ref.upper()).strip("_")

    @classmethod
    def from_env(cls, ref: str) -> "BrokerCredentials":
        prefix = cls.env_prefix(ref)
        return cls(
            ref=ref,
            app_key=os.getenv(f"{prefix}_APP_KEY"),
            app_secret=os.getenv(f"{prefix}_APP_SECRET"),
            is_mock=(os.getenv(f"{prefix}_IS_MOCK", "0").lower() in {"1", "true", "yes", "on"}),
        )


class BrokerClient(ABC):
    def __init__(self, account: AccountConfig, credentials: BrokerCredentials):
        self.account = account
        self.credentials = credentials
        self.connected = False
        self.last_message = "not connected"

    @abstractmethod
    async def connect(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def refresh_token(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        raise NotImplementedError

    @abstractmethod
    async def place_order(self, order: TargetOrder) -> OrderResult:
        raise NotImplementedError

    async def place_market_order(self, order: TargetOrder) -> OrderResult:
        return await self.place_order(order)

    @abstractmethod
    def get_capabilities(self) -> BrokerCapabilities:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {
            "account_id": self.account.account_id,
            "broker": self.account.broker.value,
            "market_scope": self.account.market_scope.value,
            "mode": self.account.mode.value,
            "connected": self.connected,
            "last_message": self.last_message,
            "capabilities": self.get_capabilities().to_dict(),
        }

    def assert_live_supported(self) -> None:
        self.assert_order_supported()

    def assert_order_supported(self, order: TargetOrder | None = None) -> None:
        capabilities = self.get_capabilities()
        if not capabilities.supports_scope(self.account.market_scope):
            raise BrokerFeatureUnavailable(
                f"{self.account.broker.value} does not support {self.account.market_scope.value}"
            )
        if not capabilities.supports_live_trading:
            raise BrokerFeatureUnavailable(f"{self.account.broker.value} live trading is disabled")
        if order is None:
            return
        if order.order_type == OrderType.MARKET and not capabilities.supports_market_order:
            raise BrokerFeatureUnavailable(f"{self.account.broker.value} does not support market orders")
        if order.order_type == OrderType.LIMIT and not capabilities.supports_limit_order:
            raise BrokerFeatureUnavailable(f"{self.account.broker.value} does not support limit orders")

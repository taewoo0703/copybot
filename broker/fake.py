from __future__ import annotations

from copy import deepcopy

from core.schemas import Holding, OrderResult, PortfolioSnapshot, TargetOrder
from core.types import OrderSide

from .base import BrokerCapabilities, BrokerClient, BrokerCredentials


class FakeBrokerClient(BrokerClient):
    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        self.orders: list[TargetOrder] = []
        self.snapshot = PortfolioSnapshot(
            account_id=account.account_id,
            total_equity=0.0,
            cash=0.0,
            holdings=[],
        )

    async def connect(self) -> bool:
        self.connected = True
        self.last_message = "fake broker connected"
        return True

    async def refresh_token(self) -> bool:
        self.connected = True
        self.last_message = "fake token refreshed"
        return True

    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        snapshot = deepcopy(self.snapshot)
        snapshot.captured_at = snapshot.captured_at
        return snapshot

    async def get_quote(self, symbol: str, exchange: str = "") -> float:
        key = f"{exchange}:{symbol}" if exchange else symbol
        for holding in self.snapshot.holdings:
            if holding.key == key:
                return holding.current_price
        return 0.0

    async def place_market_order(self, order: TargetOrder) -> OrderResult:
        self.orders.append(order)
        self._apply_order(order)
        return OrderResult(order=order, accepted=True, order_id=f"fake-{len(self.orders)}")

    def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker="fake",
            supports_domestic_stock=True,
            supports_global_stock=True,
            supports_market_order=True,
            supports_live_trading=True,
            notes="in-memory test broker",
        )

    def _apply_order(self, order: TargetOrder) -> None:
        key = order.instrument_key
        holdings = {holding.key: holding for holding in self.snapshot.holdings}
        holding = holdings.get(key)
        if holding is None:
            holding = Holding(
                symbol=order.symbol,
                exchange=order.exchange,
                quantity=0,
                current_price=order.estimated_price,
                market_value=0.0,
                currency=self.snapshot.currency,
            )
            self.snapshot.holdings.append(holding)

        signed_qty = order.quantity if order.side == OrderSide.BUY else -order.quantity
        holding.quantity = max(0, holding.quantity + signed_qty)
        holding.current_price = order.estimated_price
        holding.market_value = holding.quantity * holding.current_price

        cash_delta = order.estimated_value * (-1 if order.side == OrderSide.BUY else 1)
        self.snapshot.cash += cash_delta
        self.snapshot.total_equity = self.snapshot.cash + sum(item.market_value for item in self.snapshot.holdings)
        self.snapshot.holdings = [item for item in self.snapshot.holdings if item.quantity > 0]

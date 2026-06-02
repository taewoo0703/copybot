from __future__ import annotations

from copy import deepcopy

from core.schemas import Holding, OpenOrder, OrderCancelResult, OrderResult, PortfolioSnapshot, Quote, TargetOrder
from core.types import OrderSide

from .base import BrokerCapabilities, BrokerClient, BrokerCredentials


class FakeBrokerClient(BrokerClient):
    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        self.orders: list[TargetOrder] = []
        self.open_orders: list[OpenOrder] = []
        self.cancelled_orders: list[OpenOrder] = []
        self.events: list[str] = []
        self.snapshot = PortfolioSnapshot(
            account_id=account.account_id,
            total_equity=0.0,
            cash=0.0,
            holdings=[],
        )
        self.quotes: dict[str, Quote] = {}

    async def connect(self) -> bool:
        self.connected = True
        self.last_message = "fake broker connected"
        return True

    async def refresh_token(self) -> bool:
        self.connected = True
        self.last_message = "fake token refreshed"
        return True

    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        self.events.append("get_portfolio_snapshot")
        snapshot = deepcopy(self.snapshot)
        snapshot.captured_at = snapshot.captured_at
        
        from core.LogManager import logManager
        await logManager.log_portfolio_snapshot_async(snapshot)
        return snapshot

    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        key = f"{exchange}:{symbol}" if exchange else symbol
        if key in self.quotes:
            return self.quotes[key]
        for holding in self.snapshot.holdings:
            if holding.key == key:
                return Quote(
                    symbol=symbol,
                    exchange=exchange,
                    last_price=holding.current_price,
                    ask_price_1=holding.current_price,
                    bid_price_1=holding.current_price,
                    currency=holding.currency,
                )
        return Quote(symbol=symbol, exchange=exchange)

    async def place_order(self, order: TargetOrder) -> OrderResult:
        self.orders.append(order)
        self._apply_order(order)
        return OrderResult(order=order, accepted=True, order_id=f"fake-{len(self.orders)}")

    async def get_open_orders(self) -> list[OpenOrder]:
        self.events.append("get_open_orders")
        return deepcopy(self.open_orders)

    async def cancel_order(self, order: OpenOrder) -> OrderCancelResult:
        self.events.append("cancel_order")
        self.open_orders = [item for item in self.open_orders if item.order_id != order.order_id]
        self.cancelled_orders.append(order)
        return OrderCancelResult(order=order, accepted=True, message="fake order cancelled")

    def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker="fake",
            supports_domestic_stock=True,
            supports_global_stock=True,
            supports_market_order=True,
            supports_limit_order=True,
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

        if order.side == OrderSide.BUY and order.estimated_value > self.snapshot.cash:
            raise ValueError("not enough cash")

        cash_delta = order.estimated_value * (-1 if order.side == OrderSide.BUY else 1)
        self.snapshot.cash += cash_delta
        self.snapshot.total_equity = self.snapshot.cash + sum(item.market_value for item in self.snapshot.holdings)
        self.snapshot.holdings = [item for item in self.snapshot.holdings if item.quantity > 0]

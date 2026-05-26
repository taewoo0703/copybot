from __future__ import annotations

from math import floor

from .schemas import AccountConfig, CopyGroupConfig, Holding, PortfolioSnapshot, TargetOrder
from .types import AccountMode, OrderSide, SyncRunMode


class PortfolioRebalancer:
    def build_orders(
        self,
        group: CopyGroupConfig,
        master: PortfolioSnapshot,
        slave: PortfolioSnapshot,
        slave_account: AccountConfig,
    ) -> list[TargetOrder]:
        if master.total_equity <= 0 or slave.total_equity <= 0:
            return []

        master_holdings = master.holding_map()
        slave_holdings = slave.holding_map()
        all_keys = sorted(set(master_holdings) | set(slave_holdings))
        orders: list[TargetOrder] = []
        mode = self._resolve_order_mode(group, slave_account)

        for key in all_keys:
            master_holding = master_holdings.get(key)
            slave_holding = slave_holdings.get(key)
            reference_holding = master_holding or slave_holding
            if reference_holding is None:
                continue

            target_value = self._target_value(master_holding, master, slave)
            price = self._price(reference_holding, slave_holding)
            if price <= 0:
                continue

            target_qty = floor(target_value / price)
            current_qty = slave_holding.quantity if slave_holding else 0
            diff_qty = target_qty - current_qty
            if diff_qty == 0:
                continue

            estimated_value = abs(diff_qty) * price
            if estimated_value < group.min_trade_value:
                continue

            orders.append(
                TargetOrder(
                    group_id=group.group_id,
                    account_id=slave.account_id,
                    broker=slave_account.broker,
                    market_scope=slave_account.market_scope,
                    symbol=reference_holding.symbol,
                    exchange=reference_holding.exchange,
                    side=OrderSide.BUY if diff_qty > 0 else OrderSide.SELL,
                    quantity=abs(diff_qty),
                    estimated_price=price,
                    estimated_value=estimated_value,
                    mode=mode,
                    reason="master_weight_sync",
                )
            )

        return sorted(orders, key=lambda order: 0 if order.side == OrderSide.SELL else 1)

    def _target_value(
        self,
        master_holding: Holding | None,
        master: PortfolioSnapshot,
        slave: PortfolioSnapshot,
    ) -> float:
        if master_holding is None:
            return 0.0
        weight = master_holding.market_value / master.total_equity
        return slave.total_equity * weight

    def _price(self, reference_holding: Holding, slave_holding: Holding | None) -> float:
        if slave_holding and slave_holding.current_price > 0:
            return slave_holding.current_price
        return reference_holding.current_price

    def _resolve_order_mode(self, group: CopyGroupConfig, slave_account: AccountConfig) -> SyncRunMode:
        if group.mode == AccountMode.LIVE and slave_account.mode == AccountMode.LIVE:
            return SyncRunMode.LIVE
        return SyncRunMode.DRY_RUN

from __future__ import annotations

import asyncio
from dataclasses import replace
from math import floor
import time
import traceback
from typing import Any

from broker import BrokerError, BrokerFeatureUnavailable, BrokerClient

from .AccountRegistry import AccountRegistry
from .LogManager import logManager
from .PortfolioRebalancer import PortfolioRebalancer
from .config_loader import load_copybot_config
from .schemas import CopyBotConfig, CopyGroupConfig, OrderCancelResult, OrderResult, PortfolioSnapshot, Quote, TargetOrder
from .types import OrderSide, OrderType, SyncRunMode


POST_ORDER_SNAPSHOT_DELAY_SECONDS = 30


class CopyEngine:
    def __init__(self):
        self.registry = AccountRegistry()
        self.rebalancer = PortfolioRebalancer()
        self.config = CopyBotConfig()
        self.pause = False
        self.group_state: dict[str, dict[str, Any]] = {}
        self.master_fingerprints: dict[str, tuple[Any, ...]] = {}
        self.master_snapshots: dict[str, PortfolioSnapshot] = {}
        self.next_poll_at: dict[str, float] = {}

    async def initialize(self) -> None:
        await self.reload_config(sync_after_load=False)

    async def reload_config(self, sync_after_load: bool = True) -> CopyBotConfig:
        return await self.apply_config(load_copybot_config(), sync_after_load=sync_after_load)

    async def apply_config(self, config: CopyBotConfig, sync_after_load: bool = True) -> CopyBotConfig:
        self.config = config
        self.registry.load(self.config)
        await self.registry.connect_all()
        self.group_state = {
            group.group_id: self._new_group_state(group)
            for group in self.config.copy_groups
        }
        self.master_fingerprints.clear()
        self.master_snapshots.clear()
        now = time.time()
        self.next_poll_at = {group.group_id: now for group in self.config.copy_groups}
        if sync_after_load:
            await self.sync_all(force=True)
        return self.config

    async def sync_all(self, force: bool = True) -> dict[str, Any]:
        results = {}
        for group in self.config.copy_groups:
            if not group.enabled:
                self.group_state.setdefault(group.group_id, self._new_group_state(group))
                self.group_state[group.group_id]["last_message"] = "group disabled"
                continue
            results[group.group_id] = await self.sync_group(group.group_id, force=force)
        return results

    async def snapshot_all_groups(self) -> dict[str, Any]:
        results = {}
        for group in self.config.copy_groups:
            master_snapshot = await self.registry.get_client(group.master_account_id).get_portfolio_snapshot()
            slave_snapshots = [
                await self.registry.get_client(slave_id).get_portfolio_snapshot()
                for slave_id in group.slave_account_ids
            ]
            await logManager.log_group_weight_comparison_async(group, master_snapshot, slave_snapshots)
            results[group.group_id] = {
                "master": master_snapshot.to_dict(),
                "slaves": [snapshot.to_dict() for snapshot in slave_snapshots],
            }
        return results

    async def get_all_open_orders(self) -> dict[str, Any]:
        results = {}
        for account_id in sorted(self.registry.clients):
            try:
                orders = await self.registry.get_client(account_id).get_open_orders()
                results[account_id] = {
                    "orders": [order.to_dict() for order in orders],
                    "errors": [],
                }
            except (BrokerError, BrokerFeatureUnavailable) as error:
                results[account_id] = {"orders": [], "errors": [str(error)]}
            except Exception as error:
                await logManager.log_error_message_async(error, "Open Orders")
                results[account_id] = {"orders": [], "errors": [str(error)]}
        return results

    async def sync_group(self, group_id: str, force: bool = True) -> dict[str, Any]:
        group = self._get_group(group_id)
        state = self.group_state.setdefault(group_id, self._new_group_state(group))
        state["last_polled_at"] = time.time()
        state["last_error"] = None

        if self.pause:
            state["last_message"] = "paused"
            return state

        try:
            master_client: BrokerClient = self.registry.get_client(group.master_account_id)
            master_snapshot: PortfolioSnapshot = await master_client.get_portfolio_snapshot()
            fingerprint = self._master_fingerprint(master_snapshot)
            previous_master_snapshot = self.master_snapshots.get(group_id)
            changed = self.master_fingerprints.get(group_id) != fingerprint

            state["last_master_snapshot"] = master_snapshot.to_dict()
            state["master_changed"] = changed
            if not force and not changed:
                state["last_message"] = "master unchanged"
                self.next_poll_at[group_id] = time.time() + group.poll_interval_seconds
                return state

            if changed and previous_master_snapshot is not None:
                await logManager.log_master_position_changed_async(group, previous_master_snapshot, master_snapshot)

            all_orders: list[TargetOrder] = []
            all_results: list[OrderResult] = []
            slave_states = {}
            for slave_id in group.slave_account_ids:
                slave_state = await self._sync_slave(group, master_snapshot, slave_id)
                slave_states[slave_id] = slave_state
                all_orders.extend(slave_state["orders_raw"])
                all_results.extend(slave_state["results_raw"])

            self.master_fingerprints[group_id] = fingerprint
            self.master_snapshots[group_id] = master_snapshot
            state["last_orders"] = [order.to_dict() for order in all_orders]
            state["last_results"] = [result.to_dict() for result in all_results]
            state["slaves"] = {
                slave_id: {
                    key: value
                    for key, value in slave_state.items()
                    if key not in {"orders_raw", "results_raw"}
                }
                for slave_id, slave_state in slave_states.items()
            }
            state["last_sync_at"] = time.time()
            state["last_message"] = f"synced {len(all_orders)} planned orders"
            self.next_poll_at[group_id] = time.time() + group.poll_interval_seconds
            if all_results:
                await asyncio.sleep(POST_ORDER_SNAPSHOT_DELAY_SECONDS)
                refreshed_slave_snapshots = [
                    await self.registry.get_client(slave_id).get_portfolio_snapshot()
                    for slave_id in group.slave_account_ids
                ]
                await logManager.log_group_weight_comparison_async(group, master_snapshot, refreshed_slave_snapshots)
            return state
        except Exception as error:
            state["last_error"] = str(error)
            state["last_traceback"] = traceback.format_exc()
            state["last_message"] = "sync failed"
            await logManager.log_error_message_async(error, "CopyEngine")
            self.next_poll_at[group_id] = time.time() + group.poll_interval_seconds
            return state

    async def _sync_slave(
        self,
        group: CopyGroupConfig,
        master_snapshot: PortfolioSnapshot,
        slave_id: str,
    ) -> dict[str, Any]:
        slave_client: BrokerClient = self.registry.get_client(slave_id)
        cancel_results: list[OrderCancelResult] = []
        errors: list[str] = []
        try:
            cancel_results = await slave_client.cancel_open_orders()
        except (BrokerError, BrokerFeatureUnavailable) as error:
            errors.append(str(error))
        except Exception as error:
            errors.append(str(error))
            await logManager.log_error_message_async(error, "Cancel Open Orders")

        if errors:
            await logManager.log_slave_sync_errors_async(group, slave_id, errors)
            return {
                "snapshot": None,
                "orders": [],
                "results": [],
                "cancel_results": [result.to_dict() for result in cancel_results],
                "errors": errors,
                "orders_raw": [],
                "results_raw": [],
            }

        slave_snapshot: PortfolioSnapshot = await slave_client.get_portfolio_snapshot()
        orders = self.rebalancer.build_orders(
            group=group,
            master=master_snapshot,
            slave=slave_snapshot,
            slave_account=slave_client.account,
        )
        if orders:
            await logManager.log_rebalance_orders_async(group, master_snapshot, slave_snapshot, orders)
        results: list[OrderResult] = []
        sell_orders = [order for order in orders if order.side == OrderSide.SELL]
        buy_orders = [order for order in orders if order.side == OrderSide.BUY]
        stop_buy_execution = False

        for order in sell_orders:
            if order.mode == SyncRunMode.DRY_RUN:
                continue
            executable_order = replace(order, order_type=OrderType.MARKET, limit_price=None)
            try:
                slave_client.assert_order_supported(executable_order)
                results.append(await slave_client.place_order(executable_order))
            except BrokerFeatureUnavailable as error:
                errors.append(str(error))
                stop_buy_execution = True
                break
            except Exception as error:
                errors.append(str(error))
                await logManager.log_error_message_async(error, "Sell Order")

        if buy_orders and not stop_buy_execution:
            buy_results, buy_errors = await self._execute_buy_orders(group, slave_client, buy_orders)
            results.extend(buy_results)
            errors.extend(buy_errors)

        if results:
            await logManager.log_order_results_async(group, slave_id, results)

        if errors:
            await logManager.log_slave_sync_errors_async(group, slave_id, errors)

        return {
            "snapshot": slave_snapshot.to_dict(),
            "orders": [order.to_dict() for order in orders],
            "results": [result.to_dict() for result in results],
            "cancel_results": [result.to_dict() for result in cancel_results],
            "errors": errors,
            "orders_raw": orders,
            "results_raw": results,
        }

    async def _execute_buy_orders(
        self,
        group: CopyGroupConfig,
        slave_client: BrokerClient,
        buy_orders: list[TargetOrder],
    ) -> tuple[list[OrderResult], list[str]]:
        results: list[OrderResult] = []
        errors: list[str] = []
        live_orders = [order for order in buy_orders if order.mode == SyncRunMode.LIVE]
        if not live_orders:
            return results, errors

        cash_snapshot = await slave_client.get_portfolio_snapshot()
        available_cash = max(0.0, cash_snapshot.cash * (1.0 - group.cash_safety_buffer))

        for order in live_orders:
            try:
                quote: Quote = await slave_client.get_quote(order.symbol, order.exchange)
                order_price = quote.ask_price_1 if quote.ask_price_1 > 0 else quote.last_price
                if quote.ask_price_1 <= 0:
                    errors.append(f"{order.instrument_key} has no ask_price_1; order price set to last_price")

                quantity = min(order.quantity, floor(available_cash / order_price))
                if quantity <= 0:
                    errors.append(f"insufficient cash for {order.instrument_key}; remaining buy orders stopped")
                    continue

                estimated_value = quantity * order_price
                if estimated_value < group.min_trade_value:
                    errors.append(
                        f"{order.instrument_key} adjusted buy value is below min_trade_value; remaining buy orders stopped"
                    )
                    continue

                executable_order = replace(
                    order,
                    quantity=quantity,
                    estimated_price=order_price,
                    estimated_value=estimated_value,
                    order_type=OrderType.LIMIT,
                    limit_price=order_price,
                    reason="master_weight_sync_buy_at_ask1",
                )
                slave_client.assert_order_supported(executable_order)
                results.append(await slave_client.place_order(executable_order))
                available_cash -= estimated_value
            except BrokerFeatureUnavailable as error:
                errors.append(str(error))
                break
            except Exception as error:
                errors.append(str(error))
                await logManager.log_error_message_async(error, "Buy Order")
                break

        return results, errors

    async def on_timer_update(self) -> None:
        if self.pause:
            return
        now = time.time()
        for group in self.config.copy_groups:
            if not group.enabled:
                continue
            if now >= self.next_poll_at.get(group.group_id, 0):
                await self.sync_group(group.group_id, force=False)

    def set_pause(self, pause: bool) -> None:
        self.pause = pause

    def get_status(self) -> dict[str, Any]:
        return {
            "paused": self.pause,
            "accounts": self.registry.status(),
            "groups": self.group_state,
            "next_poll_at": self.next_poll_at,
        }

    def get_config(self) -> dict[str, Any]:
        return self.config.to_dict()

    def _get_group(self, group_id: str) -> CopyGroupConfig:
        for group in self.config.copy_groups:
            if group.group_id == group_id:
                return group
        raise ValueError(f"unknown group_id: {group_id}")

    def _new_group_state(self, group: CopyGroupConfig) -> dict[str, Any]:
        return {
            "group_id": group.group_id,
            "enabled": group.enabled,
            "mode": group.mode.value,
            "master_account_id": group.master_account_id,
            "slave_account_ids": group.slave_account_ids,
            "last_message": "not synced",
            "last_master_snapshot": None,
            "last_orders": [],
            "last_results": [],
            "slaves": {},
            "last_error": None,
            "last_sync_at": None,
            "last_polled_at": None,
            "master_changed": None,
        }

    def _master_fingerprint(self, snapshot: PortfolioSnapshot) -> tuple[Any, ...]:
        return snapshot.fingerprint()

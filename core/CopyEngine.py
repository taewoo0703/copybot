from __future__ import annotations

import time
import traceback
from typing import Any

from broker import BrokerFeatureUnavailable, BrokerClient

from .AccountRegistry import AccountRegistry
from .LogManager import logManager
from .PortfolioRebalancer import PortfolioRebalancer
from .config_loader import load_copybot_config
from .schemas import CopyBotConfig, CopyGroupConfig, OrderResult, PortfolioSnapshot, TargetOrder
from .types import SyncRunMode


class CopyEngine:
    def __init__(self):
        self.registry = AccountRegistry()
        self.rebalancer = PortfolioRebalancer()
        self.config = CopyBotConfig()
        self.pause = False
        self.group_state: dict[str, dict[str, Any]] = {}
        self.master_fingerprints: dict[str, tuple[tuple[str, int], ...]] = {}
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
            fingerprint: tuple[tuple[str, int], ...] = master_snapshot.fingerprint()
            changed = self.master_fingerprints.get(group_id) != fingerprint

            state["last_master_snapshot"] = master_snapshot.to_dict()
            state["master_changed"] = changed
            if not force and not changed:
                state["last_message"] = "master unchanged"
                self.next_poll_at[group_id] = time.time() + group.poll_interval_seconds
                return state

            all_orders: list[TargetOrder] = []
            all_results: list[OrderResult] = []
            slave_states = {}
            for slave_id in group.slave_account_ids:
                slave_state = await self._sync_slave(group, master_snapshot, slave_id)
                slave_states[slave_id] = slave_state
                all_orders.extend(slave_state["orders_raw"])
                all_results.extend(slave_state["results_raw"])

            self.master_fingerprints[group_id] = fingerprint
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
            await logManager.log_sync_message_async(group_id, state["last_message"])
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
        slave_snapshot: PortfolioSnapshot = await slave_client.get_portfolio_snapshot()
        orders = self.rebalancer.build_orders(
            group=group,
            master=master_snapshot,
            slave=slave_snapshot,
            slave_account=slave_client.account,
        )
        results: list[OrderResult] = []
        errors: list[str] = []
        for order in orders:
            if order.mode == SyncRunMode.DRY_RUN:
                continue
            try:
                slave_client.assert_live_supported()
                results.append(await slave_client.place_market_order(order))
            except BrokerFeatureUnavailable as error:
                errors.append(str(error))
                break
            except Exception as error:
                errors.append(str(error))
                await logManager.log_error_message_async(error, "Order")

        return {
            "snapshot": slave_snapshot.to_dict(),
            "orders": [order.to_dict() for order in orders],
            "results": [result.to_dict() for result in results],
            "errors": errors,
            "orders_raw": orders,
            "results_raw": results,
        }

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

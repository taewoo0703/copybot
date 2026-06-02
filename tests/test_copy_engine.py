import unittest

from core.CopyEngine import CopyEngine
from core.schemas import AccountConfig, CopyBotConfig, Holding, OpenOrder, PortfolioSnapshot, Quote
from core.types import AccountMode, BrokerName, MarketScope, OrderSide, OrderType


def fake_account(account_id, mode=AccountMode.DRY_RUN):
    return AccountConfig(
        account_id=account_id,
        broker=BrokerName.FAKE,
        market_scope=MarketScope.DOMESTIC,
        credentials_ref=account_id,
        mode=mode,
    )


def fake_snapshot(account_id, holdings, cash=0, total_equity=None):
    if total_equity is None:
        total_equity = cash + sum(item["quantity"] * item["current_price"] for item in holdings)
    return PortfolioSnapshot(
        account_id=account_id,
        total_equity=total_equity,
        cash=cash,
        holdings=[
            Holding(
                symbol=item["symbol"],
                exchange=item.get("exchange", ""),
                quantity=item["quantity"],
                current_price=item["current_price"],
                market_value=item.get("market_value", item["quantity"] * item["current_price"]),
            )
            for item in holdings
        ],
    )


class CopyEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_does_not_place_orders(self):
        config = CopyBotConfig.from_dict(
            {
                "accounts": [
                    fake_account("master").to_dict(),
                    fake_account("slave").to_dict(),
                ],
                "copy_groups": [
                    {
                        "group_id": "g1",
                        "master_account_id": "master",
                        "slave_account_ids": ["slave"],
                        "enabled": True,
                    }
                ],
            }
        )
        engine = CopyEngine()
        await engine.apply_config(config, sync_after_load=False)
        engine.registry.get_client("master").snapshot = fake_snapshot(
            "master",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            total_equity=1000,
        )
        engine.registry.get_client("slave").snapshot = fake_snapshot("slave", [], cash=1000, total_equity=1000)

        state = await engine.sync_group("g1", force=True)
        slave_client = engine.registry.get_client("slave")

        self.assertEqual(len(state["last_orders"]), 1)
        self.assertEqual(slave_client.orders, [])

    async def test_multiple_master_trees_are_independent(self):
        config = CopyBotConfig.from_dict(
            {
                "accounts": [
                    fake_account("master-a").to_dict(),
                    fake_account("slave-a").to_dict(),
                    fake_account("master-b").to_dict(),
                    fake_account("slave-b").to_dict(),
                ],
                "copy_groups": [
                    {
                        "group_id": "g-a",
                        "master_account_id": "master-a",
                        "slave_account_ids": ["slave-a"],
                    },
                    {
                        "group_id": "g-b",
                        "master_account_id": "master-b",
                        "slave_account_ids": ["slave-b"],
                    },
                ],
            }
        )
        engine = CopyEngine()
        await engine.apply_config(config, sync_after_load=False)
        engine.registry.get_client("master-a").snapshot = fake_snapshot(
            "master-a",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            total_equity=1000,
        )
        engine.registry.get_client("slave-a").snapshot = fake_snapshot("slave-a", [], cash=1000, total_equity=1000)
        engine.registry.get_client("master-b").snapshot = fake_snapshot(
            "master-b",
            [{"symbol": "B", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            total_equity=1000,
        )
        engine.registry.get_client("slave-b").snapshot = fake_snapshot("slave-b", [], cash=1000, total_equity=1000)

        await engine.sync_group("g-a", force=True)

        self.assertEqual(engine.group_state["g-a"]["last_orders"][0]["symbol"], "A")
        self.assertEqual(engine.group_state["g-b"]["last_orders"], [])

    async def test_master_price_only_change_is_unchanged(self):
        config = CopyBotConfig.from_dict(
            {
                "accounts": [
                    fake_account("master").to_dict(),
                    fake_account("slave").to_dict(),
                ],
                "copy_groups": [
                    {
                        "group_id": "g1",
                        "master_account_id": "master",
                        "slave_account_ids": ["slave"],
                    }
                ],
            }
        )
        engine = CopyEngine()
        await engine.apply_config(config, sync_after_load=False)
        engine.registry.get_client("master").snapshot = fake_snapshot(
            "master",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
        )
        engine.registry.get_client("slave").snapshot = fake_snapshot(
            "slave",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
        )

        await engine.sync_group("g1", force=True)

        engine.registry.get_client("master").snapshot = fake_snapshot(
            "master",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 101}],
        )

        state = await engine.sync_group("g1", force=False)

        self.assertFalse(state["master_changed"])
        self.assertEqual(state["last_message"], "master unchanged")

    async def test_sync_slave_cancels_open_orders_before_snapshot(self):
        config = CopyBotConfig.from_dict(
            {
                "accounts": [
                    fake_account("master").to_dict(),
                    fake_account("slave", mode=AccountMode.LIVE).to_dict(),
                ],
                "copy_groups": [
                    {
                        "group_id": "g1",
                        "master_account_id": "master",
                        "slave_account_ids": ["slave"],
                        "mode": "live",
                    }
                ],
            }
        )
        engine = CopyEngine()
        await engine.apply_config(config, sync_after_load=False)
        engine.registry.get_client("master").snapshot = fake_snapshot(
            "master",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            total_equity=1000,
        )
        slave_client = engine.registry.get_client("slave")
        slave_client.snapshot = fake_snapshot("slave", [], cash=1000, total_equity=1000)
        slave_client.open_orders = [
            OpenOrder(
                account_id="slave",
                order_id="open-1",
                symbol="A",
                exchange="KRX",
                side=OrderSide.BUY,
                quantity=3,
                remaining_quantity=3,
                price=100,
            )
        ]
        slave_client.quotes["KRX:A"] = Quote(
            symbol="A",
            exchange="KRX",
            last_price=100,
            ask_price_1=100,
            bid_price_1=99,
        )

        state = await engine.sync_group("g1", force=True)

        self.assertEqual(slave_client.open_orders, [])
        self.assertEqual(slave_client.cancelled_orders[0].order_id, "open-1")
        self.assertLess(
            slave_client.events.index("cancel_order"),
            slave_client.events.index("get_portfolio_snapshot"),
        )
        self.assertEqual(state["slaves"]["slave"]["cancel_results"][0]["order"]["order_id"], "open-1")

    async def test_live_mode_places_orders_when_supported(self):
        config = CopyBotConfig.from_dict(
            {
                "accounts": [
                    fake_account("master").to_dict(),
                    fake_account("slave", mode=AccountMode.LIVE).to_dict(),
                ],
                "copy_groups": [
                    {
                        "group_id": "g1",
                        "master_account_id": "master",
                        "slave_account_ids": ["slave"],
                        "mode": "live",
                    }
                ],
            }
        )
        engine = CopyEngine()
        await engine.apply_config(config, sync_after_load=False)
        engine.registry.get_client("master").snapshot = fake_snapshot(
            "master",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            total_equity=1000,
        )
        engine.registry.get_client("slave").snapshot = fake_snapshot("slave", [], cash=1000, total_equity=1000)
        engine.registry.get_client("slave").quotes["KRX:A"] = Quote(
            symbol="A",
            exchange="KRX",
            last_price=100,
            ask_price_1=105,
            bid_price_1=99,
        )

        await engine.sync_group("g1", force=True)
        slave_client = engine.registry.get_client("slave")

        self.assertEqual(len(slave_client.orders), 1)
        self.assertEqual(slave_client.orders[0].order_type, OrderType.LIMIT)
        self.assertEqual(slave_client.orders[0].limit_price, 105)

    async def test_live_buy_quantity_is_limited_by_cash_buffer(self):
        config = CopyBotConfig.from_dict(
            {
                "accounts": [
                    fake_account("master").to_dict(),
                    fake_account("slave", mode=AccountMode.LIVE).to_dict(),
                ],
                "copy_groups": [
                    {
                        "group_id": "g1",
                        "master_account_id": "master",
                        "slave_account_ids": ["slave"],
                        "mode": "live",
                        "cash_safety_buffer": 0.02,
                    }
                ],
            }
        )
        engine = CopyEngine()
        await engine.apply_config(config, sync_after_load=False)
        engine.registry.get_client("master").snapshot = fake_snapshot(
            "master",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            total_equity=1000,
        )
        engine.registry.get_client("slave").snapshot = fake_snapshot("slave", [], cash=250, total_equity=1000)
        engine.registry.get_client("slave").quotes["KRX:A"] = Quote(
            symbol="A",
            exchange="KRX",
            last_price=100,
            ask_price_1=100,
            bid_price_1=99,
        )

        await engine.sync_group("g1", force=True)
        slave_client = engine.registry.get_client("slave")

        self.assertEqual(len(slave_client.orders), 1)
        self.assertEqual(slave_client.orders[0].quantity, 2)
        self.assertEqual(slave_client.orders[0].estimated_value, 200)

    async def test_live_sells_are_market_then_buys_use_refreshed_cash(self):
        config = CopyBotConfig.from_dict(
            {
                "accounts": [
                    fake_account("master").to_dict(),
                    fake_account("slave", mode=AccountMode.LIVE).to_dict(),
                ],
                "copy_groups": [
                    {
                        "group_id": "g1",
                        "master_account_id": "master",
                        "slave_account_ids": ["slave"],
                        "mode": "live",
                        "cash_safety_buffer": 0.02,
                    }
                ],
            }
        )
        engine = CopyEngine()
        await engine.apply_config(config, sync_after_load=False)
        engine.registry.get_client("master").snapshot = fake_snapshot(
            "master",
            [{"symbol": "A", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            total_equity=1000,
        )
        engine.registry.get_client("slave").snapshot = fake_snapshot(
            "slave",
            [{"symbol": "C", "exchange": "KRX", "quantity": 10, "current_price": 100}],
            cash=0,
            total_equity=1000,
        )
        engine.registry.get_client("slave").quotes["KRX:A"] = Quote(
            symbol="A",
            exchange="KRX",
            last_price=100,
            ask_price_1=100,
            bid_price_1=99,
        )

        await engine.sync_group("g1", force=True)
        slave_client = engine.registry.get_client("slave")

        self.assertEqual([order.side for order in slave_client.orders], [OrderSide.SELL, OrderSide.BUY])
        self.assertEqual(slave_client.orders[0].order_type, OrderType.MARKET)
        self.assertEqual(slave_client.orders[1].order_type, OrderType.LIMIT)
        self.assertEqual(slave_client.orders[1].quantity, 9)


if __name__ == "__main__":
    unittest.main()

import unittest

from core.PortfolioRebalancer import PortfolioRebalancer
from core.schemas import AccountConfig, CopyGroupConfig, Holding, PortfolioSnapshot
from core.types import AccountMode, BrokerName, MarketScope, OrderSide, SyncRunMode


class PortfolioRebalancerTests(unittest.TestCase):
    def setUp(self):
        self.rebalancer = PortfolioRebalancer()
        self.group = CopyGroupConfig(
            group_id="g1",
            master_account_id="master",
            slave_account_ids=["slave"],
            min_trade_value=0,
        )
        self.slave_account = AccountConfig(
            account_id="slave",
            broker=BrokerName.FAKE,
            market_scope=MarketScope.DOMESTIC,
            credentials_ref="SLAVE",
        )

    def test_builds_weight_based_orders(self):
        master = PortfolioSnapshot(
            account_id="master",
            total_equity=1000,
            cash=100,
            holdings=[
                Holding(symbol="A", exchange="KRX", quantity=6, current_price=100, market_value=600),
                Holding(symbol="B", exchange="KRX", quantity=3, current_price=100, market_value=300),
            ],
        )
        slave = PortfolioSnapshot(account_id="slave", total_equity=500, cash=500, holdings=[])

        orders = self.rebalancer.build_orders(self.group, master, slave, self.slave_account)

        self.assertEqual([(order.symbol, order.side, order.quantity) for order in orders], [
            ("A", OrderSide.BUY, 3),
            ("B", OrderSide.BUY, 1),
        ])
        self.assertTrue(all(order.mode == SyncRunMode.DRY_RUN for order in orders))

    def test_sells_slave_only_positions_first(self):
        master = PortfolioSnapshot(
            account_id="master",
            total_equity=1000,
            cash=500,
            holdings=[Holding(symbol="A", exchange="KRX", quantity=5, current_price=100, market_value=500)],
        )
        slave = PortfolioSnapshot(
            account_id="slave",
            total_equity=1000,
            cash=0,
            holdings=[
                Holding(symbol="A", exchange="KRX", quantity=1, current_price=100, market_value=100),
                Holding(symbol="C", exchange="KRX", quantity=2, current_price=100, market_value=200),
            ],
        )

        orders = self.rebalancer.build_orders(self.group, master, slave, self.slave_account)

        self.assertEqual(orders[0].symbol, "C")
        self.assertEqual(orders[0].side, OrderSide.SELL)
        self.assertEqual(orders[0].quantity, 2)
        self.assertEqual(orders[1].symbol, "A")
        self.assertEqual(orders[1].side, OrderSide.BUY)

    def test_min_trade_value_skips_small_orders(self):
        group = CopyGroupConfig(
            group_id="g1",
            master_account_id="master",
            slave_account_ids=["slave"],
            min_trade_value=1000,
        )
        master = PortfolioSnapshot(
            account_id="master",
            total_equity=1000,
            cash=900,
            holdings=[Holding(symbol="A", exchange="KRX", quantity=1, current_price=100, market_value=100)],
        )
        slave = PortfolioSnapshot(account_id="slave", total_equity=1000, cash=1000, holdings=[])

        self.assertEqual(self.rebalancer.build_orders(group, master, slave, self.slave_account), [])

    def test_live_mode_requires_group_and_account_live(self):
        group = CopyGroupConfig(
            group_id="g1",
            master_account_id="master",
            slave_account_ids=["slave"],
            mode=AccountMode.LIVE,
        )
        slave_account = AccountConfig(
            account_id="slave",
            broker=BrokerName.FAKE,
            market_scope=MarketScope.DOMESTIC,
            credentials_ref="SLAVE",
            mode=AccountMode.LIVE,
        )
        master = PortfolioSnapshot(
            account_id="master",
            total_equity=1000,
            cash=0,
            holdings=[Holding(symbol="A", exchange="KRX", quantity=10, current_price=100, market_value=1000)],
        )
        slave = PortfolioSnapshot(account_id="slave", total_equity=1000, cash=1000, holdings=[])

        orders = self.rebalancer.build_orders(group, master, slave, slave_account)

        self.assertEqual(orders[0].mode, SyncRunMode.LIVE)


if __name__ == "__main__":
    unittest.main()

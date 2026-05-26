import asyncio
import unittest

from broker.base import BrokerCredentials
from broker.db import DBBrokerClient
from broker.kiwoom import KiwoomBrokerClient
from broker.miraeasset import MiraeAssetBrokerClient
from core.schemas import AccountConfig
from core.types import AccountMode, BrokerName, MarketScope


class BrokerImplementationTests(unittest.TestCase):
    def test_db_capabilities_cover_domestic_and_global(self):
        client = DBBrokerClient(
            self._account("db-master", BrokerName.DB, MarketScope.GLOBAL),
            BrokerCredentials(ref="DB_MASTER"),
        )

        capabilities = client.get_capabilities()

        self.assertTrue(capabilities.supports_domestic_stock)
        self.assertTrue(capabilities.supports_global_stock)
        self.assertTrue(capabilities.supports_market_order)
        self.assertTrue(capabilities.supports_limit_order)

    def test_kiwoom_capabilities_are_domestic_only(self):
        client = KiwoomBrokerClient(
            self._account("kiwoom-slave", BrokerName.KIWOOM, MarketScope.DOMESTIC),
            BrokerCredentials(ref="KIWOOM_SLAVE"),
        )

        capabilities = client.get_capabilities()

        self.assertTrue(capabilities.supports_domestic_stock)
        self.assertFalse(capabilities.supports_global_stock)
        self.assertTrue(capabilities.supports_limit_order)

    def test_token_payloads_match_broker_oauth_shapes(self):
        db = DBBrokerClient(
            self._account("db-master", BrokerName.DB, MarketScope.DOMESTIC),
            BrokerCredentials(ref="DB_MASTER", app_key="key", app_secret="secret"),
        )
        kiwoom = KiwoomBrokerClient(
            self._account("kiwoom-slave", BrokerName.KIWOOM, MarketScope.DOMESTIC),
            BrokerCredentials(ref="KIWOOM_SLAVE", app_key="key", app_secret="secret"),
        )

        self.assertEqual(db._token_request_body()["appsecretkey"], "secret")
        self.assertEqual(kiwoom._token_request_body()["secretkey"], "secret")

    def test_miraeasset_shell_is_not_live_capable(self):
        client = MiraeAssetBrokerClient(
            self._account("mirae", BrokerName.MIRAE_ASSET, MarketScope.DOMESTIC),
            BrokerCredentials(ref="MIRAE"),
        )

        connected = asyncio.run(client.connect())
        capabilities = client.get_capabilities()

        self.assertFalse(connected)
        self.assertFalse(capabilities.supports_live_trading)
        self.assertFalse(capabilities.supports_domestic_stock)

    def _account(self, account_id, broker, market_scope):
        return AccountConfig(
            account_id=account_id,
            broker=broker,
            market_scope=market_scope,
            credentials_ref=account_id,
            mode=AccountMode.DRY_RUN,
        )


if __name__ == "__main__":
    unittest.main()

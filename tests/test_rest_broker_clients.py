from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import unittest

from broker.base import BrokerCredentials, BrokerError
from broker.db import DBBrokerClient
from broker.kiwoom import KiwoomBrokerClient
from core.schemas import AccountConfig, TargetOrder
from core.types import AccountMode, BrokerName, MarketScope, OrderSide, OrderType, SyncRunMode


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None, data=None):
        self.requests.append({"url": url, "headers": headers or {}, "json": json, "data": data})
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.popleft()


async def no_rate_limit(*args, **kwargs):
    return None


def account(account_id, broker, market_scope):
    return AccountConfig(
        account_id=account_id,
        broker=broker,
        market_scope=market_scope,
        credentials_ref=account_id,
        mode=AccountMode.LIVE,
    )


def order(broker, market_scope, side=OrderSide.BUY, order_type=OrderType.LIMIT, exchange=None):
    return TargetOrder(
        group_id="g",
        account_id="a",
        broker=broker,
        market_scope=market_scope,
        symbol="005930" if market_scope == MarketScope.DOMESTIC else "TSLA",
        exchange=exchange or ("KRX" if market_scope == MarketScope.DOMESTIC else "US"),
        side=side,
        quantity=3,
        estimated_price=100.0,
        estimated_value=300.0,
        mode=SyncRunMode.LIVE,
        order_type=order_type,
        limit_price=101.0 if order_type == OrderType.LIMIT else None,
    )


def ready(client, now):
    client.access_token = "old-token"
    client.connected = True
    client._now_utc = lambda: now
    client._last_token_refresh_key = client._market_refresh_key()
    client._rate_limit = no_rate_limit


class DBRestBrokerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_db_token_request_uses_form_shape(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.DOMESTIC),
            BrokerCredentials(ref="DB", app_key="key", app_secret="secret"),
        )
        fake = FakeHttpClient([FakeResponse({"access_token": "db-token", "expires_in": 86400})])
        client._http_client = lambda: fake
        client._now_utc = lambda: datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc)

        self.assertTrue(await client.refresh_token())

        request = fake.requests[0]
        self.assertTrue(request["url"].endswith("/oauth2/token"))
        self.assertEqual(request["headers"]["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(request["data"]["appsecretkey"], "secret")
        self.assertEqual(request["data"]["scope"], "oob")
        self.assertEqual(client.access_token, "db-token")

    async def test_db_domestic_balance_quote_and_order_mapping(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.DOMESTIC),
            BrokerCredentials(ref="DB"),
        )
        ready(client, datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient(
            [
                FakeResponse(
                    {
                        "Out": {"Dps2": "1000000", "DpsastAmt": "1550000", "TotEvalAmt": "550000"},
                        "Out1": [
                            {"IsuNo": "A005930", "BalQty0": "0000000000010", "NowPrc": "55,000", "EvalAmt": "550000"}
                        ],
                        "rsp_cd": "00000",
                    }
                ),
                FakeResponse({"Out": {"Prpr": "55500", "Askp1": "", "Bidp1": ""}, "rsp_cd": "00000"}),
                FakeResponse({"Out": {"Askp1": "55600", "Bidp1": "55500"}, "rsp_cd": "00000"}),
                FakeResponse({"Out": {"OrdNo": 1234}, "rsp_cd": "00000", "rsp_msg": "ok"}),
            ]
        )
        client._http_client = lambda: fake

        snapshot = await client.get_portfolio_snapshot()
        quote = await client.get_quote("A005930")
        result = await client.place_order(order(BrokerName.DB, MarketScope.DOMESTIC))

        self.assertEqual(snapshot.cash, 1000000)
        self.assertEqual(snapshot.total_equity, 1550000)
        self.assertEqual(snapshot.holdings[0].symbol, "005930")
        self.assertEqual(snapshot.holdings[0].exchange, "KRX")
        self.assertEqual(quote.last_price, 55500)
        self.assertEqual(quote.ask_price_1, 55600)
        self.assertEqual(result.order_id, "1234")

        self.assertEqual(fake.requests[0]["json"], {"In": {"QryTpCode": "0"}})
        self.assertEqual(fake.requests[1]["json"]["In"]["InputCondMrktDivCode"], "J")
        self.assertEqual(fake.requests[3]["json"]["In"]["BnsTpCode"], "2")
        self.assertEqual(fake.requests[3]["json"]["In"]["OrdprcPtnCode"], "00")

    async def test_db_global_balance_quote_and_order_mapping(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.GLOBAL),
            BrokerCredentials(ref="DB"),
        )
        ready(client, datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient(
            [
                FakeResponse(
                    {
                        "Out": {"Dps": "5000", "AssetAmtTotamt": "10000"},
                        "Out2": [
                            {
                                "AstkIsuNo": "TSLA.US",
                                "SymCode": "TSLA",
                                "AstkSeNm": "뉴욕",
                                "CrcyCode": "USD",
                                "AstkSettBaseQty": "2.000000",
                                "AstkNowPrc": "200.5000",
                                "AstkEvalAmt": "401.0000",
                            }
                        ],
                        "rsp_cd": "00000",
                    }
                ),
                FakeResponse(
                    {
                        "Out1": [
                            {
                                "CrcyCode": "USD",
                                "AstkOrdAbleAmt": "5000",
                            }
                        ],
                        "rsp_cd": "00000",
                    }
                ),
                FakeResponse({"Out": {"Prpr": "200.0000", "askp1": "201.0000", "bidp1": "199.0000"}, "rsp_cd": "00000"}),
                FakeResponse({"Out": {"OrdNo": 77}, "rsp_cd": "00000"}),
            ]
        )
        client._http_client = lambda: fake

        snapshot = await client.get_portfolio_snapshot()
        quote = await client.get_quote("TSLA")
        result = await client.place_order(order(BrokerName.DB, MarketScope.GLOBAL, order_type=OrderType.MARKET))

        self.assertEqual(snapshot.currency, "USD")
        self.assertEqual(snapshot.holdings[0].symbol, "TSLA")
        self.assertEqual(snapshot.holdings[0].exchange, "FY")
        self.assertEqual(snapshot.holdings[0].market_value, 401.0)
        self.assertEqual(quote.ask_price_1, 201.0)
        self.assertEqual(result.order_id, "77")
        self.assertEqual(fake.requests[2]["json"]["In"]["InputCondMrktDivCode"], "FN")
        self.assertEqual(fake.requests[3]["json"]["In"]["AstkOrdprcPtnCode"], "2")

    async def test_db_global_order_does_not_send_unknown_market_code(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.GLOBAL),
            BrokerCredentials(ref="DB"),
        )
        ready(client, datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient([FakeResponse({"Out": {"OrdNo": 88}, "rsp_cd": "00000"})])
        client._http_client = lambda: fake

        result = await client.place_order(
            order(BrokerName.DB, MarketScope.GLOBAL, order_type=OrderType.MARKET, exchange="FY")
        )

        self.assertEqual(result.order_id, "88")
        self.assertNotIn("AstkMktCode", fake.requests[0]["json"]["In"])

    async def test_db_domestic_cancels_open_orders(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.DOMESTIC),
            BrokerCredentials(ref="DB"),
        )
        ready(client, datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient(
            [
                FakeResponse(
                    {
                        "Out1": [
                            {
                                "OrdNo": 123,
                                "IsuNo": "A005930",
                                "BnsTpCode": "2",
                                "OrdQty": 10,
                                "AllExecQty": 3,
                                "MrcAbleQty": 7,
                                "OrdPrc": "55000.00",
                            }
                        ],
                        "rsp_cd": "00000",
                    }
                ),
                FakeResponse({"Out": {"OrdNo": 124, "PrntOrdNo": 123}, "rsp_cd": "00000"}),
            ]
        )
        client._http_client = lambda: fake

        results = await client.cancel_open_orders()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].accepted)
        self.assertEqual(results[0].order.remaining_quantity, 7)
        self.assertTrue(fake.requests[0]["url"].endswith("/api/v1/trading/kr-stock/inquiry/transaction-history"))
        self.assertTrue(fake.requests[1]["url"].endswith("/api/v1/trading/kr-stock/order-cancel"))
        self.assertEqual(fake.requests[1]["json"]["In"]["OrgOrdNo"], 123)
        self.assertEqual(fake.requests[1]["json"]["In"]["OrdQty"], 7)

    async def test_db_global_cancels_open_orders(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.GLOBAL),
            BrokerCredentials(ref="DB"),
        )
        ready(client, datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient(
            [
                FakeResponse(
                    {
                        "Out": [
                            {
                                "OrdNo": 77,
                                "SymCode": "TSLA",
                                "AstkBnsTpCode": "2",
                                "AstkOrdQty": "10.000000",
                                "AstkExecQty": "4.000000",
                                "AstkOrdRmqty": "6.000000",
                                "AstkOrdPrc": "200.000000",
                                "AstkSeNm": "NASDAQ",
                                "OrdTrdTpCode": "0",
                            }
                        ],
                        "rsp_cd": "00000",
                    }
                ),
                FakeResponse({"Out": {"OrdNo": 78}, "rsp_cd": "00000"}),
            ]
        )
        client._http_client = lambda: fake

        results = await client.cancel_open_orders()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].accepted)
        self.assertEqual(results[0].order.remaining_quantity, 6)
        self.assertTrue(fake.requests[0]["url"].endswith("/api/v1/trading/overseas-stock/inquiry/transaction-history"))
        self.assertTrue(fake.requests[1]["url"].endswith("/api/v1/trading/overseas-stock/order"))
        self.assertEqual(fake.requests[1]["json"]["In"]["OrdTrdTpCode"], "2")
        self.assertEqual(fake.requests[1]["json"]["In"]["OrgOrdNo"], 77)

    async def test_db_market_open_refresh_and_error_handling(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.DOMESTIC),
            BrokerCredentials(ref="DB", app_key="key", app_secret="secret"),
        )
        client.access_token = "old-token"
        client.connected = True
        client._now_utc = lambda: datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc)
        client._rate_limit = no_rate_limit
        fake = FakeHttpClient(
            [
                FakeResponse({"access_token": "new-token", "expires_in": 86400}),
                FakeResponse({"ok": True, "rsp_cd": "00000"}),
                FakeResponse({"rsp_cd": "99999", "rsp_msg": "failed"}),
            ]
        )
        client._http_client = lambda: fake

        payload = await client._request_json("/api/test", {"In": {}}, "PRICE")

        self.assertEqual(payload["ok"], True)
        self.assertEqual(fake.requests[1]["headers"]["authorization"], "Bearer new-token")
        with self.assertRaises(BrokerError):
            await client._request_json("/api/test", {"In": {}}, "PRICE")

    async def test_db_http_error_is_broker_error(self):
        client = DBBrokerClient(
            account("db", BrokerName.DB, MarketScope.GLOBAL),
            BrokerCredentials(ref="DB"),
        )
        ready(client, datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient([FakeResponse({"rsp_msg": "모의투자 장종료 입니다."}, status_code=500)])
        client._http_client = lambda: fake

        with self.assertRaisesRegex(BrokerError, "모의투자 장종료"):
            await client.get_open_orders()


class KiwoomRestBrokerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_kiwoom_token_request_uses_json_shape(self):
        client = KiwoomBrokerClient(
            account("kiwoom", BrokerName.KIWOOM, MarketScope.DOMESTIC),
            BrokerCredentials(ref="KIWOOM", app_key="key", app_secret="secret"),
        )
        fake = FakeHttpClient([FakeResponse({"token": "kw-token", "expires_dt": "20260526235959"})])
        client._http_client = lambda: fake
        client._now_utc = lambda: datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc)

        self.assertTrue(await client.refresh_token())

        request = fake.requests[0]
        self.assertTrue(request["url"].endswith("/oauth2/token"))
        self.assertEqual(request["json"]["secretkey"], "secret")
        self.assertEqual(client.access_token, "kw-token")

    async def test_kiwoom_balance_quote_and_order_mapping(self):
        client = KiwoomBrokerClient(
            account("kiwoom", BrokerName.KIWOOM, MarketScope.DOMESTIC),
            BrokerCredentials(ref="KIWOOM"),
        )
        ready(client, datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient(
            [
                FakeResponse(
                    {
                        "return_code": 0,
                        "tot_evlt_amt": "550000",
                        "prsm_dpst_aset_amt": "1550000",
                        "acnt_evlt_remn_indv_tot": [
                            {"stk_cd": "A005930", "rmnd_qty": "0000000000010", "cur_prc": "-55000", "evlt_amt": "550000"}
                        ],
                    }
                ),
                FakeResponse({"return_code": 0, "ord_alow_amt": "1000000"}),
                FakeResponse({"return_code": 0, "sel_fpr_bid": "55600", "buy_fpr_bid": "55500"}),
                FakeResponse({"return_code": 0, "cur_prc": "-55500"}),
                FakeResponse({"return_code": 0, "ord_no": "999"}),
            ]
        )
        client._http_client = lambda: fake

        snapshot = await client.get_portfolio_snapshot()
        quote = await client.get_quote("005930", "KRX")
        result = await client.place_order(order(BrokerName.KIWOOM, MarketScope.DOMESTIC, side=OrderSide.SELL, order_type=OrderType.MARKET))

        self.assertEqual(snapshot.cash, 1000000)
        self.assertEqual(snapshot.holdings[0].symbol, "005930")
        self.assertEqual(snapshot.holdings[0].current_price, 55000)
        self.assertEqual(quote.last_price, 55500)
        self.assertEqual(quote.ask_price_1, 55600)
        self.assertEqual(result.order_id, "999")

        self.assertEqual(fake.requests[0]["headers"]["api-id"], "kt00018")
        self.assertEqual(fake.requests[1]["headers"]["api-id"], "kt00001")
        self.assertEqual(fake.requests[2]["headers"]["api-id"], "ka10004")
        self.assertEqual(fake.requests[4]["headers"]["api-id"], "kt10001")
        self.assertEqual(fake.requests[4]["json"]["trde_tp"], "3")

    async def test_kiwoom_cancels_open_orders(self):
        client = KiwoomBrokerClient(
            account("kiwoom", BrokerName.KIWOOM, MarketScope.DOMESTIC),
            BrokerCredentials(ref="KIWOOM"),
        )
        ready(client, datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient(
            [
                FakeResponse(
                    {
                        "return_code": 0,
                        "acnt_ord_cntr_prst_array": [
                            {
                                "ord_no": "123",
                                "stk_cd": "A005930",
                                "trde_tp": "2",
                                "ord_qty": "10",
                                "cntr_qty": "3",
                                "ord_uv": "55000",
                                "dmst_stex_tp": "KRX",
                            }
                        ],
                    }
                ),
                FakeResponse({"return_code": 0, "ord_no": "123"}),
            ]
        )
        client._http_client = lambda: fake

        results = await client.cancel_open_orders()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].accepted)
        self.assertEqual(results[0].order.remaining_quantity, 7)
        self.assertEqual(fake.requests[0]["headers"]["api-id"], "kt00009")
        self.assertEqual(fake.requests[1]["headers"]["api-id"], "kt10003")
        self.assertEqual(fake.requests[1]["json"]["orig_ord_no"], "123")
        self.assertEqual(fake.requests[1]["json"]["ord_qty"], "0")

    async def test_kiwoom_token_expired_retry_and_error_handling(self):
        client = KiwoomBrokerClient(
            account("kiwoom", BrokerName.KIWOOM, MarketScope.DOMESTIC),
            BrokerCredentials(ref="KIWOOM", app_key="key", app_secret="secret"),
        )
        ready(client, datetime(2026, 5, 26, 1, 0, tzinfo=timezone.utc))
        fake = FakeHttpClient(
            [
                FakeResponse({"return_code": 3, "return_msg": "token expired"}),
                FakeResponse({"token": "new-token", "expires_dt": "20260526235959"}),
                FakeResponse({"return_code": 0, "value": "ok"}),
                FakeResponse({"return_code": 99, "return_msg": "failed"}),
            ]
        )
        client._http_client = lambda: fake

        payload = await client._request_json("/api/test", {"x": 1}, "ka10004")

        self.assertEqual(payload["value"], "ok")
        self.assertEqual(fake.requests[0]["headers"]["authorization"], "Bearer old-token")
        self.assertEqual(fake.requests[2]["headers"]["authorization"], "Bearer new-token")
        with self.assertRaises(BrokerError):
            await client._request_json("/api/test", {"x": 1}, "ka10004")


if __name__ == "__main__":
    unittest.main()

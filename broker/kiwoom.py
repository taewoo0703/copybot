from __future__ import annotations

from core.types import MarketScope, OrderSide

from .base import BrokerCapabilities
from .rest import OAuthRestBrokerClient


class KiwoomBrokerClient(OAuthRestBrokerClient):
    default_base_url = "https://api.kiwoom.com"

    def __init__(self, account, credentials):
        super().__init__(account, credentials)
        if credentials.is_mock and not account.base_url and not credentials.base_url:
            self.base_url = "https://mockapi.kiwoom.com"

    def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker="kiwoom",
            supports_domestic_stock=True,
            supports_global_stock=False,
            supports_market_order=True,
            supports_limit_order=True,
            supports_live_trading=True,
            supports_fractional_quantity=False,
            notes="Kiwoom REST API public guide exposes domestic stock categories; global scope is disabled.",
        )

    def _token_request_body(self) -> dict:
        return {
            "grant_type": "client_credentials",
            "appkey": self.credentials.app_key,
            "secretkey": self.credentials.app_secret,
        }

    def _order_request_body(self, order) -> dict:
        body = super()._order_request_body(order)
        body.update(
            {
                "broker": "kiwoom",
                "ord_dvsn": "00" if order.limit_price else "03",
                "trde_tp": "BUY" if order.side == OrderSide.BUY else "SELL",
                "mrkt_tp": "KRX" if order.market_scope == MarketScope.DOMESTIC else order.exchange,
                "ord_prc": order.limit_price or 0,
            }
        )
        return body

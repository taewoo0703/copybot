from __future__ import annotations

from core.types import MarketScope, OrderSide

from .base import BrokerCapabilities
from .rest import OAuthRestBrokerClient


class DBBrokerClient(OAuthRestBrokerClient):
    default_base_url = "https://openapi.dbsec.co.kr:8443"

    def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker="db",
            supports_domestic_stock=True,
            supports_global_stock=True,
            supports_market_order=True,
            supports_limit_order=True,
            supports_live_trading=True,
            supports_fractional_quantity=False,
            notes="DB Open API supports domestic and global stock categories; endpoint TR paths are configured per credential ref.",
        )

    def _token_request_body(self) -> dict:
        return {
            "grant_type": "client_credentials",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
        }

    def _order_request_body(self, order) -> dict:
        body = super()._order_request_body(order)
        body.update(
            {
                "broker": "db",
                "buy_sell": "2" if order.side == OrderSide.BUY else "1",
                "market_code": "KR" if order.market_scope == MarketScope.DOMESTIC else order.exchange,
                "order_price": order.limit_price or 0,
            }
        )
        return body

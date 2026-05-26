from __future__ import annotations

try:
    import httpx
except ImportError:
    httpx = None

from core.schemas import Holding, OrderResult, PortfolioSnapshot, Quote, TargetOrder
from core.types import MarketScope, OrderSide

from .base import BrokerClient, BrokerCredentials, BrokerError, BrokerFeatureUnavailable


class OAuthRestBrokerClient(BrokerClient):
    token_path = "/oauth2/token"
    default_base_url = ""

    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        self.base_url = account.base_url or credentials.base_url or self.default_base_url
        self.access_token = credentials.access_token

    async def connect(self) -> bool:
        if self.access_token:
            self.connected = True
            self.last_message = "connected with configured access token"
            return True
        return await self.refresh_token()

    async def refresh_token(self) -> bool:
        if httpx is None:
            self.connected = False
            self.last_message = "httpx is required for REST broker connections"
            return False
        if not self.credentials.app_key or not self.credentials.app_secret:
            self.connected = False
            self.last_message = "missing app key or app secret"
            return False
        if not self.base_url:
            self.connected = False
            self.last_message = "missing broker base url"
            return False

        body = self._token_request_body()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}{self.token_path}",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json=body,
            )
        response.raise_for_status()
        payload = response.json()
        token = self._extract_token(payload)
        if not token:
            self.connected = False
            self.last_message = f"token not found in response: {payload}"
            return False
        self.access_token = token
        self.connected = True
        self.last_message = "connected"
        return True

    def _token_request_body(self) -> dict:
        return {
            "grant_type": "client_credentials",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
        }

    def _extract_token(self, payload: dict) -> str | None:
        return payload.get("access_token") or payload.get("token") or payload.get("ACCESS_TOKEN")

    def _auth_headers(self, tr_id: str | None = None) -> dict[str, str]:
        if not self.access_token:
            raise BrokerError("broker access token is not available")
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": f"Bearer {self.access_token}",
        }
        if tr_id:
            headers["api-id"] = tr_id
            headers["tr_id"] = tr_id
        return headers

    async def _post_json(self, path: str, body: dict, tr_id: str | None = None) -> dict:
        if httpx is None:
            raise BrokerFeatureUnavailable("httpx is required for REST broker requests")
        if not self.connected:
            await self.connect()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=self._auth_headers(tr_id),
                json=body,
            )
        response.raise_for_status()
        return response.json()

    def _configured_path(self, key: str) -> str | None:
        return self.credentials.extra.get(key)

    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        path = self._configured_path(self._balance_path_key())
        if not path:
            raise BrokerFeatureUnavailable(
                f"{self.account.broker.value} balance endpoint is not configured for {self.account.market_scope.value}"
            )
        payload = await self._post_json(path, self._balance_request_body(), self.credentials.extra.get("BALANCE_TR_ID"))
        return self._parse_snapshot(payload)

    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        path = self._configured_path(self._quote_path_key())
        if not path:
            raise BrokerFeatureUnavailable(
                f"{self.account.broker.value} quote endpoint is not configured for {self.account.market_scope.value}"
            )
        payload = await self._post_json(
            path,
            {"symbol": symbol, "exchange": exchange, "market_scope": self.account.market_scope.value},
            self.credentials.extra.get("QUOTE_TR_ID"),
        )
        return self._parse_quote(symbol, exchange, payload)

    async def place_order(self, order: TargetOrder) -> OrderResult:
        path = self._configured_path(self._order_path_key())
        if not path:
            raise BrokerFeatureUnavailable(
                f"{self.account.broker.value} order endpoint is not configured for {self.account.market_scope.value}"
            )
        payload = await self._post_json(path, self._order_request_body(order), self.credentials.extra.get("ORDER_TR_ID"))
        order_id = str(payload.get("order_id") or payload.get("ord_no") or payload.get("OrdNo") or "")
        return OrderResult(order=order, accepted=True, order_id=order_id or None, message=str(payload))

    def _balance_path_key(self) -> str:
        return "DOMESTIC_BALANCE_PATH" if self.account.market_scope == MarketScope.DOMESTIC else "GLOBAL_BALANCE_PATH"

    def _quote_path_key(self) -> str:
        return "DOMESTIC_QUOTE_PATH" if self.account.market_scope == MarketScope.DOMESTIC else "GLOBAL_QUOTE_PATH"

    def _order_path_key(self) -> str:
        return "DOMESTIC_ORDER_PATH" if self.account.market_scope == MarketScope.DOMESTIC else "GLOBAL_ORDER_PATH"

    def _balance_request_body(self) -> dict:
        return {
            "account_no": self.credentials.account_no,
            "market_scope": self.account.market_scope.value,
        }

    def _order_request_body(self, order: TargetOrder) -> dict:
        return {
            "account_no": self.credentials.account_no,
            "symbol": order.symbol,
            "exchange": order.exchange,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": order.quantity,
            "estimated_price": order.estimated_price,
            "limit_price": order.limit_price,
            "market_scope": order.market_scope.value,
        }

    def _parse_quote(self, symbol: str, exchange: str, payload: dict) -> Quote:
        return Quote(
            symbol=str(payload.get("symbol") or payload.get("code") or payload.get("pdno") or symbol),
            exchange=str(payload.get("exchange") or exchange),
            last_price=float(
                payload.get("last_price")
                or payload.get("price")
                or payload.get("current_price")
                or payload.get("stck_prpr")
                or 0.0
            ),
            ask_price_1=float(
                payload.get("ask_price_1")
                or payload.get("ask_price")
                or payload.get("ask1")
                or payload.get("askp1")
                or payload.get("sel_fprc")
                or 0.0
            ),
            bid_price_1=float(
                payload.get("bid_price_1")
                or payload.get("bid_price")
                or payload.get("bid1")
                or payload.get("bidp1")
                or payload.get("buy_fprc")
                or 0.0
            ),
            currency=str(payload.get("currency") or ("KRW" if self.account.market_scope == MarketScope.DOMESTIC else "USD")),
        )

    def _parse_snapshot(self, payload: dict) -> PortfolioSnapshot:
        raw_holdings = payload.get("holdings") or payload.get("positions") or payload.get("items") or []
        currency = str(payload.get("currency") or ("KRW" if self.account.market_scope == MarketScope.DOMESTIC else "USD"))
        holdings = []
        for item in raw_holdings:
            symbol = str(item.get("symbol") or item.get("code") or item.get("pdno") or item.get("stk_cd") or "")
            if not symbol:
                continue
            quantity = int(float(item.get("quantity") or item.get("qty") or item.get("hldg_qty") or 0))
            price = float(item.get("current_price") or item.get("price") or item.get("prpr") or 0.0)
            market_value = float(item.get("market_value") or item.get("eval_amt") or quantity * price)
            holdings.append(
                Holding(
                    symbol=symbol,
                    exchange=str(item.get("exchange") or ""),
                    quantity=quantity,
                    current_price=price,
                    market_value=market_value,
                    currency=str(item.get("currency") or currency),
                )
            )
        cash = float(payload.get("cash") or payload.get("cash_balance") or payload.get("dnca_tot_amt") or 0.0)
        total_equity = float(payload.get("total_equity") or payload.get("total_value") or cash + sum(h.market_value for h in holdings))
        return PortfolioSnapshot(
            account_id=self.account.account_id,
            total_equity=total_equity,
            cash=cash,
            holdings=holdings,
            currency=currency,
        )

    @staticmethod
    def side_to_buy_sell(side: OrderSide) -> str:
        return "buy" if side == OrderSide.BUY else "sell"

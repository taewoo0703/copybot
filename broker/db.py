# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import httpx
except ImportError:  # pragma: no cover - exercised only when dependency is missing.
    httpx = None

from core.schemas import Holding, OrderResult, PortfolioSnapshot, Quote, TargetOrder
from core.types import MarketScope, OrderSide, OrderType

from .base import BrokerCapabilities, BrokerClient, BrokerCredentials, BrokerError, BrokerFeatureUnavailable


class DBBrokerClient(BrokerClient):
    """DB Securities direct REST broker adapter."""

    default_base_url = "https://openapi.dbsec.co.kr:8443"
    token_path = "/oauth2/token"

    domestic_balance_path = "/api/v1/trading/kr-stock/inquiry/balance"
    domestic_quote_path = "/api/v1/quote/kr-stock/inquiry/price"
    domestic_hoga_path = "/api/v1/quote/kr-stock/inquiry/orderbook"
    domestic_order_path = "/api/v1/trading/kr-stock/order"

    global_balance_path = "/api/v1/trading/overseas-stock/inquiry/balance-margin"
    global_quote_path = "/api/v1/quote/overseas-stock/inquiry/price"
    global_hoga_path = "/api/v1/quote/overseas-stock/inquiry/orderbook"
    global_order_path = "/api/v1/trading/overseas-stock/order"

    tr_ids = {
        "domestic_balance": "CSPAQ03420",
        "domestic_quote": "PRICE",
        "domestic_hoga": "HOGA",
        "domestic_order": "CSPAT00600",
        "global_balance": "CAZCQ00400",
        "global_quote": "FSTKPRICE",
        "global_hoga": "FSTKHOGA",
        "global_order": "CAZCT00100",
    }

    request_intervals = {
        "CSPAQ03420": 0.50,
        "PRICE": 0.20,
        "HOGA": 0.34,
        "CSPAT00600": 0.10,
        "CAZCQ00400": 0.34,
        "FSTKPRICE": 0.50,
        "FSTKHOGA": 0.50,
        "CAZCT00100": 0.10,
    }

    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        self.base_url = self.default_base_url
        self.access_token: str | None = None
        self.token_expires_at: datetime | None = None
        self._last_token_refresh_key: str | None = None
        self._last_request_at: dict[str, float] = {}
        self._request_lock = asyncio.Lock()

    async def connect(self) -> bool:
        if self.access_token:
            self.connected = True
            self.last_message = "connected with configured access token"
            return True
        return await self.refresh_token()

    async def refresh_token(self) -> bool:
        if not self.credentials.app_key or not self.credentials.app_secret:
            self.connected = False
            self.last_message = "missing DB app key or app secret"
            return False

        try:
            async with self._http_client() as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}{self.token_path}",
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    data=self._token_request_body(),
                )
        except BrokerFeatureUnavailable as error:
            self.connected = False
            self.last_message = str(error)
            return False
        response.raise_for_status()

        payload = response.json()
        token = self._extract_token(payload)
        if not token:
            self.connected = False
            self.last_message = f"DB token not found in response: {payload}"
            return False

        self.access_token = token
        self.token_expires_at = self._token_expiry(payload)
        self._last_token_refresh_key = self._market_refresh_key()
        self.connected = True
        self.last_message = "connected"
        return True

    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        if self.account.market_scope == MarketScope.DOMESTIC:
            payload = await self._request_all_pages(
                self.domestic_balance_path,
                self._domestic_balance_body(),
                self.tr_ids["domestic_balance"],
            )
            return self._parse_domestic_snapshot(payload)

        payload = await self._request_all_pages(
            self.global_balance_path,
            self._global_balance_body(),
            self.tr_ids["global_balance"],
        )
        return self._parse_global_snapshot(payload)

    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        if self.account.market_scope == MarketScope.DOMESTIC:
            body = self._quote_body(symbol, domestic=True)
            quote_payload = await self._request_json(
                self.domestic_quote_path,
                body,
                self.tr_ids["domestic_quote"],
            )
            quote = self._parse_quote(symbol, exchange or self._domestic_exchange(), quote_payload)
            if quote.ask_price_1 > 0 and quote.bid_price_1 > 0:
                return quote

            hoga_payload = await self._request_json(
                self.domestic_hoga_path,
                body,
                self.tr_ids["domestic_hoga"],
            )
            return self._parse_quote(symbol, exchange or self._domestic_exchange(), hoga_payload, fallback=quote)

        body = self._quote_body(symbol, domestic=False)
        quote_payload = await self._request_json(
            self.global_quote_path,
            body,
            self.tr_ids["global_quote"],
        )
        quote = self._parse_quote(symbol, exchange, quote_payload)
        if quote.ask_price_1 > 0 and quote.bid_price_1 > 0:
            return quote

        hoga_payload = await self._request_json(
            self.global_hoga_path,
            body,
            self.tr_ids["global_hoga"],
        )
        return self._parse_quote(symbol, exchange, hoga_payload, fallback=quote)

    async def place_order(self, order: TargetOrder) -> OrderResult:
        self.assert_order_supported(order)
        if order.market_scope == MarketScope.DOMESTIC:
            path = self.domestic_order_path
            tr_id = self.tr_ids["domestic_order"]
        else:
            path = self.global_order_path
            tr_id = self.tr_ids["global_order"]

        payload = await self._request_json(path, self._order_request_body(order), tr_id)
        order_id = self._extract_order_id(payload)
        return OrderResult(order=order, accepted=True, order_id=order_id, message=str(payload))

    def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker="db",
            supports_domestic_stock=True,
            supports_global_stock=True,
            supports_market_order=True,
            supports_limit_order=True,
            supports_live_trading=True,
            supports_fractional_quantity=False,
            notes="DB direct REST adapter supports domestic and overseas stock trading.",
        )

    def _http_client(self):
        if httpx is None:
            raise BrokerFeatureUnavailable("httpx is required for DB broker requests")
        return httpx.AsyncClient(timeout=10)

    def _token_request_body(self) -> dict[str, str | None]:
        return {
            "appkey": self.credentials.app_key,
            "appsecretkey": self.credentials.app_secret,
            "grant_type": "client_credentials",
            "scope": "oob",
        }

    def _extract_token(self, payload: dict[str, Any]) -> str | None:
        return payload.get("access_token") or payload.get("token") or payload.get("ACCESS_TOKEN")

    def _token_expiry(self, payload: dict[str, Any]) -> datetime | None:
        expires_in = self._parse_number(payload.get("expires_in"))
        if expires_in <= 0:
            return None
        return self._now_utc() + timedelta(seconds=max(0, int(expires_in) - 60))

    async def _ensure_token(self) -> None:
        if not self.access_token:
            if not await self.refresh_token():
                raise BrokerError(self.last_message)
            return

        should_refresh = False
        if self.token_expires_at and self._now_utc() >= self.token_expires_at:
            should_refresh = True

        refresh_key = self._market_refresh_key()
        if refresh_key and refresh_key != self._last_token_refresh_key:
            should_refresh = True

        if should_refresh:
            if await self.refresh_token():
                return
            if not self.access_token:
                raise BrokerError(self.last_message)
            self.connected = True

    async def _request_json(self, path: str, body: dict[str, Any], tr_id: str) -> dict[str, Any]:
        payload, _ = await self._request_json_with_headers(path, body, tr_id)
        return payload

    async def _request_all_pages(self, path: str, body: dict[str, Any], tr_id: str) -> dict[str, Any]:
        payload, headers = await self._request_json_with_headers(path, body, tr_id)
        combined = self._copy_payload(payload)

        while self._header_value(headers, "cont_yn").upper() == "Y":
            cont_key = self._header_value(headers, "cont_key")
            payload, headers = await self._request_json_with_headers(path, body, tr_id, cont_yn="Y", cont_key=cont_key)
            self._merge_payload(combined, payload)

        return combined

    async def _request_json_with_headers(
        self,
        path: str,
        body: dict[str, Any],
        tr_id: str,
        cont_yn: str = "",
        cont_key: str = "",
        retry_on_token: bool = True,
    ) -> tuple[dict[str, Any], Any]:
        await self._ensure_token()
        await self._rate_limit(tr_id)

        async with self._http_client() as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=self._auth_headers(cont_yn=cont_yn, cont_key=cont_key),
                json=body,
            )

        payload = self._response_json(response)
        if retry_on_token and self._is_token_expired_response(response, payload):
            if not await self.refresh_token():
                raise BrokerError(self.last_message)
            return await self._request_json_with_headers(path, body, tr_id, cont_yn, cont_key, retry_on_token=False)

        response.raise_for_status()
        self._raise_for_error(payload)
        return payload, response.headers

    async def _rate_limit(self, tr_id: str) -> None:
        interval = self.request_intervals.get(tr_id, 0.20)
        loop = asyncio.get_running_loop()
        async with self._request_lock:
            now = loop.time()
            previous = self._last_request_at.get(tr_id, 0.0)
            wait_seconds = interval - (now - previous)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                now = loop.time()
            self._last_request_at[tr_id] = now

    def _auth_headers(self, cont_yn: str = "", cont_key: str = "") -> dict[str, str]:
        if not self.access_token:
            raise BrokerError("DB access token is not available")
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "cont_yn": cont_yn,
            "cont_key": cont_key,
        }

    def _raise_for_error(self, payload: dict[str, Any]) -> None:
        code = payload.get("rsp_cd")
        if code is not None and str(code) != "00000":
            raise BrokerError(str(payload.get("rsp_msg") or payload))

    def _is_token_expired_response(self, response, payload: dict[str, Any]) -> bool:
        if getattr(response, "status_code", None) == 401:
            return True
        message = f"{payload.get('rsp_msg', '')} {payload.get('message', '')}".lower()
        return "token" in message and ("expired" in message or "invalid" in message or "만료" in message)

    def _response_json(self, response) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _domestic_exchange(self) -> str:
        return "KRX"

    def _global_market_div_code(self) -> str:
        return "FN"

    def _domestic_balance_body(self) -> dict[str, Any]:
        return {"In": {"QryTpCode": "0"}}

    def _global_balance_body(self) -> dict[str, Any]:
        return {
            "In": {
                "WonFcurrTpCode": "2",
                "TrxTpCode": "2",
                "CmsnTpCode": "2",
                "DpntBalTpCode": "1",
            }
        }

    def _quote_body(self, symbol: str, domestic: bool) -> dict[str, Any]:
        return {
            "In": {
                "InputIscd1": self._domestic_symbol(symbol) if domestic else self._global_symbol(symbol),
                "InputCondMrktDivCode": "J" if domestic else self._global_market_div_code(),
            }
        }

    def _order_request_body(self, order: TargetOrder) -> dict[str, Any]:
        if order.market_scope == MarketScope.DOMESTIC:
            is_limit = order.order_type == OrderType.LIMIT
            return {
                "In": {
                    "IsuNo": self._domestic_symbol(order.symbol),
                    "OrdQty": int(order.quantity),
                    "OrdPrc": self._order_price(order) if is_limit else 0,
                    "BnsTpCode": "2" if order.side == OrderSide.BUY else "1",
                    "OrdprcPtnCode": "00" if is_limit else "03",
                    "MgntrnCode": "000",
                    "LoanDt": "00000000",
                    "OrdCndiTpCode": "0",
                    "TrchNo": 0,
                }
            }

        is_limit = order.order_type == OrderType.LIMIT
        return {
            "In": {
                "AstkIsuNo": self._global_symbol(order.symbol),
                "AstkBnsTpCode": "2" if order.side == OrderSide.BUY else "1",
                "AstkOrdprcPtnCode": "1" if is_limit else "2",
                "AstkOrdCndiTpCode": "1",
                "AstkOrdQty": int(order.quantity),
                "AstkOrdPrc": self._order_price(order) if is_limit else 0,
                "OrdTrdTpCode": "0",
                "OrgOrdNo": 0,
            }
        }

    def _order_price(self, order: TargetOrder) -> float | int:
        price = order.limit_price if order.limit_price is not None else order.estimated_price
        return int(price) if self.account.market_scope == MarketScope.DOMESTIC else float(price)

    def _parse_domestic_snapshot(self, payload: dict[str, Any]) -> PortfolioSnapshot:
        summary = payload.get("Out") or {}
        raw_holdings = payload.get("Out1") or []
        holdings: list[Holding] = []

        for item in raw_holdings:
            symbol = self._normalize_symbol(item.get("IsuNo") or item.get("ShtnIsuNo") or item.get("symbol"))
            quantity = self._parse_int(item.get("BalQty0") or item.get("BalQty") or item.get("AbleQty"))
            if not symbol or quantity <= 0:
                continue
            price = self._parse_number(item.get("NowPrc") or item.get("ExecPrc"), absolute=True)
            market_value = self._parse_number(item.get("EvalAmt")) or quantity * price
            holdings.append(
                Holding(
                    symbol=symbol,
                    exchange=self._domestic_exchange(),
                    quantity=quantity,
                    current_price=price,
                    market_value=market_value,
                    currency="KRW",
                )
            )

        cash = self._parse_number(summary.get("Dps2") or summary.get("DpsastAmt"))
        total_equity = (
            self._parse_number(summary.get("DpsastAmt"))
            or cash + self._parse_number(summary.get("TotEvalAmt"))
            or cash + sum(holding.market_value for holding in holdings)
        )
        return PortfolioSnapshot(
            account_id=self.account.account_id,
            total_equity=total_equity,
            cash=cash,
            holdings=holdings,
            currency="KRW",
        )

    def _parse_global_snapshot(self, payload: dict[str, Any]) -> PortfolioSnapshot:
        summary = payload.get("Out") or {}
        raw_holdings = payload.get("Out2") or payload.get("Out1") or []
        holdings: list[Holding] = []

        for item in raw_holdings:
            symbol = self._global_symbol(item.get("SymCode") or item.get("AstkIsuNo") or "")
            quantity = self._parse_int(
                item.get("AstkSettBaseQty") or item.get("AstkExecBaseQty") or item.get("AstkOrdAbleQty")
            )
            if not symbol or quantity <= 0:
                continue
            price = self._parse_number(item.get("AstkNowPrc"), absolute=True)
            market_value = self._parse_number(item.get("AstkEvalAmt")) or quantity * price
            holdings.append(
                Holding(
                    symbol=symbol,
                    exchange=str(item.get("AstkMktCode") or item.get("ShtnCntrySymCode") or ""),
                    quantity=quantity,
                    current_price=price,
                    market_value=market_value,
                    currency=str(item.get("CrcyCode") or "USD"),
                )
            )

        cash = self._parse_number(summary.get("Dps") or summary.get("OrdAbleAmt"))
        total_equity = self._parse_number(summary.get("AssetAmtTotamt")) or cash + sum(
            holding.market_value for holding in holdings
        )
        return PortfolioSnapshot(
            account_id=self.account.account_id,
            total_equity=total_equity,
            cash=cash,
            holdings=holdings,
            currency="USD",
        )

    def _parse_quote(
        self,
        symbol: str,
        exchange: str,
        payload: dict[str, Any],
        fallback: Quote | None = None,
    ) -> Quote:
        data = payload.get("Out") if isinstance(payload.get("Out"), dict) else payload
        last_price = self._parse_number(
            data.get("Prpr") or data.get("prpr") or data.get("last") or (fallback.last_price if fallback else 0),
            absolute=True,
        )
        ask_price = self._parse_number(
            data.get("Askp1") or data.get("askp1") or data.get("ask") or (fallback.ask_price_1 if fallback else 0),
            absolute=True,
        )
        bid_price = self._parse_number(
            data.get("Bidp1") or data.get("bidp1") or data.get("bid") or (fallback.bid_price_1 if fallback else 0),
            absolute=True,
        )
        return Quote(
            symbol=self._normalize_symbol(symbol),
            exchange=exchange,
            last_price=last_price,
            ask_price_1=ask_price,
            bid_price_1=bid_price,
            currency="KRW" if self.account.market_scope == MarketScope.DOMESTIC else "USD",
        )

    def _extract_order_id(self, payload: dict[str, Any]) -> str | None:
        out = payload.get("Out") if isinstance(payload.get("Out"), dict) else payload
        value = out.get("OrdNo") or out.get("ord_no") or out.get("order_id")
        return str(value) if value not in (None, "") else None

    def _is_success_response(self, payload: dict[str, Any]) -> bool:
        return str(payload.get("rsp_cd", "00000")) == "00000"

    def _normalize_symbol(self, symbol: Any) -> str:
        raw = str(symbol or "").strip()
        if "." in raw:
            raw = raw.split(".", 1)[0]
        if len(raw) >= 2 and raw[0] in {"A", "J", "Q"} and raw[1:].isdigit():
            return raw[1:]
        return raw

    def _domestic_symbol(self, symbol: Any) -> str:
        return self._normalize_symbol(symbol)

    def _global_symbol(self, symbol: Any) -> str:
        return self._normalize_symbol(symbol).removeprefix("FN")

    def _parse_number(self, value: Any, absolute: bool = False) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            number = float(value)
            return abs(number) if absolute else number
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        text = re.sub(r"[^0-9+\-.]", "", text)
        if text in {"", "+", "-", ".", "+.", "-."}:
            return 0.0
        number = float(text)
        return abs(number) if absolute else number

    def _parse_int(self, value: Any) -> int:
        return int(self._parse_number(value))

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _market_refresh_key(self) -> str | None:
        if self.account.market_scope == MarketScope.GLOBAL:
            market_tz = self._timezone("America/New_York")
            open_time = time(9, 30)
            scope = "global"
        else:
            market_tz = self._timezone("Asia/Seoul")
            open_time = time(9, 0)
            scope = "domestic"

        market_now = self._now_utc().astimezone(market_tz)
        if market_now.time() < open_time:
            return None
        return f"{scope}:{market_now.date().isoformat()}"

    def _timezone(self, name: str):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            if name == "Asia/Seoul":
                return timezone(timedelta(hours=9))
            if name == "America/New_York":
                return timezone(timedelta(hours=self._us_eastern_offset_hours()))
            raise

    def _us_eastern_offset_hours(self) -> int:
        now = self._now_utc()
        year = now.year
        dst_start = self._nth_weekday_utc(year, 3, 6, 2, 7)
        dst_end = self._nth_weekday_utc(year, 11, 6, 1, 6)
        return -4 if dst_start <= now < dst_end else -5

    def _nth_weekday_utc(self, year: int, month: int, weekday: int, nth: int, hour: int) -> datetime:
        first = datetime(year, month, 1, hour, tzinfo=timezone.utc)
        delta_days = (weekday - first.weekday()) % 7
        return first + timedelta(days=delta_days + 7 * (nth - 1))

    def _header_value(self, headers: Any, key: str) -> str:
        if headers is None:
            return ""
        if hasattr(headers, "get"):
            return str(headers.get(key) or headers.get(key.replace("_", "-")) or "")
        return ""

    def _copy_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload)

    def _merge_payload(self, combined: dict[str, Any], payload: dict[str, Any]) -> None:
        for key, value in payload.items():
            current = combined.get(key)
            if isinstance(current, list) and isinstance(value, list):
                current.extend(value)
            elif isinstance(current, dict) and isinstance(value, dict):
                self._merge_payload(current, value)
            elif key not in combined:
                combined[key] = value

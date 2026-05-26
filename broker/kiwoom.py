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


class KiwoomBrokerClient(BrokerClient):
    """Kiwoom direct REST broker adapter for domestic stocks."""

    default_base_url = "https://api.kiwoom.com"
    default_mock_base_url = "https://mockapi.kiwoom.com"

    token_path = "/oauth2/token"
    account_path = "/api/dostk/acnt"
    quote_path = "/api/dostk/mrkcond"
    stock_info_path = "/api/dostk/stkinfo"
    order_path = "/api/dostk/ordr"

    tr_ids = {
        "balance": "kt00018",
        "cash": "kt00001",
        "hoga": "ka10004",
        "stock_info": "ka10001",
        "buy_order": "kt10000",
        "sell_order": "kt10001",
    }

    request_interval = 0.205
    mock_request_interval = 1.0

    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        self.base_url = self.default_base_url
        if credentials.is_mock:
            self.base_url = self.default_mock_base_url
        self.access_token: str | None = None
        self.token_expires_at: datetime | None = None
        self._last_token_refresh_key: str | None = None
        self._last_request_at = 0.0
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
            self.last_message = "missing Kiwoom app key or secret key"
            return False

        try:
            async with self._http_client() as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}{self.token_path}",
                    headers={"Content-Type": "application/json;charset=UTF-8"},
                    json=self._token_request_body(),
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
            self.last_message = f"Kiwoom token not found in response: {payload}"
            return False

        self.access_token = token
        self.token_expires_at = self._token_expiry(payload)
        self._last_token_refresh_key = self._market_refresh_key()
        self.connected = True
        self.last_message = "connected"
        return True

    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        if self.account.market_scope != MarketScope.DOMESTIC:
            raise BrokerFeatureUnavailable("kiwoom global stock trading is not supported")

        balance = await self._request_all_pages(
            self.account_path,
            self._balance_request_body(),
            self.tr_ids["balance"],
        )
        cash = await self._request_json(
            self.account_path,
            self._cash_request_body(),
            self.tr_ids["cash"],
        )
        return self._parse_snapshot(balance, cash)

    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        if self.account.market_scope != MarketScope.DOMESTIC:
            raise BrokerFeatureUnavailable("kiwoom global stock quotes are not supported")

        quote_symbol = self._quote_symbol(symbol, exchange)
        hoga = await self._request_json(
            self.quote_path,
            {"stk_cd": quote_symbol},
            self.tr_ids["hoga"],
        )
        info = await self._request_json(
            self.stock_info_path,
            {"stk_cd": quote_symbol},
            self.tr_ids["stock_info"],
        )
        return self._parse_quote(symbol, exchange or self._domestic_exchange(), hoga, info)

    async def place_order(self, order: TargetOrder) -> OrderResult:
        self.assert_order_supported(order)
        if order.market_scope != MarketScope.DOMESTIC:
            raise BrokerFeatureUnavailable("kiwoom global stock trading is not supported")

        tr_id = self.tr_ids["buy_order"] if order.side == OrderSide.BUY else self.tr_ids["sell_order"]
        payload = await self._request_json(
            self.order_path,
            self._order_request_body(order),
            tr_id,
        )
        order_id = self._extract_order_id(payload)
        return OrderResult(order=order, accepted=True, order_id=order_id, message=str(payload))

    def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker="kiwoom",
            supports_domestic_stock=True,
            supports_global_stock=False,
            supports_market_order=True,
            supports_limit_order=True,
            supports_live_trading=True,
            supports_fractional_quantity=False,
            notes="Kiwoom direct REST adapter supports domestic stock trading only.",
        )

    def _http_client(self):
        if httpx is None:
            raise BrokerFeatureUnavailable("httpx is required for Kiwoom broker requests")
        return httpx.AsyncClient(timeout=10)

    def _token_request_body(self) -> dict[str, str | None]:
        return {
            "grant_type": "client_credentials",
            "appkey": self.credentials.app_key,
            "secretkey": self.credentials.app_secret,
        }

    def _extract_token(self, payload: dict[str, Any]) -> str | None:
        return payload.get("token") or payload.get("access_token") or payload.get("ACCESS_TOKEN")

    def _token_expiry(self, payload: dict[str, Any]) -> datetime | None:
        expires_dt = payload.get("expires_dt") or payload.get("expires_at")
        if expires_dt:
            parsed = self._parse_expiry_datetime(str(expires_dt))
            if parsed:
                return parsed - timedelta(seconds=60)

        expires_in = self._parse_number(payload.get("expires_in"))
        if expires_in <= 0:
            return None
        return self._now_utc() + timedelta(seconds=max(0, int(expires_in) - 60))

    def _parse_expiry_datetime(self, value: str) -> datetime | None:
        text = value.strip()
        formats = ("%Y%m%d%H%M%S", "%Y%m%d %H%M%S", "%Y-%m-%d %H:%M:%S")
        for fmt in formats:
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=self._timezone("Asia/Seoul")).astimezone(timezone.utc)
            except ValueError:
                pass
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._timezone("Asia/Seoul"))
        return parsed.astimezone(timezone.utc)

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
        combined = dict(payload)

        while self._header_value(headers, "cont-yn").upper() == "Y":
            next_key = self._header_value(headers, "next-key")
            payload, headers = await self._request_json_with_headers(path, body, tr_id, cont_yn="Y", next_key=next_key)
            self._merge_payload(combined, payload)

        return combined

    async def _request_json_with_headers(
        self,
        path: str,
        body: dict[str, Any],
        tr_id: str,
        cont_yn: str = "N",
        next_key: str = "",
        retry_on_token: bool = True,
    ) -> tuple[dict[str, Any], Any]:
        await self._ensure_token()
        await self._rate_limit()

        async with self._http_client() as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=self._auth_headers(tr_id, cont_yn=cont_yn, next_key=next_key),
                json=body,
            )

        payload = self._response_json(response)
        if retry_on_token and self._is_token_expired_response(response, payload):
            if not await self.refresh_token():
                raise BrokerError(self.last_message)
            return await self._request_json_with_headers(path, body, tr_id, cont_yn, next_key, retry_on_token=False)

        response.raise_for_status()
        self._raise_for_error(payload)
        return payload, response.headers

    async def _rate_limit(self) -> None:
        interval = self.mock_request_interval if self.credentials.is_mock else self.request_interval
        loop = asyncio.get_running_loop()
        async with self._request_lock:
            now = loop.time()
            wait_seconds = interval - (now - self._last_request_at)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                now = loop.time()
            self._last_request_at = now

    def _auth_headers(self, tr_id: str, cont_yn: str = "N", next_key: str = "") -> dict[str, str]:
        if not self.access_token:
            raise BrokerError("Kiwoom access token is not available")
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "cont-yn": cont_yn,
            "next-key": next_key,
            "api-id": tr_id,
        }

    def _raise_for_error(self, payload: dict[str, Any]) -> None:
        if "return_code" not in payload:
            return
        code = str(payload.get("return_code"))
        if code in {"0", "20"}:
            return
        raise BrokerError(str(payload.get("return_msg") or payload.get("message") or payload))

    def _is_token_expired_response(self, response, payload: dict[str, Any]) -> bool:
        if getattr(response, "status_code", None) == 401:
            return True
        return str(payload.get("return_code")) == "3"

    def _response_json(self, response) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _domestic_exchange(self, exchange: str = "") -> str:
        value = exchange or "KRX"
        value = value.upper()
        return value if value in {"KRX", "NXT", "SOR"} else "KRX"

    def _balance_request_body(self) -> dict[str, Any]:
        return {
            "qry_tp": "1",
            "dmst_stex_tp": self._domestic_exchange(),
        }

    def _cash_request_body(self) -> dict[str, Any]:
        return {"qry_tp": "2"}

    def _order_request_body(self, order: TargetOrder) -> dict[str, str]:
        is_limit = order.order_type == OrderType.LIMIT
        return {
            "dmst_stex_tp": self._domestic_exchange(order.exchange),
            "stk_cd": self._order_symbol(order.symbol),
            "ord_qty": str(int(order.quantity)),
            "ord_uv": str(self._order_price(order) if is_limit else 0),
            "trde_tp": "0" if is_limit else "3",
            "cond_uv": "",
        }

    def _order_price(self, order: TargetOrder) -> int:
        price = order.limit_price if order.limit_price is not None else order.estimated_price
        return int(price)

    def _parse_snapshot(self, balance: dict[str, Any], cash_payload: dict[str, Any]) -> PortfolioSnapshot:
        raw_holdings = balance.get("acnt_evlt_remn_indv_tot") or balance.get("items") or []
        holdings: list[Holding] = []

        for item in raw_holdings:
            symbol = self._normalize_symbol(item.get("stk_cd") or item.get("symbol"))
            quantity = self._parse_int(item.get("rmnd_qty") or item.get("quantity"))
            if not symbol or quantity <= 0:
                continue
            price = self._parse_number(item.get("cur_prc"), absolute=True)
            market_value = self._parse_number(item.get("evlt_amt")) or quantity * price
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

        cash = self._parse_number(
            cash_payload.get("ord_alow_amt")
            or cash_payload.get("pymn_alow_amt")
            or cash_payload.get("entr")
            or cash_payload.get("d2_entra")
        )
        total_equity = (
            self._parse_number(balance.get("prsm_dpst_aset_amt"))
            or cash + self._parse_number(balance.get("tot_evlt_amt"))
            or cash + sum(holding.market_value for holding in holdings)
        )
        return PortfolioSnapshot(
            account_id=self.account.account_id,
            total_equity=total_equity,
            cash=cash,
            holdings=holdings,
            currency="KRW",
        )

    def _parse_quote(self, symbol: str, exchange: str, hoga: dict[str, Any], info: dict[str, Any]) -> Quote:
        return Quote(
            symbol=self._normalize_symbol(symbol),
            exchange=self._domestic_exchange(exchange),
            last_price=self._parse_number(info.get("cur_prc"), absolute=True),
            ask_price_1=self._parse_number(hoga.get("sel_fpr_bid"), absolute=True),
            bid_price_1=self._parse_number(hoga.get("buy_fpr_bid"), absolute=True),
            currency="KRW",
        )

    def _extract_order_id(self, payload: dict[str, Any]) -> str | None:
        value = payload.get("ord_no") or payload.get("order_id")
        return str(value) if value not in (None, "") else None

    def _normalize_symbol(self, symbol: Any) -> str:
        raw = str(symbol or "").strip()
        if "_" in raw:
            raw = raw.split("_", 1)[0]
        if len(raw) >= 2 and raw[0] in {"A", "J", "Q"} and raw[1:].isdigit():
            return raw[1:]
        return raw

    def _quote_symbol(self, symbol: Any, exchange: str = "") -> str:
        raw = str(symbol or "").strip()
        if "_" in raw:
            return raw
        normalized = self._normalize_symbol(raw)
        market = self._domestic_exchange(exchange)
        if market == "NXT":
            return f"{normalized}_NX"
        if market == "SOR":
            return f"{normalized}_AL"
        return normalized

    def _order_symbol(self, symbol: Any) -> str:
        return self._normalize_symbol(symbol)

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
        market_now = self._now_utc().astimezone(self._timezone("Asia/Seoul"))
        if market_now.time() < time(9, 0):
            return None
        return f"domestic:{market_now.date().isoformat()}"

    def _timezone(self, name: str):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            if name == "Asia/Seoul":
                return timezone(timedelta(hours=9))
            raise

    def _header_value(self, headers: Any, key: str) -> str:
        if headers is None:
            return ""
        if hasattr(headers, "get"):
            return str(headers.get(key) or "")
        return ""

    def _merge_payload(self, combined: dict[str, Any], payload: dict[str, Any]) -> None:
        for key, value in payload.items():
            current = combined.get(key)
            if isinstance(current, list) and isinstance(value, list):
                current.extend(value)
            elif isinstance(current, dict) and isinstance(value, dict):
                self._merge_payload(current, value)
            elif key not in combined:
                combined[key] = value

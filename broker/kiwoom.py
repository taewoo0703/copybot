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

    # 키움 REST API는 실서버와 모의서버 host가 다르다.
    # 이 클래스는 copybot 공통 BrokerClient 인터페이스를 키움 REST의
    # `api-id` 기반 요청 방식으로 변환하는 국내주식 전용 어댑터다.
    default_base_url = "https://api.kiwoom.com"
    default_mock_base_url = "https://mockapi.kiwoom.com"

    # 키움 REST는 업무 도메인별 endpoint를 나누고, 실제 업무 식별자는 헤더의 `api-id`에 넣는다.
    # 예: 계좌 endpoint(`/api/dostk/acnt`)에 `api-id=kt00018`을 넣으면 잔고 조회가 된다.
    token_path = "/oauth2/token"
    account_path = "/api/dostk/acnt"
    quote_path = "/api/dostk/mrkcond"
    stock_info_path = "/api/dostk/stkinfo"
    order_path = "/api/dostk/ordr"

    # 이 어댑터가 실제로 호출하는 키움 REST TR/API ID 목록.
    # - kt00018: 계좌평가잔고내역 요청. 보유종목/총평가금액을 얻는다.
    # - kt00001: 예수금상세현황 요청. 주문가능금액/예수금을 얻는다.
    # - ka10004: 주식호가 요청. 최우선 매도/매수호가를 얻는다.
    # - ka10001: 주식기본정보 요청. 현재가(`cur_prc`)를 얻는다.
    # - kt10000: 주식 매수주문. 매수 여부는 body가 아니라 api-id로 결정된다.
    # - kt10001: 주식 매도주문. 매도 여부도 api-id로 결정된다.
    tr_ids = {
        "balance": "kt00018",
        "cash": "kt00001",
        "hoga": "ka10004",
        "stock_info": "ka10001",
        "buy_order": "kt10000",
        "sell_order": "kt10001",
    }

    # 키움은 실서버와 모의서버의 요청 제한 체감이 다르므로 간격을 분리한다.
    # 초 단위 최소 간격이며, 모든 키움 REST 호출이 하나의 lock을 공유해 순차적으로 나간다.
    request_interval = 0.205
    mock_request_interval = 1.0

    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        # credentials.is_mock이 켜져 있으면 모의투자 REST host로 보낸다.
        self.base_url = self.default_base_url
        if credentials.is_mock:
            self.base_url = self.default_mock_base_url
        # 토큰은 프로세스 메모리에만 보관한다. 만료되거나 장 시작 키가 바뀌면 refresh한다.
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
                # 토큰 발급 Input reference:
                # - grant_type="client_credentials": 앱키 기반 서버 인증 방식
                # - appkey: 키움 REST 앱 키
                # - secretkey: 키움 REST 시크릿 키
                #
                # Output reference:
                # - token 또는 access_token: 이후 요청의 Bearer 토큰
                # - expires_dt 또는 expires_at: 만료 일시. 한국시간 문자열로 오는 경우가 많다.
                # - expires_in: 초 단위 만료 시간 fallback. 60초 여유를 두고 만료 처리한다.
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

        # 키움은 잔고와 현금성 금액을 별도 TR로 조회한다.
        # kt00018에서 보유종목/평가금액을, kt00001에서 예수금/주문가능금액을 가져와
        # copybot의 PortfolioSnapshot 하나로 합친다.
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

        # 현재가와 최우선 호가는 서로 다른 API ID에서 온다.
        # - ka10004: 최우선 매도/매수호가(sel_fpr_bid/buy_fpr_bid)
        # - ka10001: 현재가(cur_prc)
        # NXT/SOR 조회는 `_quote_symbol`에서 종목코드 suffix를 붙여 요청한다.
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

        # 키움 주문은 매수/매도를 `api-id`로 나눈다.
        # body의 `trde_tp`는 매수/매도 방향이 아니라 지정가/시장가 같은 거래유형이다.
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
        # 키움 토큰 API는 JSON body를 사용한다.
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
        # expires_dt가 "20260526235959", "20260526 235959",
        # "2026-05-26 23:59:59" 등으로 올 수 있어 여러 포맷을 허용한다.
        # timezone이 없으면 한국시간으로 해석한 뒤 UTC로 저장한다.
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

        # 키움 REST 연속조회:
        # - 응답 헤더 `cont-yn`이 "Y"면 다음 데이터가 남아 있다.
        # - 다음 요청 헤더에 `cont-yn="Y"`와 응답 헤더의 `next-key`를 넣는다.
        # - body 조건은 동일하게 유지한다.
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
            # 키움 업무 API 공통 헤더:
            # - authorization: Bearer token
            # - api-id: 실제 TR/API ID. 같은 endpoint라도 api-id가 다르면 업무가 달라진다.
            # - cont-yn/next-key: 연속조회 제어. 첫 요청은 cont-yn="N", next-key="".
            # body는 키움 REST 문서의 Input 필드명을 그대로 사용한다.
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
        # 키움은 HTTP 200이어도 본문에 return_code/return_msg로 업무 성공 여부를 싣는다.
        # - 0: 정상
        # - 20: 일부 조회에서 정상/연속조회성 응답으로 취급해야 하는 값이라 허용한다.
        # 그 외 코드는 BrokerError로 올려 copybot 상위 로직이 실패로 처리하게 한다.
        if "return_code" not in payload:
            return
        code = str(payload.get("return_code"))
        if code in {"0", "20"}:
            return
        raise BrokerError(str(payload.get("return_msg") or payload.get("message") or payload))

    def _is_token_expired_response(self, response, payload: dict[str, Any]) -> bool:
        # 키움 토큰 만료는 HTTP 401 또는 return_code 3으로 표현될 수 있다.
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
        # 국내 거래소 옵션:
        # - KRX: 한국거래소
        # - NXT: 대체거래소
        # - SOR: 스마트 주문 라우팅
        # 키움 일부 조회 API는 전체를 의미하는 "%"를 받을 수 있지만,
        # copybot 주문/시세 모델에서는 실제 주문 가능 시장을 명시적으로 KRX/NXT/SOR 중 하나로 제한한다.
        value = exchange or "KRX"
        value = value.upper()
        return value if value in {"KRX", "NXT", "SOR"} else "KRX"

    def _balance_request_body(self) -> dict[str, Any]:
        # kt00018 계좌평가잔고내역 Input reference:
        # - qry_tp: 조회 구분. 현재 구현은 잔고/평가 조회값 "1"을 사용한다.
        # - dmst_stex_tp: 국내거래소 구분. KRX/NXT/SOR 중 기본 KRX를 사용한다.
        #
        # Output reference:
        # - acnt_evlt_remn_indv_tot[]: 보유종목 배열.
        #   stk_cd(종목코드), rmnd_qty(잔고수량), cur_prc(현재가), evlt_amt(평가금액)를 사용한다.
        # - prsm_dpst_aset_amt: 추정예탁자산. total_equity의 1순위 후보.
        # - tot_evlt_amt: 총평가금액. 예수금과 더해 total_equity fallback으로 사용한다.
        return {
            "qry_tp": "1",
            "dmst_stex_tp": self._domestic_exchange(),
        }

    def _cash_request_body(self) -> dict[str, Any]:
        # kt00001 예수금상세현황 Input reference:
        # - qry_tp: 조회 구분. 현재 구현은 예수금/주문가능금액 조회값 "2"를 사용한다.
        #
        # Output reference:
        # - ord_alow_amt: 주문가능금액.
        # - pymn_alow_amt: 출금/지급 가능 금액.
        # - entr, d2_entra: 예수금/D+2 예수금 계열 fallback.
        return {"qry_tp": "2"}

    def _order_request_body(self, order: TargetOrder) -> dict[str, str]:
        is_limit = order.order_type == OrderType.LIMIT
        # kt10000/kt10001 국내주식 주문 Input reference:
        # - dmst_stex_tp: 거래소. KRX/NXT/SOR.
        # - stk_cd: 종목코드. 주문 API에는 suffix 없는 6자리 종목코드를 넣는다.
        # - ord_qty: 주문수량. 키움 REST는 문자열 숫자로 받는다.
        # - ord_uv: 주문단가. 지정가는 가격, 시장가는 "0".
        # - trde_tp: 거래유형. 현재 구현은 "0"=지정가, "3"=시장가로 매핑한다.
        # - cond_uv: 조건가격. 조건부 주문을 쓰지 않으므로 빈 문자열.
        #
        # 매수/매도 방향:
        # - kt10000(api-id)=매수, kt10001(api-id)=매도.
        #
        # Output reference:
        # - ord_no: 주문번호. copybot OrderResult.order_id로 보관한다.
        # - return_code/return_msg: 업무 성공/실패 메시지. 원문 payload는 OrderResult.message에 남긴다.
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
        # 키움 계좌 응답을 copybot 공통 포트폴리오 모델로 변환한다.
        # 실계좌/모의계좌 또는 API 버전에 따라 배열명이 달라질 수 있어 `items` fallback도 허용한다.
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
        # ka10004/ka10001 Output reference:
        # - info.cur_prc: 현재가. 키움은 등락 방향 때문에 "+", "-" 부호를 붙여 보내는 경우가 있어 절댓값 처리한다.
        # - hoga.sel_fpr_bid: 최우선 매도호가.
        # - hoga.buy_fpr_bid: 최우선 매수호가.
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
        # 키움 응답은 종목코드를 A005930처럼 접두어와 함께 주거나,
        # NXT/SOR 조회용 005930_NX 같은 suffix를 붙일 수 있다.
        # copybot 내부 모델에는 기본 6자리 코드만 저장한다.
        raw = str(symbol or "").strip()
        if "_" in raw:
            raw = raw.split("_", 1)[0]
        if len(raw) >= 2 and raw[0] in {"A", "J", "Q"} and raw[1:].isdigit():
            return raw[1:]
        return raw

    def _quote_symbol(self, symbol: Any, exchange: str = "") -> str:
        # ka10004/ka10001 조회용 종목코드 생성 규칙:
        # - KRX: 005930
        # - NXT: 005930_NX
        # - SOR: 005930_AL
        # 이미 suffix가 붙어 있으면 사용자가 의도적으로 넘긴 값으로 보고 그대로 사용한다.
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
        # 주문 API에는 조회 suffix 없이 기본 종목코드를 보낸다.
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
        # 한국 장 시작(09:00) 전에는 토큰을 굳이 새로 받지 않는다.
        # 장 시작 이후에는 날짜 단위 키를 써서 하루 한 번 refresh 기회를 준다.
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

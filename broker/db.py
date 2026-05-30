# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from core.schemas import Holding, OrderResult, PortfolioSnapshot, Quote, TargetOrder
from core.types import MarketScope, OrderSide, OrderType

from .base import BrokerCapabilities, BrokerClient, BrokerCredentials, BrokerError, BrokerFeatureUnavailable


class DBBrokerClient(BrokerClient):
    """DB Securities direct REST broker adapter."""

    # DB OpenAPI는 운영 REST 서버가 `openapi.dbsec.co.kr:8443`이고,
    # 토큰 발급만 form-urlencoded, 나머지 거래/시세 API는 JSON body를 사용한다.
    # 이 클래스는 copybot 내부의 공통 BrokerClient 규격을 DB REST 규격으로 번역하는 어댑터다.
    default_base_url = "https://openapi.dbsec.co.kr:8443"
    token_path = "/oauth2/token"

    # REST path는 업무 영역별로 나뉜다.
    # - trading/kr-stock: 국내 주식 계좌/주문
    # - quote/kr-stock: 국내 주식 현재가/호가
    # - trading/overseas-stock: 해외 주식 계좌/주문
    # - quote/overseas-stock: 해외 주식 현재가/호가
    domestic_balance_path = "/api/v1/trading/kr-stock/inquiry/balance"
    domestic_quote_path = "/api/v1/quote/kr-stock/inquiry/price"
    domestic_hoga_path = "/api/v1/quote/kr-stock/inquiry/orderbook"
    domestic_order_path = "/api/v1/trading/kr-stock/order"

    global_balance_path = "/api/v1/trading/overseas-stock/inquiry/balance-margin"
    global_quote_path = "/api/v1/quote/overseas-stock/inquiry/price"
    global_hoga_path = "/api/v1/quote/overseas-stock/inquiry/orderbook"
    global_order_path = "/api/v1/trading/overseas-stock/order"

    # DB OpenAPI의 TR ID 목록이다. HTTP URL만으로 업무가 결정되는 것이 아니라,
    # API 문서/테스트베드에서는 각 업무를 TR ID로도 식별한다.
    #
    # 국내:
    # - CSPAQ03420: 주식잔고조회. 응답은 요약 `Out` + 보유종목 배열 `Out1` 형태.
    # - PRICE: 국내주식 현재가 조회. `Out.Prpr` 등 현재가 중심 필드가 내려온다.
    # - HOGA: 국내주식 호가 조회. 현재가 응답에 1호가가 비어 있을 때 보강용으로 조회한다.
    # - CSPAT00600: 국내주식 현물 정상 주문. 매수/매도, 지정가/시장가를 body 코드값으로 구분한다.
    #
    # 해외:
    # - CAZCQ00400: 해외주식 잔고/증거금 조회. 보유종목은 보통 `Out2` 또는 `Out1`.
    # - FSTKPRICE/FSTKHOGA: 해외주식 현재가/호가 조회.
    # - CAZCT00100: 해외주식 주문.
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

    # 증권사 API는 초당/분당 호출 제한이 있는 경우가 많다.
    # 여기서는 TR별 최소 간격을 두어 같은 TR을 너무 촘촘하게 호출하지 않도록 한다.
    # 값은 초 단위이며, 주문 TR은 체감 지연을 줄이기 위해 짧게 잡혀 있다.
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
        # 토큰과 만료시각은 메모리에 보관한다. 서버 재시작 후에는 다시 발급된다.
        self.base_url = self.default_base_url
        self.access_token: str | None = None
        self.token_expires_at: datetime | None = None
        # 장 시작 이후 날짜가 바뀌면 토큰을 한 번 새로 받도록 하는 키.
        # 일부 증권 API는 영업일 전환/장 시작 시 토큰 또는 권한 상태가 바뀌는 경우가 있다.
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
                # DB 토큰 API Input:
                # - appkey/appsecretkey: DB OpenAPI 포털에서 발급받은 앱 키/시크릿
                # - grant_type="client_credentials": 서버 간 인증 방식
                # - scope="oob": DB 샘플에서 사용하는 토큰 발급 scope
                #
                # Output:
                # - access_token: 이후 모든 REST 호출의 Bearer 토큰
                # - expires_in: 초 단위 만료 시간. 안전하게 60초 일찍 만료 처리한다.
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
        # copybot은 "포트폴리오 스냅샷"이라는 공통 모델을 사용한다.
        # DB 응답의 현금/평가금액/보유종목 필드를 읽어 PortfolioSnapshot으로 정규화한다.
        snapshot: PortfolioSnapshot = None
        if self.account.market_scope == MarketScope.DOMESTIC:
            payload = await self._request_all_pages(
                self.domestic_balance_path,
                self._domestic_balance_body(),
                self.tr_ids["domestic_balance"],
            )
            snapshot = self._parse_domestic_snapshot(payload)
        else:
            payload = await self._request_all_pages(
                self.global_balance_path,
                self._global_balance_body(),
                self.tr_ids["global_balance"],
            )
            snapshot = self._parse_global_snapshot(payload)

        from core.LogManager import logManager
        await logManager.log_portfolio_snapshot_async(snapshot)
        return snapshot

    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        # 현재가 조회에서 ask/bid 1호가가 같이 내려오지 않는 경우가 있어,
        # 먼저 PRICE/FSTKPRICE를 호출하고 1호가가 비어 있으면 HOGA/FSTKHOGA를 한 번 더 호출한다.
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
        # TargetOrder는 copybot 내부 주문 모델이다.
        # 여기서 국내/해외 TR과 DB body 필드명으로 변환한 뒤 주문 API에 전달한다.
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
        return httpx.AsyncClient(timeout=10)

    def _token_request_body(self) -> dict[str, str | None]:
        # 토큰 API는 JSON이 아니라 form-urlencoded data로 보낸다.
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

        # DB OpenAPI 연속조회:
        # - 응답 헤더 `cont_yn`이 "Y"면 다음 페이지가 있다.
        # - 다음 요청 헤더에 `cont_yn="Y"`와 이전 응답의 `cont_key`를 넣어 이어 받는다.
        # - body는 같은 조회 조건을 그대로 재사용한다.
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
            # 모든 업무 API는 공통 헤더를 쓴다.
            # - authorization: Bearer access_token
            # - cont_yn/cont_key: 연속조회 제어. 단건 요청은 빈 문자열.
            # body는 DB 문서의 `In` 블록 모양을 그대로 따른다.
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
        # DB 정상 응답 코드는 `rsp_cd == "00000"`이다.
        # HTTP 200이어도 업무 오류가 payload에 담길 수 있으므로 별도로 검사한다.
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
        # CSPAQ03420 Input reference:
        # - In.QryTpCode: 잔고 조회 구분. 현재 구현은 전체/기본 조회 용도로 "0"을 사용한다. (0:전체, 1:비상장제외, 2:비상장,코넥스,kotc 제외)
        #
        # Output reference:
        # - Out: 계좌 요약. Dps2(예수금), DpsastAmt(예탁자산), TotEvalAmt(총평가금액) 등을 읽는다.
        # - Out1[]: 종목별 잔고. IsuNo/ShtnIsuNo(종목코드), BalQty0/BalQty/AbleQty(수량),
        #   NowPrc(현재가), ExecPrc(체결/평균가), EvalAmt(평가금액)를 사용한다.
        return {"In": {"QryTpCode": "0"}}

    def _global_balance_body(self) -> dict[str, Any]:
        # CAZCQ00400 Input reference:
        # - WonFcurrTpCode: 원화/외화 표시 구분. 현재 구현은 해외잔고 기본 조회값 "2"를 사용한다.
        # - TrxTpCode: 거래/조회 구분. 현재 구현은 잔고 조회용 "2"를 사용한다.
        # - CmsnTpCode: 수수료 반영 구분. 현재 구현은 API 기본 조회값 "2"를 사용한다.
        # - DpntBalTpCode: 예수금/잔고 구분. 현재 구현은 보유잔고 중심 조회값 "1"을 사용한다.
        #
        # Output reference:
        # - Out: 계좌 요약. Dps(예수금), OrdAbleAmt(주문가능금액), AssetAmtTotamt(총자산) 등을 읽는다.
        # - Out2/Out1[]: 해외 종목 잔고. SymCode/AstkIsuNo(종목), AstkSettBaseQty(결제기준수량),
        #   AstkNowPrc(현재가), AstkEvalAmt(평가금액), CrcyCode(통화)를 사용한다.
        # 세부 코드값 의미는 DB 해외주식 API 명세가 최종 기준이다.
        return {
            "In": {
                "WonFcurrTpCode": "2",
                "TrxTpCode": "2",
                "CmsnTpCode": "2",
                "DpntBalTpCode": "1",
            }
        }

    def _quote_body(self, symbol: str, domestic: bool) -> dict[str, Any]:
        # PRICE/FSTKPRICE/HOGA/FSTKHOGA Input reference:
        # - InputIscd1: 종목코드. 국내는 6자리 코드, 해외는 내부 정규화된 심볼을 넣는다.
        # - InputCondMrktDivCode: 시장 구분. 국내 현재가 샘플 기준 "J"는 주식,
        #   해외 현재 구현의 "FN"은 해외주식 조회 기본 market division 값으로 사용한다.
        #
        # Output reference:
        # - 현재가 API: Out.Prpr/prpr/last를 최종 체결가로 사용한다.
        # - 호가 API: Out.Askp1/askp1/ask, Out.Bidp1/bidp1/bid를 최우선 매도/매수호가로 사용한다.
        return {
            "In": {
                "InputIscd1": self._domestic_symbol(symbol) if domestic else self._global_symbol(symbol),
                "InputCondMrktDivCode": "J" if domestic else self._global_market_div_code(),
            }
        }

    def _order_request_body(self, order: TargetOrder) -> dict[str, Any]:
        if order.market_scope == MarketScope.DOMESTIC:
            is_limit = order.order_type == OrderType.LIMIT
            # CSPAT00600 국내주식 주문 Input reference:
            # - IsuNo: 종목코드. DB 샘플은 "A005930" 형태지만 이 구현은 내부 표준 6자리도 허용한다.
            # - OrdQty: 주문수량. 국내주식은 정수 주식 수량만 지원한다.
            # - OrdPrc: 주문가격. 지정가는 가격, 시장가는 0.
            # - BnsTpCode: 매매구분. "2"=매수, "1"=매도.
            # - OrdprcPtnCode: 호가유형. "00"=지정가, "03"=시장가.
            # - MgntrnCode: 신용/대출 구분. "000"=현금 주문.
            # - LoanDt: 대출일. 현금 주문에서는 "00000000".
            # - OrdCndiTpCode: 주문조건. "0"=일반 조건.
            # - TrchNo: 트랜치 번호. 분할/특수 주문을 쓰지 않아 0.
            #
            # Output reference:
            # - Out.OrdNo: 주문번호. copybot OrderResult.order_id로 보관한다.
            # - Out.OrdTime/ShtnIsuNo/MnyOrdAmt/IsuNm 등은 현재 message 문자열에 원문으로 남긴다.
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
        # CAZCT00100 해외주식 주문 Input reference:
        # - AstkIsuNo: 해외주식 종목코드/심볼.
        # - AstkBnsTpCode: 매매구분. "2"=매수, "1"=매도.
        # - AstkOrdprcPtnCode: 호가유형. 현재 구현은 "1"=지정가, "2"=시장가로 매핑한다.
        # - AstkOrdCndiTpCode: 주문조건. 현재 구현은 일반 주문값 "1"을 사용한다.
        # - AstkOrdQty/AstkOrdPrc: 주문수량/주문가격. 시장가는 가격 0.
        # - OrdTrdTpCode: 주문거래유형. 현재 구현은 일반 주문값 "0"을 사용한다.
        # - OrgOrdNo: 원주문번호. 정정/취소가 아니라 신규 주문이므로 0.
        #
        # 해외 주문 코드값은 국가/시장별로 달라질 수 있으므로 DB 해외주식 주문 명세와 대조해야 한다.
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
        # DB 국내 잔고 응답을 copybot 공통 포트폴리오 모델로 변환한다.
        # 응답 필드가 계좌/권한에 따라 비거나 이름이 조금 다를 수 있어 fallback 후보를 함께 둔다.
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
        # 해외 잔고는 보유종목 배열명이 Out2로 내려오는 케이스와 Out1로 내려오는 케이스를 모두 허용한다.
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
        # 현재가/호가 응답은 API마다 필드 대소문자와 루트 구조가 조금 다르다.
        # `Out` 블록이 있으면 그 안을 우선 보고, 없으면 payload 자체를 응답 본문으로 취급한다.
        # DB 일부 가격 필드는 부호가 붙어 올 수 있어 `_parse_number(..., absolute=True)`로 절댓값 처리한다.
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
        # API마다 종목코드를 A005930, J005930, 005930.KS처럼 표현할 수 있다.
        # copybot 내부에서는 접두/접미를 제거한 기본 심볼을 사용한다.
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
        # 장 시작 전에는 토큰을 불필요하게 새로 받지 않는다.
        # 장 시작 후에는 시장(scope)과 날짜를 키로 삼아 하루 한 번 refresh 기회를 준다.
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

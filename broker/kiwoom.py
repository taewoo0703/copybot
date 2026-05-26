# -*- coding: utf-8 -*-
from __future__ import annotations

try:
    import httpx
except ImportError:
    httpx = None

from core.schemas import Holding, OrderResult, PortfolioSnapshot, Quote, TargetOrder
from core.types import MarketScope, OrderSide

from .base import BrokerCapabilities, BrokerClient, BrokerCredentials, BrokerError, BrokerFeatureUnavailable


class KiwoomBrokerClient(BrokerClient):
    """키움증권 브로커 어댑터.

    이 클래스는 키움 REST API를 CopyEngine이 기대하는 공통 BrokerClient 인터페이스에
    맞춰 연결한다. 일부 흐름은 현재 REST API의 일반적인 형태로 작성되어 있지만,
    토큰 URL, 헤더, 잔고 조회, 호가 조회, 주문 요청/응답 필드는 반드시 키움 공식 문서를
    기준으로 이 파일 안에서 직접 확인하고 수정해야 한다.
    """

    token_path = "/oauth2/token"
    default_base_url = "https://api.kiwoom.com"

    def __init__(self, account, credentials: BrokerCredentials):
        super().__init__(account, credentials)
        # 계좌 설정의 base_url을 가장 우선하고, 없으면 인증 환경변수, 그것도 없으면 기본 URL을 쓴다.
        self.base_url = account.base_url or credentials.base_url or self.default_base_url
        # mock 계정으로 설정했고 별도 URL이 없으면 키움 mock URL을 사용한다.
        if credentials.is_mock and not account.base_url and not credentials.base_url:
            self.base_url = "https://mockapi.kiwoom.com"
        # access token을 환경변수로 미리 넣어둔 경우 refresh 없이 바로 사용할 수 있다.
        self.access_token = credentials.access_token

    async def connect(self) -> bool:
        """키움 API 호출 준비를 수행한다.

        입력:
            - self.account:
              AccountConfig. account_id, broker, market_scope, mode, base_url 정보를 가진다.
            - self.credentials:
              BrokerCredentials. credentials_ref 환경변수에서 읽은 app_key, app_secret,
              account_no, access_token, base_url, is_mock 등을 가진다.

        출력:
            - bool:
              사용 가능한 access token이 있거나 refresh_token()으로 토큰 발급에 성공하면 True.
              인증 정보 누락, endpoint 누락, httpx 미설치, 토큰 발급 실패이면 False.
            - 부수효과:
              self.connected, self.last_message, self.access_token을 갱신한다.

        역할:
            - 이미 access_token이 있으면 연결 완료로 처리한다.
            - access_token이 없으면 refresh_token()으로 키움 OAuth 토큰을 발급받는다.

        구현 시 주의:
            - 임시로 True를 반환하면 안 된다. 실제 token이 있거나 토큰 발급이 성공한 경우에만
              True여야 한다.
            - 키움 토큰 만료 시각이 응답에 포함된다면 이 클래스에 저장하고 자동 갱신 로직을
              추가하는 것이 좋다.
        """
        if self.access_token:
            self.connected = True
            self.last_message = "connected with configured access token"
            return True
        return await self.refresh_token()

    async def refresh_token(self) -> bool:
        """키움 OAuth access token을 발급하거나 갱신한다.

        입력:
            - self.credentials.app_key:
              키움 REST API app key.
            - self.credentials.app_secret:
              키움 REST API secret key. 현재 token body에서는 secretkey로 보낸다.
            - self.base_url:
              token_path와 합쳐 token endpoint를 만들 기본 URL.

        출력:
            - bool:
              토큰 발급 후 self.access_token 저장까지 성공하면 True, 실패하면 False.
            - 부수효과:
              self.access_token, self.connected, self.last_message를 갱신한다.

        역할:
            - _token_request_body()로 키움 토큰 요청 body를 만든다.
            - 키움 토큰 endpoint에 POST한다.
            - 응답에서 access token을 추출한다.

        구현 시 주의:
            - 현재 token endpoint, 요청 필드, 응답 필드는 일반적인 형태다. 키움 공식 문서와
              다르면 이 함수, _token_request_body(), _extract_token()을 반드시 수정해야 한다.
            - 에러 응답 형식도 공식 문서에 맞춰 처리해야 운영 중 원인 파악이 쉽다.
        """
        if httpx is None:
            self.connected = False
            self.last_message = "httpx is required for Kiwoom broker connections"
            return False
        if not self.credentials.app_key or not self.credentials.app_secret:
            self.connected = False
            self.last_message = "missing Kiwoom app key or secret key"
            return False
        if not self.base_url:
            self.connected = False
            self.last_message = "missing Kiwoom broker base url"
            return False

        # 키움 토큰 endpoint로 인증 정보를 보내 access token을 요청한다.
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}{self.token_path}",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json=self._token_request_body(),
            )
        response.raise_for_status()

        # 브로커 응답에서 토큰 필드를 찾아 내부 상태에 저장한다.
        payload = response.json()
        token = self._extract_token(payload)
        if not token:
            self.connected = False
            self.last_message = f"Kiwoom token not found in response: {payload}"
            return False

        self.access_token = token
        self.connected = True
        self.last_message = "connected"
        return True

    async def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        """키움 계좌의 현재 포트폴리오를 조회한다.

        입력:
            - self.account.market_scope:
              현재 키움 어댑터는 domestic만 지원한다.
            - self.credentials.account_no:
              조회할 실제 키움 계좌번호.
            - 환경변수 extra path:
              {credentials_ref}_DOMESTIC_BALANCE_PATH.

        출력:
            - PortfolioSnapshot:
              account_id, total_equity, cash, holdings, currency, captured_at을 포함한다.
              holdings의 각 항목은 Holding(symbol, exchange, quantity, current_price,
              market_value, currency) 형태여야 한다.

        역할:
            - 키움 잔고 API를 호출한다.
            - 키움 고유 응답을 CopyEngine이 이해하는 PortfolioSnapshot으로 변환한다.

        구현 시 주의:
            - 현재 _balance_request_body()와 _parse_snapshot()은 일반적인 필드명을 기준으로 한다.
              키움 실제 응답과 다르면 반드시 이 파일에서 키움 전용으로 수정해야 한다.
            - 실거래에서는 필수 금액/수량 필드 누락을 조용히 0으로 처리하면 위험하다.
        """
        path = self._configured_path(self._balance_path_key())
        if not path:
            raise BrokerFeatureUnavailable(
                f"kiwoom balance endpoint is not configured for {self.account.market_scope.value}"
            )

        # 잔고 조회는 계좌번호와 시장 구분을 body에 담아 보낸다.
        payload = await self._post_json(path, self._balance_request_body(), self.credentials.extra.get("BALANCE_TR_ID"))
        return self._parse_snapshot(payload)

    async def get_quote(self, symbol: str, exchange: str = "") -> Quote:
        """키움에서 주문 가격 산정에 필요한 현재가와 1호가를 조회한다.

        입력:
            - symbol:
              조회할 국내 종목 코드. 예: "005930".
            - exchange:
              키움 endpoint가 시장 코드를 요구할 때 쓰는 값. 기본적으로 빈 문자열을 사용할 수 있다.

        출력:
            - Quote:
              symbol, exchange, last_price, ask_price_1, bid_price_1, currency, captured_at.

        역할:
            - CopyEngine은 live 매수 주문을 ask_price_1 기준 지정가로 넣는다.
            - ask_price_1이 0 이하이면 CopyEngine은 매수 주문을 중단한다.

        구현 시 주의:
            - 키움 호가 endpoint, 요청 body, TR ID, 응답 필드는 공식 문서 기준으로 확인해야 한다.
            - 실제 응답이 현재 _parse_quote()와 다르면 키움 전용 파서를 수정해야 한다.
        """
        path = self._configured_path(self._quote_path_key())
        if not path:
            raise BrokerFeatureUnavailable(
                f"kiwoom quote endpoint is not configured for {self.account.market_scope.value}"
            )

        # 호가 조회에 필요한 최소 공통 정보만 보낸다. 키움이 다른 필드를 요구하면 여기서 수정한다.
        payload = await self._post_json(
            path,
            {"symbol": symbol, "exchange": exchange, "market_scope": self.account.market_scope.value},
            self.credentials.extra.get("QUOTE_TR_ID"),
        )
        return self._parse_quote(symbol, exchange, payload)

    async def place_order(self, order: TargetOrder) -> OrderResult:
        """키움 계좌로 live 주문을 전송한다.

        입력:
            - order:
              TargetOrder. group_id, account_id, broker, market_scope, symbol, exchange,
              side, quantity, order_type, limit_price, estimated_price, estimated_value,
              mode를 포함한다.

        출력:
            - OrderResult:
              원본 order, accepted 여부, broker order_id, message를 포함한다.

        역할:
            - TargetOrder를 키움 주문 API payload로 변환한다.
            - 키움 주문 endpoint에 POST한다.
            - 키움 응답을 OrderResult로 변환한다.

        구현 시 주의:
            - 성공 여부를 고정 True로 반환하면 안 된다. 키움 응답의 성공/실패 코드로 accepted를
              판단해야 한다.
            - 주문번호 필드, 실패 메시지 필드, 오류 코드 체계는 키움 공식 문서 기준으로 맞춰야 한다.
        """
        path = self._configured_path(self._order_path_key())
        if not path:
            raise BrokerFeatureUnavailable(
                f"kiwoom order endpoint is not configured for {self.account.market_scope.value}"
            )

        # 주문 body는 키움 전용 매핑 함수에서 만든다. 주문 구분값은 공식 문서 확인이 필요하다.
        payload = await self._post_json(path, self._order_request_body(order), self.credentials.extra.get("ORDER_TR_ID"))
        order_id = str(payload.get("order_id") or payload.get("ord_no") or payload.get("OrdNo") or "")
        accepted = self._is_success_response(payload)
        return OrderResult(order=order, accepted=accepted, order_id=order_id or None, message=str(payload))

    def get_capabilities(self) -> BrokerCapabilities:
        """키움 어댑터가 지원한다고 선언하는 기능을 반환한다.

        출력:
            - BrokerCapabilities:
              국내/해외 주식 지원 여부, 시장가/지정가 지원 여부, live trading 가능 여부,
              소수점 수량 지원 여부를 포함한다.

        역할:
            - CopyEngine이 주문 전 assert_order_supported()로 주문 가능 여부를 검사할 때 사용한다.
        """
        return BrokerCapabilities(
            broker="kiwoom",
            supports_domestic_stock=True,
            supports_global_stock=False,
            supports_market_order=True,
            supports_limit_order=True,
            supports_live_trading=True,
            supports_fractional_quantity=False,
            notes="Kiwoom broker is implemented directly in broker/kiwoom.py; global scope is disabled.",
        )

    def _token_request_body(self) -> dict:
        """키움 OAuth token endpoint에 보낼 요청 body를 만든다."""
        return {
            "grant_type": "client_credentials",
            "appkey": self.credentials.app_key,
            "secretkey": self.credentials.app_secret,
        }

    def _extract_token(self, payload: dict) -> str | None:
        """키움 토큰 응답에서 access token 값을 추출한다."""
        return payload.get("access_token") or payload.get("token") or payload.get("ACCESS_TOKEN")

    def _auth_headers(self, tr_id: str | None = None) -> dict[str, str]:
        """키움 API 요청에 사용할 인증 헤더를 만든다."""
        if not self.access_token:
            raise BrokerError("Kiwoom access token is not available")

        # Authorization 형식과 TR ID 헤더명은 키움 공식 문서와 다르면 여기서 수정한다.
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": f"Bearer {self.access_token}",
        }
        if tr_id:
            headers["api-id"] = tr_id
            headers["tr_id"] = tr_id
        return headers

    async def _post_json(self, path: str, body: dict, tr_id: str | None = None) -> dict:
        """키움 endpoint에 JSON POST 요청을 보내고 응답 dict를 반환한다."""
        if httpx is None:
            raise BrokerFeatureUnavailable("httpx is required for Kiwoom broker requests")
        if not self.connected:
            await self.connect()

        # path는 환경변수에서 상대 경로로 받기 때문에 base_url과 안전하게 합친다.
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=self._auth_headers(tr_id),
                json=body,
            )
        response.raise_for_status()
        return response.json()

    def _configured_path(self, key: str) -> str | None:
        """credentials_ref 기반 환경변수 extra에서 endpoint path를 꺼낸다."""
        return self.credentials.extra.get(key)

    def _balance_path_key(self) -> str:
        """시장 구분에 맞는 잔고 조회 path 환경변수 이름을 반환한다."""
        return "DOMESTIC_BALANCE_PATH" if self.account.market_scope == MarketScope.DOMESTIC else "GLOBAL_BALANCE_PATH"

    def _quote_path_key(self) -> str:
        """시장 구분에 맞는 호가 조회 path 환경변수 이름을 반환한다."""
        return "DOMESTIC_QUOTE_PATH" if self.account.market_scope == MarketScope.DOMESTIC else "GLOBAL_QUOTE_PATH"

    def _order_path_key(self) -> str:
        """시장 구분에 맞는 주문 path 환경변수 이름을 반환한다."""
        return "DOMESTIC_ORDER_PATH" if self.account.market_scope == MarketScope.DOMESTIC else "GLOBAL_ORDER_PATH"

    def _balance_request_body(self) -> dict:
        """키움 잔고 조회 요청 body를 만든다."""
        return {
            "account_no": self.credentials.account_no,
            "market_scope": self.account.market_scope.value,
        }

    def _order_request_body(self, order: TargetOrder) -> dict:
        """TargetOrder를 키움 주문 API 요청 body로 변환한다.

        입력:
            - order:
              CopyEngine/PortfolioRebalancer가 생성한 공통 주문 모델.

        출력:
            - dict:
              키움 주문 endpoint에 보낼 요청 body.

        구현 시 주의:
            - ord_dvsn, trde_tp, mrkt_tp, ord_prc 값은 현재 임시 매핑이다.
              키움 공식 주문 구분값과 시장 코드를 확인해 틀리면 반드시 수정해야 한다.
        """
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
            "broker": "kiwoom",
            "ord_dvsn": "00" if order.limit_price else "03",
            "trde_tp": "BUY" if order.side == OrderSide.BUY else "SELL",
            "mrkt_tp": "KRX" if order.market_scope == MarketScope.DOMESTIC else order.exchange,
            "ord_prc": order.limit_price or 0,
        }

    def _parse_quote(self, symbol: str, exchange: str, payload: dict) -> Quote:
        """키움 호가 응답을 공통 Quote 모델로 변환한다."""
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
        """키움 잔고 응답을 공통 PortfolioSnapshot 모델로 변환한다."""
        raw_holdings = payload.get("holdings") or payload.get("positions") or payload.get("items") or []
        currency = str(payload.get("currency") or ("KRW" if self.account.market_scope == MarketScope.DOMESTIC else "USD"))
        holdings = []

        # 응답 안의 보유종목 배열을 CopyEngine의 Holding 모델로 하나씩 변환한다.
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

        # 총평가금액이 없으면 현금 + 보유종목 평가금액으로 보정한다.
        cash = float(payload.get("cash") or payload.get("cash_balance") or payload.get("dnca_tot_amt") or 0.0)
        total_equity = float(payload.get("total_equity") or payload.get("total_value") or cash + sum(h.market_value for h in holdings))
        return PortfolioSnapshot(
            account_id=self.account.account_id,
            total_equity=total_equity,
            cash=cash,
            holdings=holdings,
            currency=currency,
        )

    def _is_success_response(self, payload: dict) -> bool:
        """키움 주문 응답이 성공인지 판정한다."""
        if "accepted" in payload:
            return bool(payload["accepted"])

        # 현재는 일반적인 성공 코드 후보를 본다. 키움 실제 성공/실패 코드는 공식 문서에 맞춰 수정한다.
        code = str(payload.get("code") or payload.get("rt_cd") or payload.get("return_code") or "0")
        return code in {"0", "0000", "OK", "SUCCESS"}

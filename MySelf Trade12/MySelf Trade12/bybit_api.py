"""Myself Trade - Bybit v5 API 연동.

공식 문서의 서명 규칙을 그대로 구현했다 (추가 라이브러리 없이 requests만 사용).
  서명 원문: timestamp + api_key + recv_window + (GET: 쿼리 문자열 / POST: JSON 본문)
  서명 방식: HMAC-SHA256, 16진수 소문자

대상 상품: USDT 무기한 선물(category="linear"), 단방향(One-Way) 포지션 모드.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import requests

from utils import log, safe_float

ENDPOINTS = {
    "live": "https://api.bybit.com",
    "demo": "https://api-demo.bybit.com",
    "testnet": "https://api-testnet.bybit.com",
}
CATEGORY = "linear"
RECV_WINDOW = "10000"


class BybitError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class BybitClient:
    def __init__(self, api_key: str = "", api_secret: str = "", env: str = "live", timeout: float = 10.0):
        if env not in ENDPOINTS:
            raise ValueError(f"알 수 없는 환경: {env}")
        self.env = env
        self.base_url = ENDPOINTS[env]
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MyselfTrade/1.0", "Content-Type": "application/json"})
        self._time_offset_ms = 0
        self._time_synced = False
        self._instruments: dict[str, dict] = {}
        self.feed = None     # 실시간 시세 피드(PriceFeed). 붙어 있으면 get_price가 이 값을 먼저 쓴다.

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)

    # ---------------------------------------------------------- 요청 공통
    def _sync_time(self) -> None:
        """PC 시계가 거래소와 어긋나 있으면 서명이 거부되므로 차이를 보정한다."""
        try:
            data = self._request("GET", "/v5/market/time", auth=False)
            server_ms = int(data.get("timeNano", "0")) // 1_000_000 or int(data.get("timeSecond", "0")) * 1000
            if server_ms:
                self._time_offset_ms = server_ms - int(time.time() * 1000)
        except Exception as exc:  # 보정 실패는 치명적이지 않다
            log.warning("Bybit 서버 시간 동기화 실패: %s", exc)
        self._time_synced = True

    def _request(self, method: str, path: str, params: dict | None = None, auth: bool = False) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if method == "GET":
            payload = urlencode(params)
            url = f"{self.base_url}{path}" + (f"?{payload}" if payload else "")
            body = None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url = f"{self.base_url}{path}"
            body = payload
        if auth:
            if not self.has_keys:
                raise BybitError("Bybit API 키가 설정되지 않았습니다.")
            if not self._time_synced:
                self._sync_time()
            timestamp = str(int(time.time() * 1000) + self._time_offset_ms)
            origin = timestamp + self.api_key + RECV_WINDOW + payload
            signature = hmac.new(self.api_secret.encode(), origin.encode(), hashlib.sha256).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": signature,
            }
        try:
            response = self.session.request(method, url, data=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BybitError(f"Bybit 연결 실패: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise BybitError(f"Bybit 응답을 해석할 수 없습니다 (HTTP {response.status_code}).") from exc
        code = data.get("retCode")
        if code != 0:
            raise BybitError(f"Bybit 오류 {code}: {data.get('retMsg')}", code)
        return data.get("result") or {}

    # ---------------------------------------------------------- 시세 (키 불필요)
    def get_klines(self, symbol: str, interval: str = "15", limit: int = 200, end: int | None = None) -> list[dict]:
        """캔들 목록을 과거 -> 최신 순서로 돌려준다. interval: 1,5,15,60,240,D 등.

        end(밀리초)를 주면 그 시각 이전의 캔들을 돌려준다. 과거 차트를 이어서 불러올 때 쓴다. 한 번에 최대 1000개.
        """
        result = self._request("GET", "/v5/market/kline",
                               {"category": CATEGORY, "symbol": symbol, "interval": interval,
                                "limit": min(int(limit), 1000), "end": end})
        candles = [{"t": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                    "c": float(row[4]), "v": float(row[5])} for row in result.get("list", [])]
        candles.sort(key=lambda c: c["t"])
        return candles

    def get_ticker(self, symbol: str) -> dict:
        result = self._request("GET", "/v5/market/tickers", {"category": CATEGORY, "symbol": symbol})
        rows = result.get("list", [])
        if not rows:
            raise BybitError(f"{symbol} 시세를 찾을 수 없습니다.")
        row = rows[0]
        return {
            "price": safe_float(row.get("lastPrice")),
            "mark_price": safe_float(row.get("markPrice")),
            "change_24h_pct": safe_float(row.get("price24hPcnt")) * 100,
            "funding_rate_pct": safe_float(row.get("fundingRate")) * 100,
            "volume_24h": safe_float(row.get("turnover24h")),
            "high_24h": safe_float(row.get("highPrice24h")),
            "low_24h": safe_float(row.get("lowPrice24h")),
        }

    def get_price(self, symbol: str) -> float:
        if self.feed is not None:
            price = self.feed.get(symbol)
            if price:
                return price
        return self.get_ticker(symbol)["price"]

    def get_instrument(self, symbol: str) -> dict:
        """주문 단위(수량/가격 스텝), 최소 주문, 최대 레버리지."""
        if symbol in self._instruments:
            return self._instruments[symbol]
        result = self._request("GET", "/v5/market/instruments-info", {"category": CATEGORY, "symbol": symbol})
        rows = result.get("list", [])
        if not rows:
            raise BybitError(f"{symbol} 상품 정보를 찾을 수 없습니다.", code=-404)
        row = rows[0]
        lot, price, lev = row.get("lotSizeFilter", {}), row.get("priceFilter", {}), row.get("leverageFilter", {})
        info = {
            "qty_step": safe_float(lot.get("qtyStep"), 0.001),
            "min_qty": safe_float(lot.get("minOrderQty"), 0.001),
            "max_qty": safe_float(lot.get("maxMktOrderQty"), 0) or safe_float(lot.get("maxOrderQty"), 0),
            "min_notional": safe_float(lot.get("minNotionalValue"), 5.0),
            "tick_size": safe_float(price.get("tickSize"), 0.01),
            "max_leverage": safe_float(lev.get("maxLeverage"), 50),
        }
        self._instruments[symbol] = info
        return info

    # ---------------------------------------------------------- 계정 (키 필요)
    def get_equity(self) -> float:
        """통합 계정(UNIFIED)의 총자산(USD 환산). 원금 + 누적 수익 + 미실현 손익."""
        result = self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, auth=True)
        rows = result.get("list", [])
        if not rows:
            return 0.0
        account = rows[0]
        total = safe_float(account.get("totalEquity"))
        if total > 0:
            return total
        for coin in account.get("coin", []):
            if coin.get("coin") == "USDT":
                return safe_float(coin.get("equity"))
        return 0.0

    def get_fee_rates(self, symbol: str) -> dict | None:
        try:
            result = self._request("GET", "/v5/account/fee-rate", {"category": CATEGORY, "symbol": symbol}, auth=True)
            row = result.get("list", [{}])[0]
            return {"taker": safe_float(row.get("takerFeeRate")), "maker": safe_float(row.get("makerFeeRate"))}
        except (BybitError, IndexError):
            return None

    def get_position(self, symbol: str) -> dict | None:
        result = self._request("GET", "/v5/position/list", {"category": CATEGORY, "symbol": symbol}, auth=True)
        for row in result.get("list", []):
            size = safe_float(row.get("size"))
            if size > 0:
                return {
                    "side": "long" if row.get("side") == "Buy" else "short",
                    "qty": size,
                    "entry_price": safe_float(row.get("avgPrice")),
                    "unrealized": safe_float(row.get("unrealisedPnl")),
                    "leverage": safe_float(row.get("leverage")),
                }
        return None

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self._request("POST", "/v5/position/set-leverage",
                          {"category": CATEGORY, "symbol": symbol,
                           "buyLeverage": str(leverage), "sellLeverage": str(leverage)}, auth=True)
        except BybitError as exc:
            if exc.code != 110043:  # 110043 = 이미 같은 레버리지 (정상)
                raise

    def place_market_order(self, symbol: str, side: str, qty: str, stop_loss: str | None = None,
                           take_profit: str | None = None, reduce_only: bool = False) -> str:
        """시장가 주문. side: "Buy" 또는 "Sell". 주문 ID를 돌려준다."""
        params = {"category": CATEGORY, "symbol": symbol, "side": side, "orderType": "Market",
                  "qty": qty, "positionIdx": 0}
        if reduce_only:
            params["reduceOnly"] = True
        else:
            if stop_loss:
                params["stopLoss"] = stop_loss
            if take_profit:
                params["takeProfit"] = take_profit
            if stop_loss or take_profit:
                params["tpslMode"] = "Full"
        result = self._request("POST", "/v5/order/create", params, auth=True)
        return str(result.get("orderId", ""))

    def set_trading_stop(self, symbol: str, stop_loss: str, take_profit: str) -> None:
        """보유 포지션의 손절/익절 가격을 바꾼다 (분할 매수로 평균 단가가 바뀌었을 때)."""
        try:
            self._request("POST", "/v5/position/trading-stop",
                          {"category": CATEGORY, "symbol": symbol, "stopLoss": stop_loss, "takeProfit": take_profit,
                           "tpslMode": "Full", "positionIdx": 0}, auth=True)
        except BybitError as exc:
            if exc.code != 34040:   # 34040 = 이미 같은 값 (정상)
                raise

    def get_last_closed_pnl(self, symbol: str) -> dict | None:
        """가장 최근에 청산된 포지션의 손익 기록."""
        result = self._request("GET", "/v5/position/closed-pnl",
                               {"category": CATEGORY, "symbol": symbol, "limit": 1}, auth=True)
        rows = result.get("list", [])
        if not rows:
            return None
        row = rows[0]
        return {
            "closed_pnl": safe_float(row.get("closedPnl")),   # 수수료가 반영된 실현 손익
            "entry_price": safe_float(row.get("avgEntryPrice")),
            "exit_price": safe_float(row.get("avgExitPrice")),
            "open_fee": safe_float(row.get("openFee")),
            "close_fee": safe_float(row.get("closeFee")),
            "qty": safe_float(row.get("closedSize")) or safe_float(row.get("qty")),
            "updated_ms": int(safe_float(row.get("updatedTime"))),
        }

    def check_connection(self) -> dict:
        """API 키가 정상인지 확인 (대시보드의 연결 확인 버튼용)."""
        equity = self.get_equity()
        return {"ok": True, "equity": equity, "env": self.env}

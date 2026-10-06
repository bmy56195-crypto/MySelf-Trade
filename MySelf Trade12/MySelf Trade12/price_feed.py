"""Myself Trade - 실시간 시세 피드.

Bybit 공개 WebSocket(v5)으로 거래 대상 코인의 체결가를 계속 받아 둔다.
  - 'websocket-client' 패키지가 설치되어 있으면 WebSocket을 쓴다 (약 0.1초 단위 갱신).
  - 설치되어 있지 않거나 연결이 끊기면 2초마다 REST로 조회하는 방식으로 자동 전환한다.

모의투자도 이 시세(실제 거래소 체결가)를 그대로 쓴다. 가상으로 만든 시세가 아니다.
"""
from __future__ import annotations

import json
import threading
import time

from utils import log, safe_float

WS_URLS = {
    "live": "wss://stream.bybit.com/v5/public/linear",
    "testnet": "wss://stream-testnet.bybit.com/v5/public/linear",
}
PING_SECONDS = 20
REST_SECONDS = 2.0


class PriceFeed:
    def __init__(self, client, symbols: list[str], env: str = "live"):
        self.client = client            # REST 조회에 쓸 BybitClient
        self.symbols = list(symbols)
        self.url = WS_URLS.get(env, WS_URLS["live"])
        self._prices: dict[str, tuple[float, float]] = {}   # symbol -> (가격, 받은 시각)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self.source = "none"            # websocket | rest | none
        self.connected = False
        self._last_warning = ""

    # ---------------------------------------------------------- 외부에서 쓰는 것
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="price-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def set_symbols(self, symbols: list[str]) -> None:
        """받을 코인 목록을 바꾼다 (설정에서 코인을 추가하거나 지웠을 때). 연결 중이면 새 코인을 바로 구독한다."""
        added = [s for s in symbols if s not in self.symbols]
        removed = [s for s in self.symbols if s not in symbols]
        self.symbols = list(symbols)
        with self._lock:
            for symbol in removed:
                self._prices.pop(symbol, None)
        ws = self._ws
        if ws is not None and self.connected:
            try:
                if added:
                    ws.send(json.dumps({"op": "subscribe", "args": [f"tickers.{s}" for s in added]}))
                if removed:
                    ws.send(json.dumps({"op": "unsubscribe", "args": [f"tickers.{s}" for s in removed]}))
            except Exception as exc:      # 보내지 못해도 다음 연결 때 새 목록으로 구독한다. 그동안은 REST로 가격을 받는다.
                self._warn(f"실시간 시세 구독 변경 실패: {exc}")

    def get(self, symbol: str, max_age: float = 10.0) -> float | None:
        """최근 max_age초 안에 받은 가격. 없거나 오래됐으면 None."""
        with self._lock:
            item = self._prices.get(symbol)
        if item and time.time() - item[1] <= max_age:
            return item[0]
        return None

    def status(self) -> dict:
        with self._lock:
            newest = max((item[1] for item in self._prices.values()), default=0.0)
        age = time.time() - newest if newest else None
        fresh = age is not None and age <= 10
        label = {"websocket": "실시간 (WebSocket)", "rest": "2초 간격 조회 (REST)"}.get(self.source, "연결 대기")
        return {"source": self.source, "label": label if fresh else "시세 연결 대기", "fresh": fresh,
                "age": round(age, 1) if age is not None else None}

    # ---------------------------------------------------------- 내부
    def _set(self, symbol: str, price: float) -> None:
        if price > 0:
            with self._lock:
                self._prices[symbol] = (price, time.time())

    def _warn(self, message: str) -> None:
        if message != self._last_warning:      # 같은 경고를 반복해서 찍지 않는다
            self._last_warning = message
            log.warning(message)

    def _run(self) -> None:
        try:
            import websocket  # 'websocket-client' 패키지
        except ImportError:
            websocket = None
            log.info("websocket-client가 설치되어 있지 않아 2초 간격 REST 조회로 시세를 받습니다. "
                     "(pip install websocket-client 로 실시간 연결을 켤 수 있습니다)")
        backoff = 5.0
        while not self._stop.is_set():
            if websocket is not None:
                started = time.time()
                try:
                    self._run_websocket(websocket)
                except Exception as exc:
                    self._warn(f"실시간 시세(WebSocket) 연결 실패: {exc}")
                self.connected = False
                backoff = 5.0 if time.time() - started > 60 else min(backoff * 2, 60.0)
                self._poll_rest(duration=backoff)      # 다시 연결할 때까지 REST로 버틴다
            else:
                self._poll_rest(duration=None)

    def _poll_rest(self, duration: float | None) -> None:
        end = time.time() + duration if duration else None
        while not self._stop.is_set() and (end is None or time.time() < end):
            try:
                for symbol in self.symbols:
                    self._set(symbol, self.client.get_ticker(symbol)["price"])
                self.source = "rest"
                self._last_warning = ""
                self._stop.wait(REST_SECONDS)
            except Exception as exc:
                self._warn(f"시세 조회 실패: {exc}")
                self._stop.wait(5.0)

    def _run_websocket(self, websocket) -> None:
        def on_open(ws):
            ws.send(json.dumps({"op": "subscribe", "args": [f"tickers.{s}" for s in self.symbols]}))
            self.connected = True
            self.source = "websocket"
            self._last_warning = ""
            log.info("실시간 시세(WebSocket)에 연결했습니다.")
            threading.Thread(target=keep_alive, args=(ws,), daemon=True).start()

        def keep_alive(ws):
            # Bybit는 20초마다 ping을 보내 연결을 유지하도록 권한다
            while not self._stop.is_set() and self._ws is ws and self.connected:
                self._stop.wait(PING_SECONDS)
                age = self.status()["age"]
                if age is not None and age > 30:      # 30초 넘게 시세가 안 오면 끊고 다시 연결한다
                    ws.close()
                    return
                try:
                    ws.send(json.dumps({"op": "ping"}))
                except Exception:
                    return

        def on_message(ws, message):
            try:
                data = json.loads(message)
            except ValueError:
                return
            topic = data.get("topic", "")
            if topic.startswith("tickers."):
                payload = data.get("data") or {}
                # delta 메시지에는 바뀐 값만 들어 있다. lastPrice가 없으면 가격은 그대로다.
                price = safe_float(payload.get("lastPrice"))
                if price > 0:
                    self._set(topic.split(".", 1)[1], price)

        def on_error(ws, error):
            self._warn(f"실시간 시세(WebSocket) 오류: {error}")

        self._ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_message, on_error=on_error)
        self._ws.run_forever(ping_interval=0)
        self._ws = None

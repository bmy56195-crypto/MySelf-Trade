"""Myself Trade - 시장 데이터.

- 미국 증시 지수(나스닥, 다우, S&P 500), 국제 금 시세, 원/달러 환율: 공개 시세 (Yahoo Finance 등, 실패하면 다른 곳에서 다시 시도)
- 코인 지표 요약: Bybit 캔들 -> 15분/1시간/일봉 지표
- SyntheticMarket: 인터넷 없이 프로그램을 점검하기 위한 가상 시세 (selftest.py에서 사용)
"""
from __future__ import annotations

import math
import random
import threading
import time

import requests

from config import MACRO_EXTRA, SYMBOLS, US_INDICES, coin_meta
from indicators import summarize
from utils import log

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_URL = "https://{host}/v8/finance/chart/{symbol}"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_IDS = {"^IXIC": "NASDAQCOM", "^DJI": "DJIA", "^GSPC": "SP500"}     # 미국 연준 공개 자료 (전일 종가)
GOLD_SPOT_URL = "https://api.gold-api.com/price/XAU"                    # 금 현물 (XAU/USD)
UPBIT_URL = "https://api.upbit.com/v1/ticker"
ER_API_URL = "https://open.er-api.com/v6/latest/USD"


def summarize_closes(closes: list[float], last: float | None = None, last_is_current: bool | None = None) -> dict | None:
    """일별 종가 흐름(과거 -> 최신)을 요약한다.

    last: 지금 가격. closes의 마지막 값이 '오늘 봉'(지금 가격과 같은 봉)인지에 따라 전일 종가가 달라진다.
    last_is_current: 마지막 값이 오늘 봉이면 True. 모르면(None) 두 값이 사실상 같은지로 판단한다.
      Yahoo는 종가를 소수 여러 자리로, 지금 가격은 반올림해서 주므로 완전히 같은지로 비교하면 안 된다.
    """
    closes = [float(c) for c in closes if c is not None]
    if len(closes) < 2:
        return None
    last = float(last or closes[-1])
    if last_is_current is None:
        last_is_current = abs(closes[-1] - last) <= abs(last) * 1e-4
    prev = closes[-2] if last_is_current else closes[-1]

    def change(bars: int) -> float:
        base = closes[-bars - 1] if len(closes) > bars else closes[0]
        return round((last / base - 1) * 100, 2) if base else 0.0

    return {
        "last": round(last, 2),
        "chg_1d": round((last / prev - 1) * 100, 2) if prev else 0.0,
        "chg_5d": change(5),
        "chg_1m": round((last / closes[0] - 1) * 100, 2) if closes[0] else 0.0,
        "closes": [round(c, 2) for c in closes[-23:]],
    }


def parse_yahoo_chart(payload: dict) -> dict | None:
    """Yahoo 차트 응답에서 종가 흐름을 뽑아 요약한다."""
    try:
        result = payload["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
    except (KeyError, IndexError, TypeError):
        return None
    meta = result.get("meta", {}) or {}
    same_bar = None
    stamps = result.get("timestamp") or []
    if stamps and meta.get("regularMarketTime"):
        # 마지막 봉의 날짜와 지금 가격의 날짜가 같으면 마지막 봉이 오늘 봉이다 (거래소 현지 날짜 기준)
        offset = int(meta.get("gmtoffset") or 0)
        same_bar = (int(stamps[-1]) + offset) // 86400 == (int(meta["regularMarketTime"]) + offset) // 86400
    summary = summarize_closes(closes, meta.get("regularMarketPrice"), same_bar)
    if summary and meta.get("regularMarketTime"):
        summary["as_of"] = int(meta["regularMarketTime"])
    return summary


def parse_fred_csv(text: str) -> dict | None:
    """FRED CSV(날짜,값)를 요약한다. 값이 없는 날은 '.'으로 온다."""
    closes, last_day = [], ""
    for line in text.strip().splitlines()[1:]:
        day, _, value = line.partition(",")
        try:
            closes.append(float(value))
            last_day = day.strip()
        except ValueError:
            continue
    summary = summarize_closes(closes[-23:])
    if summary:
        summary["as_of_day"] = last_day
    return summary


class MacroData:
    """미국 증시 3대 지수, 국제 금 시세, 원/달러 환율.

    - 지수: Yahoo Finance -> 막히면 FRED(미국 연준 공개 자료, 전일 종가)
    - 금: Yahoo 금 선물(COMEX, USD/온스) -> 막히면 금 현물 시세(gold-api.com)
    - 환율: 업비트 USDT/KRW -> 막히면 공개 환율(USD/KRW)
    10분 동안은 저장해 둔 값을 다시 쓴다. 모두 실패하면 1분 뒤에 다시 시도한다.
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self._cache: dict | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()
        self._busy = False

    # ---- 받아 오기
    @staticmethod
    def _get(url: str, params: dict | None = None, timeout: float = 6.0) -> requests.Response:
        return requests.get(url, params=params, timeout=timeout,
                            headers={"User-Agent": BROWSER_UA, "Accept": "application/json,text/csv,*/*"})

    def _yahoo(self, symbol: str) -> tuple[dict | None, str]:
        reason = ""
        for host in YAHOO_HOSTS:
            try:
                response = self._get(YAHOO_URL.format(host=host, symbol=requests.utils.quote(symbol)),
                                     {"range": "1mo", "interval": "1d"})
                if response.status_code != 200:
                    reason = f"Yahoo 응답 코드 {response.status_code}"
                    continue
                summary = parse_yahoo_chart(response.json())
                if summary:
                    return summary, ""
                reason = "Yahoo 응답에 시세가 없음"
            except Exception as exc:
                reason = f"Yahoo 접속 실패 ({type(exc).__name__})"
        return None, reason

    def _fred(self, series: str) -> tuple[dict | None, str]:
        try:
            since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 50 * 86400))
            response = self._get(FRED_URL, {"id": series, "cosd": since})
            if response.status_code != 200:
                return None, f"FRED 응답 코드 {response.status_code}"
            summary = parse_fred_csv(response.text)
            return (summary, "") if summary else (None, "FRED 응답에 시세가 없음")
        except Exception as exc:
            return None, f"FRED 접속 실패 ({type(exc).__name__})"

    def _gold_spot(self) -> tuple[dict | None, str]:
        try:
            response = self._get(GOLD_SPOT_URL)
            price = float(response.json()["price"])
            return {"last": round(price, 2), "chg_1d": None, "chg_5d": None, "chg_1m": None, "closes": []}, ""
        except Exception as exc:
            return None, f"금 현물 시세 접속 실패 ({type(exc).__name__})"

    def _fx(self) -> dict | None:
        """1 USDT(달러)가 몇 원인지."""
        try:
            data = self._get(UPBIT_URL, {"markets": "KRW-USDT"}, timeout=5).json()
            rate = float(data[0]["trade_price"])
            if rate > 0:
                return {"rate": rate, "source": "업비트 USDT/KRW"}
        except Exception:
            pass
        try:
            rate = float(self._get(ER_API_URL, timeout=5).json()["rates"]["KRW"])
            if rate > 0:
                return {"rate": rate, "source": "공개 환율 USD/KRW (하루 한 번 갱신)"}
        except Exception:
            pass
        summary, _ = self._yahoo("KRW=X")
        if summary and summary["last"] > 0:
            return {"rate": summary["last"], "source": "Yahoo USD/KRW"}
        return None

    # ---- 묶어서 돌려주기
    def peek(self) -> dict | None:
        """저장해 둔 값 (없으면 None). 새로 받아 오지 않는다."""
        return self._cache

    def stale(self) -> bool:
        if self._cache is None:
            return True
        ttl = self.ttl if self._cache.get("available") else 60
        return time.time() - self._cached_at >= ttl

    def refresh_async(self, force: bool = False) -> bool:
        """화면을 기다리게 하지 않도록 뒤에서 새로 받아 온다. 이미 받는 중이면 아무것도 하지 않는다."""
        if self._busy or not (force or self.stale()):
            return self._busy
        self._busy = True
        threading.Thread(target=self.fetch, kwargs={"force": True}, name="macro", daemon=True).start()
        return True

    @property
    def busy(self) -> bool:
        return self._busy

    def fetch(self, force: bool = False) -> dict:
        if not force and not self.stale():
            return self._cache
        with self._lock:
            if not force and not self.stale():       # 기다리는 동안 다른 쪽에서 이미 받아 왔다
                return self._cache
            self._busy = True
            try:
                data = self._fetch()
            finally:
                self._busy = False
            previous = self._cache
            if not data["available"] and previous and previous.get("available"):
                # 이번에는 실패했지만 앞서 받은 값이 있으면 그 값을 계속 보여 준다 (언제 것인지 표시)
                data = {**previous, "errors": data["errors"], "missing": data["missing"], "old": True,
                        "fx": data.get("fx") or previous.get("fx")}
            self._cache, self._cached_at = data, time.time()
            return data

    def _fetch(self) -> dict:
        indices, extra, errors, items = {}, {}, {}, []
        for symbol, name in US_INDICES.items():
            summary, reason = self._yahoo(symbol)
            source = "Yahoo Finance"
            if summary is None:
                summary, reason2 = self._fred(FRED_IDS[symbol])
                source = "FRED (전일 종가)"
                if summary is None:
                    errors[name] = f"{reason}, {reason2}"
                    log.warning("미국 증시 데이터(%s) 조회 실패: %s, %s", name, reason, reason2)
            if summary:
                summary["source"] = source
                indices[name] = summary
            items.append({"key": symbol, "name": name, "kind": "index", **(summary or {"error": errors.get(name, "")})})
        for symbol, name in MACRO_EXTRA.items():
            summary, reason = self._yahoo(symbol)
            source = "Yahoo Finance, COMEX 금 선물 (USD/온스)"
            if summary is None:
                summary, reason2 = self._gold_spot()
                source = "gold-api.com, 금 현물 XAU/USD"
                if summary is None:
                    errors[name] = f"{reason}, {reason2}"
            if summary:
                summary["source"] = source
                extra[name] = summary
            items.append({"key": symbol, "name": name, "kind": "gold", **(summary or {"error": errors.get(name, "")})})
        data = {"indices": indices, "extra": extra, "available": bool(indices), "missing": list(errors),
                "errors": errors, "items": items, "fx": self._fx(), "fetched_at": time.time()}
        if indices:
            average = sum(v["chg_1d"] for v in indices.values()) / len(indices)
            data["avg_chg_1d"] = round(average, 2)
            data["mood"] = "risk_on" if average > 0.5 else "risk_off" if average < -0.5 else "neutral"
        return data


def _sig(value: float, digits: int = 6) -> float:
    """유효숫자 6자리로 줄인다 (프롬프트 길이 절약)."""
    return float(f"{value:.{digits}g}")


def chart_candles(market, symbol: str, tf: str, count: int = 300, end: int | None = None) -> tuple[list[dict], bool]:
    """차트용 캔들과 '더 과거 데이터가 있는지'를 돌려준다.

    end(밀리초)를 주면 그 이전 구간을 돌려준다. 화면을 과거로 옮길 때 이어서 불러오는 데 쓴다.
    Bybit에 없는 3시간봉과 년봉은 1시간봉, 월봉을 묶어서 만든다.
    """
    extra = {"end": end} if end else {}
    if tf == "Y":
        base = market.get_klines(symbol, "M", 1000)          # 월봉은 상장 이후 전체가 한 번에 들어온다
        key = lambda c: time.gmtime(c["t"] / 1000).tm_year
        has_more = False
    elif tf == "180":
        need = min(1000, count * 3 + 3)
        base = market.get_klines(symbol, "60", need, **extra)
        span = 3 * 3600 * 1000
        key = lambda c: c["t"] - c["t"] % span
        has_more = len(base) >= need
    else:
        candles = market.get_klines(symbol, tf, count, **extra)
        return candles, len(candles) >= count
    grouped: dict = {}
    for candle in base:
        bucket = grouped.get(key(candle))
        if bucket is None:
            grouped[key(candle)] = dict(candle)
        else:
            bucket["h"], bucket["l"] = max(bucket["h"], candle["h"]), min(bucket["l"], candle["l"])
            bucket["c"], bucket["v"] = candle["c"], bucket["v"] + candle["v"]
    result = list(grouped.values())
    if has_more:
        result = result[1:]                                  # 맨 앞 묶음은 잘려 있을 수 있어 버린다
    if tf == "Y" and end:
        result = [c for c in result if c["t"] <= end]
    return result[-count:], has_more


def coin_snapshot(market, symbol: str) -> dict:
    """한 코인의 15분/1시간/일봉 지표와 현재 시세."""
    snapshot: dict = {"symbol": symbol, "name": coin_meta(symbol)["name"]}
    try:
        ticker = market.get_ticker(symbol)
        snapshot.update({"price": ticker["price"], "chg_24h": round(ticker["change_24h_pct"], 2),
                         "funding_pct": round(ticker["funding_rate_pct"], 4)})
        m15_candles = market.get_klines(symbol, "15", 120)
        h1_candles = market.get_klines(symbol, "60", 120)
        snapshot["m15"] = summarize(m15_candles)
        snapshot["h1"] = summarize(h1_candles)
        # 캔들 모양(가격 행동)을 AI가 직접 볼 수 있게 최근 봉의 [시가, 고가, 저가, 종가]를 함께 넘긴다
        snapshot["recent_m15"] = [[_sig(c[k]) for k in ("o", "h", "l", "c")] for c in m15_candles[-24:]]
        snapshot["recent_h1"] = [[_sig(c[k]) for k in ("o", "h", "l", "c")] for c in h1_candles[-12:]]
        snapshot["d1"] = summarize(market.get_klines(symbol, "D", 60))
        snapshot["ok"] = bool(snapshot["m15"].get("ok") and snapshot["h1"].get("ok"))
    except Exception as exc:
        snapshot["ok"] = False
        snapshot["error"] = str(exc)
        log.warning("%s 지표 계산 실패: %s", symbol, exc)
    return snapshot


def all_snapshots(market, symbols: list[str] | None = None) -> dict[str, dict]:
    return {symbol: coin_snapshot(market, symbol) for symbol in (symbols or SYMBOLS)}


# ------------------------------------------------------------------ 가상 시세
class SyntheticMarket:
    """점검용 가상 시세. 5분봉을 무작위(추세 구간 포함)로 만들어 Bybit 시세처럼 제공한다."""

    BASE = {"BTCUSDT": 65000.0, "ETHUSDT": 3200.0, "DOGEUSDT": 0.16, "XRPUSDT": 0.62}
    STEPS = {"BTCUSDT": (0.001, 0.1), "ETHUSDT": (0.01, 0.01), "DOGEUSDT": (1.0, 0.00001), "XRPUSDT": (1.0, 0.0001)}

    def __init__(self, seed: int = 7, history_bars: int = 6000, start_ms: int | None = None,
                 trend: float | None = None, revert: float = 0.0):
        """trend: 봉(5분)마다의 고정 추세 (예: -0.00025면 꾸준한 하락장). None이면 추세가 무작위로 바뀐다.
        revert: 0보다 크면 가격이 기준가로 되돌아가려는 힘 (박스권 시세)."""
        self.trend, self.revert = trend, revert
        self.base, self.steps = dict(self.BASE), dict(self.STEPS)
        self.history_bars = history_bars
        self.random = random.Random(seed)
        self.bar_ms = 5 * 60 * 1000
        self.now_ms = start_ms if start_ms is not None else int(time.time() * 1000)
        self.now_ms -= self.now_ms % self.bar_ms
        self.bars: dict[str, list[dict]] = {}
        self._drift = {s: 0.0 for s in self.base}
        for symbol in self.base:
            self._fill(symbol)
        # 솔라나는 따로 떨어진 난수로 만든다 (다른 코인의 가상 시세가 예전과 똑같이 나오게)
        self._own_random = {"SOLUSDT": random.Random(seed * 7919 + 17)}
        self.add_symbol("SOLUSDT", 150.0, qty_step=0.1, tick=0.01)

    def _fill(self, symbol: str) -> None:
        self.bars[symbol] = []
        price, t = self.base[symbol], self.now_ms - self.history_bars * self.bar_ms
        for _ in range(self.history_bars):
            price = self._append_bar(symbol, t, price)
            t += self.bar_ms

    def add_symbol(self, symbol: str, base: float, qty_step: float = 0.1, tick: float = 0.001) -> None:
        """점검용: 코인을 하나 더 만든다 (사용자가 직접 추가한 코인을 흉내 낸다)."""
        self.base[symbol], self.steps[symbol], self._drift[symbol] = base, (qty_step, tick), 0.0
        self._fill(symbol)

    def _append_bar(self, symbol: str, t: int, price: float) -> float:
        rng = getattr(self, "_own_random", {}).get(symbol, self.random)
        if rng.random() < 0.02:  # 가끔 추세 방향이 바뀐다
            self._drift[symbol] = rng.gauss(0, 0.0006)
        drift = self._drift[symbol]
        if self.trend is not None:
            drift = self.trend
        if self.revert:
            drift = -self.revert * math.log(price / self.base[symbol])
        vol = 0.002 if symbol in ("BTCUSDT", "ETHUSDT") else 0.0035
        close = max(price * math.exp(drift + rng.gauss(0, vol)), 1e-9)
        high = max(price, close) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(price, close) * (1 - abs(rng.gauss(0, vol / 2)))
        self.bars[symbol].append({"t": t, "o": price, "h": high, "l": low, "c": close,
                                  "v": abs(rng.gauss(1000, 300))})
        if len(self.bars[symbol]) > 12000:
            del self.bars[symbol][:2000]
        return close

    def advance(self, bars: int = 1) -> None:
        for _ in range(bars):
            self.now_ms += self.bar_ms
            for symbol in self.base:
                self._append_bar(symbol, self.now_ms, self.bars[symbol][-1]["c"])

    def get_price(self, symbol: str) -> float:
        return self.bars[symbol][-1]["c"]

    def get_ticker(self, symbol: str) -> dict:
        bars = self.bars[symbol]
        day_ago = bars[-288]["c"] if len(bars) > 288 else bars[0]["c"]
        price = bars[-1]["c"]
        day = bars[-288:]
        return {"price": price, "mark_price": price, "change_24h_pct": (price / day_ago - 1) * 100,
                "funding_rate_pct": 0.01, "volume_24h": sum(b["v"] * b["c"] for b in day),
                "high_24h": max(b["h"] for b in day), "low_24h": min(b["l"] for b in day)}

    def get_klines(self, symbol: str, interval: str = "15", limit: int = 200, end: int | None = None) -> list[dict]:
        size = {"1": 1, "3": 1, "5": 1, "15": 3, "30": 6, "60": 12, "120": 24, "240": 48, "360": 72,
                "720": 144, "D": 288, "W": 2016, "M": 8640}.get(str(interval), 3)
        bars = self.bars[symbol]
        if end is not None:
            bars = [b for b in bars if b["t"] <= end]
        if size == 1:
            return [dict(b) for b in bars[-limit:]]
        span = size * self.bar_ms                 # 실제 거래소처럼 시각에 맞춰 묶는다 (마지막 봉은 진행 중일 수 있다)
        out: list[dict] = []
        for b in bars[-(limit + 1) * size:]:
            key = b["t"] - b["t"] % span
            if out and out[-1]["t"] == key:
                last = out[-1]
                last["h"], last["l"] = max(last["h"], b["h"]), min(last["l"], b["l"])
                last["c"], last["v"] = b["c"], last["v"] + b["v"]
            else:
                out.append({"t": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
        return out[-limit:]

    def get_instrument(self, symbol: str) -> dict:
        if symbol not in self.steps:
            raise KeyError(f"{symbol} 상품 정보를 찾을 수 없습니다.")
        qty_step, tick = self.steps[symbol]
        return {"qty_step": qty_step, "min_qty": qty_step, "max_qty": 0, "min_notional": 5.0,
                "tick_size": tick, "max_leverage": 50.0}

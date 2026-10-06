"""Myself Trade - 기술 지표 (외부 라이브러리 없이 순수 파이썬으로 계산)."""
from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], period: int) -> float:
    window = values[-period:]
    return sum(window) / len(window) if window else 0.0


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    if len(closes) < slow:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "hist_prev": 0.0}
    fast_line, slow_line = ema(closes, fast), ema(closes, slow)
    line = [f - s for f, s in zip(fast_line, slow_line)]
    signal_line = ema(line, signal)
    hist = [m - s for m, s in zip(line, signal_line)]
    return {"macd": line[-1], "signal": signal_line[-1], "hist": hist[-1],
            "hist_prev": hist[-2] if len(hist) > 1 else hist[-1]}


def atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    ranges = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        ranges.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    value = sum(ranges[:period]) / min(period, len(ranges))
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def bollinger(closes: list[float], period: int = 20, width: float = 2.0) -> dict:
    window = closes[-period:]
    if len(window) < 2:
        price = closes[-1] if closes else 0.0
        return {"mid": price, "upper": price, "lower": price, "pos": 0.5}
    mid = sum(window) / len(window)
    std = (sum((v - mid) ** 2 for v in window) / len(window)) ** 0.5
    upper, lower = mid + width * std, mid - width * std
    pos = (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5
    return {"mid": mid, "upper": upper, "lower": lower, "pos": pos}


def efficiency(closes: list[float], period: int = 30) -> float:
    """추세 효율(0~1). 한 방향으로 곧게 움직였으면 1에 가깝고, 오르내리기만 했으면 0에 가깝다."""
    window = closes[-(period + 1):]
    path = sum(abs(b - a) for a, b in zip(window, window[1:]))
    return abs(window[-1] - window[0]) / path if path > 0 else 0.0


def slope_pct(closes: list[float], period: int = 30) -> float:
    """추세선(최근 종가의 회귀 직선) 기울기. 봉 하나마다 평균 몇 % 움직였는지."""
    window = closes[-period:]
    n = len(window)
    if n < 3:
        return 0.0
    mean_x, mean_y = (n - 1) / 2, sum(window) / n
    cov = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(window))
    var = sum((i - mean_x) ** 2 for i in range(n))
    return (cov / var) / mean_y * 100 if var and mean_y else 0.0


def structure(candles: list[dict], span: int = 10) -> str:
    """고점과 저점의 구조. 최근 구간의 고점과 저점이 앞 구간보다 함께 높으면 up, 함께 낮으면 down."""
    if len(candles) < span * 2:
        return "mixed"
    recent, before = candles[-span:], candles[-span * 2:-span]
    high_now, low_now = max(c["h"] for c in recent), min(c["l"] for c in recent)
    high_old, low_old = max(c["h"] for c in before), min(c["l"] for c in before)
    if high_now > high_old and low_now > low_old:
        return "up"
    if high_now < high_old and low_now < low_old:
        return "down"
    return "mixed"


RANGE_ER = 0.22      # 추세 효율이 이보다 낮으면 박스권(횡보)으로 본다


def summarize(candles: list[dict]) -> dict:
    """캔들 목록(과거 -> 최신)을 AI와 규칙 전략이 읽을 요약 지표로 바꾼다."""
    if len(candles) < 30:
        return {"ok": False}
    closes = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]
    price = closes[-1]
    ema20, ema50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    macd_now = macd(closes)
    atr_value = atr(candles)
    band = bollinger(closes)
    avg_volume = sma(volumes[:-1], 20) or 1.0

    def change(bars: int) -> float:
        if len(closes) <= bars or closes[-bars - 1] == 0:
            return 0.0
        return (price / closes[-bars - 1] - 1) * 100

    if price > ema20 > ema50:
        trend = "up"
    elif price < ema20 < ema50:
        trend = "down"
    else:
        trend = "side"
    er = efficiency(closes)
    net = price - closes[-31] if len(closes) > 30 else price - closes[0]
    regime = "range" if er < RANGE_ER else ("up" if net > 0 else "down")
    return {
        "ok": True,
        "regime": regime,                         # 장세: up(상승 추세), down(하락 추세), range(박스권)
        "er": round(er, 2),                       # 추세 효율 0~1
        "slope_pct": round(slope_pct(closes), 4), # 추세선 기울기 (봉당 %)
        "structure": structure(candles),          # 고점/저점 구조: up, down, mixed
        "ema_gap_pct": round((ema20 / ema50 - 1) * 100, 3) if ema50 else 0.0,
        "macd_line": macd_now["macd"],
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "trend": trend,
        "rsi": round(rsi(closes), 1),
        "macd_hist": macd_now["hist"],
        "macd_rising": macd_now["hist"] > macd_now["hist_prev"],
        "atr_pct": round(atr_value / price * 100, 3) if price else 0.0,
        "bb_pos": round(band["pos"], 2),          # 0=하단 밴드, 1=상단 밴드
        "chg_4": round(change(4), 2),             # 최근 4개 봉 변동률(%)
        "chg_16": round(change(16), 2),
        "vol_ratio": round(volumes[-1] / avg_volume, 2),
        "high_20": max(c["h"] for c in candles[-20:]),
        "low_20": min(c["l"] for c in candles[-20:]),
    }

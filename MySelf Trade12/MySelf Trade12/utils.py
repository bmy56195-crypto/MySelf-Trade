"""Myself Trade - 공용 도구 (시간, 반올림, 손익/수수료 계산, 로그)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

log = logging.getLogger("myself_trade")


def setup_logging(log_path=None, level=logging.INFO) -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("myself_trade")
    root.setLevel(level)
    if root.handlers:
        return
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


# ------------------------------------------------------------------ 시간
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """UTC 기준 ISO 문자열 (DB 저장용, 문자열 정렬 = 시간 정렬)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def get_tz(name: str):
    try:
        return ZoneInfo(name)
    except Exception:  # 시간대 데이터가 없으면(tzdata 미설치 등) UTC로 대체
        log.warning("시간대 '%s'를 찾을 수 없어 UTC를 사용합니다. 'pip install tzdata'를 실행해 주세요.", name)
        return timezone.utc


def to_local(dt: datetime, tz_name: str) -> datetime:
    return dt.astimezone(get_tz(tz_name))


def local_day(dt: datetime, tz_name: str) -> str:
    """설정한 시간대 기준 날짜 (YYYY-MM-DD). 일일 손익/거래 횟수의 기준이 된다."""
    return to_local(dt, tz_name).strftime("%Y-%m-%d")


def day_start_utc(dt: datetime, tz_name: str) -> datetime:
    """dt가 속한 현지 날짜의 0시를 UTC로 돌려준다."""
    local = to_local(dt, tz_name)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def day_fraction(dt: datetime, tz_name: str) -> float:
    """현지 하루 중 지나간 비율 (0~1)."""
    local = to_local(dt, tz_name)
    return (local.hour * 3600 + local.minute * 60 + local.second) / 86400


def fmt_local(text_or_dt, tz_name: str, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not text_or_dt:
        return ""
    dt = parse_iso(text_or_dt) if isinstance(text_or_dt, str) else text_or_dt
    return to_local(dt, tz_name).strftime(pattern)


def minutes_between(start: datetime, end: datetime) -> float:
    return (end - start) / timedelta(minutes=1)


# ------------------------------------------------------------------ 숫자
def round_step(value: float, step: float, up: bool = False) -> float:
    """거래소 수량/가격 단위(step)에 맞춰 내림(기본) 또는 올림."""
    if not step or step <= 0:
        return float(value)
    d_value, d_step = Decimal(str(value)), Decimal(str(step))
    units = (d_value / d_step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return float(units * d_step)


def fmt_step(value: float, step: float) -> str:
    """step의 소수 자릿수에 맞춘 문자열 (거래소 주문 파라미터용)."""
    d_step = Decimal(str(step)).normalize()
    decimals = max(0, -d_step.as_tuple().exponent)
    return f"{Decimal(str(value)):.{decimals}f}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 손익/수수료
def calc_trade_pnl(side: str, entry: float, exit_price: float, qty: float,
                   fee_in_rate: float, fee_out_rate: float) -> dict:
    """수수료를 반영한 한 번의 거래(진입+청산) 손익.

    side: "long" 또는 "short"
    fee_*_rate: 소수 (0.055% -> 0.00055)
    수수료는 '거래 금액(가격 x 수량)' 기준으로 진입, 청산 각각 부과된다.
    레버리지를 쓰면 증거금 대비 수수료 부담이 레버리지 배수만큼 커진다.
    """
    direction = 1.0 if side == "long" else -1.0
    gross = (exit_price - entry) * qty * direction
    fee_in = entry * qty * fee_in_rate
    fee_out = exit_price * qty * fee_out_rate
    return {
        "gross_pnl": gross,
        "fee_in": fee_in,
        "fee_out": fee_out,
        "fee": fee_in + fee_out,
        "net_pnl": gross - fee_in - fee_out,
    }


def breakeven_move_pct(fee_in_rate: float, fee_out_rate: float) -> float:
    """수수료를 내고 본전이 되려면 가격이 유리하게 움직여야 하는 폭(%)."""
    return (fee_in_rate + fee_out_rate) * 100


def unrealized_pnl(side: str, entry: float, price: float, qty: float) -> float:
    return (price - entry) * qty * (1.0 if side == "long" else -1.0)


def trade_context(trade: dict) -> dict:
    """거래 기록에 함께 저장한 진입 당시 정보(JSON)를 꺼낸다."""
    import json
    try:
        data = json.loads(trade.get("context") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def split_left(trade: dict) -> int:
    """분할 매수에서 아직 사지 않은 회차 수. 분할 매수가 아니면 0."""
    split = trade_context(trade).get("split") or {}
    return max(0, int(split.get("total", 0)) - int(split.get("done", 0)))

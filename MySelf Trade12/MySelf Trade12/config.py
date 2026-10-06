"""Myself Trade - 설정 모듈.

- 고정 상수(코인 목록, 시간대, 수수료 기본값)
- 사용자 설정(data/settings.json) 읽기/쓰기와 값 검증
- API 키(.env) 읽기/쓰기
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

APP_NAME = "Myself Trade"
VERSION = "12.0.0"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "myself_trade.db"
ENV_PATH = BASE_DIR / ".env"
METHODS_PATH = BASE_DIR / "methodologies.json"
LOG_PATH = DATA_DIR / "myself_trade.log"

# 기본 거래 대상 코인 5종 (Bybit USDT 무기한 선물 심볼: Trade > Futures > Perpetual > USDT)
BASE_SYMBOLS: dict[str, dict] = {
    "BTCUSDT": {"ticker": "BTC", "name": "비트코인", "short": "비트"},
    "ETHUSDT": {"ticker": "ETH", "name": "이더리움", "short": "이더"},
    "SOLUSDT": {"ticker": "SOL", "name": "솔라나", "short": "솔라나"},
    "XRPUSDT": {"ticker": "XRP", "name": "리플", "short": "리플"},
    "DOGEUSDT": {"ticker": "DOGE", "name": "도지코인", "short": "도지"},
}
# 지금 쓸 수 있는 코인 = 기본 5종 + 사용자가 설정에서 직접 추가한 코인.
# 프로그램 전체가 이 dict 하나를 함께 보므로, 코인을 추가하거나 지울 때는 새로 만들지 않고 안의 내용만 바꾼다.
SYMBOLS: dict[str, dict] = dict(BASE_SYMBOLS)
MAX_CUSTOM_SYMBOLS = 8
_SYMBOL = re.compile(r"^[A-Z0-9]{1,16}USDT$")


def normalize_symbol(text) -> str:
    """'sol', 'SOL/USDT', 'solusdt' 같은 입력을 Bybit 심볼(SOLUSDT)로 바꾼다. 형식이 틀리면 빈 문자열."""
    symbol = re.sub(r"[\s/\-_]", "", str(text or "")).upper()
    if symbol and not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol if _SYMBOL.match(symbol) else ""


def coin_meta(symbol: str) -> dict:
    """코인의 표시 이름. 목록에서 지운 코인의 옛 거래 기록도 깨지지 않게 기본값을 준다."""
    meta = SYMBOLS.get(symbol)
    if meta:
        return meta
    ticker = symbol[:-4] if symbol.endswith("USDT") else symbol
    return {"ticker": ticker, "name": ticker, "short": ticker}


def apply_custom_symbols(items: list[dict]) -> None:
    """사용자가 추가한 코인 목록을 SYMBOLS에 반영한다."""
    for key in [k for k in SYMBOLS if k not in BASE_SYMBOLS]:
        del SYMBOLS[key]
    for item in items or []:
        symbol = item["symbol"]
        ticker = symbol[:-4]
        name = item.get("name") or ticker
        SYMBOLS[symbol] = {"ticker": ticker, "name": name, "short": name if len(name) <= 4 else ticker, "custom": True}

# 참고용 미국 증시 지수 (Yahoo Finance 심볼)
US_INDICES: dict[str, str] = {
    "^IXIC": "나스닥",
    "^DJI": "다우존스",
    "^GSPC": "S&P 500",
}

# 참고용 추가 지표 (거래 대상은 아니고 시장 분위기를 읽는 데만 쓴다)
MACRO_EXTRA: dict[str, str] = {
    "GC=F": "국제 금 시세",
}

# 차트 타임프레임 (값, 화면 이름, 묶음)
TIMEFRAMES: list[tuple[str, str, str]] = [
    ("1", "1분", "분"), ("3", "3분", "분"), ("5", "5분", "분"), ("15", "15분", "분"), ("30", "30분", "분"),
    ("60", "1시간", "시간"), ("180", "3시간", "시간"), ("360", "6시간", "시간"), ("720", "12시간", "시간"),
    ("D", "일봉", "일"), ("W", "주봉", "일"), ("M", "월봉", "일"), ("Y", "년봉", "일"),
]

# 지원 시간대 (화면 표시 이름 -> IANA 시간대)
TIMEZONES: dict[str, str] = {
    "한국 (서울)": "Asia/Seoul",
    "미국 (뉴욕)": "America/New_York",
    "미국 (워싱턴 D.C.)": "America/New_York",
    "미국 (LA)": "America/Los_Angeles",
    "캐나다 (밴쿠버)": "America/Vancouver",
    "캐나다 (토론토)": "America/Toronto",
    "캐나다 (캘거리)": "America/Edmonton",
    "스페인 (마드리드)": "Europe/Madrid",
    "독일 (베를린)": "Europe/Berlin",
    "영국 (런던)": "Europe/London",
    "프랑스 (파리)": "Europe/Paris",
    "이탈리아 (로마)": "Europe/Rome",
    "UTC": "UTC",
}

# 거래 모드
MODES: dict[str, str] = {
    "paper": "모의 투자 (실제 주문 없음)",
    "demo": "Bybit 데모 트레이딩",
    "testnet": "Bybit 테스트넷",
    "live": "실전 거래",
}

DEFAULT_SETTINGS: dict = {
    # --- 실행 제어 ---
    "trading_enabled": False,        # 자동 매매 On/Off
    "mode": "paper",                 # paper | demo | testnet | live
    "real_mode": "demo",             # 모의투자 모드를 껐을 때 주문이 나갈 계정 (demo | testnet | live)
    "close_on_off": False,           # Off로 바꿀 때 보유 포지션을 모두 청산할지
    # --- 자금/리스크 ---
    "invest_pct": 10,                # 증거금 투자율: 총자산 중 증거금으로 쓸 비율(%)
    "max_open_positions": 2,         # 동시에 보유할 수 있는 포지션 수
    "trade_symbols": list(BASE_SYMBOLS),  # 거래할 코인 (고른 코인만 새로 진입한다)
    "custom_symbols": [],            # 직접 추가한 코인 [{"symbol": "SOLUSDT", "name": "솔라나"}]
    "entry_mode": "auto",            # 매수 방식: auto(AI가 결정) | single(일괄 매수) | split(분할 매수)
    "split_count": 3,                # 분할 매수 때 몇 번에 나눠 살지
    "exit_mode": "auto",             # 매도 방식: auto(AI가 결정) | single(일괄 매도) | split(분할 매도)
    "exit_split_count": 3,           # 분할 매도 때 몇 번에 나눠 팔지
    "direction_mode": "auto",        # 진입 방향: auto(롱/숏 모두) | long(롱만) | short(숏만)
    "leverage_mode": "auto",         # auto(AI 결정) | manual(수동 고정)
    "manual_leverage": 5,            # 수동 고정 레버리지
    "leverage_min": 1,
    "leverage_max": 50,
    # --- 전체 원금 기준 (하루 단위) ---
    "daily_target_unit": "pct",      # 하루 목표 수익을 정하는 방식: pct(원금 대비 %) | invest(투자금 대비 %) | amount(금액)
    "daily_target_pct": 30,          # 하루 목표 수익률(%)
    "daily_target_invest_pct": 50,   # 하루 목표 수익: 투자금(오늘 원금 x 증거금 투자율) 대비 %
    "daily_target_amount": 300.0,    # 하루 목표 수익 금액(USDT)
    "stop_on_target": False,         # 목표 달성 시 그날 신규 진입 중지
    "daily_loss_unit": "pct",        # 하루 허용 손실을 정하는 방식: pct | invest | amount
    "daily_loss_limit_pct": 25,      # 하루 최대 손실(%) - 도달 시 당일 매매 중지
    "daily_loss_invest_pct": 50,     # 하루 최대 손실: 투자금(오늘 원금 x 증거금 투자율) 대비 %
    "daily_loss_amount": 250.0,      # 하루 최대 손실 금액(USDT)
    # --- 해당 회차 진입 투자금(증거금) 기준 (포지션 단위) ---
    "tpsl_mode": "auto",             # 포지션 익절/손절: auto(AI가 변동성과 차트로 정함) | manual(아래 값으로 고정)
    "pos_tp_unit": "pct",            # 익절: pct(투자금 대비 %) | amount(금액)
    "pos_tp_pct": 50,                # 투자금의 몇 % 수익에서 익절할지
    "pos_tp_amount": 50.0,           # 포지션 하나에서 얼마(USDT) 수익이 나면 익절할지
    "pos_sl_unit": "pct",
    "pos_sl_pct": 20,                # 투자금의 몇 % 손실에서 손절할지
    "pos_sl_amount": 20.0,
    "cross_check": True,             # Gemini 판단과 규칙 점수가 같은 방향일 때만 진입
    # --- 거래 횟수 ---
    "min_trades_per_day": 3,
    "max_trades_per_day": 100,
    "max_hold_minutes": 240,         # 한 포지션 최대 보유 시간(분)
    # --- 분석 ---
    "min_confidence": 0.6,           # 진입에 필요한 최소 확신도(0~1)
    "ai_interval_minutes": 15,       # 진입 판단 주기(분)
    "loop_seconds": 5,               # 포지션 감시 주기(초). 실시간 시세로 손절/익절을 확인한다.
    "gemini_model": "gemini-3.8-flash",
    "gemini_backup_model": "auto",   # 기본 모델이 혼잡할 때(HTTP 503) 대신 부를 모델. auto면 프로그램이 고른다
    # --- 수수료 (Bybit 일반 등급 USDT 무기한 기준, %) ---
    "taker_fee_pct": 0.055,
    "maker_fee_pct": 0.02,
    # --- 기타 ---
    "timezone": "Asia/Seoul",
    "paper_start_balance": 1000.0,   # 모의 투자 시작 잔고(USDT)
    # --- 일일 보고서 메일 ---
    "mail_enabled": False,
    "mail_recipients": [],           # 받는 메일 주소 목록
    "mail_method": "simple",         # simple: 주소만으로 발송(메일 전달 서비스) | smtp: 내 메일 계정으로 발송
    "mail_from": "",                 # 보내는 메일 계정 (예: Gmail 주소)
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "mail_hour": 21,                 # 보고서를 보내는 시각(시, 선택한 시간대 기준)
    "settings_version": 8,
}

# 값 검증 규칙: (타입, 최소, 최대) 또는 허용 목록
_RULES: dict = {
    "trading_enabled": bool,
    "mode": list(MODES.keys()),
    "real_mode": ["demo", "testnet", "live"],
    "close_on_off": bool,
    "invest_pct": (float, 1, 100),
    "max_open_positions": (int, 1, 5),
    "trade_symbols": "symbols",
    "custom_symbols": "coins",
    "entry_mode": ["auto", "single", "split"],
    "split_count": (int, 2, 5),
    "exit_mode": ["auto", "single", "split"],
    "exit_split_count": (int, 2, 5),
    "direction_mode": ["auto", "long", "short"],
    "leverage_mode": ["auto", "manual"],
    "manual_leverage": (int, 1, 50),
    "leverage_min": (int, 1, 50),
    "leverage_max": (int, 1, 50),
    "daily_target_unit": ["pct", "invest", "amount"],
    "daily_target_pct": (float, 1, 1000),
    "daily_target_invest_pct": (float, 1, 10000),
    "daily_target_amount": (float, 0.1, 100_000_000),
    "stop_on_target": bool,
    "daily_loss_unit": ["pct", "invest", "amount"],
    "daily_loss_limit_pct": (float, 1, 100),
    "daily_loss_invest_pct": (float, 1, 10000),
    "daily_loss_amount": (float, 0.1, 100_000_000),
    "tpsl_mode": ["auto", "manual"],
    "pos_tp_unit": ["pct", "amount"],
    "pos_tp_pct": (float, 1, 1000),
    "pos_tp_amount": (float, 0.1, 100_000_000),
    "pos_sl_unit": ["pct", "amount"],
    "pos_sl_pct": (float, 1, 60),
    "pos_sl_amount": (float, 0.1, 100_000_000),
    "cross_check": bool,
    "min_trades_per_day": (int, 0, 100),
    "max_trades_per_day": (int, 1, 100),
    "max_hold_minutes": (int, 5, 1440),
    "min_confidence": (float, 0.3, 0.95),
    "ai_interval_minutes": (int, 1, 240),
    "loop_seconds": (int, 5, 600),
    "gemini_model": "model",
    "gemini_backup_model": "model",
    "taker_fee_pct": (float, 0, 1),
    "maker_fee_pct": (float, 0, 1),
    "timezone": sorted(set(TIMEZONES.values())),
    "paper_start_balance": (float, 10, 10_000_000),
    "mail_enabled": bool,
    "mail_recipients": "emails",
    "mail_method": ["simple", "smtp"],
    "mail_from": "email",
    "smtp_host": str,
    "smtp_port": (int, 1, 65535),
    "mail_hour": (int, 0, 23),
    "settings_version": (int, 1, 99),
}

# 오류 문구에 쓸 설정 이름 (화면에 보이는 이름과 같게)
_LABELS: dict[str, str] = {
    "trade_symbols": "거래할 코인", "custom_symbols": "직접 추가한 코인",
    "invest_pct": "증거금 투자율", "max_open_positions": "동시 보유 포지션", "split_count": "분할 매수 횟수", "exit_split_count": "분할 매도 횟수",
    "manual_leverage": "레버리지", "leverage_min": "레버리지 최소값", "leverage_max": "레버리지 최대값",
    "daily_target_pct": "하루 목표 수익률(%)", "daily_target_amount": "하루 목표 수익 금액(USDT)",
    "daily_loss_limit_pct": "하루 허용 손실(%)", "daily_loss_amount": "하루 허용 손실 금액(USDT)",
    "daily_target_invest_pct": "하루 목표 수익(투자금 대비 %)", "daily_loss_invest_pct": "하루 허용 손실(투자금 대비 %)",
    "pos_tp_pct": "포지션 익절(투자금 대비 %)", "pos_tp_amount": "포지션 익절 금액(USDT)",
    "pos_sl_pct": "포지션 손절(투자금 대비 %)", "pos_sl_amount": "포지션 손절 금액(USDT)",
    "min_trades_per_day": "하루 최소 거래 횟수", "max_trades_per_day": "하루 최대 거래 횟수",
    "max_hold_minutes": "보유 시간", "min_confidence": "최소 확신도", "ai_interval_minutes": "판단 주기(분)",
    "paper_start_balance": "모의투자 시작 잔고", "mail_hour": "보고서 발송 시각", "smtp_port": "메일 서버 포트",
}
HOLD_MIN, HOLD_MAX = 5, 1440      # 포지션 보유 시간 제한(분): 최소 5분, 최대 24시간

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SettingsError(ValueError):
    """설정 값이 올바르지 않을 때."""


def _coerce(key: str, value):
    """값을 규칙에 맞는 형태로 바꾼다. 맞출 수 없으면 SettingsError (다른 종류의 오류는 밖으로 내보내지 않는다)."""
    try:
        return _coerce_value(key, value)
    except SettingsError:
        raise
    except Exception as exc:
        raise SettingsError(f"{_LABELS.get(key, key)}: 값의 형식이 올바르지 않습니다.") from exc


def _coerce_value(key: str, value):
    rule = _RULES[key]
    if rule is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if rule == "model":                      # Gemini 모델 이름 (영문, 숫자, 점, 줄표만)
        text = str(value or "").strip()
        if key == "gemini_backup_model" and not text:
            return "auto"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,79}", text):
            raise SettingsError("Gemini 모델 이름이 올바르지 않습니다. 목록에서 골라 주세요.")
        return text
    if rule == "email":                      # 비워 둘 수 있는 메일 주소
        text = str(value or "").strip()
        if text and not _EMAIL.match(text):
            raise SettingsError(f"메일 주소 형식이 올바르지 않습니다 ({text}).")
        return text
    if rule == "coins":                      # 직접 추가한 코인 목록
        result = []
        for item in value or []:
            if isinstance(item, str):
                item = {"symbol": item}
            symbol = normalize_symbol(item.get("symbol"))
            if not symbol:
                raise SettingsError(f"코인 이름을 알아볼 수 없습니다 ({item.get('symbol')}). SOL처럼 영문 약자로 넣어 주세요.")
            if symbol in BASE_SYMBOLS or symbol in [r["symbol"] for r in result]:
                continue
            name = re.sub(r"[<>&\"'`\\]", "", str(item.get("name") or "")).strip()[:12]     # 화면에 그대로 나오는 글자라 기호는 뺀다
            result.append({"symbol": symbol, "name": name or symbol[:-4]})
        if len(result) > MAX_CUSTOM_SYMBOLS:
            raise SettingsError(f"직접 추가하는 코인은 {MAX_CUSTOM_SYMBOLS}개까지 넣을 수 있습니다.")
        return result
    if rule == "symbols":                    # 거래할 코인 목록 (한 개 이상)
        items = value.split(",") if isinstance(value, str) else list(value or [])
        chosen = [symbol for symbol in SYMBOLS if symbol in [str(i).strip().upper() for i in items]]
        if not chosen:
            raise SettingsError("거래할 코인을 한 개 이상 골라 주세요.")
        return chosen
    if rule == "emails":                     # 메일 주소 목록 (중복 제거, 최대 10개)
        items = value.split(",") if isinstance(value, str) else list(value or [])
        result: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            if not _EMAIL.match(text):
                raise SettingsError(f"메일 주소 형식이 올바르지 않습니다 ({text}).")
            if text.lower() not in [r.lower() for r in result]:
                result.append(text)
        if len(result) > 10:
            raise SettingsError("받는 메일 주소는 10개까지 넣을 수 있습니다.")
        return result
    if rule is str:
        text = str(value).strip()
        if not text:
            raise SettingsError(f"{key}: 값이 비어 있습니다.")
        return text
    if isinstance(rule, list):
        if value not in rule:
            raise SettingsError(f"{key}: 허용되지 않는 값입니다 ({value}).")
        return value
    kind, low, high = rule
    label = _LABELS.get(key, key)
    try:
        raw = float(value)
        if raw != raw or raw in (float("inf"), float("-inf")):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{label}: 숫자를 입력해 주세요 (입력값 {value!r}).") from exc
    if key == "max_hold_minutes" and not (low <= raw <= high):
        # 내림 처리로 4.9분이 4분이 되는 일이 없도록 원래 값으로 먼저 확인한다
        side = "미만이" if raw < low else "초과"
        raise SettingsError(f"보유 시간은 최소 {low}분, 최대 {high}분(24시간)까지 입력할 수 있습니다. "
                            f"입력한 {raw:g}분은 {low if raw < low else high}분 {side}라 저장하지 않았습니다.")
    number = kind(raw)
    if not (low <= number <= high):
        raise SettingsError(f"{label}: {low:g} ~ {high:g} 범위로 입력해 주세요 (입력값 {raw:g}).")
    return number


class Settings:
    """data/settings.json에 저장되는 사용자 설정. 여러 스레드에서 안전하게 쓸 수 있다."""

    def __init__(self, path: Path | str = SETTINGS_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if not isinstance(raw, dict):
                return
            # 예전 버전 설정 파일: 감시 주기 기본값(30초)을 새 기본값(5초)으로 올린다
            if int(raw.get("settings_version") or 1) < 2 and raw.get("loop_seconds") == 30:
                raw.pop("loop_seconds")
            # 예전 버전의 받는 주소(한 개)를 새 목록으로 옮긴다. 예전에 쓰던 SMTP 방식은 그대로 유지한다.
            if raw.get("mail_to") and not raw.get("mail_recipients"):
                raw["mail_recipients"] = [raw["mail_to"]]
                raw.setdefault("mail_method", "smtp" if raw.get("mail_from") else "simple")
            # 버전 10: 솔라나(SOL)가 기본 코인이 되었다. 예전 설정에서는 거래할 코인에 한 번 넣어 준다 (이후에는 설정에서 끌 수 있다).
            if int(raw.get("settings_version") or 1) < 6 and isinstance(raw.get("trade_symbols"), list) \
                    and "SOLUSDT" not in raw["trade_symbols"]:
                raw["trade_symbols"] = [*raw["trade_symbols"], "SOLUSDT"]
            # 버전 11: 동시 보유 포지션을 5개까지 넣을 수 있다. 예전 최대값(4개)으로 써 왔다면 새 최대값으로 올려 준다.
            if int(raw.get("settings_version") or 1) < 7 and raw.get("max_open_positions") == 4:
                raw["max_open_positions"] = 5
            raw["settings_version"] = DEFAULT_SETTINGS["settings_version"]
            try:      # 직접 추가한 코인을 먼저 반영해야 '거래할 코인' 값이 맞는지 확인할 수 있다
                self._data["custom_symbols"] = _coerce("custom_symbols", raw.get("custom_symbols", []))
            except SettingsError:
                self._data["custom_symbols"] = []
            apply_custom_symbols(self._data["custom_symbols"])
            for key, value in raw.items():
                if key in _RULES and key != "custom_symbols":
                    try:
                        self._data[key] = _coerce(key, value)
                    except SettingsError:
                        pass  # 잘못된 값은 기본값 유지
            # 손으로 고친 파일에서 서로 맞지 않는 값이 들어와도 프로그램이 켜지도록 기본값으로 되돌린다
            if self._data["leverage_min"] > self._data["leverage_max"]:
                self._data["leverage_min"], self._data["leverage_max"] = DEFAULT_SETTINGS["leverage_min"], DEFAULT_SETTINGS["leverage_max"]
            if self._data["min_trades_per_day"] > self._data["max_trades_per_day"]:
                self._data["min_trades_per_day"] = min(DEFAULT_SETTINGS["min_trades_per_day"], self._data["max_trades_per_day"])
            self._data["trade_symbols"] = [s for s in self._data["trade_symbols"] if s in SYMBOLS] or list(BASE_SYMBOLS)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, key: str):
        with self._lock:
            return self._data[key]

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict) -> dict:
        """검증 후 저장. 하나라도 잘못되면 아무것도 바꾸지 않는다."""
        with self._lock:
            new = dict(self._data)
            coins_changed = "custom_symbols" in changes
            try:
                if coins_changed:      # 코인 목록부터 바꿔야 '거래할 코인'을 새 목록으로 확인할 수 있다
                    new["custom_symbols"] = _coerce("custom_symbols", changes["custom_symbols"])
                    apply_custom_symbols(new["custom_symbols"])
                    new["trade_symbols"] = [s for s in new["trade_symbols"] if s in SYMBOLS] or list(BASE_SYMBOLS)
                for key, value in changes.items():
                    if key not in _RULES or key == "custom_symbols":
                        continue
                    new[key] = _coerce(key, value)
                if new["leverage_min"] > new["leverage_max"]:
                    raise SettingsError("레버리지 최소값이 최대값보다 큽니다.")
                if new["min_trades_per_day"] > new["max_trades_per_day"]:
                    raise SettingsError("하루 최소 거래 횟수가 최대 거래 횟수보다 큽니다.")
            except Exception:
                if coins_changed:
                    apply_custom_symbols(self._data["custom_symbols"])     # 저장하지 않으므로 코인 목록도 되돌린다
                raise
            self._data = new
            self.save()
            return dict(new)


# ---------------------------------------------------------------- API 키(.env)
SECRET_NAMES = ("BYBIT_API_KEY", "BYBIT_SECRET_KEY", "GEMINI_API_KEY", "MAIL_PASSWORD")
# 예전 이름으로 적어 둔 .env도 읽을 수 있게 한다
_ALIASES = {"BYBIT_SECRET_KEY": ("BYBIT_API_SECRET",)}


def load_env(path: Path | str = ENV_PATH) -> dict[str, str]:
    """.env 파일을 읽어 os.environ에 반영한다. (외부 라이브러리 없이 동작)"""
    values: dict[str, str] = {}
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        if value:
            os.environ[key] = value
    return values


def save_env(changes: dict[str, str], path: Path | str = ENV_PATH) -> None:
    """.env 파일의 지정한 키만 바꿔 저장한다. 빈 문자열을 주면 그 키를 지운다."""
    path = Path(path)
    current = load_env(path) if path.exists() else {}
    for key, value in changes.items():
        if key not in SECRET_NAMES:
            continue
        value = "".join(str(value or "").split())      # 키에는 띄어쓰기나 줄바꿈이 없다. 섞여 들어오면 뺀다.
        if value:
            current[key] = value
            os.environ[key] = value
        else:
            for name in (key, *_ALIASES.get(key, ())):
                current.pop(name, None)
                os.environ.pop(name, None)
    lines = ["# Myself Trade API 키 - 이 파일은 절대 다른 사람과 공유하지 마세요."]
    lines += [f"{key}={value}" for key, value in current.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        for alias in _ALIASES.get(name, ()):
            value = os.environ.get(alias, "").strip()
            if value:
                break
    return value


def has_bybit_keys() -> bool:
    return bool(get_secret("BYBIT_API_KEY") and get_secret("BYBIT_SECRET_KEY"))


def has_gemini_key() -> bool:
    return bool(get_secret("GEMINI_API_KEY"))


def has_mail_password() -> bool:
    return bool(get_secret("MAIL_PASSWORD"))


def load_reference_channels(path: Path | str = METHODS_PATH) -> list[str]:
    """참고하는 유튜브 채널 이름 목록 (methodologies.json의 channels)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    channels = data.get("channels", []) if isinstance(data, dict) else []
    return [str(c) for c in channels if str(c).strip()]


def load_methodologies(path: Path | str = METHODS_PATH) -> list[dict]:
    """참고할 차트 분석 방법론(유튜버 등) 목록을 읽는다."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("methodologies", []) if isinstance(data, dict) else data
    return [m for m in items if isinstance(m, dict) and m.get("enabled", True)]

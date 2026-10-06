"""Myself Trade - Gemini AI 분석.

세 가지 일을 한다.
  1) 오늘의 계획: 미국 증시 + 코인 지표 + 지난 학습 노트 -> 시장 방향, 추천 레버리지, 코인별 방향
  2) 진입 판단: 코인별 롱/숏/관망/청산과 그 이유
  3) 하루 회고: 그날 거래를 돌아보고 '학습 노트'를 남긴다 -> 다음 날 계획과 판단에 다시 투입

Gemini REST API를 직접 호출하므로 추가 라이브러리가 필요 없다.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

import requests

from config import SYMBOLS, coin_meta, get_secret, load_methodologies, load_reference_channels
from risk_manager import daily_limit_text, pos_limit_text
from utils import clamp, log, trade_context

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
CONNECTED_TEXT = "Gemini API가 성공적으로 연결되었습니다"


class GeminiError(Exception):
    def __init__(self, message: str, status: int | None = None, auth: bool = False):
        super().__init__(message)
        self.status = status          # HTTP 응답 코드 (연결 자체가 안 됐으면 None)
        self.auth = auth              # 키가 틀렸거나 권한이 없는 경우


# ---------------------------------------------------------------- 연결 상태
# 마지막 호출 결과를 기억해 두고 화면의 '연결됨' 표시에 쓴다. 키가 바뀌면 처음부터 다시 확인한다.
_STATUS = {"key": "", "ok": None, "at": 0.0, "message": "", "model": "", "note": ""}
_STATUS_LOCK = threading.Lock()


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else ""


def _record(ok: bool, message: str = "", model: str = "", auth: bool = False, key: str | None = None,
            note: str = "") -> None:
    """key: 이 호출을 보낼 때 쓴 키. 응답을 기다리는 사이 키가 바뀌었으면 그 결과는 새 키의 상태로 치지 않는다.
    note: 호출은 성공했지만 알려 둘 일 (기본 모델이 혼잡해 예비 모델로 대신 불렀을 때)."""
    with _STATUS_LOCK:
        mark = _fingerprint(get_secret("GEMINI_API_KEY"))
        if key is not None and _fingerprint(key) != mark:
            return
        if _STATUS["key"] != mark:
            _STATUS.update(key=mark, ok=None, message="", model="", note="")
        _STATUS["at"] = time.time()
        if ok:
            _STATUS.update(ok=True, message="", model=model or _STATUS["model"], note=note)
        else:
            # 한 번 연결에 성공한 키는, 잠깐의 통신 오류나 사용량 제한으로는 '연결 실패'로 바꾸지 않는다
            if auth or _STATUS["ok"] is not True:
                _STATUS["ok"] = False
            _STATUS["message"] = message


def gemini_status() -> dict:
    """화면에 보여 줄 Gemini 연결 상태. state: none(키 없음) | unchecked(확인 전) | ok(연결됨) | error(연결 실패)"""
    key = get_secret("GEMINI_API_KEY")
    if not key:
        return {"state": "none", "label": "키 없음", "connected": False, "message": "", "model": "", "note": "", "at": 0}
    with _STATUS_LOCK:
        status = dict(_STATUS)
    if status["key"] != _fingerprint(key) or status["ok"] is None:
        return {"state": "unchecked", "label": "연결 확인 전", "connected": False, "message": "", "model": "", "note": "", "at": 0}
    if status["ok"]:
        # 키는 맞지만 가장 최근 호출이 실패했으면(사용량 제한, 통신 오류 등) 그 사실을 함께 알린다
        return {"state": "ok", "label": "연결됨 (Connected)", "connected": True, "message": status["message"],
                "warning": bool(status["message"]), "model": status["model"], "note": status.get("note", ""),
                "at": status["at"]}
    return {"state": "error", "label": "연결 실패", "connected": False, "message": status["message"],
            "model": status["model"], "note": "", "at": status["at"]}


def extract_json(text: str) -> dict:
    """응답 문자열에서 JSON을 꺼낸다 (코드 블록으로 감싸져 있어도 처리)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ---------------------------------------------------------------- 모델 목록과 예비 모델
DETOUR_SECONDS = 15 * 60          # 기본 모델이 혼잡하면 이 시간 동안은 응답한 예비 모델을 먼저 부른다
MODELS_CACHE_SECONDS = 6 * 3600
_NOT_TEXT = ("image", "tts", "audio", "live", "embedding", "robotics", "computer-use", "aqa")
_MODELS_CACHE = {"key": "", "at": 0.0, "items": []}
_MODELS_LOCK = threading.Lock()


class _Busy(Exception):
    """이 모델이 지금 응답하지 못한다 (서버 혼잡 등). 다른 모델로 넘어가 볼 수 있는 실패."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _model_version(name: str) -> float:
    found = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(found.group(1)) if found else 0.0


def describe_model(name: str, display: str = "") -> dict:
    """모델 이름으로 종류를 가늠한다. stable: 미리보기(preview)나 실험(exp) 모델이 아닌 정식 모델."""
    lower = name.lower()
    kind = "lite" if "lite" in lower else "flash" if "flash" in lower else "pro" if "pro" in lower else "other"
    stable = not any(word in lower for word in ("preview", "exp", "latest"))
    return {"name": name, "label": display or name, "kind": kind, "stable": stable, "version": _model_version(lower)}


def rank_models(items: list[dict]) -> list[dict]:
    """예비 모델로 쓰기 좋은 순서: 정식 모델 먼저, Flash > Flash-Lite > Pro > 기타, 같은 종류 안에서는 최신 버전 먼저."""
    order = {"flash": 0, "lite": 1, "pro": 2, "other": 3}
    return sorted(items, key=lambda m: (0 if m["stable"] else 1, order.get(m["kind"], 3), -m["version"], m["name"]))


class GeminiClient:
    def __init__(self, model: str, timeout: float = 45.0, backup: str = "auto"):
        self.model = model
        self.backup = backup                      # 예비 모델: "auto"(프로그램이 고름) 또는 모델 이름
        self.timeout = timeout
        self._fallback_model: str | None = None   # 설정한 모델이 없어졌을 때(404) 대신 쓰는 모델
        self._fallback_for = ""                   # 그 대체가 어떤 설정 모델을 대신한 것인지
        self._detour: tuple[str, float] | None = None   # (혼잡할 때 응답한 예비 모델, 이 시각까지 먼저 부른다)
        self.last_model = ""                      # 마지막으로 응답한 모델

    @property
    def api_key(self) -> str:
        return get_secret("GEMINI_API_KEY")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _primary(self) -> str:
        """지금 기본으로 부를 모델. 설정에서 모델을 바꾸면 예전의 대체/우회 기록은 버린다."""
        if self._fallback_model and self._fallback_for != self.model:
            self._fallback_model, self._detour = None, None
        return self._fallback_model or self.model

    def _post(self, model: str, prompt: str, parts: list[dict] | None = None, timeout: float | None = None) -> requests.Response:
        """parts: 글 말고 함께 보낼 자료 (이미지: inline_data, 유튜브 영상: file_data)."""
        return requests.post(
            f"{API_ROOT}/models/{model}:generateContent",
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [*(parts or []), {"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}},
            timeout=timeout or self.timeout)

    def list_models(self, refresh: bool = False, timeout: float = 10.0) -> list[dict]:
        """이 키로 쓸 수 있는 글 생성 모델 목록 (예비 모델로 쓰기 좋은 순서). 실패하면 빈 목록."""
        key = self.api_key
        if not key:
            return []
        mark = _fingerprint(key)
        with _MODELS_LOCK:
            fresh = _MODELS_CACHE["key"] == mark and time.time() - _MODELS_CACHE["at"] < MODELS_CACHE_SECONDS
            if fresh and not refresh and _MODELS_CACHE["items"]:
                return list(_MODELS_CACHE["items"])
        try:
            response = requests.get(f"{API_ROOT}/models", headers={"x-goog-api-key": key},
                                    params={"pageSize": 200}, timeout=timeout)
            rows = response.json().get("models", []) if response.status_code == 200 else []
        except Exception:
            rows = []
        items = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or "generateContent" not in (row.get("supportedGenerationMethods") or []):
                continue
            name = str(row.get("name", "")).split("/")[-1]
            if not name.startswith("gemini") or any(word in name.lower() for word in _NOT_TEXT):
                continue
            items.append(describe_model(name, str(row.get("displayName") or "")))
        items = rank_models(items)
        if items:
            with _MODELS_LOCK:
                _MODELS_CACHE.update(key=mark, at=time.time(), items=list(items))
        return items

    def _find_flash_model(self) -> str | None:
        """설정한 모델 이름이 더 이상 없을 때, 사용 가능한 모델 중 가장 무난한 것을 찾아 대신 쓴다."""
        items = self.list_models(refresh=True, timeout=20.0)
        return items[0]["name"] if items else None

    def _backup_candidates(self, tried: list[str], limit: int = 2) -> list[str]:
        """기본 모델이 혼잡할 때 대신 불러 볼 모델 (최대 limit개). 사용자가 정한 예비 모델이 있으면 그것부터."""
        names = []
        wanted = (self.backup or "auto").strip()
        if wanted and wanted != "auto":
            names.append(wanted)
        names += [m["name"] for m in self.list_models()]
        out = []
        for name in names:
            if name not in tried and name not in out:
                out.append(name)
        return out[:limit]

    def generate_json(self, prompt: str, parts: list[dict] | None = None, timeout: float | None = None,
                      track: bool = True) -> dict:
        """track=False면 실패해도 '연결 상태'를 바꾸지 않는다 (자료 하나가 문제인 경우와 키 문제를 구분)."""
        key = self.api_key
        try:
            result, model = self._generate(prompt, parts, timeout)
        except GeminiError as exc:
            if track or exc.auth:
                _record(False, str(exc), auth=exc.auth, key=key)
            raise
        primary = self._fallback_model or self.model
        note = (f"기본 모델({primary})이 혼잡해 예비 모델({model})로 대신 호출했습니다. "
                f"기본 모델이 다시 응답하면 자동으로 돌아갑니다." if model != primary else "")
        self.last_model = model
        _record(True, model=model, key=key, note=note)
        return result

    def _call(self, model: str, prompt: str, parts: list[dict] | None, timeout: float | None,
              primary: bool) -> tuple[dict, str]:
        """모델 하나를 부른다. (결과, 실제로 답한 모델). 그 모델이 지금 응답하지 못하면 _Busy.

        이 호출은 매매 엔진 안에서 일어난다. 기다리는 동안 포지션 감시가 늦어지므로 오래 붙잡지 않는다:
        응답이 없으면(시간 초과) 바로 포기하고, 기본 모델의 일시적인 오류만 한 번 더 시도한다.
        """
        last_error, last_status = "", None
        tries = 3 if primary else 1
        for attempt in range(tries):
            try:
                response = self._post(model, prompt, parts, timeout)
            except requests.Timeout as exc:
                raise GeminiError(f"Gemini가 제때 응답하지 않았습니다 ({type(exc).__name__})") from exc
            except requests.RequestException as exc:
                last_error = f"Gemini 서버에 접속하지 못했습니다 ({type(exc).__name__})"
                if attempt >= 1 or not primary:
                    raise GeminiError(f"Gemini 호출 실패 ({last_error})") from exc
                time.sleep(1)
                continue
            last_status = response.status_code
            if response.status_code == 404:
                if primary and not self._fallback_model:
                    found = self._find_flash_model()
                    if found and found != model:
                        log.warning("Gemini 모델 '%s'를 찾을 수 없어 '%s'로 바꿔 호출합니다.", model, found)
                        self._fallback_model, self._fallback_for, model = found, self.model, found
                        continue
                if not primary:
                    raise _Busy(f"예비 모델 {model}을(를) 찾을 수 없습니다 (HTTP 404)", 404)
            if response.status_code == 429:      # 바로 다시 불러도 같은 답이 온다. 다음 판단 주기에 다시 시도된다.
                if not primary:
                    raise _Busy(f"예비 모델 {model}의 사용량 제한 (HTTP 429)", 429)
                raise GeminiError("Gemini 사용량 제한에 걸렸습니다 (HTTP 429). 다음 판단 주기에 다시 시도합니다. "
                                  "자주 나오면 설정의 판단 주기(분)를 늘려 주세요.", 429)
            if response.status_code in (500, 503):
                last_error = f"Gemini 서버 오류 (HTTP {response.status_code})"
                if attempt >= 1 or not primary:
                    raise _Busy(last_error, response.status_code)
                time.sleep(2)
                continue
            if response.status_code != 200:
                body = response.text[:300]
                auth = response.status_code in (401, 403) or (response.status_code == 400 and "API key" in body)
                hint = "API 키가 올바르지 않거나 사용할 수 없는 키입니다. " if auth else ""
                raise GeminiError(f"{hint}Gemini 오류 (HTTP {response.status_code}): {body}", response.status_code, auth)
            try:
                parts_out = response.json()["candidates"][0]["content"]["parts"]
                text = "".join(part.get("text", "") for part in parts_out if not part.get("thought"))
                return extract_json(text), model
            except (KeyError, IndexError, ValueError) as exc:
                last_error = f"응답 해석 실패: {exc}"
        raise GeminiError(f"Gemini 호출 실패 ({last_error})", last_status)

    def _generate(self, prompt: str, parts: list[dict] | None, timeout: float | None) -> tuple[dict, str]:
        """기본 모델을 부르고, 서버가 혼잡하면(HTTP 500/503) 예비 모델로 넘어간다."""
        if not self.available:
            raise GeminiError("Gemini API 키가 없습니다.")
        primary = self._primary()
        order = [primary]
        if self._detour and self._detour[1] > time.time() and self._detour[0] != primary:
            order = [self._detour[0], primary]       # 방금 혼잡했으니 응답했던 예비 모델부터 부른다
        tried: list[str] = []
        busy: _Busy | None = None
        extended = False
        while order:
            model = order.pop(0)
            tried.append(model)
            is_primary = model == primary
            try:
                result, answered = self._call(model, prompt, parts, timeout, is_primary)
            except _Busy as exc:
                busy = busy if (busy and not is_primary) else exc      # 안내에는 기본 모델의 오류를 남긴다
                if is_primary:
                    primary = self._fallback_model or self.model
                if not order and not extended:       # 처음 막혔을 때 한 번만 예비 모델을 찾아 붙인다
                    extended = True
                    order = self._backup_candidates(tried + [primary])
                continue
            if is_primary:
                if self._detour:
                    log.info("Gemini 기본 모델 '%s'가 다시 응답합니다.", answered)
                self._detour = None
            else:
                if not self._detour or self._detour[0] != answered:
                    log.warning("Gemini 기본 모델 '%s'가 혼잡해 예비 모델 '%s'로 대신 호출했습니다.", primary, answered)
                self._detour = (answered, time.time() + DETOUR_SECONDS)
            return result, answered
        others = len(tried) - 1
        status = busy.status if busy else None
        raise GeminiError(
            f"Gemini 호출 실패 ({busy}). Google 서버가 혼잡할 때 나오는 일시적인 오류이며, API 키나 설정 문제가 아닙니다. "
            + (f"예비 모델 {others}개도 응답하지 않았습니다. " if others > 0 else "예비로 부를 모델을 찾지 못했습니다. ")
            + "다음 판단 주기에 자동으로 다시 시도합니다.", status)

    def generate_youtube_json(self, prompt: str, url: str) -> dict:
        """공개 유튜브 영상을 Gemini가 직접 보고 답하게 한다.

        먼저 generateContent에 영상 주소를 file_data로 넘기고, 그 형식을 받지 않으면 interactions 방식으로 한 번 더 시도한다.
        """
        try:
            return self.generate_json(prompt, parts=[{"file_data": {"file_uri": url}}], timeout=240, track=False)
        except GeminiError as first:
            if first.auth or first.status not in (400, 404):
                raise
            try:
                return self._interactions_json(prompt, url)
            except GeminiError as second:
                raise GeminiError(f"{first} / 다른 방식으로 다시 시도: {second}", second.status) from second

    def _interactions_json(self, prompt: str, url: str) -> dict:
        model = self._primary()
        try:
            response = requests.post(
                f"{API_ROOT}/interactions",
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json={"model": model, "input": [{"type": "text", "text": prompt}, {"type": "video", "uri": url}]},
                timeout=240)
        except requests.RequestException as exc:
            raise GeminiError(f"Gemini 서버에 접속하지 못했습니다 ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise GeminiError(f"Gemini 오류 (HTTP {response.status_code}): {response.text[:300]}", response.status_code)

        def texts(node) -> list[str]:
            if isinstance(node, dict):
                found = [node["text"]] if node.get("type") == "text" and isinstance(node.get("text"), str) else []
                return found + [t for value in node.values() for t in texts(value)]
            if isinstance(node, list):
                return [t for item in node for t in texts(item)]
            return []

        try:
            data = response.json()
            return extract_json("".join(texts(data.get("outputs", data))))
        except ValueError as exc:
            raise GeminiError(f"응답 해석 실패: {exc}", 200) from exc

    def check(self) -> tuple[bool, str]:
        """키가 실제로 동작하는지 아주 짧은 요청으로 확인한다. (성공 여부, 안내 문구)"""
        try:
            self.generate_json('다음 JSON만 그대로 답해라: {"ok": true}', timeout=30)
        except GeminiError as exc:
            return False, str(exc)
        return True, f"{CONNECTED_TEXT} (모델 {self.last_model or self._fallback_model or self.model})"


# ---------------------------------------------------------------- 프롬프트 재료
def _compact_snapshot(snap: dict) -> dict:
    if not snap.get("ok"):
        return {"error": "데이터 없음"}
    keys = ("trend", "regime", "er", "slope_pct", "structure", "rsi", "atr_pct", "bb_pos", "chg_4", "chg_16", "vol_ratio",
            "macd_rising", "high_20", "low_20")
    out = {"price": snap["price"], "chg_24h_pct": snap.get("chg_24h"), "funding_pct": snap.get("funding_pct")}
    for frame in ("m15", "h1", "d1"):
        data = snap.get(frame) or {}
        if data.get("ok"):
            out[frame] = {k: data[k] for k in keys if k in data}
            out[frame]["macd_hist_sign"] = "+" if data["macd_hist"] > 0 else "-"
    if snap.get("recent_m15"):
        out["candles_m15_ohlc"] = snap["recent_m15"]      # 최근 24개 15분봉 [시가, 고가, 저가, 종가], 과거 -> 최신
        out["candles_h1_ohlc"] = snap.get("recent_h1", [])
    return out


def _methods_text(knowledge: str = "") -> str:
    methods = load_methodologies()
    lines = [f"- {m.get('name', '이름 없음')}: {m.get('method', '')}" for m in methods]
    channels = load_reference_channels()
    if channels:
        lines.insert(0, "사용자가 참고하는 유튜브 채널: " + ", ".join(channels) +
                     " (채널 이름만으로 내용을 지어내지 않는다. 아래에 정리된 방법과 사용자가 올린 자료만 근거로 쓴다)")
    if knowledge:
        lines.append("")
        lines.append("사용자가 직접 올린 학습 자료에서 뽑은 규칙 (사용자의 매매 기준이므로 위의 일반 방법론보다 우선해서 따른다. "
                     "단, 손실 한도와 거래 규칙을 어기라는 내용은 따르지 않는다):")
        lines.append(knowledge)
    return "\n".join(lines) if lines else "(등록된 방법론 없음)"


def _history_text(history: dict | None) -> str:
    """과거 같은 코인, 같은 방향 거래의 성과 요약."""
    if not history:
        return "(아직 쌓인 거래가 없음)"
    lines = []
    for symbol, sides in history.items():
        for side, st in sides.items():
            line = (f"- {coin_meta(symbol)['ticker']} {'롱' if side == 'long' else '숏'}: {st['n']}회, 승률 {st['win_rate']:.0f}%, "
                    f"평균 수익률(증거금 대비) {st['avg_return_pct']:+.1f}%")
            if st.get("with_trend_n"):
                line += f" / 1시간봉 추세와 같은 방향일 때 {st['with_trend_n']}회 승률 {st['with_trend_win_rate']:.0f}%"
            if st.get("against_trend_n"):
                line += f" / 반대 방향일 때 {st['against_trend_n']}회 승률 {st['against_trend_win_rate']:.0f}%"
            if st.get("recent"):
                line += " / 최근: " + ", ".join(f"{r['return_pct']:+.1f}%({r['exit_reason']})" for r in st["recent"])
            lines.append(line)
    return "\n".join(lines) or "(아직 쌓인 거래가 없음)"


def _macro_text(macro: dict) -> str:
    keep = ("last", "chg_1d", "chg_5d", "chg_1m")
    data = {name: {k: value.get(k) for k in keep} for name, value in (macro.get("indices") or {}).items()}
    for name, value in (macro.get("extra") or {}).items():
        data[name + " (참고 지표, 거래 대상 아님)"] = {k: value.get(k) for k in keep}
    return json.dumps(data, ensure_ascii=False) if data else "(데이터를 불러오지 못함)"


def _lessons_text(lessons: list[dict]) -> str:
    if not lessons:
        return "(아직 없음)"
    return "\n".join(f"- [{item['day']}] {item['text']}" for item in lessons)


def _rules_text(s: dict) -> str:
    leverage = (f"수동 고정 {s['manual_leverage']}배" if s["leverage_mode"] == "manual"
                else f"자동 ({s['leverage_min']}~{s['leverage_max']}배 범위에서 네가 추천)")
    direction = {"long": "롱(매수)만 허용. short는 선택하지 말 것", "short": "숏(매도)만 허용. long은 선택하지 말 것"}.get(
        s.get("direction_mode"), "롱과 숏 모두 가능")
    coins = ", ".join(coin_meta(symbol)["ticker"] for symbol in s.get("trade_symbols") or SYMBOLS)
    entry = {"single": "일괄 매수로 고정 (entry는 항상 single)",
             "split": f"분할 매수로 고정 ({s.get('split_count', 3)}회에 나눠 삼, entry는 항상 split)"}.get(
        s.get("entry_mode"), f"네가 선택. 한 번에 사는 single, 또는 {s.get('split_count', 3)}회에 나눠 사는 split")
    parts = s.get("exit_split_count", 3)
    selling = {"single": "일괄 매도로 고정 (exit는 항상 single, reduce는 쓰지 않는다)",
               "split": f"분할 매도로 고정 ({parts}회에 나눠 팖, exit는 항상 split)"}.get(
        s.get("exit_mode"), f"네가 선택. 익절가나 손절가에서 한 번에 정리하는 single, 또는 {parts}회에 나눠 파는 split")
    target, loss = daily_limit_text(s)
    if s.get("tpsl_mode") == "manual":
        tpsl = (f"사용자가 투자금(증거금) 기준으로 고정 ({pos_limit_text(s)}, 수수료를 뺀 순손익 기준). "
                f"프로그램이 가격으로 바꿔 건다. 너는 sl_pct, tp_pct를 0으로 둔다")
    else:
        tpsl = "네가 코인의 변동성(atr_pct)과 최근 고점/저점, 캔들 구조를 보고 거래마다 정한다"
    return (f"- 대상: {coins}의 USDT 무기한 선물 (사용자가 거래하기로 고른 코인. 다른 코인은 새로 진입하지 않는다)\n"
            f"- 매수 방식: {entry}\n"
            f"- 매도 방식: {selling}\n"
            f"- 진입 방향: {direction}\n"
            f"- 레버리지: {leverage}\n"
            f"- 포지션 익절/손절: {tpsl}\n"
            f"- 한 포지션 최대 보유 시간 {s.get('max_hold_minutes', 240)}분 (넘으면 프로그램이 시장가로 정리한다. 이 시간 안에 닿을 만한 익절 폭을 잡는다)\n"
            f"- 하루 목표 수익 {target} (오늘 원금 기준. 목표일 뿐이며, 무리한 진입의 근거가 되어서는 안 된다)\n"
            f"- 하루 손실 한도 {loss} (오늘 원금 기준. 도달하면 프로그램이 그날 매매를 중지한다)\n"
            f"- 왕복 수수료 약 {s['taker_fee_pct'] * 2:.3f}% (거래 금액 기준, 레버리지를 쓰면 증거금 대비 부담이 배수로 커진다)\n"
            f"- 하루 거래 횟수 {s['min_trades_per_day']}~{s['max_trades_per_day']}회")


SYSTEM = ("너는 암호화폐 선물 단기 매매를 돕는 신중한 분석가다. 주어진 수치만 근거로 판단하고, "
          "근거가 약하면 관망(hold)을 택한다. 선물은 오를 때(롱)와 내릴 때(숏) 모두 수익을 낼 수 있으므로 "
          "두 방향을 같은 기준으로 평가하고, 어느 한쪽을 기본값으로 두지 않는다. "
          "모든 설명은 한국어로, 초보자도 이해할 수 있게 쓴다. 반드시 지정한 JSON 형식으로만 답한다.\n\n")

INDICATOR_GUIDE = ("m15=15분봉, h1=1시간봉, d1=일봉 / trend: 이동평균 배열 (up 정배열, down 역배열, side 혼조) / "
                   "regime: 장세 (up 상승 추세, down 하락 추세, range 박스권) / er: 추세 효율 0~1 (낮을수록 박스권) / "
                   "slope_pct: 추세선 기울기(봉당 %) / structure: 고점과 저점의 구조 (up 함께 높아짐, down 함께 낮아짐) / "
                   "bb_pos: 0=볼린저 하단, 1=상단")


class GeminiAnalyzer:
    def __init__(self, settings):
        self.settings = settings
        self.client = GeminiClient(settings.get("gemini_model"), backup=self._backup_setting())
        self.last_error = ""
        self.knowledge = None            # 사용자가 올린 학습 자료를 글로 돌려주는 함수 (엔진이 연결한다)

    def _methods(self) -> str:
        text = ""
        if callable(self.knowledge):
            try:
                text = self.knowledge() or ""
            except Exception as exc:     # 자료를 못 읽어도 판단은 계속한다
                log.warning("학습 자료를 읽지 못했습니다: %s", exc)
        return _methods_text(text)

    def _backup_setting(self) -> str:
        try:
            return str(self.settings.get("gemini_backup_model") or "auto")
        except KeyError:
            return "auto"

    def _sync(self) -> None:
        """설정 화면에서 바꾼 모델을 호출 직전에 반영한다."""
        self.client.model = self.settings.get("gemini_model")
        self.client.backup = self._backup_setting()

    def check(self) -> tuple[bool, str]:
        self._sync()
        return self.client.check()

    def list_models(self, refresh: bool = False) -> list[dict]:
        self._sync()
        return self.client.list_models(refresh=refresh)

    @property
    def available(self) -> bool:
        self._sync()
        return self.client.available

    # ------------------------------------------------------ 1) 오늘의 계획
    def daily_plan(self, macro: dict, snapshots: dict, lessons: list[dict], last_review: dict | None) -> dict:
        s = self.settings.all()
        coin_keys = ", ".join(f'"{symbol}": {{"bias": "long | short | neutral", "note": "한 문장"}}' for symbol in snapshots)
        prompt = SYSTEM + f"""[오늘의 매매 계획 수립]

거래 규칙:
{_rules_text(s)}

미국 증시와 금 (last=현재 값, chg_1d=전일 대비 %, chg_5d=5일, chg_1m=1개월):
{_macro_text(macro)}

코인 지표 ({INDICATOR_GUIDE}):
{json.dumps({k: _compact_snapshot(v) for k, v in snapshots.items()}, ensure_ascii=False)}

참고할 차트 분석 방법론:
{self._methods()}

지금까지 쌓인 학습 노트:
{_lessons_text(lessons)}

어제 회고의 다음 날 계획: {json.dumps((last_review or {}).get('next_day_plan', ''), ensure_ascii=False)}

아래 JSON으로 답해라.
{{
  "market_bias": "bullish | bearish | neutral (지표가 하락을 가리키면 bearish라고 분명히 적는다)",
  "us_market_summary": "미국 증시와 금 시세가 오늘 코인 시장에 주는 영향 (2~3문장)",
  "crypto_summary": "위 코인들의 현재 흐름 (2~3문장)",
  "recommended_leverage": 1에서 50 사이 정수 (변동성이 크거나 방향이 불확실하면 낮게),
  "risk_level": "low | medium | high",
  "coins": {{{coin_keys}}},
  "strategy": "오늘 적용할 매매 방침 (3~4문장). 상승 추세 코인, 하락 추세 코인, 박스권 코인을 각각 어떻게 다룰지 포함",
  "cautions": ["주의할 점", "..."]
}}"""
        plan = self.client.generate_json(prompt)
        plan["source"] = "gemini"
        try:
            wanted = float(plan.get("recommended_leverage") or 0)
            # 값이 없으면 '추천 없음'으로 둔다 (최소 배율로 바꿔 버리면 1배를 추천한 것처럼 보인다)
            plan["recommended_leverage"] = int(clamp(wanted, s["leverage_min"], s["leverage_max"])) if wanted >= 1 else None
        except (TypeError, ValueError, OverflowError):
            plan["recommended_leverage"] = None
        if not isinstance(plan.get("coins"), dict):
            plan["coins"] = {}
        if not isinstance(plan.get("cautions"), list):
            plan["cautions"] = []
        return plan

    # ------------------------------------------------------ 2) 진입 판단
    def decide(self, macro: dict, snapshots: dict, plan: dict | None, lessons: list[dict],
               open_trades: list[dict], state: dict, history: dict | None = None,
               rule_views: dict | None = None, balance: dict | None = None) -> list[dict]:
        """rule_views: 프로그램의 규칙 점수 {심볼: {long, short, regime}} (상호 검증용)
        balance: 최근 24시간 진입 방향 {long: n, short: m}"""
        s = self.settings.all()
        positions = []
        for t in open_trades:
            context = trade_context(t)
            split = context.get("split") or {}
            selling = context.get("exit") or {}
            if s.get("exit_mode") == "single":
                sell_total = 1
            elif int(selling.get("total") or 0) > 1:
                sell_total = int(selling["total"])
            else:      # 계획이 없는 포지션: 분할 매도 고정이면 설정 횟수, 자동이면 reduce를 고를 때 설정 횟수로 나눈다
                sell_total = int(s.get("exit_split_count", 3))
            positions.append({"symbol": t["symbol"], "side": t["side"], "avg_entry_price": t["entry_price"],
                              "leverage": t["leverage"], "sl": t["sl_price"], "tp": t["tp_price"],
                              "split_bought": split.get("done", 1), "split_total": split.get("total", 1),
                              "exit_sold": min(int(selling.get("done") or 0), sell_total) if sell_total > 1 else 0,
                              "exit_total": sell_total})
        views = {symbol: {k: view[k] for k in ("long", "short", "regime")}
                 for symbol, view in (rule_views or {}).items() if view}
        balance = balance or {"long": 0, "short": 0}
        manual = s.get("tpsl_mode") == "manual"
        if manual:
            stops = "- 익절/손절은 사용자가 투자금 기준으로 고정해 두었다. sl_pct와 tp_pct는 0으로 둔다."
        else:
            stops = ("- sl_pct, tp_pct는 진입 가격 대비 가격 변동 폭(%)이다 (레버리지 적용 전). 고정값을 쓰지 말고 거래마다 정한다.\n"
                     "  손절: 15분봉 atr_pct의 1~2.5배 범위에서, 롱이면 최근 저점(low_20) 바로 아래, 숏이면 최근 고점(high_20) 바로 위에 놓이게 한다.\n"
                     "  익절: 다음 저항/지지(high_20, low_20, 볼린저 반대편)까지의 거리를 넘지 않게 하되 손절 폭의 1.5배 이상, 왕복 수수료의 3배 이상으로 잡는다. "
                     "박스권에서는 박스 중간까지만 짧게 잡는다.\n"
                     "  변동성(atr_pct)이 평소보다 크면 폭을 넓히고 레버리지를 낮춘다. 손절 폭 x 레버리지가 60%를 넘지 않게 한다.")
        cross = ("- 프로그램은 네 판단과 규칙 점수를 서로 맞춰 본다(상호 검증). 규칙 점수가 반대 방향을 뚜렷하게 가리키는 진입은 실행되지 않는다. "
                 "규칙 점수와 다르게 판단했다면 숫자로 확인되는 근거를 reason에 적는다.\n" if s.get("cross_check") else "")
        prompt = SYSTEM + f"""[지금 진입/청산 판단]

거래 규칙:
{_rules_text(s)}

오늘의 계획: {json.dumps({k: (plan or {}).get(k) for k in ('market_bias', 'strategy', 'coins', 'recommended_leverage')}, ensure_ascii=False)}

오늘 현재 상태: 수익률 {state['pnl_pct']:+.2f}%, 완료한 거래 {state['trades_done']}회, 남은 손실 한도 {state['loss_room']:.2f}%p

미국 증시와 금: {_macro_text(macro)}

코인 지표 ({INDICATOR_GUIDE}):
{json.dumps({k: _compact_snapshot(v) for k, v in snapshots.items()}, ensure_ascii=False)}

프로그램의 규칙 점수 (RSI, MACD, 이동평균, 추세선, 고점/저점 구조로 롱과 숏을 같은 기준으로 채점한 값, 0~1):
{json.dumps(views, ensure_ascii=False)}

최근 24시간 진입 방향: 롱 {balance.get('long', 0)}회, 숏 {balance.get('short', 0)}회

보유 중인 포지션: {json.dumps(positions, ensure_ascii=False)}

참고할 차트 분석 방법론:
{self._methods()}

학습 노트:
{_lessons_text(lessons)}

이 프로그램의 과거 거래 성과 (같은 코인, 같은 방향):
{_history_text(history)}
과거 성과가 뚜렷하게 나쁜 조합은 근거가 더 강할 때만 진입한다.

candles_m15_ohlc, candles_h1_ohlc는 최근 캔들의 [시가, 고가, 저가, 종가]다. 고점/저점 갱신 구조, 직전 고점/저점을 살짝 넘겼다 되돌아오는 움직임, 긴 꼬리 같은 가격 행동은 이 숫자에서 직접 확인하고, 확인되지 않는 패턴은 근거로 쓰지 않는다.

방향을 정하는 원칙:
- 코인마다 먼저 롱의 근거와 숏의 근거를 따로 평가해 p_long, p_short를 적는다. 각각 '그 방향으로 지금 진입하면 손절보다 익절에 먼저 닿을 가능성'에 대한 네 추정(0~1)이다. 두 값의 합이 1일 필요는 없고, 근거가 약하면 둘 다 낮게 적는다.
- 하락 추세(regime이 down, 이동평균 역배열, 고점과 저점이 함께 낮아짐, MACD가 0 아래에서 확대)에서는 반등이 약해지는 자리에서 short를 우선 검토한다. '많이 떨어졌으니 오를 것'이라는 이유만으로 long을 고르지 않는다.
- 박스권(regime이 range)에서는 박스 상단(bb_pos 0.85 이상이고 RSI 60 이상)에서 short, 박스 하단(bb_pos 0.15 이하이고 RSI 40 이하)에서 long만 검토한다. 박스 중간에서는 hold.
- 상승 추세에서는 눌렸다가 다시 오르는 자리에서 long을 우선 검토한다. '많이 올랐으니 내릴 것'이라는 이유만으로 short를 고르지 않는다.
- 진입은 고른 방향의 확률이 반대 방향보다 0.15 이상 높고, 근거가 둘 이상의 시간대나 지표(RSI, MACD, 이동평균, 추세선, 캔들 구조)에서 겹칠 때만 한다. 그렇지 않으면 hold. 거래 횟수를 채우려고 진입하지 않는다.
- 최근 진입이 한 방향으로 쏠려 있으면, 지금 장세가 정말 그 방향인지 지표로 다시 확인한다. 장세가 뒷받침하지 않으면 그 방향의 확률을 낮춘다.
{cross}
위 '코인 지표'에 있는 코인 각각에 대해 판단해라.
- 포지션이 없는 코인: long(매수 진입), short(매도 진입), hold(관망) 중 하나
- 포지션이 있는 코인: hold(유지), close(지금 전량 청산), reduce(분할 매도의 다음 회차를 지금 팔아 일부만 정리), add(분할 매수의 다음 회차를 지금 삼) 중 하나
- reduce는 exit_total이 2 이상이고 exit_sold가 exit_total - 1보다 작은 포지션에만 쓸 수 있다. 보유 방향의 힘이 약해졌지만(거래량 감소, MACD 히스토그램 축소, 저항/지지 근처) 추세가 꺾였다고 보기는 이를 때 수익 일부를 먼저 확정하는 용도다. 보유 방향의 근거가 무너졌으면 reduce가 아니라 close를 고른다. 가격이 익절가 쪽으로 구간을 지날 때마다 프로그램이 한 회차씩 자동으로 팔기 때문에, 그때까지 기다리는 편이 낫다고 보면 hold를 고른다.
- exit_sold가 1 이상인 포지션(이미 나눠 팔기 시작한 포지션)에는 add를 쓰지 않는다.
- add는 split_bought가 split_total보다 작은 포지션에만 쓸 수 있다. 보유 방향의 근거가 여전히 유효하고 지금 가격이 추가로 사기에 나쁘지 않을 때만 고른다. 손실을 만회하려고 근거 없이 물타기하지 않는다.
- 새로 진입할 때는 entry도 정한다. single은 한 번에 전량, split은 나눠서 산다. 방향은 맞아 보이지만 진입 시점이 불확실하거나 변동성이 크면 split, 근거가 여러 시간대에서 일치하고 지금이 좋은 자리면 single이 맞다.
- 새로 진입할 때는 exit(매도 방식)도 정한다. single은 익절가나 손절가에서 전량을 한 번에 정리한다. split은 익절가까지 가는 길을 여러 구간으로 나눠 구간마다 일부를 팔아 수익을 차례로 확정한다. 추세가 강하고 익절가까지 막힘이 없어 보이면 single, 익절가가 멀거나 변동성이 커서 중간에 되돌릴 걱정이 있으면 split이 맞다. 박스권의 짧은 목표는 single이 맞다.
{stops}
- confidence는 0~1이고, 고른 방향의 확률(p_long 또는 p_short)과 같게 적는다. 근거가 여러 시간대에서 일치할 때만 0.7 이상을 준다.
- reason에는 어떤 지표의 어떤 값 때문에 그 방향인지, 반대 방향은 왜 아닌지를 적는다.

JSON 형식:
{{"decisions": [{{"symbol": "BTCUSDT", "p_long": 0.0, "p_short": 0.0, "action": "long | short | hold | close | reduce | add", "entry": "single | split", "exit": "single | split", "confidence": 0.0, "leverage": 정수, "sl_pct": 0.0, "tp_pct": 0.0, "reason": "2~3문장"}}, ...]}}"""
        raw = self.client.generate_json(prompt)

        def number(value) -> float | None:
            """0~1 사이의 확률. 72처럼 퍼센트로 온 값은 0.72로 읽고, 숫자가 아니면 None."""
            try:
                x = float(value)
            except (TypeError, ValueError):
                return None
            if x != x or x in (float("inf"), float("-inf")):
                return None
            if 1 < x <= 100:
                x /= 100
            return round(clamp(x, 0, 1), 2)

        decisions = []
        items = raw.get("decisions", []) if isinstance(raw, dict) else []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            action = str(item.get("action", "hold")).lower()
            if symbol not in SYMBOLS or action not in ("long", "short", "hold", "close", "add", "reduce"):
                continue
            entry = str(item.get("entry") or "").lower()
            way_out = str(item.get("exit") or "").lower()
            p_long, p_short = number(item.get("p_long")), number(item.get("p_short"))
            confidence = number(item.get("confidence")) or 0.0
            p_side = {"long": p_long, "short": p_short}.get(action)
            if p_side is not None:
                confidence = min(confidence, p_side)       # 확신도는 고른 방향의 확률보다 높을 수 없다
            try:
                decisions.append({
                    "symbol": symbol, "action": action, "source": "gemini",
                    "entry": entry if entry in ("single", "split") else None,
                    "exit": way_out if way_out in ("single", "split") else None,
                    "confidence": confidence,
                    "p_long": p_long, "p_short": p_short,
                    "leverage": float(item.get("leverage") or 0) or None,
                    "sl_pct": None if manual else (float(item.get("sl_pct") or 0) or None),
                    "tp_pct": None if manual else (float(item.get("tp_pct") or 0) or None),
                    "reason": str(item.get("reason", "")).strip() or "설명 없음",
                })
            except (TypeError, ValueError):
                continue
        if not decisions:
            raise GeminiError("판단 결과가 비어 있습니다.")
        return decisions

    # ------------------------------------------------------ 학습 자료 읽기
    def digest_material(self, kind: str, title: str, text: str = "", url: str = "",
                        image_b64: str = "", mime: str = "") -> dict:
        """사용자가 올린 자료(전략 글, 유튜브 영상, 차트 이미지)에서 매매 규칙을 뽑는다.

        결과는 지식 베이스에 저장되어 이후 모든 계획/판단 프롬프트에 함께 들어간다.
        """
        self._sync()
        what = {"text": "아래 글", "youtube": "함께 준 유튜브 영상", "image": "함께 준 차트 이미지"}.get(kind, "자료")
        note = f"\n사용자가 적은 메모:\n{text[:6000]}\n" if text and kind != "text" else ""
        body = f"\n자료 본문:\n{text[:12000]}\n" if kind == "text" else ""
        prompt = SYSTEM + f"""[학습 자료 정리]

사용자가 자신의 매매 기준으로 삼으려고 자료를 올렸다. {what}에서 암호화폐 선물 단기 매매에 바로 쓸 수 있는 규칙을 뽑아라.
- 자료에 실제로 있는 내용만 쓴다. 없는 내용을 지어내거나 일반론으로 채우지 않는다.
- 규칙은 '어떤 조건이면 어떻게 한다'가 드러나게, 한 문장씩 쓴다. 가능하면 숫자(지표 값, 시간대, 비율)를 그대로 옮긴다.
- 차트 이미지라면 보이는 것(추세, 지지/저항 가격대, 패턴, 표시된 지표 값, 시간대)을 읽고, 그 차트가 보여 주는 진입/청산 기준을 정리한다. 글자가 안 보이는 부분은 추측하지 않는다.
- 롱(매수) 조건과 숏(매도) 조건을 구분해서 적는다. 자료가 한쪽만 다루면 다른 쪽은 빈 목록으로 둔다.
- 매매와 관련 없는 자료이거나 내용을 확인할 수 없으면 rules를 빈 목록으로 두고 summary에 그 사실을 적는다.

자료 제목: {title}
{note}{body}
JSON 형식:
{{"summary": "이 자료가 말하는 매매 방식 2~3문장", "rules": ["규칙 (최대 8개)"], "long_conditions": ["롱 진입 조건 (최대 4개)"], "short_conditions": ["숏 진입 조건 (최대 4개)"], "cautions": ["주의할 점 (최대 3개)"]}}"""
        if kind == "youtube":
            raw = self.client.generate_youtube_json(prompt, url)
        elif kind == "image":
            raw = self.client.generate_json(prompt, parts=[{"inline_data": {"mime_type": mime, "data": image_b64}}],
                                            timeout=120, track=False)
        else:
            raw = self.client.generate_json(prompt, track=False)
        result = {"summary": str(raw.get("summary", "")).strip()}
        for key, limit in (("rules", 8), ("long_conditions", 4), ("short_conditions", 4), ("cautions", 3)):
            items = raw.get(key)
            result[key] = [str(x).strip() for x in items if str(x).strip()][:limit] if isinstance(items, list) else []
        return result

    # ------------------------------------------------------ 3) 하루 회고
    def review(self, day: str, trades: list[dict], pnl_pct: float, plan: dict | None, lessons: list[dict]) -> dict:
        s = self.settings.all()
        rows = [{"symbol": t["symbol"], "side": t["side"], "leverage": t["leverage"],
                 "return_pct": round(t["return_pct"] or 0, 2), "net_pnl": round(t["net_pnl"] or 0, 4),
                 "fee": round(t["fee"] or 0, 4), "exit_reason": t["exit_reason"],
                 "entry_reason": (t["entry_reason"] or "")[:160]} for t in trades]
        prompt = SYSTEM + f"""[하루 매매 회고]

날짜: {day}
하루 수익률: {pnl_pct:+.2f}% (목표 {daily_limit_text(s)[0]}, 손실 한도 {daily_limit_text(s)[1]}, 모두 오늘 원금 기준)
그날의 계획: {json.dumps({k: (plan or {}).get(k) for k in ('market_bias', 'strategy')}, ensure_ascii=False)}
거래 내역 (return_pct는 증거금 대비): {json.dumps(rows, ensure_ascii=False)}
기존 학습 노트:
{_lessons_text(lessons)}

무엇이 통했고 무엇이 틀렸는지 구체적으로 돌아보고, 다음 거래에 바로 적용할 수 있는 교훈을 남겨라.
롱과 숏을 나눠서 본다. 한 방향으로만 진입했다면 그날 장세가 정말 그 방향이었는지, 반대 방향의 기회를 놓쳤는지도 평가한다.
교훈은 기존 노트와 겹치지 않게, 한 문장씩, 최대 4개.

JSON 형식:
{{"summary": "하루 요약 2~3문장", "what_worked": ["..."], "what_failed": ["..."], "lessons": ["..."], "next_day_plan": "다음 날 방침 2~3문장"}}"""
        review = self.client.generate_json(prompt)
        review["source"] = "gemini"
        for key in ("what_worked", "what_failed", "lessons"):
            if not isinstance(review.get(key), list):
                review[key] = []
        review["lessons"] = [str(x) for x in review["lessons"]][:4]
        return review

    # ------------------------------------------------------ 4) 일일 보고서 (메일용 복기)
    def daily_report(self, day: str, trades: list[dict], pnl_pct: float, plan: dict | None, lessons: list[dict]) -> dict:
        s = self.settings.all()
        rows = []
        for t in trades:
            try:
                context = json.loads(t.get("context") or "{}")
            except (TypeError, ValueError):
                context = {}
            rows.append({"symbol": t["symbol"], "side": t["side"], "leverage": t["leverage"],
                         "entry_price": t["entry_price"], "exit_price": t["exit_price"],
                         "sl_price": t["sl_price"], "tp_price": t["tp_price"],
                         "return_pct": round(t["return_pct"] or 0, 2), "net_pnl": round(t["net_pnl"] or 0, 4),
                         "fee": round(t["fee"] or 0, 4), "exit_reason": t["exit_reason"],
                         "entry_reason": (t["entry_reason"] or "")[:200], "market_at_entry": context})
        prompt = SYSTEM + f"""[일일 매매 보고서 작성]

너는 사용자의 트레이딩 비서다. 오늘의 매매를 복기해서 보고한다. 듣기 좋은 말보다 정확한 평가를 한다.
숫자는 아래 자료에 있는 것만 쓰고, 없는 사실을 만들지 않는다.

날짜: {day}
하루 수익률: {pnl_pct:+.2f}% (목표 {daily_limit_text(s)[0]}, 손실 한도 {daily_limit_text(s)[1]}, 모두 오늘 원금 기준)
오늘의 계획: {json.dumps({k: (plan or {}).get(k) for k in ('market_bias', 'strategy')}, ensure_ascii=False)}
거래 내역 (return_pct는 증거금 대비, market_at_entry는 진입 당시 지표): {json.dumps(rows, ensure_ascii=False)}
참고할 차트 분석 방법론:
{self._methods()}
학습 노트:
{_lessons_text(lessons)}

JSON 형식:
{{"summary": "오늘 매매를 2~3문장으로 요약", "good": ["잘한 판단 (어느 거래가 왜 좋았는지)"], "mistakes": ["실수 (어느 거래에서 무엇이 틀렸는지)"], "improvements": ["다음에 바꿀 점"], "tomorrow": "내일 방침 2~3문장"}}
각 목록은 최대 4개, 한 항목은 한두 문장."""
        report = self.client.generate_json(prompt)
        for key in ("good", "mistakes", "improvements"):
            if not isinstance(report.get(key), list):
                report[key] = []
            report[key] = [str(x) for x in report[key]][:4]
        return report

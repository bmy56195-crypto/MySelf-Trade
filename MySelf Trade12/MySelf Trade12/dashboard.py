"""Myself Trade - 대시보드 (웹 화면 + 화면이 쓰는 API).

내 컴퓨터(127.0.0.1)에서만 접속할 수 있다. 화면 구성은 templates/index.html.
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request, send_file

import config
from bybit_api import BybitClient, BybitError
from config import (APP_NAME, BASE_SYMBOLS, HOLD_MAX, HOLD_MIN, MAX_CUSTOM_SYMBOLS, MODES, SYMBOLS, TIMEFRAMES, TIMEZONES,
                    SettingsError, coin_meta, normalize_symbol)
from gemini_analyzer import CONNECTED_TEXT, GeminiAnalyzer, gemini_status
from knowledge import (MaterialError, add_material, material_view, process_async, process_material, remove_material,
                       used_ids)
from mailer import MailError, deliver_mail, mail_ready, record_status, summarize_results
from market_data import chart_candles
from reports import PERIODS, build_pnl_board, build_report, export_xlsx, trade_view
from trade_engine import StopsError      # OrderError(직접 주문)도 이 종류에 포함된다
from utils import fmt_local, iso, local_day, log, parse_iso, utcnow

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
TF_NAMES = {value: name for value, name, _ in TIMEFRAMES}
MAIL_KEYS = ("mail_enabled", "mail_method", "mail_from", "smtp_host", "smtp_port", "mail_hour")


def _clean(value):
    """JSON으로 보낼 수 없는 숫자(NaN, 무한대)를 None으로 바꾼다."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def create_app(engine, settings, storage) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.json.ensure_ascii = False
    price_cache: dict[tuple, tuple[float, dict]] = {}
    quote_cache: dict[tuple, tuple[float, dict]] = {}       # 시세 줄: (모드, 코인) -> (받은 시각, 24시간 시세)
    spark_cache: dict[tuple, tuple[float, list]] = {}       # 시세 줄의 작은 그래프: 최근 24시간의 1시간봉 종가

    def ok(**data):
        return jsonify(_clean({"ok": True, **data}))

    def fail(message: str, status: int = 400):
        return jsonify({"ok": False, "error": message}), status

    def ai():
        """Gemini 분석기. 엔진이 쓰는 것을 그대로 쓴다 (점검용 가짜 엔진이면 새로 만든다)."""
        analyzer = engine.analyzer
        return analyzer if hasattr(analyzer, "digest_material") else GeminiAnalyzer(settings)

    def symbols_view() -> list[dict]:
        return [{"symbol": k, **v, "custom": bool(v.get("custom"))} for k, v in list(SYMBOLS.items())]

    def gemini_view() -> dict:
        """Gemini 연결 상태 (마지막 확인 시각은 선택한 시간대로)."""
        status = gemini_status()
        if status.get("at"):
            status["at"] = fmt_local(datetime.fromtimestamp(status["at"], tz=timezone.utc), settings.get("timezone"), "%m-%d %H:%M")
        return status

    def macro_cache() -> dict | None:
        macro = engine.macro
        return macro.peek() if hasattr(macro, "peek") else getattr(macro, "_cache", None)

    @app.before_request
    def guard():
        # 다른 웹사이트가 이 프로그램에 몰래 요청을 보내지 못하게 막는다
        host = (request.host or "").split(":")[0]
        if host not in ALLOWED_HOSTS:
            return fail("허용되지 않은 주소입니다.", 403)
        if request.method == "POST":
            if not request.is_json:
                return fail("잘못된 요청 형식입니다.", 415)
            if not isinstance(request.get_json(silent=True), dict):
                return fail("잘못된 요청 내용입니다.", 400)
        return None

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------ 화면
    @app.get("/")
    def index():
        boot = {
            "app": APP_NAME,
            "version": config.VERSION,
            "symbols": symbols_view(),
            "max_custom": MAX_CUSTOM_SYMBOLS,
            "timezones": [{"label": k, "value": v} for k, v in TIMEZONES.items()],
            "modes": [{"value": k, "label": v} for k, v in MODES.items()],
            "periods": [{"value": k, "label": v[0]} for k, v in PERIODS.items()],
            "timeframes": [{"value": v, "label": n, "group": g} for v, n, g in TIMEFRAMES],
            "hold": {"min": HOLD_MIN, "max": HOLD_MAX},
            "connected_text": CONNECTED_TEXT,
        }
        return render_template("index.html", boot=boot, app_name=APP_NAME)

    # ------------------------------------------------------------ 상태
    @app.get("/api/state")
    def api_state():
        state = engine.get_state()
        tz = state["timezone"]
        now = utcnow()
        state["now"] = fmt_local(now, tz, "%Y-%m-%d %H:%M:%S")
        state["tz_label"] = next((k for k, v in TIMEZONES.items() if v == tz), tz)
        state["mode_label"] = MODES.get(state["mode"], state["mode"])
        # 다음 판단까지 남은 시간(초). 화면의 운용 상태 칸이 1초마다 줄여 가며 보여 준다.
        state["next_decision_in"] = (max(0, int((parse_iso(state["next_decision"]) - now).total_seconds()))
                                     if state.get("next_decision") else None)
        for key in ("last_step", "next_decision", "last_decision"):
            if state.get(key):
                state[key] = fmt_local(state[key], tz, "%H:%M:%S")
        state["gemini"] = gemini_view()
        # 엔진이 가진 원본을 건드리지 않도록 복사본을 만들어 화면용 값으로 바꾼다
        state["positions"] = [{**position, "ticker": coin_meta(position["symbol"])["ticker"],
                               "held_minutes": max(0, int((now - parse_iso(position["entry_time"])).total_seconds() // 60)),
                               "entry_time": fmt_local(position["entry_time"], tz, "%m-%d %H:%M")}
                              for position in state.get("positions", [])]
        return ok(state=state)

    @app.get("/api/price")
    def api_price():
        """차트용 캔들.

        tf: 1,3,5,15,30(분) 60,180,360,720(시간) D,W,M,Y
        end: 이 시각(밀리초) 이전의 캔들을 달라는 뜻. 화면을 과거로 옮길 때 이어서 불러온다.
        count: 캔들 개수 (최대 1000)
        """
        symbol = request.args.get("symbol", "BTCUSDT")
        tf = request.args.get("tf", "15")
        end = request.args.get("end", type=int)
        count = max(3, min(1000, request.args.get("count", 300, type=int) or 300))
        if symbol not in SYMBOLS:
            return fail("지원하지 않는 코인입니다.")
        if tf not in TF_NAMES:
            return fail("지원하지 않는 타임프레임입니다.")
        mode, tz = settings.get("mode"), settings.get("timezone")
        key = (symbol, tf, tz, end, count)
        cached = price_cache.get(key)
        fresh_for = 120 if end else (4 if tf in ("1", "3", "5") else 12)     # 지나간 구간은 바뀌지 않는다
        if cached and time.time() - cached[0] < fresh_for:
            return ok(**cached[1])
        try:
            candles, has_more = chart_candles(engine.market_for(mode), symbol, tf, count, end)
        except Exception as exc:
            log.warning("차트 시세 조회 실패: %s", exc)
            return fail("시세를 불러오지 못했습니다. 인터넷 연결과, 지금 있는 지역에서 Bybit 접속이 되는지 확인해 주세요.", 502)
        daily = tf in ("D", "W", "M", "Y")
        pattern = {"D": "%Y-%m-%d", "W": "%Y-%m-%d", "M": "%Y-%m", "Y": "%Y"}.get(tf, "%Y-%m-%d %H:%M")
        zone = "UTC" if daily else tz         # 일봉 이상은 Bybit 기준(UTC 0시)에 시작하므로 날짜도 UTC로 표시한다
        rows = [{"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v") or 0,
                 "label": fmt_local(datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc), zone, pattern)} for c in candles]
        first, last = (candles[0]["c"], candles[-1]["c"]) if candles else (0, 0)
        data = {"symbol": symbol, "tf": tf, "tf_name": TF_NAMES[tf], "candles": rows, "price": last, "has_more": has_more,
                "change_pct": (last / first - 1) * 100 if first else 0}
        if len(price_cache) > 400:
            price_cache.clear()
        price_cache[key] = (time.time(), data)
        return ok(**data)

    @app.get("/api/ticker")
    def api_ticker():
        """지금 가격 (실시간 피드 값). 대시보드이 1초마다 부른다."""
        symbol = request.args.get("symbol", "BTCUSDT")
        if symbol not in SYMBOLS:
            return fail("지원하지 않는 코인입니다.")
        market = engine.market_for(settings.get("mode"))
        feed = getattr(market, "feed", None)
        try:
            value = feed.get(symbol) if feed is not None else market.get_price(symbol)
        except Exception:
            value = None
        return ok(symbol=symbol, price=value, feed=engine.feed_status())

    @app.get("/api/tickers")
    def api_tickers():
        """대시보드 위쪽 시세 줄: 목록에 있는 모든 코인의 지금 가격과 24시간 변동.

        24시간 시세(변동률, 고가, 저가, 거래 대금, 펀딩비)는 10초, 작은 그래프는 5분 동안 저장해 둔 값을 쓴다.
        한 코인을 받지 못해도 나머지는 보여 준다.
        """
        mode = settings.get("mode")
        market = engine.market_for(mode)
        feed = getattr(market, "feed", None)
        now = time.time()

        def one(symbol: str) -> dict:
            meta = SYMBOLS.get(symbol) or {}
            row = {"symbol": symbol, "ticker": meta.get("ticker", symbol), "name": meta.get("name", symbol), "short": meta.get("short", symbol),
                   "price": None, "change_24h_pct": None, "high_24h": None, "low_24h": None, "turnover_24h": None,
                   "funding_rate_pct": None, "spark": []}
            cached = quote_cache.get((mode, symbol))
            if not cached or now - cached[0] >= 10:
                try:
                    cached = (now, market.get_ticker(symbol))
                    quote_cache[(mode, symbol)] = cached
                except Exception as exc:
                    log.debug("%s 시세 줄 조회 실패: %s", symbol, exc)
            if cached:
                quote = cached[1]
                row.update(price=quote.get("price"), change_24h_pct=quote.get("change_24h_pct"), high_24h=quote.get("high_24h"),
                           low_24h=quote.get("low_24h"), turnover_24h=quote.get("volume_24h"),
                           funding_rate_pct=quote.get("funding_rate_pct"))
            try:
                live = feed.get(symbol) if feed is not None else None       # 실시간 값이 있으면 그 가격을 쓴다
            except Exception:
                live = None
            if live:
                row["price"] = live
            spark = spark_cache.get((mode, symbol))
            if not spark or now - spark[0] >= 300:
                try:
                    spark = (now, [c["c"] for c in market.get_klines(symbol, "60", 24)])
                    spark_cache[(mode, symbol)] = spark
                except Exception as exc:
                    log.debug("%s 시세 줄 그래프 조회 실패: %s", symbol, exc)
            if spark:
                row["spark"] = spark[1]
            return row

        symbols = list(SYMBOLS)
        if len(symbols) > 1:
            with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
                rows = list(pool.map(one, symbols))
        else:
            rows = [one(s) for s in symbols]
        return ok(tickers=rows, feed=engine.feed_status())

    # ------------------------------------------------------------ 기록/수익률
    @app.get("/api/trades")
    def api_trades():
        """청산이 끝난 거래를 최신순으로. 대시보드의 '더보기'가 limit을 늘려 가며 부른다."""
        mode, tz = settings.get("mode"), settings.get("timezone")
        limit = max(1, min(500, request.args.get("limit", 10, type=int) or 10))
        offset = max(0, request.args.get("offset", 0, type=int) or 0)
        rows = storage.closed_page(mode, limit, offset)
        total = storage.count_closed(mode)
        today = local_day(utcnow(), tz)
        return ok(trades=[{**trade_view(t, tz), "today": t["day"] == today} for t in rows], total=total,
                  today=len(storage.trades_on_day(mode, today)), has_more=offset + len(rows) < total)     # 기록 건수 (분할 매도 회차도 한 줄씩)

    @app.get("/api/board")
    def api_board():
        """오늘의 코인별 손익 (수익 코인 표 / 손실 코인 표 / 그래프)와 원화 환산용 환율."""
        state = engine.get_state()
        board = build_pnl_board(storage, state["mode"], state["timezone"], state.get("positions", []),
                                state.get("start_equity") or 0.0)
        if hasattr(engine.macro, "refresh_async"):
            engine.macro.refresh_async()
        board["fx"] = (macro_cache() or {}).get("fx")
        board["pnl_amount"], board["pnl_pct"] = state.get("pnl_amount"), state.get("pnl_pct")
        return ok(board=board)

    @app.get("/api/macro")
    def api_macro():
        """글로벌 지수(나스닥, 다우존스, S&P 500)와 국제 금 시세. 화면을 기다리게 하지 않도록 뒤에서 받아 온다."""
        macro, tz = engine.macro, settings.get("timezone")
        loading = False
        if hasattr(macro, "refresh_async"):
            loading = macro.refresh_async(force=bool(request.args.get("refresh")))
        data = macro_cache()
        view = None
        if data:
            items = []
            for item in data.get("items", []):
                when = ""
                if item.get("as_of"):
                    when = fmt_local(datetime.fromtimestamp(item["as_of"], tz=timezone.utc), tz, "%m-%d %H:%M")
                elif item.get("as_of_day"):
                    when = item["as_of_day"] + " 종가"
                items.append({**item, "when": when})
            fetched = data.get("fetched_at")
            view = {"items": items, "available": bool(data.get("available")), "mood": data.get("mood"),
                    "avg_chg_1d": data.get("avg_chg_1d"), "errors": data.get("errors", {}), "old": bool(data.get("old")),
                    "fx": data.get("fx"),
                    "fetched": fmt_local(datetime.fromtimestamp(fetched, tz=timezone.utc), tz, "%m-%d %H:%M:%S") if fetched else ""}
        return ok(macro=view, loading=bool(loading), gemini=gemini_view())

    @app.get("/api/report")
    def api_report():
        period = request.args.get("period", "1d")
        return ok(report=build_report(storage, settings.get("mode"), period, settings.get("timezone")))

    @app.get("/api/returns")
    def api_returns():
        mode, tz = settings.get("mode"), settings.get("timezone")
        reports = {p: build_report(storage, mode, p, tz) for p in PERIODS}
        return ok(periods=[{"period": p, "label": PERIODS[p][0], **reports[p]["summary"]} for p in PERIODS],
                  today=reports["1d"]["series"], by_coin=reports["1m"]["by_coin"], daily=reports["1m"]["daily"])

    @app.get("/api/export")
    def api_export():
        period = request.args.get("period", "1d")
        if period not in PERIODS:
            period = "1d"
        mode, tz = settings.get("mode"), settings.get("timezone")
        buffer = export_xlsx(storage, mode, period, tz)
        name = f"MyselfTrade_{period}_{local_day(utcnow(), tz)}.xlsx"
        return send_file(buffer, as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ------------------------------------------------------------ 분석
    @app.get("/api/analysis")
    def api_analysis():
        mode, tz = settings.get("mode"), settings.get("timezone")
        day = local_day(utcnow(), tz)
        today = storage.get_day(mode, day) or {}
        previous = storage.last_day_before(mode, day) or {}
        plan = today.get("plan")
        if plan and plan.get("created_at"):
            plan = {**plan, "created_at": fmt_local(plan["created_at"], tz, "%m-%d %H:%M")}
        decisions = []
        for row in storage.recent_analysis(mode, 120):
            decisions.append({
                "time": fmt_local(row["ts"], tz, "%m-%d %H:%M"), "symbol": row["symbol"],
                "ticker": coin_meta(row["symbol"])["ticker"],
                "name": coin_meta(row["symbol"])["name"],
                "action": row["action"], "confidence": row["confidence"], "source": row["source"],
                "reason": row["reason"], "executed": bool(row["executed"]), "note": row["note"]})
        return ok(plan=plan, review=previous.get("review"), review_day=previous.get("day"),
                  lessons=storage.recent_lessons(12), decisions=decisions, gemini=gemini_view(),
                  ai_error=getattr(engine, "_ai_error", ""),
                  ai_error_at=(fmt_local(engine._ai_error_at, tz, "%H:%M")
                               if getattr(engine, "_ai_error", "") and getattr(engine, "_ai_error_at", None) else ""),
                  cross_check=settings.get("cross_check"),
                  materials=len(used_ids(storage)))

    @app.post("/api/replan")
    def api_replan():
        engine.request_replan()
        return ok(message="다시 분석을 요청했습니다. 잠시 후 결과가 표시됩니다.")

    # ------------------------------------------------------------ 제어
    @app.post("/api/toggle")
    def api_toggle():
        enabled = bool(request.get_json().get("enabled"))
        if enabled and settings.get("mode") != "paper" and not config.has_bybit_keys():
            return fail("Bybit API 키와 시크릿을 먼저 입력해야 이 모드로 거래할 수 있습니다.")
        settings.update({"trading_enabled": enabled})
        engine.wake()
        log.info("자동 매매 %s", "켜짐" if enabled else "꺼짐")
        return ok(enabled=enabled)

    @app.post("/api/close_all")
    def api_close_all():
        engine.request_close_all()
        return ok(message="보유 포지션 전체 청산을 요청했습니다.")

    @app.post("/api/close_position")
    def api_close_position():
        try:
            trade_id = int((request.get_json() or {}).get("id"))
        except (TypeError, ValueError):
            return fail("청산할 포지션을 찾을 수 없습니다.")
        trade = next((t for t in storage.open_trades(settings.get("mode")) if t["id"] == trade_id), None)
        if trade is None:
            return fail("이미 청산됐거나 없는 포지션입니다.")
        engine.request_close(trade_id)
        return ok(message=f"{coin_meta(trade['symbol'])['ticker']} 포지션 청산을 요청했습니다.")

    @app.post("/api/position_stops")
    def api_position_stops():
        """보유 포지션의 익절/손절을 그 포지션의 투자금 기준으로 직접 고친다.

        {"id": 3, "tp": {"unit": "pct", "value": 20}, "sl": {"unit": "amount", "value": 25}, "preview": true}
        tp나 sl을 빼면 그쪽은 지금 값을 그대로 둔다. preview가 참이면 계산만 해서 돌려준다.
        """
        body = request.get_json() or {}
        try:
            trade_id = int(body.get("id"))
        except (TypeError, ValueError):
            return fail("고칠 포지션을 찾을 수 없습니다.")
        specs = {}
        for key in ("tp", "sl"):
            spec = body.get(key)
            if spec is not None and not isinstance(spec, dict):
                return fail("잘못된 요청 내용입니다.")
            specs[key] = spec
        try:
            result = engine.position_stops(trade_id, specs["tp"], specs["sl"], apply=not body.get("preview"))
        except StopsError as exc:
            return fail(str(exc))
        except Exception as exc:
            log.exception("익절/손절 수정 실패: %s", exc)
            return fail("익절/손절을 바꾸는 중 오류가 났습니다. 자세한 내용은 data/myself_trade.log에 남겼습니다.", 500)
        if body.get("preview"):
            return ok(stops=result)
        message = (f"{result['ticker']} 포지션의 익절을 투자금의 {result['tp_margin_pct']:+.1f}%, 손절을 {result['sl_margin_pct']:+.1f}%로 바꿨습니다."
                   if result["applied"] else "바뀐 값이 없어 그대로 두었습니다.")
        return ok(stops=result, message=message)

    @app.post("/api/manual_order")
    def api_manual_order():
        """대시보드의 직접 주문 창. 시장가로 넣는다.

        {"symbol": "BTCUSDT", "side": "long", "margin": 250, "leverage": 5,
         "tp": {"unit": "pct", "value": 20}, "sl": {"unit": "pct", "value": 10}, "preview": true}
        tp, sl을 빼면 그쪽은 변동성 기준으로 자동. preview가 참이면 계산만 해서 돌려준다.
        """
        body = request.get_json() or {}
        specs = {}
        for key in ("tp", "sl"):
            spec = body.get(key)
            if spec is not None and not isinstance(spec, dict):
                return fail("잘못된 요청 내용입니다.")
            specs[key] = spec
        preview = bool(body.get("preview"))
        if not preview and settings.get("mode") == "live" and not body.get("confirm_live"):
            return fail("실전 거래 모드의 주문은 확인이 필요합니다.")
        try:
            result = engine.manual_order(body.get("symbol"), body.get("side"), body.get("margin"), body.get("leverage"),
                                         specs["tp"], specs["sl"], apply=not preview)
        except StopsError as exc:
            return fail(str(exc))
        except Exception as exc:
            log.exception("직접 주문 처리 실패: %s", exc)
            return fail("주문을 처리하는 중 오류가 났습니다. 자세한 내용은 data/myself_trade.log에 남겼습니다.", 500)
        if preview:
            return ok(order=result)
        word = "롱" if result["side"] == "long" else "숏"
        return ok(order=result, message=f"{result['ticker']} {word} 주문이 체결되었습니다. 투자금 {result['margin']:,.2f} USDT, "
                                        f"{result['leverage']}배, 진입가 {result['entry_price']:,.6g}.")

    @app.get("/api/calendar")
    def api_calendar():
        """기록 화면의 달력: 그 달의 날짜별 손익과 기록 유무. month=YYYY-MM (없으면 이번 달)"""
        mode, tz = settings.get("mode"), settings.get("timezone")
        today = local_day(utcnow(), tz)
        month = str(request.args.get("month") or today[:7])
        try:
            first = datetime.strptime(month + "-01", "%Y-%m-%d")
        except ValueError:
            return fail("달을 알아볼 수 없습니다.")
        after = (first.replace(day=28) + timedelta(days=4)).replace(day=1)        # 다음 달 1일
        last = after - timedelta(days=1)
        rows = storage.month_summary(mode, first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d"))
        before = (first - timedelta(days=1)).strftime("%Y-%m")
        earliest = storage.first_day(mode)
        return ok(month=first.strftime("%Y-%m"), today=today, days_in_month=last.day,
                  first_weekday=(first.weekday() + 1) % 7,          # 0 = 일요일
                  days=list(rows.values()), earliest=earliest,
                  prev=before if earliest and before >= earliest[:7] else None,
                  next=after.strftime("%Y-%m") if after.strftime("%Y-%m") <= today[:7] else None)

    @app.get("/api/day")
    def api_day():
        """달력에서 고른 하루의 수익, 손실, 거래, 계획과 그날 한 판단 기록.

        date=YYYY-MM-DD, 판단 기록은 offset부터 limit개 (처음 15개, 이후 '더 보기'로 20개씩).
        """
        mode, tz = settings.get("mode"), settings.get("timezone")
        today = local_day(utcnow(), tz)
        date = str(request.args.get("date") or today)
        try:
            picked = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return fail("날짜를 알아볼 수 없습니다.")
        if date > today:
            return fail("아직 오지 않은 날짜입니다.")
        limit = max(1, min(100, request.args.get("limit", 15, type=int) or 15))
        offset = max(0, request.args.get("offset", 0, type=int) or 0)
        total = storage.count_analysis_on_day(mode, date)
        decisions = [{
            "time": fmt_local(row["ts"], tz, "%H:%M"), "symbol": row["symbol"],
            "ticker": coin_meta(row["symbol"])["ticker"], "name": coin_meta(row["symbol"])["name"],
            "action": row["action"], "confidence": row["confidence"], "source": row["source"],
            "reason": row["reason"], "executed": bool(row["executed"]), "note": row["note"]}
            for row in storage.analysis_on_day(mode, date, limit, offset)]
        analysis = {"rows": decisions, "total": total, "offset": offset, "has_more": offset + len(decisions) < total}
        if request.args.get("only") == "analysis":
            return ok(date=date, analysis=analysis)
        trades = storage.trades_on_day(mode, date)
        wins = [t for t in trades if (t["net_pnl"] or 0) > 0]
        profit = sum(t["net_pnl"] or 0 for t in wins)
        loss = sum(t["net_pnl"] or 0 for t in trades if (t["net_pnl"] or 0) < 0)
        day_row = storage.get_day(mode, date) or {}
        start = day_row.get("start_equity") or 0.0
        state = engine.get_state() if date == today else {}
        if date == today and state.get("pnl_pct") is not None:
            return_pct, open_net = state["pnl_pct"], state.get("open_net") or 0.0      # 오늘은 보유 중인 포지션까지 포함
        else:
            end = day_row.get("end_equity")
            return_pct = ((end - start) / start * 100 if end is not None else (profit + loss) / start * 100) if start else None
            open_net = None
        plan = day_row.get("plan")
        if plan and plan.get("created_at"):
            plan = {**plan, "created_at": fmt_local(plan["created_at"], tz, "%m-%d %H:%M")}
        return ok(date=date, weekday="월화수목금토일"[picked.weekday()], is_today=date == today, has_record=bool(day_row or trades or total),
                  prev_date=(picked - timedelta(days=1)).strftime("%Y-%m-%d"),
                  next_date=(picked + timedelta(days=1)).strftime("%Y-%m-%d") if date < today else None,
                  summary={"profit": profit, "loss": loss, "net": profit + loss, "trades": len(trades), "wins": len(wins),
                           "losses": len(trades) - len(wins), "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
                           "fee": sum(t["fee"] or 0 for t in trades), "return_pct": return_pct, "start_equity": start or None,
                           "open_net": open_net, "locked": bool(day_row.get("locked")), "lock_reason": day_row.get("lock_reason") or ""},
                  trades=[trade_view(t, tz) for t in reversed(trades)], plan=plan, review=day_row.get("review"),
                  analysis=analysis)

    @app.get("/api/paper")
    def api_paper():
        """모의투자 화면용 요약: 가상 잔고와 누적 성과."""
        trades = storage.closed_trades("paper")
        open_trades = storage.open_trades("paper")
        net = sum(t["net_pnl"] or 0 for t in trades)
        balance = storage.kv_get("paper_balance")
        balance = float(balance) if balance is not None else float(settings.get("paper_start_balance"))
        base = balance - net + sum(t["fee"] or 0 for t in open_trades)   # 마지막 초기화 때의 시작 잔고
        return ok(balance=balance, base=base, net_pnl=net, return_pct=net / base * 100 if base else 0.0,
                  trades=len(trades), wins=sum(1 for t in trades if (t["net_pnl"] or 0) > 0),
                  open=len(open_trades), start_setting=settings.get("paper_start_balance"))

    @app.post("/api/paper_reset")
    def api_paper_reset():
        body = request.get_json() or {}
        changes = {}
        if body.get("start_balance") not in (None, ""):
            changes["paper_start_balance"] = body["start_balance"]
        if settings.get("mode") == "paper":
            changes["trading_enabled"] = False
        try:
            if changes:
                settings.update(changes)
        except SettingsError as exc:
            return fail(str(exc))
        engine.reset_paper()
        return ok(message=f"모의투자 기록을 지우고 {settings.get('paper_start_balance'):,.0f} USDT로 다시 시작합니다.")

    # ------------------------------------------------------------ 설정
    def settings_view() -> dict:
        return {**settings.all(), "has_bybit_keys": config.has_bybit_keys(),
                "has_gemini_key": config.has_gemini_key(), "gemini": gemini_view()}

    @app.get("/api/settings")
    def api_settings_get():
        return ok(settings=settings_view())

    @app.post("/api/settings")
    def api_settings_post():
        body = request.get_json() or {}
        # 코인 추가/삭제는 Bybit 확인을 거치는 /api/coins 로만 한다
        changes = {k: v for k, v in body.items() if k not in ("confirm_live", "trading_enabled", "custom_symbols")}
        new_mode = changes.get("mode", settings.get("mode"))
        mode_changed = new_mode != settings.get("mode")
        if mode_changed and new_mode != "paper" and not config.has_bybit_keys():
            return fail("Bybit API 키와 시크릿을 먼저 저장해야 이 모드를 선택할 수 있습니다.")
        if mode_changed and new_mode == "live" and not body.get("confirm_live"):
            return fail("실전 거래 전환은 확인이 필요합니다.")
        if mode_changed and storage.open_trades(settings.get("mode")):
            return fail("보유 중인 포지션이 있습니다. 대시보드에서 '보유 포지션 모두 청산'을 누른 뒤 다시 바꿔 주세요.")
        if new_mode != "paper":
            changes["real_mode"] = new_mode       # 모의투자를 껐을 때 돌아갈 계정으로 기억
        if mode_changed:
            changes["trading_enabled"] = False   # 모드를 바꾸면 자동 매매는 꺼진 상태로 시작
        try:
            settings.update(changes)
        except SettingsError as exc:
            return fail(str(exc))
        if mode_changed:
            log.info("거래 모드 변경: %s", MODES[new_mode])
        engine.wake()
        return ok(settings=settings_view())

    # ------------------------------------------------------------ 거래할 코인 직접 추가/삭제
    @app.get("/api/coins")
    def api_coins():
        return ok(symbols=symbols_view(), settings=settings_view())

    @app.post("/api/coins")
    def api_coins_post():
        """{"add": "SOL", "name": "솔라나"} 코인 추가 | {"remove": "SOLUSDT"} 직접 추가한 코인 삭제"""
        body = request.get_json() or {}
        custom = list(settings.get("custom_symbols"))
        picked = list(settings.get("trade_symbols"))
        if body.get("add"):
            symbol = normalize_symbol(body["add"])
            if not symbol:
                return fail("코인 이름을 알아볼 수 없습니다. SOL처럼 영문 약자로 넣어 주세요.")
            ticker = symbol[:-4]
            if symbol in SYMBOLS:
                return fail(f"{ticker}는 이미 목록에 있습니다.")
            if len(custom) >= MAX_CUSTOM_SYMBOLS:
                return fail(f"직접 추가하는 코인은 {MAX_CUSTOM_SYMBOLS}개까지 넣을 수 있습니다. 쓰지 않는 코인을 지운 뒤 다시 추가해 주세요.")
            market = engine.market_for(settings.get("mode"))
            try:      # Bybit USDT 무기한 선물에 실제로 있는 코인인지, 시세가 나오는지 확인한다
                market.get_instrument(symbol)
                price = market.get_ticker(symbol)["price"]
            except (BybitError, KeyError) as exc:
                if isinstance(exc, BybitError) and exc.code is None:
                    return fail("Bybit에 접속하지 못해 코인을 확인하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.", 502)
                return fail(f"{ticker}는 Bybit USDT 무기한 선물에서 찾을 수 없습니다. 약자를 다시 확인해 주세요. "
                            "(가격이 아주 작은 코인은 1000PEPE처럼 앞에 숫자가 붙기도 합니다)")
            except Exception as exc:
                log.warning("코인 확인 실패 (%s): %s", symbol, exc)
                return fail("Bybit에 접속하지 못해 코인을 확인하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.", 502)
            try:
                settings.update({"custom_symbols": custom + [{"symbol": symbol, "name": body.get("name", "")}],
                                 "trade_symbols": picked + [symbol]})
            except SettingsError as exc:
                return fail(str(exc))
            engine.sync_symbols()
            log.info("코인 추가: %s", symbol)
            return ok(message=f"{coin_meta(symbol)['name']}({ticker})을 추가하고 거래할 코인에 넣었습니다. 지금 가격 {price:,.6g} USDT.",
                      symbols=symbols_view(), settings=settings_view())
        if body.get("remove"):
            symbol = str(body["remove"]).upper()
            if symbol in BASE_SYMBOLS:
                return fail("기본 코인은 지울 수 없습니다. 거래하지 않으려면 '거래할 코인'에서 선택만 꺼 주세요.")
            if symbol not in SYMBOLS:
                return fail("목록에 없는 코인입니다.")
            if any(t["symbol"] == symbol for t in storage.open_trades(settings.get("mode"))):
                return fail(f"{symbol[:-4]} 포지션을 보유 중입니다. 대시보드에서 청산한 뒤 지워 주세요.")
            if picked == [symbol]:
                # 그냥 지우면 거래할 코인이 하나도 남지 않는다. 다른 코인이 말없이 켜져 거래되는 일이 없도록 막는다.
                return fail(f"지금은 {symbol[:-4]}만 거래하도록 골라져 있습니다. 거래할 코인에서 다른 코인을 켜고 설정 저장을 누른 뒤 지워 주세요.")
            name = coin_meta(symbol)["name"]
            settings.update({"custom_symbols": [c for c in custom if c["symbol"] != symbol]})
            engine.sync_symbols()
            log.info("코인 삭제: %s", symbol)
            return ok(message=f"{name}({symbol[:-4]})을 목록에서 지웠습니다. 지난 거래 기록은 그대로 남습니다.",
                      symbols=symbols_view(), settings=settings_view())
        return fail("추가하거나 지울 코인이 없습니다.")

    @app.post("/api/keys")
    def api_keys():
        body = request.get_json() or {}
        changes = {}
        for field, name in (("bybit_key", "BYBIT_API_KEY"), ("bybit_secret", "BYBIT_SECRET_KEY"),
                            ("gemini_key", "GEMINI_API_KEY")):
            if field in body and (body[field] or body.get("clear")):
                changes[name] = "" if body.get("clear") else str(body[field])
        if not changes:
            return fail("입력된 키가 없습니다.")
        if not body.get("clear") and ("BYBIT_API_KEY" in changes or "BYBIT_SECRET_KEY" in changes):
            key = changes.get("BYBIT_API_KEY") or config.get_secret("BYBIT_API_KEY")
            secret = changes.get("BYBIT_SECRET_KEY") or config.get_secret("BYBIT_SECRET_KEY")
            if not (key and secret):
                return fail("Bybit는 API Key와 API Secret을 둘 다 입력해야 합니다.")
        config.save_env(changes)
        if not config.has_bybit_keys() and settings.get("mode") != "paper":
            settings.update({"mode": "paper", "trading_enabled": False})
        gemini = None
        if "GEMINI_API_KEY" in changes and config.has_gemini_key():
            # 키를 저장하면 바로 연결을 확인하고, 성공하면 AI 분석과 글로벌 지수, 대기 중인 학습 자료를 즉시 켠다
            connected, message = ai().check()
            gemini = {"ok": connected, "message": message}
            log.info("Gemini 연결 확인: %s", message)
            if hasattr(engine.macro, "refresh_async"):
                engine.macro.refresh_async(force=True)
            if connected:
                process_async(storage, ai())
                engine.request_replan()
        engine.wake()
        return ok(settings=settings_view(), gemini=gemini)

    @app.post("/api/test_connection")
    def api_test_connection():
        if not config.has_bybit_keys():
            return fail("Bybit API 키와 시크릿을 먼저 저장해 주세요.")
        env = (request.get_json() or {}).get("env") or settings.get("mode")
        if env not in ("live", "demo", "testnet"):
            env = "live"
        try:
            client = BybitClient(config.get_secret("BYBIT_API_KEY"), config.get_secret("BYBIT_SECRET_KEY"), env=env)
            result = client.check_connection()
        except BybitError as exc:
            hints = {
                10003: "API Key가 올바르지 않거나, 고른 계정 종류(데모/실계정/테스트넷)와 키를 만든 곳이 다릅니다.",
                10004: "API Secret이 올바르지 않습니다. 복사할 때 빠진 글자가 없는지 확인해 주세요.",
                10005: "이 키에는 필요한 권한이 없습니다. 읽기-쓰기와 계약 거래 권한을 켜 주세요.",
                10010: "키에 걸어 둔 IP 제한에 지금 쓰는 인터넷 주소(IP)가 들어 있지 않습니다.",
            }
            if exc.code is None:   # 응답 자체를 받지 못한 경우
                log.warning("Bybit 연결 확인 실패: %s", exc)
                return fail("연결 실패: Bybit 서버에 접속하지 못했습니다. 인터넷 연결과, 지금 있는 지역에서 "
                            "Bybit 접속이 되는지 확인해 주세요. 자세한 내용은 data/myself_trade.log에 남겼습니다.")
            hint = hints.get(exc.code, "")
            return fail(f"연결 실패: {hint} ({exc})" if hint else f"연결 실패: {exc}")
        return ok(message=f"{MODES[env]} 연결 성공. 총자산 {result['equity']:.2f} USDT")

    @app.post("/api/test_gemini")
    def api_test_gemini():
        if not config.has_gemini_key():
            return fail("Gemini API 키를 먼저 저장해 주세요.")
        connected, message = ai().check()
        if not connected:
            return fail(f"연결 실패: {message}")
        process_async(storage, ai())          # 분석을 기다리던 학습 자료가 있으면 처리한다
        return ok(message=message, gemini=gemini_view())

    @app.get("/api/gemini_models")
    def api_gemini_models():
        """이 키로 쓸 수 있는 Gemini 모델 목록 (API 연결 화면의 모델 선택용)."""
        current, backup = settings.get("gemini_model"), settings.get("gemini_backup_model")
        if not config.has_gemini_key():
            return ok(models=[], current=current, backup=backup, message="Gemini 키를 저장하면 쓸 수 있는 모델 목록을 불러옵니다.")
        analyzer = ai()
        models = analyzer.list_models(refresh=bool(request.args.get("refresh"))) if hasattr(analyzer, "list_models") else []
        message = "" if models else "모델 목록을 불러오지 못했습니다. 인터넷 연결과 Gemini 키를 확인한 뒤 다시 눌러 주세요."
        return ok(models=models, current=current, backup=backup, message=message)

    # ------------------------------------------------------------ 학습 자료 (지식 베이스)
    def materials_view() -> dict:
        tz = settings.get("timezone")
        used = used_ids(storage)      # 프롬프트 길이 한도 안에 실제로 들어가는 자료
        items = [material_view(row, tz, used) for row in storage.materials()]
        return {"materials": items, "gemini": gemini_view(), "has_key": config.has_gemini_key(),
                "in_use": sum(1 for item in items if item["in_use"])}

    @app.get("/api/materials")
    def api_materials():
        return ok(**materials_view())

    @app.post("/api/materials")
    def api_materials_add():
        """자료 추가. kind: text(전략 글) | youtube(유튜브 링크) | image(차트 이미지, base64)"""
        body = request.get_json() or {}
        kind = str(body.get("kind") or "")
        try:
            material_id = add_material(storage, kind, title=body.get("title", ""), text=body.get("text", ""),
                                       url=body.get("url", ""), image_b64=body.get("image", ""),
                                       mime=body.get("mime", ""), filename=body.get("filename", ""))
        except MaterialError as exc:
            return fail(str(exc))
        if config.has_gemini_key():
            process_material(storage, ai(), material_id)      # 바로 분석해서 다음 판단부터 반영한다
            row = storage.get_material(material_id)
            if row["status"] == "ok":
                message = f"자료를 분석해 규칙 {len((row['summary'] or {}).get('rules', []))}개를 지식 베이스에 넣었습니다. 다음 판단부터 반영됩니다."
            else:
                message = "자료는 저장했지만 분석에 실패했습니다: " + (row.get("error") or "")
            return ok(message=message, warning=row["status"] != "ok", **materials_view())
        if kind == "text":
            message = "저장했습니다. Gemini 키를 연결하면 그때 규칙으로 정리해 AI 판단에 씁니다."
        else:
            message = "저장했습니다. 영상과 이미지는 Gemini가 직접 봐야 하므로, API 연결 화면에서 Gemini 키를 연결하면 그때 분석합니다."
        return ok(message=message, warning=True, **materials_view())

    @app.post("/api/materials/update")
    def api_materials_update():
        """{"id": 번호, "enabled": true/false} 켜고 끄기 | {"id": 번호, "delete": true} 삭제 | {"id": 번호, "retry": true} 다시 분석"""
        body = request.get_json() or {}
        try:
            material_id = int(body.get("id"))
        except (TypeError, ValueError):
            return fail("자료를 찾을 수 없습니다.")
        row = storage.get_material(material_id)
        if row is None:
            return fail("이미 지워졌거나 없는 자료입니다.")
        if body.get("delete"):
            remove_material(storage, material_id)
            return ok(message=f"'{row['title']}' 자료를 지웠습니다.", **materials_view())
        if body.get("retry"):
            if not config.has_gemini_key():
                return fail("Gemini 키가 있어야 분석할 수 있습니다. API 연결 화면에서 키를 먼저 연결해 주세요.")
            storage.update_material(material_id, status="pending", error="")
            process_material(storage, ai(), material_id)
            row = storage.get_material(material_id)
            good = row["status"] == "ok"
            return ok(message="다시 분석했습니다." if good else "분석에 실패했습니다: " + (row.get("error") or ""),
                      warning=not good, **materials_view())
        if "enabled" in body:
            storage.update_material(material_id, enabled=1 if body["enabled"] else 0)
            return ok(message=f"'{row['title']}' 자료를 " + ("AI 판단에 다시 씁니다." if body["enabled"] else "AI 판단에서 뺐습니다."),
                      **materials_view())
        return fail("바꿀 내용이 없습니다.")

    # ------------------------------------------------------------ 일일 보고서 메일
    def mail_view() -> dict:
        s = settings.all()
        tz = s["timezone"]
        ready, reason = mail_ready(settings)
        last = storage.kv_get("mail_last") or {}
        status = storage.kv_get("mail_status") or {}
        recipients = []
        for email in s["mail_recipients"]:
            item = status.get(email) or {}
            state = "ok" if item.get("ok") else "pending" if item.get("pending") else "failed" if item else "new"
            recipients.append({"email": email, "state": state, "message": item.get("message", ""),
                               "at": fmt_local(item["at"], tz, "%m-%d %H:%M") if item.get("at") else ""})
        return {
            "settings": {k: s[k] for k in MAIL_KEYS}, "recipients": recipients, "has_password": config.has_mail_password(),
            "ready": ready, "reason": reason, "tz_label": next((k for k, v in TIMEZONES.items() if v == tz), tz),
            "last": {"ok": last.get("ok"), "error": last.get("error", ""), "subject": last.get("subject", ""),
                     "summary": last.get("summary", ""), "trigger": last.get("trigger", ""), "html": last.get("html", ""),
                     "at": fmt_local(last["at"], tz, "%Y-%m-%d %H:%M") if last.get("at") else ""} if last else None,
        }

    def disable_if_not_ready() -> str:
        ready, reason = mail_ready(settings)
        if settings.get("mail_enabled") and not ready:
            settings.update({"mail_enabled": False})
            return f" 자동 발송은 꺼졌습니다. {reason}"
        return ""

    @app.get("/api/mail")
    def api_mail_get():
        return ok(**mail_view())

    @app.post("/api/mail")
    def api_mail_post():
        body = request.get_json() or {}
        password = str(body.get("password") or "").replace(" ", "")   # Gmail 앱 비밀번호는 띄어쓰기를 빼도 된다
        if body.get("clear_password"):
            config.save_env({"MAIL_PASSWORD": ""})
        elif password:
            config.save_env({"MAIL_PASSWORD": password})
        wanted = bool(body.get("mail_enabled")) if "mail_enabled" in body else None
        try:
            settings.update({k: body[k] for k in MAIL_KEYS if k in body})
        except SettingsError as exc:
            return fail(str(exc))
        note = disable_if_not_ready()
        if wanted and note:
            return ok(message="자동 발송을 켜지 못했습니다." + note.replace(" 자동 발송은 꺼졌습니다.", ""), warning=True, **mail_view())
        return ok(message="저장했습니다." + note, warning=bool(note), **mail_view())

    @app.post("/api/mail/recipients")
    def api_mail_recipients():
        """받는 주소 추가/삭제. {"add": "주소"} 또는 {"remove": "주소"}"""
        body = request.get_json() or {}
        current = list(settings.get("mail_recipients"))
        if body.get("add"):
            email = str(body["add"]).strip()
            if email.lower() in [c.lower() for c in current]:
                return fail("이미 목록에 있는 주소입니다.")
            try:
                settings.update({"mail_recipients": current + [email]})
            except SettingsError as exc:
                return fail(str(exc))
            return ok(message=f"{email} 주소를 추가했습니다.", **mail_view())
        if body.get("remove"):
            email = str(body["remove"]).strip()
            settings.update({"mail_recipients": [c for c in current if c.lower() != email.lower()]})
            status = storage.kv_get("mail_status") or {}
            status.pop(email, None)
            storage.kv_set("mail_status", status)
            return ok(message=f"{email} 주소를 삭제했습니다." + disable_if_not_ready(), **mail_view())
        return fail("추가하거나 삭제할 주소가 없습니다.")

    @app.post("/api/mail/test")
    def api_mail_test():
        """확인 메일. {"to": "주소"}가 있으면 그 주소만, 없으면 목록 전체에 보낸다."""
        target = str((request.get_json() or {}).get("to") or "").strip()
        recipients = [target] if target else list(settings.get("mail_recipients"))
        if target and target.lower() not in [c.lower() for c in settings.get("mail_recipients")]:
            return fail("목록에 없는 주소입니다.")
        text = "이 메일이 보이면 메일 설정이 정상입니다. 매일 정해 둔 시각에 일일 매매 보고서가 이 주소로 발송됩니다."
        try:
            results = deliver_mail(settings, recipients, f"[{APP_NAME}] 메일 연결 확인", text, None, {"안내": text})
        except MailError as exc:
            return fail(str(exc))
        record_status(storage, results, iso(utcnow()))
        failed = any(not r["ok"] and not r["pending"] for r in results)
        return ok(message=summarize_results(results), warning=failed or any(r["pending"] for r in results), **mail_view())

    @app.post("/api/mail/preview")
    def api_mail_preview():
        result = engine.send_report(trigger="preview", deliver=False)
        if not result["ok"]:
            return fail(result["error"])
        return ok(subject=result["subject"], html=result["html"], source=result["source"])

    @app.post("/api/mail/send_now")
    def api_mail_send_now():
        result = engine.send_report(trigger="manual", deliver=True)
        if not result["ok"]:
            return fail(result["error"])
        return ok(message="오늘 보고서: " + result.get("summary", ""), warning=bool(result.get("waiting")), **mail_view())

    return app

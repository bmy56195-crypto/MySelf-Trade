"""Myself Trade - 자동 매매 엔진.

한 번의 순환(step)에서 하는 일:
  1. 보유 포지션 감시 (손절/익절/보유 시간 초과 -> 청산 기록)
  2. 날짜가 바뀌었으면 전날 회고(학습) 후 새 날 시작
  3. 일일 손실 제한 확인 -> 도달 시 전부 청산하고 그날 잠금
  4. 판단 주기가 되면 AI(또는 규칙 전략)로 진입/청산 판단 후 주문
  5. 자산 변화 기록, 대시보드용 상태 갱신
"""
from __future__ import annotations

import json
import threading
from datetime import timedelta

from broker import LiveBroker, PaperBroker
from bybit_api import BybitClient
from config import SYMBOLS, coin_meta, get_secret, has_bybit_keys
from gemini_analyzer import GeminiAnalyzer, gemini_status
from knowledge import knowledge_text, process_async
from mailer import MailError, build_daily_report, deliver_mail, record_status, summarize_results
from market_data import MacroData, all_snapshots, coin_snapshot
from price_feed import PriceFeed
from risk_manager import (MAX_SL_MARGIN_PCT, MAX_TP_MOVE_PCT, MIN_SL_MOVE_PCT, RiskManager, daily_limit_text, max_sl_move,
                          pos_limit_text)
from strategy import EXIT_SINGLE_SCORE, rule_decide, rule_plan, rule_review, rule_scores
from utils import (day_fraction, iso, local_day, log, minutes_between, parse_iso, round_step, split_left, to_local,
                   trade_context, utcnow)


MANUAL_COOLDOWN_MINUTES = 30


class StopsError(ValueError):
    """보유 포지션의 익절/손절을 고칠 수 없을 때 (화면에 그대로 보여 줄 안내 문구)."""


class OrderError(StopsError):
    """직접 넣은 주문을 받을 수 없을 때 (화면에 그대로 보여 줄 안내 문구)."""

PROB_MARGIN = 0.15         # AI가 본 롱 확률과 숏 확률의 차이가 이보다 작으면 진입하지 않는다
RULE_LEAD = 0.05           # 같은 방향의 규칙 점수가 반대 방향보다 이만큼은 높아야 '같은 방향'으로 본다
RULE_FLOOR = 0.40          # 같은 방향의 규칙 점수가 이보다 낮으면 근거 부족
SIDE_KO = {"long": "롱", "short": "숏"}
STATUS_ON = "자동 매매 중입니다."
STATUS_OFF = "자동 매매가 꺼져 있습니다."


class TradeEngine:
    def __init__(self, settings, storage, market=None, clock=None, analyzer=None, macro=None):
        self.settings = settings
        self.storage = storage
        self.clock = clock or utcnow
        self.analyzer = analyzer or GeminiAnalyzer(settings)
        self.macro = macro or MacroData()
        self.risk = RiskManager(settings)
        self._fixed_market = market          # 점검용 가상 시세를 넣을 수 있다
        self._markets: dict[str, object] = {}
        self._brokers: dict[tuple, object] = {}
        self._step_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._state: dict = {"ready": False, "status": "시작하는 중입니다.", "positions": []}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._flags = {"close_all": False, "replan": False}
        self._last_decision_at = None
        self._last_snapshot_at = None
        self._was_enabled = bool(settings.get("trading_enabled"))
        self._ai_error = ""
        self._ai_error_at = None             # 그 오류가 난 시각
        self._mail_busy = False
        self.mail_async = True               # 점검할 때는 False로 두고 바로 보낸다
        self._close_ids: set[int] = set()    # 화면에서 '청산'을 누른 포지션
        self._cooldown: dict[str, object] = {}   # 수동 청산한 코인은 잠시 다시 진입하지 않는다
        try:                                 # 사용자가 올린 학습 자료를 AI 프롬프트에 연결한다
            self.analyzer.knowledge = lambda: knowledge_text(storage)
        except AttributeError:
            pass

    # ---------------------------------------------------------- 실행 제어
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trade-engine", daemon=True)
        self._thread.start()
        if self.analyzer.available and hasattr(self.analyzer, "check"):
            # 저장된 Gemini 키가 실제로 연결되는지 켤 때 한 번 확인하고, 분석을 기다리던 학습 자료도 처리한다
            threading.Thread(target=self.analyzer.check, name="gemini-check", daemon=True).start()
            process_async(self.storage, self.analyzer)
        if hasattr(self.macro, "refresh_async"):
            self.macro.refresh_async()       # 글로벌 지수는 자동 매매를 켜지 않아도 바로 보이게 미리 받아 둔다

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for market in self._markets.values():
            if getattr(market, "feed", None) is not None:
                market.feed.stop()

    def wake(self) -> None:
        """설정이 바뀌었을 때 기다리지 않고 바로 한 번 순환한다."""
        self._wake.set()

    def request_close_all(self) -> None:
        self._flags["close_all"] = True
        self.wake()

    def request_close(self, trade_id: int) -> None:
        self._close_ids.add(int(trade_id))
        self.wake()

    def request_replan(self) -> None:
        self._flags["replan"] = True
        self.wake()

    def reset_paper(self) -> None:
        with self._step_lock:
            self.storage.reset_mode("paper")
            self.storage.kv_set("paper_balance", float(self.settings.get("paper_start_balance")))
            self._last_decision_at = None
        self.wake()

    def _run(self) -> None:
        log.info("매매 엔진을 시작했습니다.")
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # 한 번의 오류로 엔진이 멈추지 않게 한다
                log.exception("엔진 순환 중 오류: %s", exc)
                self._update_state(error=str(exc), status=f"오류가 발생해 다음 주기에 다시 시도합니다: {exc}")
            self._wake.wait(timeout=float(self.settings.get("loop_seconds")))
            self._wake.clear()
        log.info("매매 엔진을 멈췄습니다.")

    # ---------------------------------------------------------- 시세/브로커
    def market_for(self, mode: str):
        if self._fixed_market is not None:
            return self._fixed_market
        env = "testnet" if mode == "testnet" else "live"   # 모의/데모/실전은 실제(메인넷) 시세를 쓴다
        if env not in self._markets:
            client = BybitClient(env=env)
            client.feed = PriceFeed(client, list(SYMBOLS), env=env)    # 실시간 시세 연결
            client.feed.start()
            self._markets[env] = client
        return self._markets[env]

    def sync_symbols(self) -> None:
        """설정에서 코인을 추가하거나 지웠을 때 실시간 시세가 받는 코인 목록을 맞춘다."""
        for market in self._markets.values():
            feed = getattr(market, "feed", None)
            if feed is not None:
                feed.set_symbols(list(SYMBOLS))
        self._last_decision_at = None        # 다음 순환에서 새 코인까지 넣어 바로 판단한다
        self.wake()

    def feed_status(self) -> dict:
        market = self.market_for(self.settings.get("mode"))
        feed = getattr(market, "feed", None)
        if feed is None:
            return {"source": "test", "label": "점검용 가상 시세", "fresh": True, "age": 0}
        return feed.status()

    def broker_for(self, mode: str, market):
        if mode == "paper":
            key = ("paper", id(market))
            if key not in self._brokers:
                self._brokers[key] = PaperBroker(market, self.storage, self.settings)
            return self._brokers[key]
        if not has_bybit_keys():
            return None  # 안전 장치: API 키와 시크릿이 모두 있어야 실제 주문 모드가 켜진다
        api_key, secret = get_secret("BYBIT_API_KEY"), get_secret("BYBIT_SECRET_KEY")
        key = (mode, api_key, hash(secret))     # 시크릿만 고쳐 저장해도 새 값으로 다시 연결한다
        if key not in self._brokers:
            client = BybitClient(api_key, secret, env=mode)
            self._brokers[key] = LiveBroker(client, market, self.settings)
        return self._brokers[key]

    # ---------------------------------------------------------- 상태
    def _update_state(self, **changes) -> None:
        with self._state_lock:
            self._state.update(changes)

    def get_state(self) -> dict:
        with self._state_lock:
            state = dict(self._state)
        s = self.settings.all()
        # 스위치를 방금 바꿨는데 엔진이 아직 한 번 돌기 전이면 안내 문구를 스위치에 맞춘다
        if state.get("status") == STATUS_ON and not s["trading_enabled"]:
            state["status"] = STATUS_OFF
        elif state.get("status") == STATUS_OFF and s["trading_enabled"] and not state.get("locked"):
            state["status"] = STATUS_ON
        base = state.get("start_equity") or 0.0
        target_pct, loss_pct = self.risk.daily_limits(base)      # 금액으로 정했으면 오늘 원금 대비 %로 바꾼 값
        target_text, loss_text = daily_limit_text(s)
        state.update({"enabled": s["trading_enabled"], "mode": s["mode"], "real_mode": s["real_mode"],
                      "paper_on": s["mode"] == "paper", "timezone": s["timezone"],
                      "direction_mode": s["direction_mode"], "entry_mode": s["entry_mode"], "split_count": s["split_count"],
                      "exit_mode": s["exit_mode"], "exit_split_count": s["exit_split_count"],
                      "trade_symbols": s["trade_symbols"], "slots": self.risk.slots(),
                      "leverage_mode": s["leverage_mode"], "max_trades": s["max_trades_per_day"],
                      "min_trades": s["min_trades_per_day"], "target_pct": target_pct, "loss_limit_pct": loss_pct,
                      "target_amount": base * target_pct / 100, "loss_amount": base * loss_pct / 100,
                      "target_text": target_text, "loss_text": loss_text,
                      "target_unit": s["daily_target_unit"], "loss_unit": s["daily_loss_unit"],
                      "tpsl_mode": s["tpsl_mode"], "tpsl_text": pos_limit_text(s), "max_hold_minutes": s["max_hold_minutes"],
                      "cross_check": s["cross_check"], "invest_pct": s["invest_pct"],
                      "taker_fee_pct": s["taker_fee_pct"], "manual_leverage": s["manual_leverage"],
                      "ai_interval_minutes": s["ai_interval_minutes"], "max_open_positions": s["max_open_positions"],
                      "last_decision": iso(self._last_decision_at) if self._last_decision_at else None,
                      "ai_available": self.analyzer.available, "ai_error": self._ai_error, "feed": self.feed_status(),
                      "gemini": gemini_status(), "has_bybit_keys": has_bybit_keys()})
        # 투자하고 남은 금액: 총자산에서 지금 포지션에 들어가 있는 증거금을 뺀 값 (모의투자도 같은 계산)
        if state.get("equity") is not None:
            used = sum(p.get("margin") or 0 for p in state.get("positions") or [])
            limit = base * s["invest_pct"] / 100
            state.update({"invested": used, "available": max(0.0, state["equity"] - used),
                          "invest_limit": limit, "limit_left": max(0.0, limit - used)})
        return state

    # ---------------------------------------------------------- 한 번의 순환
    def step(self) -> None:
        with self._step_lock:
            self._step()

    def _step(self) -> None:
        s = self.settings.all()
        mode, tz = s["mode"], s["timezone"]
        now = self.clock()
        day = local_day(now, tz)
        market = self.market_for(mode)
        broker = self.broker_for(mode, market)
        if broker is None:
            self._update_state(ready=False, error="", positions=[],
                               status="이 모드는 Bybit API 키와 시크릿을 입력해야 동작합니다. 설정 화면에서 입력해 주세요.")
            return

        # 1) 날짜 확인 (새 날이면 전날 회고). 포지션 감시보다 먼저 해서, 오늘 원금을 '오늘의 첫 거래가 끝나기 전' 값으로 잡는다.
        fresh_day = self.storage.get_day(mode, day) is None
        equity = self._net_equity(broker, market, mode)[0]
        day_row = self._ensure_day(mode, day, equity, broker, market)

        # 2) 보유 포지션 감시
        closed_now = 0
        for trade in self.storage.open_trades(mode):
            try:
                closure = broker.check_closed(trade)
                if closure is None:
                    held = minutes_between(parse_iso(trade["entry_time"]), now)
                    if held >= s["max_hold_minutes"]:
                        closure = broker.close_position(trade, "보유 시간 초과")
                    elif not fresh_day:
                        # 분할 매도: 익절가로 가는 길의 다음 구간에 닿았으면 한 회차를 판다 (자동 매매 스위치와 관계없이 동작)
                        self._exit_stage(mode, market, broker, now, day, trade, s)
            except Exception as exc:      # 한 코인의 조회가 실패해도 나머지 포지션과 손실 제한 확인은 계속한다
                log.warning("%s 포지션 확인 실패, 다음 순환에 다시 확인합니다: %s", trade["symbol"], exc)
                continue
            if closure:
                self._record_close(trade, closure, now, day)
                closed_now += 1
                if fresh_day:
                    # 오늘 원금을 잰 바로 그 순환에서 끝난 거래(프로그램이 꺼져 있던 사이 끝난 거래 포함)는
                    # 손익이 이미 오늘 원금에 들어 있다. 오늘의 코인별 손익에는 0으로 잡히게 한다.
                    carry = self.storage.kv_get(f"carry_{mode}_{day}") or {}
                    carry[str(trade["id"])] = closure["net_pnl"]
                    self.storage.kv_set(f"carry_{mode}_{day}", carry)
        equity = self._net_equity(broker, market, mode)[0]
        if fresh_day and closed_now:
            previous = self.storage.last_day_before(mode, day)
            if previous:
                self.storage.update_day(mode, previous["day"], end_equity=equity)
            self.storage.update_day(mode, day, start_equity=equity)
            day_row["start_equity"] = equity
        start_equity = day_row["start_equity"]
        pnl_pct = self.risk.daily_pnl_pct(equity, start_equity)

        # 3) 일일 손실 제한 (전체 원금 기준. 금액으로 정했으면 오늘 원금 대비 %로 바꿔서 본다)
        target_pct, loss_pct = self.risk.daily_limits(start_equity)
        if not day_row["locked"] and self.risk.should_lock(pnl_pct, loss_pct):
            log.warning("일일 손실 제한 도달 (%.2f%%). 오늘 매매를 중지합니다.", pnl_pct)
            self._close_all(broker, mode, now, day, "일일 손실 제한")
            limit_text = daily_limit_text(s)[1]
            if s["daily_loss_unit"] == "amount":
                limit_text += f" (오늘 원금의 -{loss_pct:.1f}%)"
            reason = f"하루 손실이 {limit_text}에 도달해 자동 매매를 멈췄습니다."
            self.storage.update_day(mode, day, locked=1, lock_reason=reason)
            day_row["locked"], day_row["lock_reason"] = 1, reason
            equity = self._net_equity(broker, market, mode)[0]
            pnl_pct = self.risk.daily_pnl_pct(equity, start_equity)

        elif day_row["locked"] and self.storage.open_trades(mode):
            self._close_all(broker, mode, now, day, "일일 손실 제한")  # 앞서 청산에 실패한 포지션 재시도

        # 목표 수익률 달성
        if not day_row["target_hit"] and self.risk.target_reached(pnl_pct, target_pct):
            log.info("오늘 목표 수익률 달성 (%.2f%%).", pnl_pct)
            self.storage.update_day(mode, day, target_hit=1)
            day_row["target_hit"] = 1
            if s["stop_on_target"]:
                self._close_all(broker, mode, now, day, "목표 수익 달성")

        # 사용자 요청: 전체 청산 / Off 전환 시 청산
        enabled = s["trading_enabled"]
        if self._flags["close_all"] or (self._was_enabled and not enabled and s["close_on_off"]):
            self._flags["close_all"] = False
            self._close_all(broker, mode, now, day, "사용자 청산")
        self._was_enabled = enabled
        if self._close_ids:                       # 사용자가 한 포지션만 골라 청산
            wanted, self._close_ids = self._close_ids, set()
            for trade in self.storage.open_trades(mode):
                if trade["id"] in wanted:
                    try:
                        self._record_close(trade, broker.close_position(trade, "수동 청산"), now, day)
                        self._cooldown[trade["symbol"]] = now + timedelta(minutes=MANUAL_COOLDOWN_MINUTES)
                    except Exception as exc:
                        log.exception("%s 수동 청산 실패: %s", trade["symbol"], exc)

        # 4) 진입/청산 판단
        replan = self._flags["replan"]
        due = self._last_decision_at is None or \
            minutes_between(self._last_decision_at, now) >= s["ai_interval_minutes"]
        if replan or (enabled and not day_row["locked"] and due):
            self._flags["replan"] = False
            self._last_decision_at = now
            self._decide(mode, market, broker, now, day, day_row, replan)
            day_row = self.storage.get_day(mode, day)

        # 5) 기록과 상태 갱신
        equity, gross_equity, exit_fee_total = self._net_equity(broker, market, mode)
        pnl_pct = self.risk.daily_pnl_pct(equity, start_equity)
        if self._last_snapshot_at is None or (now - self._last_snapshot_at).total_seconds() >= 20:
            self.storage.add_equity(mode, iso(now), day, equity, pnl_pct)
            self._last_snapshot_at = now

        positions = []
        taker = broker.fee_rates()["taker"]
        for trade in self.storage.open_trades(mode):
            try:
                price = market.get_price(trade["symbol"])
                gross = broker.position_pnl(trade)
            except Exception:
                price, gross = trade["entry_price"], 0.0
            fee_paid = trade["fee"] or 0.0                  # 진입할 때 이미 낸 수수료
            fee_exit = price * trade["qty"] * taker         # 지금 청산하면 낼 수수료(예상)
            pnl = gross - fee_paid - fee_exit               # 수수료를 모두 뺀 순손익
            context = trade_context(trade)
            split = context.get("split") or {}
            selling = self._exit_plan(trade, s)
            margin = trade["margin"] or 0.0

            def outcome(level, trade=trade, fee_paid=fee_paid):
                """그 가격에서 청산될 때의 순손익(수수료 차감 후)."""
                if not level:
                    return None
                move = (level - trade["entry_price"]) * trade["qty"] * (1.0 if trade["side"] == "long" else -1.0)
                return move - fee_paid - level * trade["qty"] * taker

            tp_net, sl_net = outcome(trade["tp_price"]), outcome(trade["sl_price"])
            held = minutes_between(parse_iso(trade["entry_time"]), now)
            positions.append({"gross_pnl": gross, "fee_paid": fee_paid, "fee_exit": fee_exit,
                              "tp_net": tp_net, "sl_net": sl_net,
                              "tp_margin_pct": tp_net / margin * 100 if tp_net is not None and margin else None,
                              "sl_margin_pct": sl_net / margin * 100 if sl_net is not None and margin else None,
                              "tpsl_mode": (context.get("tpsl") or {}).get("mode", "auto"),
                              "tpsl_edited": bool((context.get("tpsl") or {}).get("edited")),
                              "manual": bool(context.get("manual")),
                              "hold_left": max(0, int(s["max_hold_minutes"] - held)),
                              "split_done": split.get("done"), "split_total": split.get("total"),
                              "exit_done": selling["done"] if selling else None,
                              "exit_total": selling["total"] if selling else None,
                              "exit_next": self._exit_next(trade, selling),
                              "id": trade["id"], "symbol": trade["symbol"], "side": trade["side"],
                              "margin": trade["margin"], "notional": trade["entry_price"] * trade["qty"],
                              "qty": trade["qty"], "leverage": trade["leverage"],
                              "entry_price": trade["entry_price"], "price": price,
                              "sl_price": trade["sl_price"], "tp_price": trade["tp_price"],
                              "unrealized": pnl, "unrealized_pct": pnl / trade["margin"] * 100 if trade["margin"] else 0,
                              "entry_time": trade["entry_time"]})

        plan = day_row.get("plan") or {}
        if s["leverage_mode"] == "manual":
            leverage = s["manual_leverage"]
        elif positions:
            leverage = positions[-1]["leverage"]
        else:
            leverage = plan.get("recommended_leverage") or self.storage.kv_get(f"last_leverage_{mode}")

        if day_row["locked"]:
            status = day_row.get("lock_reason") or "오늘 매매가 중지되었습니다."
        elif not enabled:
            status = STATUS_OFF
        elif day_row["target_hit"] and s["stop_on_target"]:
            status = "오늘 목표 수익률을 달성해 신규 진입을 멈췄습니다."
        else:
            status = STATUS_ON
        next_decision = self._last_decision_at + timedelta(minutes=s["ai_interval_minutes"]) \
            if self._last_decision_at else now
        self._update_state(
            ready=True, error="", status=status, day=day, equity=equity, start_equity=start_equity,
            pnl_pct=pnl_pct, pnl_amount=equity - start_equity, gross_equity=gross_equity,
            invested=sum(p["margin"] or 0 for p in positions), invest_limit=start_equity * s["invest_pct"] / 100,
            open_net=sum(p["unrealized"] for p in positions),
            closed_net=(equity - start_equity) - sum(p["unrealized"] for p in positions),
            fees_today=sum(t["fee"] or 0 for t in self.storage.trades_on_day(mode, day))
            + sum(p["fee_paid"] + p["fee_exit"] for p in positions),
            trades_today=self.storage.count_trades_on_day(mode, day),
            leverage=leverage, locked=bool(day_row["locked"]), lock_reason=day_row.get("lock_reason") or "",
            target_hit=bool(day_row["target_hit"]), positions=positions,
            last_step=iso(now), next_decision=iso(next_decision))
        self._maybe_send_report(mode, day, now, s, pnl_pct)

    # ---------------------------------------------------------- 순자산
    def _net_equity(self, broker, market, mode: str) -> tuple[float, float, float]:
        """(순자산, 거래소 기준 자산, 청산 수수료 예상)을 돌려준다.

        거래소 기준 자산에는 진입 수수료만 반영되어 있다. 보유 포지션을 지금 모두 청산할 때 낼 수수료까지
        빼야 '실제로 손에 남는 금액'이 된다. 오늘 수익률, 손실 제한, 목표 달성은 모두 이 순자산으로 판단한다.
        """
        gross = broker.get_equity()
        rate = broker.fee_rates()["taker"]
        exit_fee = 0.0
        for trade in self.storage.open_trades(mode):
            try:
                price = market.get_price(trade["symbol"])
            except Exception:
                price = trade["entry_price"]
            exit_fee += price * trade["qty"] * rate
        return gross - exit_fee, gross, exit_fee

    # ---------------------------------------------------------- 날짜 전환/회고
    def _ensure_day(self, mode: str, day: str, equity: float, broker=None, market=None) -> dict:
        row = self.storage.get_day(mode, day)
        if row:
            return row
        previous = self.storage.last_day_before(mode, day)
        if previous:
            self.storage.update_day(mode, previous["day"], end_equity=equity)
            if previous["review"] is None:
                self._review_day(mode, previous, equity)
            self.storage.prune_equity(mode, iso(self.clock() - timedelta(days=3)))
            self.storage.prune_analysis(mode)
            self.storage.kv_delete(f"carry_{mode}_{previous['day']}")
        # 날짜를 넘겨 들고 가는 포지션: 지금까지의 순손익은 이미 전날 결과(=오늘 원금)에 들어 있다.
        # 그 값을 적어 두어, 오늘의 코인별 손익에는 0시 이후에 생긴 손익만 세도록 한다.
        carry = {}
        if broker is not None and market is not None:
            taker = broker.fee_rates()["taker"]
            for trade in self.storage.open_trades(mode):
                try:
                    price = market.get_price(trade["symbol"])
                    carry[str(trade["id"])] = broker.position_pnl(trade) - (trade["fee"] or 0.0) - price * trade["qty"] * taker
                except Exception:
                    continue
        if carry:
            self.storage.kv_set(f"carry_{mode}_{day}", carry)
        self.storage.create_day(mode, day, equity)
        self._last_decision_at = None
        log.info("새 거래일 시작: %s (시작 자산 %.2f USDT)", day, equity)
        return self.storage.get_day(mode, day)

    def _review_day(self, mode: str, day_row: dict, end_equity: float) -> None:
        day = day_row["day"]
        trades = self.storage.trades_on_day(mode, day)
        pnl_pct = self.risk.daily_pnl_pct(end_equity, day_row["start_equity"])
        review = None
        if trades and self.analyzer.available:
            try:
                review = self.analyzer.review(day, trades, pnl_pct, day_row.get("plan"),
                                              self.storage.recent_lessons(15))
            except Exception as exc:
                self._set_ai_error(f"회고 실패: {exc}", self.clock())
                log.warning("Gemini 회고 실패, 통계 요약으로 대체합니다: %s", exc)
        if review is None:
            review = rule_review(day, trades, pnl_pct)
        self.storage.update_day(mode, day, review=review)
        self.storage.add_lessons(mode, day, review.get("lessons", []))
        log.info("%s 회고 완료: %s", day, review.get("summary", ""))

    # ---------------------------------------------------------- 판단과 주문
    def _set_ai_error(self, message: str, at=None) -> None:
        """가장 최근 Gemini 호출의 실패 사유. 다음 호출이 성공하면 비운다."""
        self._ai_error = message
        self._ai_error_at = at if message else None

    def _upgrade_plan(self, mode, day, now, plan, macro, snapshots, lessons) -> dict:
        """규칙으로 세운 오늘의 계획을 Gemini 계획으로 바꾼다. 실패하면 있던 계획을 그대로 쓰고 다음 판단 때 다시 시도한다."""
        try:
            previous = self.storage.last_day_before(mode, day)
            fresh = self.analyzer.daily_plan(macro, snapshots, lessons, (previous or {}).get("review"))
        except Exception as exc:
            log.warning("오늘의 계획을 AI 계획으로 바꾸지 못했습니다. 다음 판단 때 다시 시도합니다: %s", exc)
            return plan
        if not isinstance(fresh, dict) or fresh.get("source") == "rule":
            return plan
        fresh["created_at"] = iso(now)
        self.storage.update_day(mode, day, plan=fresh)
        log.info("Gemini가 응답해 오늘의 계획을 AI 계획으로 바꿨습니다.")
        return fresh

    def _decide(self, mode, market, broker, now, day, day_row, replan: bool) -> None:
        s = self.settings.all()
        held_symbols = {t["symbol"] for t in self.storage.open_trades(mode)}
        snapshots = all_snapshots(market, [sym for sym in list(SYMBOLS) if sym in s["trade_symbols"] or sym in held_symbols])
        macro = self._macro()
        lessons = self.storage.recent_lessons(15)
        views = {symbol: rule_scores(snap, macro) for symbol, snap in snapshots.items()}   # 롱/숏 규칙 점수
        balance = self._direction_balance(mode, now)

        plan = day_row.get("plan")
        failed = False                       # 이번 순환에서 계획 호출이 실패했는지
        if plan is None or replan:
            plan = None
            if self.analyzer.available:
                try:
                    previous = self.storage.last_day_before(mode, day)
                    plan = self.analyzer.daily_plan(macro, snapshots, lessons, (previous or {}).get("review"))
                    self._set_ai_error("")
                except Exception as exc:
                    failed = True
                    self._set_ai_error(f"계획 수립 실패: {exc}", now)
                    log.warning("Gemini 계획 수립 실패, 규칙 기반 계획으로 대체합니다: %s", exc)
            if plan is None:
                plan = rule_plan(snapshots, macro, reason="failed" if failed else "no_key")
            plan["created_at"] = iso(now)
            self.storage.update_day(mode, day, plan=plan)

        open_trades = self.storage.open_trades(mode)
        equity = self._net_equity(broker, market, mode)[0]
        pnl_pct = self.risk.daily_pnl_pct(equity, day_row["start_equity"])
        trades_done = self.storage.count_trades_on_day(mode, day)
        history = self._history(mode)
        loss_pct = self.risk.daily_limits(day_row["start_equity"])[1]
        decisions = None
        if self.analyzer.available:
            try:
                decisions = self.analyzer.decide(macro, snapshots, plan, lessons, open_trades, {
                    "pnl_pct": pnl_pct, "trades_done": trades_done,
                    "loss_room": loss_pct + pnl_pct}, history=history, rule_views=views, balance=balance)
                self._set_ai_error("")
                for decision in decisions:      # AI 혼자 정하지 않는다. 규칙 점수와 맞춰 본다.
                    self._verify(decision, views.get(decision["symbol"]), s)
            except Exception as exc:
                decisions = None
                self._set_ai_error(f"판단 실패: {exc}", now)
                log.warning("Gemini 판단 실패, 규칙 기반 전략으로 대체합니다: %s", exc)
            if decisions is not None and not failed and (plan or {}).get("source") == "rule":
                # Gemini가 지금 응답한다. 오늘의 계획이 규칙으로 세운 것이면(키가 없었거나 호출이 실패했던 때) AI 계획으로 바꾼다.
                plan = self._upgrade_plan(mode, day, now, plan, macro, snapshots, lessons)
        if decisions is None:
            decisions = rule_decide(snapshots, macro, open_trades, s["direction_mode"])
            for decision in decisions:      # 규칙 전략도 과거 성과를 반영한다
                past = history.get(decision["symbol"], {}).get(decision["action"])
                if past and past["n"] >= 8:
                    if past["win_rate"] < 35:
                        decision["confidence"] = round(max(0.0, decision["confidence"] - 0.08), 2)
                        decision["reason"] += f" 과거 같은 방향 거래 {past['n']}회의 승률이 {past['win_rate']:.0f}%로 낮아 확신도를 낮췄습니다."
                    elif past["win_rate"] > 60:
                        decision["confidence"] = round(min(0.95, decision["confidence"] + 0.04), 2)

        threshold = self.risk.confidence_threshold(trades_done, day_fraction(now, s["timezone"]))
        can_trade = s["trading_enabled"] and not day_row["locked"]
        for decision in sorted(decisions, key=lambda d: d.get("confidence") or 0, reverse=True):
            symbol, action = decision["symbol"], decision["action"]
            open_by_symbol = {t["symbol"]: t for t in self.storage.open_trades(mode)}
            executed, note = False, ""
            try:
                if not can_trade and action != "hold":
                    note = "자동 매매가 꺼져 있거나 잠겨 있어 실행하지 않았습니다."
                elif action == "close":
                    if symbol in open_by_symbol:
                        closure = broker.close_position(open_by_symbol[symbol], "AI 판단 청산")
                        self._record_close(open_by_symbol[symbol], closure, now, day)
                        executed = True
                    else:
                        note = "보유 중인 포지션이 없습니다."
                elif action == "reduce":
                    if symbol not in open_by_symbol:
                        note = "보유 중인 포지션이 없습니다."
                    else:
                        executed, note = self._reduce_by_decision(mode, market, broker, now, day, open_by_symbol[symbol], decision, s)
                elif action == "add":
                    if symbol not in open_by_symbol:
                        note = "보유 중인 포지션이 없습니다."
                    elif split_left(open_by_symbol[symbol]) <= 0:
                        note = "분할 매수로 살 회차가 남아 있지 않습니다."
                    elif (self._exit_plan(open_by_symbol[symbol], s) or {}).get("done"):
                        note = "분할 매도를 시작한 포지션이라 추가로 사지 않습니다."
                    else:
                        note = self._add_blocked(decision, open_by_symbol[symbol], views.get(symbol), s)
                        if not note:
                            executed, note = self._add(mode, market, broker, now, day_row, open_by_symbol[symbol])
                elif action in ("long", "short"):
                    if symbol in open_by_symbol:
                        note = "이미 이 코인을 보유 중입니다."
                    elif symbol not in s["trade_symbols"]:
                        note = "설정에서 거래할 코인으로 고르지 않아 진입하지 않았습니다."
                    elif s["direction_mode"] != "auto" and action != s["direction_mode"]:
                        note = f"설정에서 {'롱' if s['direction_mode'] == 'long' else '숏'}만 거래하도록 정해 두어 실행하지 않았습니다."
                    elif symbol in self._cooldown and now < self._cooldown[symbol]:
                        note = f"직접 청산한 코인이라 {MANUAL_COOLDOWN_MINUTES}분 동안 다시 진입하지 않습니다."
                    elif decision.get("blocked"):
                        note = decision["blocked"]
                    elif (decision.get("confidence") or 0) < threshold + self._tilt(action, balance, views, s)[0]:
                        extra, why = self._tilt(action, balance, views, s)
                        note = f"확신도 {decision.get('confidence', 0):.2f}가 기준 {threshold + extra:.2f}보다 낮습니다." + (" " + why if why else "")
                    else:
                        ok, why = self.risk.can_open(
                            enabled=True, locked=bool(day_row["locked"]), target_hit=bool(day_row["target_hit"]),
                            trades_done=self.storage.count_trades_on_day(mode, day), open_count=len(open_by_symbol))
                        if ok:
                            executed, note = self._open(mode, market, broker, now, day_row, decision,
                                                        snapshots[symbol], plan)
                        else:
                            note = why
            except Exception as exc:
                note = f"주문 오류: {exc}"
                log.exception("%s 주문 처리 중 오류", symbol)
            self.storage.add_analysis(mode=mode, ts=iso(now), day=day, symbol=symbol, action=action,
                                      confidence=decision.get("confidence"), source=decision.get("source", ""),
                                      reason=decision.get("reason", ""), executed=int(executed), note=note)

    def _macro(self) -> dict:
        """글로벌 지수 요약. 받아 오는 데 시간이 걸려도 포지션 감시가 멈추지 않도록, 저장해 둔 값을 쓰고 갱신은 뒤에서 한다."""
        macro = self.macro
        if hasattr(macro, "refresh_async"):
            macro.refresh_async()
            return macro.peek() or {"indices": {}, "extra": {}, "available": False}
        return macro.fetch()

    # ---------------------------------------------------------- 상호 검증과 방향 쏠림 점검
    @staticmethod
    def _verify(decision: dict, view: dict | None, s: dict) -> None:
        """Gemini의 진입 판단을 규칙 점수와 맞춰 본다.

        - AI가 본 롱 확률과 숏 확률의 차이가 작으면 진입하지 않는다.
        - 규칙 점수가 반대 방향을 가리키거나 같은 방향의 근거가 너무 약하면 진입하지 않는다.
        - 둘이 같은 방향이면 두 값을 합친 '종합 확신도'로 진입 여부를 정한다.
        막힌 진입은 decision["blocked"]에 이유를 적어 두고, 분석 기록에 '실행하지 않음'으로 남긴다.
        """
        action = decision["action"]
        if action not in ("long", "short"):
            return
        other = "short" if action == "long" else "long"
        mine_ko, other_ko = SIDE_KO[action], SIDE_KO[other]
        p_side, p_other = decision.get(f"p_{action}"), decision.get(f"p_{other}")
        if p_side is not None and p_other is not None and p_side - p_other < PROB_MARGIN:
            decision["blocked"] = (f"AI가 본 {mine_ko} 확률 {p_side:.2f}와 {other_ko} 확률 {p_other:.2f}의 차이가 "
                                   f"{PROB_MARGIN:.2f}보다 작아 방향이 뚜렷하지 않습니다.")
            return
        if not s.get("cross_check") or view is None:
            return
        decision["scores"] = view
        ai, mine, theirs = decision.get("confidence") or 0.0, view[action], view[other]
        if mine - theirs < RULE_LEAD:
            decision["blocked"] = (f"상호 검증 불일치: AI는 {mine_ko}을 골랐지만 규칙 점수는 {mine_ko} {mine:.2f}, "
                                   f"{other_ko} {theirs:.2f}로 같은 방향을 가리키지 않습니다.")
        elif mine < RULE_FLOOR:
            decision["blocked"] = (f"상호 검증: 규칙 점수({mine_ko} {mine:.2f})가 낮아 AI의 판단을 뒷받침할 지표 근거가 부족합니다.")
        else:
            combined = round(0.6 * ai + 0.4 * mine, 2)
            decision["confidence"] = combined
            decision["reason"] += (f" [상호 검증 통과: AI 확신도 {ai:.2f}, 규칙 점수 {mine_ko} {mine:.2f} / {other_ko} {theirs:.2f}, "
                                   f"종합 확신도 {combined:.2f}]")

    @staticmethod
    def _add_blocked(decision: dict, held: dict, view: dict | None, s: dict) -> str:
        """AI가 분할 매수의 다음 회차를 사자고 할 때도 새 진입과 같은 기준으로 확인한다. 막을 이유가 있으면 그 문구."""
        if decision.get("source") != "gemini":
            return ""                      # 규칙 전략은 보유 방향 점수가 0.55 이상일 때만 추가 매수를 제안한다
        side = held["side"]
        other = "short" if side == "long" else "long"
        confidence = decision.get("confidence") or 0.0
        if confidence < 0.55:
            return f"확신도 {confidence:.2f}가 추가 매수 기준 0.55보다 낮아 사지 않았습니다."
        if s.get("cross_check") and view is not None and view[side] - view[other] < RULE_LEAD:
            return (f"상호 검증 불일치: 규칙 점수({SIDE_KO[side]} {view[side]:.2f}, {SIDE_KO[other]} {view[other]:.2f})가 "
                    f"보유 방향을 뒷받침하지 않아 추가로 사지 않았습니다.")
        return ""

    def _direction_balance(self, mode: str, now) -> dict:
        """최근 24시간 동안 진입한 방향의 횟수."""
        rows = self.storage.recent_entries(mode, iso(now - timedelta(hours=24)))
        return {"long": sum(1 for r in rows if r["side"] == "long"), "short": sum(1 for r in rows if r["side"] == "short")}

    @staticmethod
    def _tilt(action: str, balance: dict, views: dict, s: dict) -> tuple[float, str]:
        """진입이 한 방향으로 쏠려 있는데 장세가 그 방향이 아니면 진입 기준을 조금 올린다. (올릴 값, 설명)"""
        if s["direction_mode"] != "auto":
            return 0.0, ""                       # 사용자가 방향을 직접 정했으면 손대지 않는다
        total = balance.get("long", 0) + balance.get("short", 0)
        if total < 6 or balance.get(action, 0) / total < 0.8:
            return 0.0, ""
        valid = [v for v in views.values() if v]
        same = sum(1 for v in valid if v["regime"] == ("up" if action == "long" else "down"))
        if valid and same * 2 >= len(valid):
            return 0.0, ""                       # 실제로 그 방향의 장이라 쏠린 것이다
        return 0.05, (f"최근 24시간 진입이 롱 {balance.get('long', 0)}회, 숏 {balance.get('short', 0)}회로 쏠려 있고 "
                      f"지금 {'상승' if action == 'long' else '하락'} 추세인 코인이 절반이 안 되어 {SIDE_KO[action]} 진입 기준을 0.05 올렸습니다.")

    def _open(self, mode, market, broker, now, day_row, decision, snapshot, plan) -> tuple[bool, str]:
        symbol, side = decision["symbol"], decision["action"]
        if not snapshot.get("ok"):
            return False, "시세 데이터를 불러오지 못했습니다."
        instrument = market.get_instrument(symbol)
        price = market.get_price(symbol)
        fee_rate = broker.fee_rates()["taker"]
        atr_pct = snapshot["m15"]["atr_pct"]
        first_sl = decision.get("sl_pct") or max(atr_pct * 1.5, 0.3)
        ai_leverage = decision.get("leverage") or (plan or {}).get("recommended_leverage")
        settings = self.settings.all()
        tpsl = {"mode": "auto"}
        if settings["tpsl_mode"] == "manual":
            # 수동: 사용자가 '진입 투자금 기준'으로 정한 익절/손절(순손익)을 가격 폭으로 바꾼다
            planned = self.risk.planned_margin(day_row["start_equity"])
            wanted_sl = self.risk.manual_targets(planned)[1]
            if settings["leverage_mode"] == "manual" or ai_leverage:
                leverage = self.risk.pick_leverage(ai_leverage, first_sl, instrument["max_leverage"])
            else:   # 레버리지 자동이고 AI 추천이 없으면: 손절이 변동성에 맞는 가격 폭에 놓이도록 배율을 고른다
                leverage = self.risk.leverage_for_stop(wanted_sl, first_sl, fee_rate, instrument["max_leverage"])
            lowered = ""
            ceiling = self.risk.max_leverage_for_stop(wanted_sl, fee_rate)
            if leverage > ceiling:
                # 이 배율에서는 수수료만으로 손절 금액을 넘는다. 정해 둔 손실 한도를 지키는 쪽을 택해 배율을 낮춘다.
                lowered = (f"손절(투자금의 {wanted_sl:g}%)을 지키려면 {ceiling}배 이하여야 해서 "
                           f"이번 거래는 레버리지를 {leverage}배에서 {ceiling}배로 낮췄습니다.")
                leverage = ceiling
            fixed = self.risk.manual_sl_tp(leverage, fee_rate, planned, instrument["max_leverage"])
            sl_pct, tp_pct = fixed["sl_pct"], fixed["tp_pct"]
            tpsl = {"mode": "manual", "tp_margin_pct": fixed["tp_margin_pct"], "sl_margin_pct": fixed["sl_margin_pct"],
                    "note": " ".join(x for x in (lowered, fixed["note"]) if x)}
        else:
            # 자동: AI(없으면 변동성 ATR 기준)가 정한 폭을 레버리지와 수수료에 맞게 다듬는다
            leverage = self.risk.pick_leverage(ai_leverage, first_sl, instrument["max_leverage"])
            sl_pct, tp_pct = self.risk.sl_tp_pct(decision.get("sl_pct"), decision.get("tp_pct"), atr_pct, leverage, fee_rate,
                                                 instrument["max_leverage"])

        equity = self._net_equity(broker, market, mode)[0]
        pnl_pct = self.risk.daily_pnl_pct(equity, day_row["start_equity"])
        # 이미 보유한 포지션이 전부 손절될 때의 손실(미실현 손익 기준 추가 손실)을 한도에서 먼저 뺀다
        open_risk, used_margin = 0.0, 0.0
        for held in self.storage.open_trades(mode):
            used_margin += held["margin"] or 0.0
            if held["sl_price"]:
                current = market.get_price(held["symbol"])
                open_risk += abs(current - held["sl_price"]) * held["qty"] + current * held["qty"] * fee_rate
        size = self.risk.position_size(equity=equity, start_equity=day_row["start_equity"], pnl_pct=pnl_pct,
                                       price=price, leverage=leverage, sl_pct=sl_pct, fee_rate=fee_rate,
                                       instrument=instrument, open_risk=open_risk, used_margin=used_margin)
        if size["qty"] <= 0:
            return False, size["note"]

        # 매수 방식: 설정이 자동이면 AI(없으면 규칙)가 고른 방식을 따른다
        setting = self.settings.get("entry_mode")
        if setting == "auto":
            choice = decision.get("entry")
            if choice not in ("single", "split"):
                choice = "split" if (decision.get("confidence") or 0) < 0.7 or atr_pct >= 0.5 else "single"
            chooser = "AI가 고름"
        else:
            choice, chooser = setting, "설정에서 고정"
        parts = int(self.settings.get("split_count")) if choice == "split" else 1
        note, qty = size["note"], size["qty"]
        if parts > 1:
            unit = round_step(size["qty"] / parts, instrument["qty_step"])
            if unit < instrument["min_qty"] or unit * price < instrument.get("min_notional", 0):
                parts = 1
                note = (note + " " if note else "") + "금액이 작아 나눠 살 수 없어 한 번에 샀습니다."
            else:
                qty = unit
                note = (note + " " if note else "") + f"분할 매수 1/{parts}회차 ({chooser}). 나머지는 방향이 유지될 때 이어서 삽니다."
        else:
            note = (note + " " if note else "") + f"일괄 매수 ({chooser})."
        split = {"total": parts, "done": 1, "unit_qty": qty} if parts > 1 else None

        # 매도 방식: 설정이 자동이면 AI(없으면 규칙)가 고른 방식을 따른다
        selling = settings["exit_mode"]
        if selling == "auto":
            way = decision.get("exit")
            if way not in ("single", "split"):
                trending = snapshot["h1"].get("regime") != "range"
                way = "split" if trending and (decision.get("confidence") or 0) < EXIT_SINGLE_SCORE else "single"
            who = "AI가 고름" if decision.get("source") == "gemini" else "규칙 전략이 고름"
        else:
            way, who = selling, "설정에서 고정"
        exit_parts = int(settings["exit_split_count"]) if way == "split" else 1
        if exit_parts > 1:
            note += (f" 분할 매도 {exit_parts}회 ({who}): 익절가까지 가는 길을 {exit_parts}구간으로 나눠, "
                     f"한 구간을 지날 때마다 1회씩 팔고 마지막 회차는 익절가나 손절가에서 정리합니다.")
        else:
            note += f" 일괄 매도 ({who}): 익절가나 손절가에서 한 번에 정리합니다."
        if tpsl["mode"] == "manual":
            note += (f" 익절/손절 수동: 투자금의 +{tpsl['tp_margin_pct']:g}% / -{tpsl['sl_margin_pct']:g}% "
                     f"(가격으로는 +{tp_pct:.2f}% / -{sl_pct:.2f}%, {leverage}배)." + (" " + tpsl["note"] if tpsl["note"] else ""))
        else:
            source = "AI" if decision.get("sl_pct") and decision.get("source") == "gemini" else "변동성(ATR)"
            note += f" 익절/손절 자동({source} 기준): 가격 +{tp_pct:.2f}% / -{sl_pct:.2f}%."

        sl_price, tp_price = self.risk.sl_tp_prices(side, price, sl_pct, tp_pct, instrument["tick_size"])
        fill = broker.open_position(symbol, side, qty, leverage, sl_price, tp_price, instrument)
        margin = fill["entry_price"] * fill["qty"] / leverage
        self.storage.open_trade(
            mode=mode, symbol=symbol, side=side, qty=fill["qty"], leverage=leverage,
            entry_price=fill["entry_price"], sl_price=sl_price, tp_price=tp_price, entry_time=iso(now),
            margin=margin, equity_at_entry=equity,
            invest_pct=margin / day_row["start_equity"] * 100 if day_row["start_equity"] else 0,   # 오늘 원금 대비
            fee=fill["fee"], confidence=decision.get("confidence"), source=decision.get("source", ""),
            entry_reason=decision.get("reason", ""),
            context=json.dumps({"h1_trend": snapshot["h1"]["trend"], "m15_trend": snapshot["m15"]["trend"],
                                "rsi": snapshot["m15"]["rsi"], "atr_pct": atr_pct, "bb_pos": snapshot["m15"]["bb_pos"],
                                "chg_24h": snapshot.get("chg_24h"), "sl_pct": sl_pct, "tp_pct": tp_pct,
                                "regime": snapshot["h1"].get("regime"), "setup": decision.get("setup"),
                                "tpsl": {k: v for k, v in tpsl.items() if k != "note"}, "split": split,
                                "exit": {"total": exit_parts, "done": 0}}))
        self.storage.kv_set(f"last_leverage_{mode}", leverage)
        log.info("진입: %s %s x%d 수량 %s @ %.6g (손절 %.6g / 익절 %.6g)",
                 coin_meta(symbol)["ticker"], side, leverage, fill["qty"], fill["entry_price"], sl_price, tp_price)
        return True, note

    def _add(self, mode, market, broker, now, day_row, trade: dict) -> tuple[bool, str]:
        """분할 매수의 다음 회차를 산다. 평균 단가가 바뀌므로 손절/익절 가격도 다시 잡는다."""
        context = trade_context(trade)
        split = context.get("split") or {}
        symbol, side, leverage = trade["symbol"], trade["side"], trade["leverage"]
        instrument = market.get_instrument(symbol)
        price = market.get_price(symbol)
        fee_rate = broker.fee_rates()["taker"]
        sl_pct, tp_pct = context.get("sl_pct") or 1.0, context.get("tp_pct") or 2.0

        # 남은 일일 손실 한도 안에서만 추가로 산다
        equity = self._net_equity(broker, market, mode)[0]
        pnl_pct = self.risk.daily_pnl_pct(equity, day_row["start_equity"])
        open_risk, used_margin = 0.0, 0.0
        for held in self.storage.open_trades(mode):
            used_margin += held["margin"] or 0.0
            if held["sl_price"]:
                current = market.get_price(held["symbol"])
                open_risk += abs(current - held["sl_price"]) * held["qty"] + current * held["qty"] * fee_rate
        room = self.risk.position_size(equity=equity, start_equity=day_row["start_equity"], pnl_pct=pnl_pct, price=price,
                                       leverage=leverage, sl_pct=sl_pct, fee_rate=fee_rate, instrument=instrument,
                                       open_risk=open_risk, used_margin=used_margin)
        # 이 포지션에 쓸 수 있는 투자 금액(오늘 원금 x 투자율 / 포지션 수)을 넘지 않게 한다
        s = self.settings.all()
        target = day_row["start_equity"] * s["invest_pct"] / 100 / self.risk.slots()
        within = max(0.0, target - (trade["margin"] or 0.0)) * leverage / price
        qty = round_step(min(float(split.get("unit_qty", 0)), room["qty"], within), instrument["qty_step"])
        if qty < instrument["min_qty"] or qty <= 0:
            if within < instrument["min_qty"]:      # 가격이 올라 이미 한도를 채웠다. 분할 매수를 여기서 마친다.
                split["done"] = split.get("total", split.get("done", 1))
                context["split"] = split
                self.storage.update_trade(trade["id"], context=json.dumps(context))
                return False, "이 포지션의 투자 한도를 이미 채워 분할 매수를 마쳤습니다."
            return False, "남은 일일 손실 한도가 부족해 추가로 사지 않았습니다."

        estimate = (trade["entry_price"] * trade["qty"] + price * qty) / (trade["qty"] + qty)
        sl_price, tp_price = self.risk.sl_tp_prices(side, estimate, sl_pct, tp_pct, instrument["tick_size"])
        fill = broker.add_position(trade, qty, sl_price, tp_price, instrument)
        average, total = fill["avg_price"], fill["total_qty"]
        sl_price, tp_price = self.risk.sl_tp_prices(side, average, sl_pct, tp_pct, instrument["tick_size"])
        broker.set_stops(trade, sl_price, tp_price, instrument)     # 실제 평균 단가 기준으로 다시 맞춘다

        split["done"] = int(split.get("done", 1)) + 1
        context["split"] = split
        margin = average * total / leverage
        base = day_row["start_equity"]         # 오늘 원금 대비 사용 비율
        self.storage.update_trade(trade["id"], qty=total, entry_price=average, margin=margin,
                                  invest_pct=margin / base * 100 if base else 0, fee=(trade["fee"] or 0) + fill["fee"],
                                  sl_price=sl_price, tp_price=tp_price, context=json.dumps(context))
        log.info("추가 매수: %s %s %d/%d회차, 수량 %s -> %s, 평균 단가 %.6g (손절 %.6g / 익절 %.6g)",
                 coin_meta(symbol)["ticker"], side, split["done"], split["total"], trade["qty"], total, average, sl_price, tp_price)
        return True, f"분할 매수 {split['done']}/{split['total']}회차. 평균 단가가 {average:.6g}로 바뀌어 손절과 익절 가격도 다시 잡았습니다."

    # ---------------------------------------------------------- 매도 방식 (일괄 매도 / 분할 매도)
    def _exit_plan(self, trade: dict, s: dict | None = None) -> dict | None:
        """이 포지션의 분할 매도 계획 {"total": 전체 회차, "done": 판 회차}. 한 번에 정리하는 포지션이면 None.

        - 설정이 '일괄 매도'면 언제나 None (이미 일부를 판 포지션도 남은 수량은 한 번에 정리한다).
        - 진입할 때 정해 둔 계획이 있으면 그 계획.
        - 설정이 '분할 매도'인데 계획이 없는 포지션(직접 주문, 예전 버전에서 들어간 포지션)은 지금 설정의 횟수로 나눈다.
        """
        s = s or self.settings.all()
        if s["exit_mode"] == "single":
            return None
        plan = trade_context(trade).get("exit")
        try:
            total, done = int((plan or {}).get("total") or 0), int((plan or {}).get("done") or 0)
        except (TypeError, ValueError, AttributeError):
            total, done = 0, 0
        if total > 1:
            return {"total": total, "done": min(done, total)}
        if s["exit_mode"] == "split":
            return {"total": int(s["exit_split_count"]), "done": 0}
        return None

    @staticmethod
    def _exit_next(trade: dict, plan: dict | None) -> float | None:
        """분할 매도의 다음 회차를 파는 가격. 마지막 회차만 남았거나 분할 매도가 아니면 None."""
        if not plan or plan["total"] - plan["done"] <= 1 or not trade.get("tp_price"):
            return None
        entry, tp = float(trade["entry_price"]), float(trade["tp_price"])
        return entry + (tp - entry) * (plan["done"] + 1) / plan["total"]

    def _exit_stage(self, mode, market, broker, now, day, trade: dict, s: dict) -> None:
        """분할 매도의 가격 기준: 익절가까지 가는 길을 N구간으로 나눠, 다음 구간에 닿으면 한 회차를 판다."""
        plan = self._exit_plan(trade, s)
        target = self._exit_next(trade, plan)
        if target is None:
            return
        entry, tp = float(trade["entry_price"]), float(trade["tp_price"])
        if entry <= 0 or tp == entry:
            return
        price = float(market.get_price(trade["symbol"]))
        progress = (price - entry) / (tp - entry)            # 롱과 숏 모두 익절가 쪽으로 갈수록 커진다
        if progress < (plan["done"] + 1) / plan["total"]:
            return
        cost = broker.fee_rates()["taker"] * 2 * 100         # 왕복 수수료 (거래 금액 대비 %)
        if abs(price / entry - 1) * 100 < cost * 1.5:
            return                                           # 수수료를 빼면 남는 것이 거의 없는 자리에서는 팔지 않는다
        why = f"익절가까지 {min(progress, 1) * 100:.0f}% 도달"
        executed, note = self._reduce(mode, market, broker, now, day, trade, plan, why)
        name = coin_meta(trade["symbol"])["name"]
        self.storage.add_analysis(
            mode=mode, ts=iso(now), day=day, symbol=trade["symbol"], action="reduce", confidence=None, source="exit",
            reason=(f"{name}: 가격이 진입가 {entry:.6g}에서 익절가 {tp:.6g}까지 가는 길의 {min(progress, 1) * 100:.0f}% 지점"
                    f"({price:.6g})에 닿아 분할 매도의 다음 회차를 팝니다."),
            executed=int(executed), note=note)

    def _reduce_by_decision(self, mode, market, broker, now, day, trade: dict, decision: dict, s: dict) -> tuple[bool, str]:
        """AI(또는 규칙 전략)가 '일부만 지금 정리'를 고른 경우."""
        if s["exit_mode"] == "single":
            return False, "설정에서 일괄 매도로 정해 두어 나눠 팔지 않았습니다. 전부 정리할 때는 청산(close)으로 판단합니다."
        plan = self._exit_plan(trade, s)
        if plan is None:                 # 매도 방식 자동: 한 번에 정리하기로 했던 포지션을 AI가 나눠 팔기로 바꿨다
            plan = {"total": int(s["exit_split_count"]), "done": 0}
        who = "AI 판단" if decision.get("source") == "gemini" else "규칙 전략 판단"
        return self._reduce(mode, market, broker, now, day, trade, plan, who)

    def _reduce(self, mode, market, broker, now, day, trade: dict, plan: dict, why: str) -> tuple[bool, str]:
        """분할 매도 한 회차: 남은 수량을 남은 회차 수로 나눈 만큼 시장가로 판다.

        판 몫은 따로 끝난 거래로 기록하고(그 몫의 손익이 바로 확정된다), 보유 기록은 수량과 투자금을 줄인다.
        손절가와 익절가는 그대로 두므로 남은 수량은 같은 가격에서 정리된다.
        """
        left = plan["total"] - plan["done"]
        if left <= 1:
            return False, "분할 매도의 마지막 회차만 남았습니다. 남은 수량은 익절가, 손절가 또는 전량 청산으로 정리합니다."
        symbol = trade["symbol"]
        instrument = market.get_instrument(symbol)
        price = float(market.get_price(symbol))
        step, least = instrument["qty_step"], instrument["min_qty"]
        floor = instrument.get("min_notional", 0)
        total_qty = float(trade["qty"])
        part = round_step(total_qty / left, step)
        rest = round_step(total_qty - part + step * 1e-6, step)
        context = trade_context(trade)
        if part < least or rest < least or part * price < floor or rest * price < floor:
            # 수량이 작아 나눌 수 없다. 남은 회차를 하나로 합쳐 다시 시도하지 않게 한다.
            context["exit"] = {"total": plan["total"], "done": plan["total"] - 1, "merged": True}
            self.storage.update_trade(trade["id"], context=json.dumps(context))
            return False, "수량이 거래소 최소 주문 단위에 가까워 나눠 팔 수 없습니다. 남은 수량은 한 번에 정리합니다."
        share = part / total_qty
        margin, fee, invest = float(trade["margin"] or 0), float(trade["fee"] or 0), float(trade["invest_pct"] or 0)
        index = plan["done"] + 1
        reason = f"분할 매도 {index}/{plan['total']} ({why})"
        piece = {**trade, "qty": part, "margin": margin * share, "fee": fee * share, "invest_pct": invest * share}
        closure = broker.reduce_position(piece, reason, instrument)

        # 판 몫을 끝난 거래로 따로 남긴다
        piece_context = {**context, "split": None, "exit": None,
                         "part_of": trade["id"], "part_index": index, "part_total": plan["total"]}
        piece_id = self.storage.open_trade(
            mode=mode, symbol=symbol, side=trade["side"], qty=part, leverage=trade["leverage"],
            entry_price=trade["entry_price"], sl_price=trade["sl_price"], tp_price=trade["tp_price"],
            entry_time=trade["entry_time"], margin=piece["margin"], equity_at_entry=trade["equity_at_entry"],
            invest_pct=piece["invest_pct"], fee=piece["fee"], confidence=trade["confidence"], source=trade["source"],
            entry_reason=trade["entry_reason"], context=json.dumps(piece_context))
        self._record_close({**piece, "id": piece_id}, closure, now, day)

        context["exit"] = {"total": plan["total"], "done": index}
        self.storage.update_trade(trade["id"], qty=rest, margin=margin - piece["margin"], fee=fee - piece["fee"],
                                  invest_pct=invest - piece["invest_pct"], context=json.dumps(context))
        net = closure["net_pnl"]
        log.info("분할 매도: %s %s %d/%d회차, 수량 %s -> %s, 순손익 %+.4f USDT (%s)",
                 coin_meta(symbol)["ticker"], trade["side"], index, plan["total"], total_qty, rest, net, why)
        return True, (f"분할 매도 {index}/{plan['total']}회차: 수량 {part:g}을 {closure['exit_price']:.6g}에 팔아 "
                      f"순손익 {net:+.2f} USDT를 확정했습니다. 남은 수량 {rest:g}은 손절가와 익절가를 그대로 둡니다.")

    # ---------------------------------------------------------- 보유 포지션의 익절/손절 직접 수정
    def position_stops(self, trade_id: int, tp: dict | None = None, sl: dict | None = None, apply: bool = False) -> dict:
        """보유 포지션 하나의 익절/손절을 '그 포지션의 투자금(증거금) 기준'으로 다시 정한다.

        tp, sl: {"unit": "pct" | "amount", "value": 숫자}. pct는 투자금 대비 %, amount는 USDT. 둘 다 수수료를 뺀 순손익 기준.
                None이면 그쪽은 지금 걸려 있는 가격을 그대로 둔다.
        apply=False면 계산만 해서 돌려준다 (화면의 미리 보기). 고칠 수 없는 값이면 StopsError.
        """
        mode = self.settings.get("mode")
        trade = next((t for t in self.storage.open_trades(mode) if t["id"] == int(trade_id)), None)
        if trade is None:
            raise StopsError("이미 청산됐거나 없는 포지션입니다.")
        market = self.market_for(mode)
        broker = self.broker_for(mode, market)
        if broker is None:
            raise StopsError("이 모드는 Bybit API 키와 시크릿이 있어야 동작합니다.")
        symbol, side, leverage = trade["symbol"], trade["side"], int(trade["leverage"] or 1)
        entry, qty, margin, fee_paid = float(trade["entry_price"]), float(trade["qty"]), float(trade["margin"] or 0), float(trade["fee"] or 0)
        if entry <= 0 or qty <= 0 or margin <= 0:
            raise StopsError("이 포지션의 투자금 정보를 읽지 못했습니다.")
        try:
            instrument = market.get_instrument(symbol)
            price = float(market.get_price(symbol))
            taker = float(broker.fee_rates()["taker"])
        except Exception as exc:
            raise StopsError("지금 시세를 불러오지 못해 계산하지 못했습니다. 잠시 뒤 다시 시도해 주세요.") from exc
        tick, long = instrument["tick_size"], side == "long"
        ticker = coin_meta(symbol)["ticker"]

        def wanted(spec: dict, label: str) -> float:
            """입력값을 금액(USDT)으로."""
            try:
                value = float(spec.get("value"))
            except (TypeError, ValueError, AttributeError):
                raise StopsError(f"{label} 값을 숫자로 입력해 주세요.") from None
            if value != value or value in (float("inf"), float("-inf")) or value <= 0:
                raise StopsError(f"{label} 값은 0보다 커야 합니다.")
            return value if spec.get("unit") == "amount" else margin * value / 100

        def level(net: float) -> float:
            """청산했을 때 순손익(수수료 차감 후)이 net이 되는 가격."""
            if long:
                return (net + fee_paid + entry * qty) / (qty * (1 - taker))
            return (entry * qty - fee_paid - net) / (qty * (1 + taker))

        def net_at(at: float) -> float:
            return (at - entry) * qty * (1.0 if long else -1.0) - fee_paid - at * qty * taker

        tp_price, sl_price = trade["tp_price"], trade["sl_price"]
        if tp is None and not tp_price or sl is None and not sl_price:
            raise StopsError("이 포지션에는 걸려 있는 값이 없어 익절과 손절을 둘 다 입력해야 합니다.")
        if tp is not None:
            amount = wanted(tp, "익절")
            tp_price = round_step(level(amount), tick, up=long)          # 목표 수익 이상이 되도록 바깥쪽 단위로 맞춘다
            if abs(tp_price / entry - 1) * 100 > MAX_TP_MOVE_PCT or tp_price <= 0:
                most = net_at(entry * (1 + MAX_TP_MOVE_PCT / 100 * (1 if long else -1))) / margin * 100
                raise StopsError(f"익절 값이 너무 큽니다. 가격이 진입가에서 {MAX_TP_MOVE_PCT:.0f}% 넘게 움직여야 하는 값은 걸 수 없어, "
                                 f"투자금의 약 +{most:.0f}%까지 넣을 수 있습니다.")
            if (long and tp_price <= price) or (not long and tp_price >= price):
                raise StopsError(f"지금 수익이 이미 그 값을 넘었습니다 (지금 청산하면 투자금의 {net_at(price) / margin * 100:+.1f}%). "
                                 "더 큰 값을 넣거나, 바로 수익을 확정하려면 청산 버튼을 눌러 주세요.")
        if sl is not None:
            amount = wanted(sl, "손절")
            if amount / margin * 100 > MAX_SL_MARGIN_PCT + 1e-6:
                raise StopsError(f"손절은 투자금의 {MAX_SL_MARGIN_PCT:.0f}%까지만 걸 수 있습니다 (강제 청산보다 먼저 나오도록). "
                                 f"넣은 값은 투자금의 {amount / margin * 100:.1f}%입니다.")
            sl_price = round_step(level(-amount), tick, up=long)         # 정한 손실을 넘지 않도록 진입가 쪽 단위로 맞춘다
            widest = max_sl_move(leverage, instrument.get("max_leverage"))
            moved = (entry - sl_price) / entry * 100 if long else (sl_price - entry) / entry * 100
            if moved > widest + 1e-9:
                edge = entry * (1 - widest / 100) if long else entry * (1 + widest / 100)
                raise StopsError(f"{leverage}배에서는 강제 청산 가격이 가까워, 손절을 투자금의 약 {abs(net_at(edge)) / margin * 100:.0f}%까지만 걸 수 있습니다.")
            if (long and sl_price >= price) or (not long and sl_price <= price):
                raise StopsError(f"지금 손실이 이미 그 값을 넘어, 저장하면 바로 손절됩니다 (지금 청산하면 투자금의 {net_at(price) / margin * 100:+.1f}%). "
                                 "더 큰 값을 넣거나, 바로 정리하려면 청산 버튼을 눌러 주세요.")
        if (long and tp_price <= sl_price) or (not long and tp_price >= sl_price):
            raise StopsError("익절 가격과 손절 가격의 위치가 뒤바뀌었습니다. 값을 다시 확인해 주세요.")

        tp_net, sl_net = net_at(tp_price), net_at(sl_price)
        result = {
            "id": trade["id"], "symbol": symbol, "ticker": ticker, "name": coin_meta(symbol)["name"], "side": side, "leverage": leverage,
            "margin": margin, "entry_price": entry, "price": price,
            "tp_price": tp_price, "sl_price": sl_price, "tp_net": tp_net, "sl_net": sl_net,
            "tp_margin_pct": tp_net / margin * 100, "sl_margin_pct": sl_net / margin * 100,
            "tp_move_pct": (tp_price / entry - 1) * 100, "sl_move_pct": (sl_price / entry - 1) * 100,
            "changed": tp_price != trade["tp_price"] or sl_price != trade["sl_price"], "applied": False,
        }
        if not apply or not result["changed"]:
            return result

        # 엔진이 주문을 내는 중과 겹치지 않게 잠근다. AI 분석이 길어지면 화면을 붙잡지 않고 다시 누르게 한다.
        if not self._step_lock.acquire(timeout=8):
            raise StopsError("프로그램이 지금 AI 분석이나 주문을 처리하는 중이라 바로 바꾸지 못했습니다. 잠시 뒤 다시 저장해 주세요.")
        try:
            current = next((t for t in self.storage.open_trades(mode) if t["id"] == trade["id"]), None)
            if current is None:
                raise StopsError("그 사이에 포지션이 청산되었습니다.")
            if current["qty"] != trade["qty"] or current["entry_price"] != trade["entry_price"]:
                raise StopsError("그 사이에 분할 매수나 분할 매도로 수량이 바뀌었습니다. 창을 닫고 다시 열어 값을 확인해 주세요.")
            try:
                broker.set_stops(current, sl_price, tp_price, instrument, strict=True)
            except Exception as exc:
                raise StopsError(f"거래소가 변경을 받지 않았습니다: {exc}") from exc
            context = trade_context(current)
            # 분할 매수로 평균 단가가 바뀌어도 같은 가격 폭을 유지하도록, 진입가 대비 폭(%)도 함께 바꿔 둔다
            context["tp_pct"] = round(abs(tp_price / entry - 1) * 100, 4)
            context["sl_pct"] = round(max(0.01, (entry - sl_price) / entry * 100 if long else (sl_price - entry) / entry * 100), 4)
            context["tpsl"] = {**(context.get("tpsl") or {}), "mode": "manual", "edited": True,
                               "tp_margin_pct": round(result["tp_margin_pct"], 2), "sl_margin_pct": round(abs(result["sl_margin_pct"]), 2)}
            self.storage.update_trade(current["id"], sl_price=sl_price, tp_price=tp_price, context=json.dumps(context))
        finally:
            self._step_lock.release()
        log.info("익절/손절 직접 수정: %s %s 익절 %.6g (투자금의 %+.1f%%) / 손절 %.6g (투자금의 %+.1f%%)",
                 ticker, side, tp_price, result["tp_margin_pct"], sl_price, result["sl_margin_pct"])
        result["applied"] = True
        self.wake()
        return result

    # ---------------------------------------------------------- 대시보드에서 직접 넣는 주문
    def manual_order(self, symbol: str, side: str, margin, leverage=None, tp: dict | None = None, sl: dict | None = None,
                     apply: bool = False) -> dict:
        """사용자가 직접 넣는 시장가 주문.

        margin: 넣을 투자금(증거금, USDT). leverage: 배율(없으면 설정의 수동 배율).
        tp, sl: {"unit": "pct" | "amount", "value": 숫자} 투자금 기준 순손익. None이면 그쪽은 변동성(ATR) 기준으로 자동.
        apply=False면 계산만 해서 돌려준다 (주문 창의 미리 보기). 받을 수 없는 주문이면 OrderError.
        들어간 포지션은 자동 매매 포지션과 같은 규칙(익절/손절, 보유 시간 제한, 하루 청산 한도, AI 판단 청산)으로 관리한다.
        """
        s = self.settings.all()
        mode, tz = s["mode"], s["timezone"]
        symbol, side = str(symbol or "").upper(), str(side or "").lower()
        if symbol not in SYMBOLS:
            raise OrderError("목록에 없는 코인입니다. 매매 설정의 '다른 코인 추가'에서 먼저 추가해 주세요.")
        if side not in ("long", "short"):
            raise OrderError("롱과 숏 중에서 방향을 골라 주세요.")
        ticker = coin_meta(symbol)["ticker"]
        market = self.market_for(mode)
        broker = self.broker_for(mode, market)
        if broker is None:
            raise OrderError("이 모드는 Bybit API 키와 시크릿이 있어야 주문할 수 있습니다. API 연결 화면에서 넣어 주세요.")
        now = self.clock()
        day = local_day(now, tz)

        def blocked() -> None:
            if any(t["symbol"] == symbol for t in self.storage.open_trades(mode)):
                raise OrderError(f"{ticker} 포지션을 이미 보유 중입니다. 한 코인에는 포지션을 하나만 둡니다. "
                                 "더 사려면 먼저 청산하거나 다른 코인을 골라 주세요.")
            row = self.storage.get_day(mode, day)
            if row and row["locked"]:
                raise OrderError("오늘은 하루 청산 한도에 닿아 매매가 멈춰 있습니다. 내일(선택한 시간대 0시)부터 주문할 수 있습니다.")

        blocked()
        try:
            margin = float(margin)
        except (TypeError, ValueError):
            raise OrderError("투자금을 숫자로 입력해 주세요.") from None
        if margin != margin or margin in (float("inf"), float("-inf")) or margin <= 0:
            raise OrderError("투자금은 0보다 커야 합니다.")
        try:
            instrument = market.get_instrument(symbol)
            price = float(market.get_price(symbol))
            taker = float(broker.fee_rates()["taker"])
            equity = self._net_equity(broker, market, mode)[0]
        except Exception as exc:
            raise OrderError("지금 시세나 잔고를 불러오지 못했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.") from exc
        top = int(min(50, instrument.get("max_leverage") or 50))
        try:
            leverage = int(float(leverage if leverage not in (None, "") else s["manual_leverage"]))
        except (TypeError, ValueError):
            raise OrderError("레버리지를 숫자로 입력해 주세요.") from None
        if not 1 <= leverage <= top:
            raise OrderError(f"{ticker}의 레버리지는 1배부터 {top}배까지 넣을 수 있습니다.")

        used = sum(t["margin"] or 0.0 for t in self.storage.open_trades(mode))
        available = max(0.0, equity - used)
        spendable = available / (1 + taker * leverage)          # 진입 수수료를 낼 몫은 남겨 둔다
        if margin > spendable + 1e-9:
            raise OrderError(f"남은 금액보다 큰 금액입니다. 지금 {leverage}배로 넣을 수 있는 투자금은 최대 {spendable:,.2f} USDT입니다 "
                             f"(남은 금액 {available:,.2f} USDT에서 진입 수수료를 남긴 값).")
        qty = round_step(margin * leverage / price, instrument["qty_step"])
        if instrument.get("max_qty"):
            qty = min(qty, instrument["max_qty"])
        least = max(instrument["min_qty"] * price, instrument.get("min_notional", 0))
        if qty < instrument["min_qty"] or qty * price < instrument.get("min_notional", 0):
            raise OrderError(f"주문 금액이 거래소 최소 주문 단위보다 작습니다. {ticker}는 거래 금액(투자금 x 레버리지)이 "
                             f"{least:,.2f} USDT 이상이어야 해서, {leverage}배에서는 투자금을 {least / leverage * 1.02:,.2f} USDT 이상 넣어야 합니다.")
        real_margin = qty * price / leverage

        # 익절/손절: 넣지 않은 쪽은 변동성(ATR) 기준으로 자동, 넣은 쪽은 투자금 기준 순손익을 가격 폭으로 바꾼다
        cost = (taker + taker) * 100                              # 왕복 수수료 (거래 금액 대비 %)
        snapshot = coin_snapshot(market, symbol)
        atr_pct = snapshot["m15"]["atr_pct"] if snapshot.get("ok") else 0.5
        sl_move, tp_move = self.risk.sl_tp_pct(None, None, atr_pct, leverage, taker, instrument.get("max_leverage"))

        def share(spec: dict, label: str) -> float:
            """입력값을 투자금 대비 %로."""
            try:
                value = float(spec.get("value"))
            except (TypeError, ValueError, AttributeError):
                raise OrderError(f"{label} 값을 숫자로 입력해 주세요.") from None
            if value != value or value in (float("inf"), float("-inf")) or value <= 0:
                raise OrderError(f"{label} 값은 0보다 커야 합니다.")
            return value / real_margin * 100 if spec.get("unit") == "amount" else value

        if sl is not None:
            sl_share = share(sl, "손절")
            if sl_share > MAX_SL_MARGIN_PCT + 1e-6:
                raise OrderError(f"손절은 투자금의 {MAX_SL_MARGIN_PCT:.0f}%까지만 걸 수 있습니다 (강제 청산보다 먼저 나오도록).")
            sl_move = sl_share / leverage - cost
            if sl_move < MIN_SL_MOVE_PCT:
                raise OrderError(f"{leverage}배에서는 수수료만으로 투자금의 약 {cost * leverage:.1f}%가 나갑니다. "
                                 f"손절을 투자금의 {(MIN_SL_MOVE_PCT + cost) * leverage:.1f}% 이상으로 넣거나 레버리지를 낮춰 주세요.")
            widest = max_sl_move(leverage, instrument.get("max_leverage"))
            if sl_move > widest + 1e-9:
                raise OrderError(f"{leverage}배에서는 강제 청산 가격이 가까워, 손절을 투자금의 약 {(widest + cost) * leverage:.0f}%까지만 걸 수 있습니다.")
        if tp is not None:
            tp_move = share(tp, "익절") / leverage + cost
            if tp_move > MAX_TP_MOVE_PCT:
                raise OrderError(f"익절 값이 너무 큽니다. 가격이 {MAX_TP_MOVE_PCT:.0f}% 넘게 움직여야 하는 값은 걸 수 없어, "
                                 f"{leverage}배에서는 투자금의 약 +{(MAX_TP_MOVE_PCT - cost) * leverage:.0f}%까지 넣을 수 있습니다.")
        sl_price, tp_price = self.risk.sl_tp_prices(side, price, sl_move, tp_move, instrument["tick_size"])
        fee_in = qty * price * taker
        result = {
            "symbol": symbol, "ticker": ticker, "name": coin_meta(symbol)["name"], "side": side, "leverage": leverage,
            "price": price, "qty": qty, "margin": real_margin, "notional": qty * price, "fee_in": fee_in,
            "tp_price": tp_price, "sl_price": sl_price, "tp_auto": tp is None, "sl_auto": sl is None,
            "tp_margin_pct": (tp_move - cost) * leverage, "sl_margin_pct": -(sl_move + cost) * leverage,
            "tp_net": real_margin * (tp_move - cost) * leverage / 100, "sl_net": -real_margin * (sl_move + cost) * leverage / 100,
            "available": available, "spendable": spendable, "left_after": max(0.0, available - real_margin - fee_in),
            "max_leverage": top, "max_hold_minutes": s["max_hold_minutes"], "applied": False,
            "asked_margin": margin, "qty_step": instrument["qty_step"],     # 주문 단위에 맞추느라 투자금이 조금 줄 수 있다
        }
        if not apply:
            return result

        if not self._step_lock.acquire(timeout=8):
            raise OrderError("프로그램이 지금 AI 분석이나 다른 주문을 처리하는 중이라 바로 넣지 못했습니다. 잠시 뒤 다시 눌러 주세요.")
        try:
            blocked()                                           # 기다리는 사이에 같은 코인을 샀거나 하루가 잠겼는지 다시 본다
            day_row = self._ensure_day(mode, day, equity)
            try:
                fill = broker.open_position(symbol, side, qty, leverage, sl_price, tp_price, instrument)
            except Exception as exc:
                log.exception("직접 주문 실패: %s", exc)
                raise OrderError(f"주문이 들어가지 않았습니다: {exc}") from exc
            margin_in = fill["entry_price"] * fill["qty"] / leverage
            base = day_row["start_equity"]
            custom = {"mode": "manual", "edited": True} if (tp is not None or sl is not None) else {"mode": "auto"}
            self.storage.open_trade(
                mode=mode, symbol=symbol, side=side, qty=fill["qty"], leverage=leverage,
                entry_price=fill["entry_price"], sl_price=sl_price, tp_price=tp_price, entry_time=iso(now),
                margin=margin_in, equity_at_entry=equity, invest_pct=margin_in / base * 100 if base else 0,
                fee=fill["fee"], confidence=None, source="manual", entry_reason="대시보드에서 직접 넣은 주문",
                context=json.dumps({"manual": True, "atr_pct": atr_pct, "sl_pct": sl_move, "tp_pct": tp_move,
                                    "h1_trend": (snapshot.get("h1") or {}).get("trend") if snapshot.get("ok") else None,
                                    "tpsl": custom, "split": None}))
            self.storage.add_analysis(
                mode=mode, ts=iso(now), day=day, symbol=symbol, action=side, confidence=None, source="manual",
                reason="대시보드의 직접 주문 창에서 사용자가 넣은 주문입니다.", executed=1,
                note=(f"투자금 {margin_in:,.2f} USDT, {leverage}배, 수량 {fill['qty']:g}, 진입가 {fill['entry_price']:.6g}. "
                      f"익절 {tp_price:.6g} ({'자동' if tp is None else '직접 입력'}), 손절 {sl_price:.6g} ({'자동' if sl is None else '직접 입력'})."))
            result.update({"applied": True, "qty": fill["qty"], "margin": margin_in, "entry_price": fill["entry_price"]})
        finally:
            self._step_lock.release()
        log.info("직접 주문: %s %s x%d 수량 %s @ %.6g (손절 %.6g / 익절 %.6g)",
                 ticker, side, leverage, result["qty"], result["entry_price"], sl_price, tp_price)
        self.wake()
        return result

    # ---------------------------------------------------------- 과거 거래에서 배우기
    def _history(self, mode: str) -> dict:
        """최근 200개 거래를 코인, 방향별로 묶은 성과. AI 판단과 규칙 전략에 함께 쓴다."""
        groups: dict = {}
        for t in self.storage.recent_closed(mode, 200):     # 최신 거래부터
            item = groups.setdefault(t["symbol"], {}).setdefault(t["side"], {"rows": []})
            try:
                context = json.loads(t.get("context") or "{}")
            except (TypeError, ValueError):
                context = {}
            item["rows"].append((t["return_pct"] or 0, t["exit_reason"] or "", context.get("h1_trend")))
        history: dict = {}
        for symbol, sides in groups.items():
            for side, item in sides.items():
                rows = item["rows"]
                aligned = "up" if side == "long" else "down"
                with_trend = [r for r in rows if r[2] == aligned]
                against = [r for r in rows if r[2] and r[2] != aligned]
                stat = {"n": len(rows), "win_rate": sum(r[0] > 0 for r in rows) / len(rows) * 100,
                        "avg_return_pct": sum(r[0] for r in rows) / len(rows),
                        "recent": [{"return_pct": r[0], "exit_reason": r[1]} for r in rows[:3]]}
                if with_trend:
                    stat["with_trend_n"] = len(with_trend)
                    stat["with_trend_win_rate"] = sum(r[0] > 0 for r in with_trend) / len(with_trend) * 100
                if against:
                    stat["against_trend_n"] = len(against)
                    stat["against_trend_win_rate"] = sum(r[0] > 0 for r in against) / len(against) * 100
                history.setdefault(symbol, {})[side] = stat
        return history

    # ---------------------------------------------------------- 일일 보고서 메일
    def _maybe_send_report(self, mode: str, day: str, now, s: dict, pnl_pct: float) -> None:
        if not s["mail_enabled"] or self._mail_busy or to_local(now, s["timezone"]).hour < s["mail_hour"]:
            return
        last = self.storage.kv_get("mail_last") or {}
        if last.get("day") == day and last.get("ok") and last.get("trigger") == "auto":
            return                                    # 오늘 보고서는 이미 보냈다
        attempts = self.storage.kv_get("mail_attempts") or {}
        same_day = attempts.get("day") == day
        if same_day and (attempts.get("count", 0) >= 3 or now.timestamp() - attempts.get("ts", 0) < 600):
            return                                    # 실패하면 10분 간격으로 최대 3번까지만 다시 시도
        self.storage.kv_set("mail_attempts", {"day": day, "ts": now.timestamp(),
                                              "count": attempts.get("count", 0) + 1 if same_day else 1})
        self._mail_busy = True
        if self.mail_async:
            threading.Thread(target=self.send_report, kwargs={"now": now, "pnl_pct": pnl_pct, "trigger": "auto"},
                             daemon=True).start()
        else:
            self.send_report(now=now, pnl_pct=pnl_pct, trigger="auto")

    def send_report(self, now=None, pnl_pct: float | None = None, trigger: str = "manual", deliver: bool = True) -> dict:
        """보고서를 만들고(deliver=True면) 메일로 보낸다. 결과는 메일 보고서 화면에서 볼 수 있게 저장한다."""
        now = now or self.clock()
        mode = self.settings.get("mode")
        if pnl_pct is None:
            with self._state_lock:
                pnl_pct = self._state.get("pnl_pct")
        result = {"ok": False, "error": "", "trigger": trigger, "mode": mode, "at": iso(now)}
        try:
            report = build_daily_report(self.storage, self.settings, self.analyzer, mode, now, pnl_pct)
            result.update(day=report["day"], subject=report["subject"], html=report["html"], text=report["text"],
                          source=report["source"])
            if deliver:
                recipients = list(self.settings.get("mail_recipients"))
                done = self.storage.kv_get("mail_delivered") or {}
                already = done.get("to", []) if done.get("day") == report["day"] else []
                if trigger == "auto":            # 자동 발송을 다시 시도할 때 이미 받은 주소에는 또 보내지 않는다
                    recipients = [to for to in recipients if to not in already]
                results = deliver_mail(self.settings, recipients, report["subject"], report["text"],
                                       report["html"], report["fields"]) if recipients else []
                record_status(self.storage, results, iso(now))
                sent = [r["to"] for r in results if r["ok"]]
                if trigger == "auto":
                    self.storage.kv_set("mail_delivered", {"day": report["day"], "to": sorted(set(already + sent))})
                failed = [r for r in results if not r["ok"] and not r["pending"]]
                result.update(sent=len(sent), waiting=sum(1 for r in results if r["pending"]),
                              summary=summarize_results(results))
                log.info("일일 보고서 메일: %s", result["summary"])
                if failed:                        # 확인 대기(pending)는 실패로 치지 않아 반복해서 보내지 않는다
                    raise MailError(result["summary"])
            result["ok"] = True
        except MailError as exc:
            result["error"] = str(exc)
            log.warning("일일 보고서 메일 발송 실패: %s", exc)
        except Exception as exc:
            result["error"] = f"보고서 작성 중 오류: {exc}"
            log.exception("일일 보고서 작성 실패")
        finally:
            self._mail_busy = False
        result["delivered"] = bool(deliver and result["ok"])
        if deliver:
            self.storage.kv_set("mail_last", result)
        return result

    def _record_close(self, trade: dict, closure: dict, now, day: str) -> None:
        margin, base = trade["margin"], trade["equity_at_entry"]
        net = closure["net_pnl"]
        self.storage.close_trade(
            trade["id"], exit_price=closure["exit_price"], exit_time=iso(now), day=day,
            gross_pnl=closure["gross_pnl"], fee=closure["fee"], net_pnl=net,
            return_pct=net / margin * 100 if margin else 0,
            equity_return_pct=net / base * 100 if base else 0, exit_reason=closure["reason"])
        log.info("청산: %s %s 순손익 %+.4f USDT (%s)", coin_meta(trade["symbol"])["ticker"], trade["side"],
                 net, closure["reason"])

    def _close_all(self, broker, mode: str, now, day: str, reason: str) -> None:
        for trade in self.storage.open_trades(mode):
            try:
                self._record_close(trade, broker.close_position(trade, reason), now, day)
            except Exception as exc:
                log.exception("%s 청산 실패: %s", trade["symbol"], exc)

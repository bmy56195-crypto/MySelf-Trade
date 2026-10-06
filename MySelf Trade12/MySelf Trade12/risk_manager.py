"""Myself Trade - 자금/리스크 관리.

- 증거금 투자율과 레버리지로 주문 수량 계산
- 손절/익절 폭을 레버리지와 수수료에 맞게 보정
- 일일 손실 제한(-25% 등) 도달 시 당일 매매 잠금
- 하루 거래 횟수 제한
"""
from __future__ import annotations

from utils import breakeven_move_pct, clamp, round_step


MAX_SL_MARGIN_PCT = 60.0     # 손절은 투자금(증거금)의 60% 손실 안쪽에 둔다 (강제 청산보다 먼저 나오도록)
MIN_SL_MOVE_PCT = 0.1        # 손절 폭(가격 변동 %)의 최소값. 이보다 좁으면 진입하자마자 손절된다.
MAX_TP_MOVE_PCT = 50.0       # 익절 폭(가격 변동 %)의 최대값. 숏의 익절 가격이 0 아래로 내려가는 일을 막는다.


def max_sl_move(leverage: int, max_symbol_leverage: float | None = None) -> float:
    """손절까지 둘 수 있는 가장 넓은 가격 폭(%). 강제 청산 가격보다 안쪽이어야 한다.

    강제 청산까지의 거리는 대략 (100 / 레버리지 - 유지 증거금률)%다. 유지 증거금률은 코인마다 다르고,
    보통 그 코인의 최대 레버리지에서 필요한 증거금률의 절반쯤이라 50 / 최대 레버리지(%)로 어림한다.
    그 거리의 80% 안쪽, 그리고 투자금의 60% 손실 안쪽 중 더 좁은 쪽을 쓴다.
    """
    leverage = max(1, int(leverage))
    widest = MAX_SL_MARGIN_PCT / leverage
    if max_symbol_leverage and max_symbol_leverage > 0:
        widest = min(widest, 0.8 * (100.0 / leverage - 50.0 / max_symbol_leverage))
    return max(widest, MIN_SL_MOVE_PCT)


def daily_limits(s: dict, start_equity: float) -> tuple[float, float]:
    """하루 목표 수익과 허용 손실을 '오늘 원금 대비 %'로 돌려준다. (목표 %, 손실 %) 모두 양수.

    정하는 방식은 세 가지다.
      pct    : 오늘 원금 대비 %
      invest : 투자금 대비 %. 투자금 = 오늘 원금 x 증거금 투자율이므로, 원금 대비로는 (값 x 투자율 / 100)%
      amount : 금액(USDT). 오늘 원금으로 나눠 %로 바꾼다
    """
    target, loss = float(s["daily_target_pct"]), float(s["daily_loss_limit_pct"])
    rate = float(s.get("invest_pct", 100)) / 100
    if s.get("daily_target_unit") == "invest":
        target = float(s.get("daily_target_invest_pct", 50)) * rate
    if s.get("daily_loss_unit") == "invest":
        loss = min(100.0, float(s.get("daily_loss_invest_pct", 50)) * rate)
    if start_equity > 0:
        if s.get("daily_target_unit") == "amount":
            target = float(s["daily_target_amount"]) / start_equity * 100
        if s.get("daily_loss_unit") == "amount":
            loss = min(100.0, float(s["daily_loss_amount"]) / start_equity * 100)
    return target, loss


def daily_limit_text(s: dict) -> tuple[str, str]:
    """설정한 그대로의 문구: ('+30%', '-25%') 또는 ('+300 USDT', '-250 USDT').

    투자금 기준이면 원금 대비 값과 함께 적는다: ('+0.8% (투자금의 +8%)', '-0.9% (투자금의 -9%)')
    """
    target_pct, loss_pct = daily_limits(s, 0.0)
    if s.get("daily_target_unit") == "amount":
        target = f"+{float(s['daily_target_amount']):,.10g} USDT"
    elif s.get("daily_target_unit") == "invest":
        target = f"+{target_pct:.4g}% (투자금의 +{float(s.get('daily_target_invest_pct', 50)):g}%)"
    else:
        target = f"+{float(s['daily_target_pct']):g}%"
    if s.get("daily_loss_unit") == "amount":
        loss = f"-{float(s['daily_loss_amount']):,.10g} USDT"
    elif s.get("daily_loss_unit") == "invest":
        loss = f"-{loss_pct:.4g}% (투자금의 -{float(s.get('daily_loss_invest_pct', 50)):g}%)"
    else:
        loss = f"-{float(s['daily_loss_limit_pct']):g}%"
    return target, loss


def pos_limit_text(s: dict) -> str:
    """포지션(투자금 기준) 익절/손절 설정을 한 줄로."""
    if s.get("tpsl_mode") != "manual":
        return "AI 자동"
    tp = f"+{float(s['pos_tp_amount']):,.10g} USDT" if s.get("pos_tp_unit") == "amount" else f"+{float(s['pos_tp_pct']):g}%"
    sl = f"-{float(s['pos_sl_amount']):,.10g} USDT" if s.get("pos_sl_unit") == "amount" else f"-{float(s['pos_sl_pct']):g}%"
    return f"익절 {tp} / 손절 {sl}"


class RiskManager:
    def __init__(self, settings):
        self.settings = settings

    # ---------------------------------------------------------- 일일 손익
    @staticmethod
    def daily_pnl_pct(equity: float, start_equity: float) -> float:
        if start_equity <= 0:
            return 0.0
        return (equity - start_equity) / start_equity * 100

    def daily_limits(self, start_equity: float) -> tuple[float, float]:
        return daily_limits(self.settings.all(), start_equity)

    def should_lock(self, pnl_pct: float, loss_pct: float | None = None) -> bool:
        if loss_pct is None:
            loss_pct = float(self.settings.get("daily_loss_limit_pct"))
        return pnl_pct <= -loss_pct

    def target_reached(self, pnl_pct: float, target_pct: float | None = None) -> bool:
        if target_pct is None:
            target_pct = float(self.settings.get("daily_target_pct"))
        return pnl_pct >= target_pct

    # ---------------------------------------------------------- 진입 가능 여부
    def can_open(self, *, enabled: bool, locked: bool, target_hit: bool,
                 trades_done: int, open_count: int) -> tuple[bool, str]:
        s = self.settings.all()
        if not enabled:
            return False, "자동 매매가 꺼져 있습니다."
        if locked:
            return False, "일일 손실 제한에 도달해 오늘 매매가 중지되었습니다."
        if target_hit and s["stop_on_target"]:
            return False, "오늘 목표 수익률을 달성해 신규 진입을 멈췄습니다."
        if trades_done + open_count >= s["max_trades_per_day"]:
            return False, f"하루 최대 거래 횟수({s['max_trades_per_day']}회)에 도달했습니다."
        slots = self.slots()
        if open_count >= slots:
            return False, f"동시 보유 한도({slots}개)가 찼습니다."
        return True, ""

    def slots(self) -> int:
        """동시에 보유할 포지션 수. 거래할 코인을 한두 개만 골랐으면 그 수에 맞춘다."""
        s = self.settings.all()
        return max(1, min(s["max_open_positions"], len(s["trade_symbols"])))

    def confidence_threshold(self, trades_done: int, day_fraction: float) -> float:
        """진입에 필요한 확신도.

        하루 최소 거래 횟수에 못 미치는 속도라면 기준을 조금 낮춘다.
        (억지로 거래를 만들지는 않는다. 기준은 최대 0.12까지만 내려가고 0.45 아래로는 내려가지 않는다.)
        """
        base = float(self.settings.get("min_confidence"))
        minimum = int(self.settings.get("min_trades_per_day"))
        if minimum <= 0 or trades_done >= minimum:
            return base
        expected = minimum * day_fraction
        behind = max(0.0, expected - trades_done) / minimum
        return max(0.45, base - 0.12 * min(1.0, behind * 1.5))

    # ---------------------------------------------------------- 레버리지
    def pick_leverage(self, ai_leverage: float | None, sl_pct: float, max_symbol_leverage: float) -> int:
        """수동 모드면 고정값, 자동 모드면 AI 추천값(없으면 변동성 기준 계산)."""
        s = self.settings.all()
        if s["leverage_mode"] == "manual":
            leverage = s["manual_leverage"]          # 수동 고정값은 AI 추천이나 자동 범위보다 우선한다
        else:
            if ai_leverage and ai_leverage > 0:
                leverage = ai_leverage
            else:
                # 손절에 걸렸을 때 증거금의 약 8%를 잃는 수준으로 맞춘다
                leverage = 8.0 / max(sl_pct, 0.1)
            leverage = clamp(leverage, s["leverage_min"], s["leverage_max"])
        return int(max(1, min(leverage, max_symbol_leverage)))

    # ---------------------------------------------------------- 손절/익절 폭
    def sl_tp_pct(self, sl_pct: float | None, tp_pct: float | None, atr_pct: float, leverage: int,
                  fee_rate: float, max_symbol_leverage: float | None = None) -> tuple[float, float]:
        """손절/익절 폭(가격 변동 %, 레버리지 적용 전).

        - 손절은 강제 청산 가격보다 반드시 안쪽 (max_sl_move 참고)
        - 익절은 왕복 수수료의 3배 이상이어야 의미가 있다
        """
        roundtrip = breakeven_move_pct(fee_rate, fee_rate)
        sl = sl_pct if sl_pct and sl_pct > 0 else max(atr_pct * 1.5, 0.3)
        widest = min(8.0, max_sl_move(leverage, max_symbol_leverage))
        sl = clamp(sl, min(0.2, widest), widest)
        tp = tp_pct if tp_pct and tp_pct > 0 else sl * 2
        tp = clamp(tp, max(roundtrip * 3, sl * 1.2), 20.0)
        return round(sl, 3), round(tp, 3)

    def planned_margin(self, start_equity: float) -> float:
        """포지션 하나에 쓰기로 한 투자금 = 오늘 원금 x 투자율 / 동시 보유 한도."""
        return max(0.0, start_equity) * float(self.settings.get("invest_pct")) / 100 / self.slots()

    def manual_targets(self, planned_margin: float) -> tuple[float, float, list[str]]:
        """수동 설정값을 '투자금 대비 %'로 돌려준다 (익절 %, 손절 %, 안내 문구).

        금액(USDT)으로 정했으면 포지션 하나의 투자금(분할 매수는 다 샀을 때 기준)으로 나눠 %로 바꾼다.
        """
        s = self.settings.all()
        notes = []

        def as_pct(unit: str, pct: float, amount: float) -> float:
            if unit == "amount":
                return float(amount) / planned_margin * 100 if planned_margin > 0 else float(pct)
            return float(pct)

        tp_margin = as_pct(s["pos_tp_unit"], s["pos_tp_pct"], s["pos_tp_amount"])
        sl_margin = as_pct(s["pos_sl_unit"], s["pos_sl_pct"], s["pos_sl_amount"])
        if sl_margin > MAX_SL_MARGIN_PCT:
            notes.append(f"손절을 투자금의 {sl_margin:.0f}%로 정했지만 강제 청산보다 먼저 나오도록 {MAX_SL_MARGIN_PCT:.0f}%로 낮췄습니다.")
            sl_margin = MAX_SL_MARGIN_PCT
        return tp_margin, sl_margin, notes

    @staticmethod
    def max_leverage_for_stop(sl_margin_pct: float, fee_rate: float) -> int:
        """정해 둔 손절(투자금의 sl%)을 지킬 수 있는 가장 높은 레버리지.

        레버리지가 높을수록 수수료가 투자금에서 차지하는 몫이 커진다. 이 배율을 넘으면 수수료만으로도 손절 금액을 넘는다.
        """
        cost = breakeven_move_pct(fee_rate, fee_rate)
        return max(1, int(sl_margin_pct / (cost + MIN_SL_MOVE_PCT)))

    def manual_sl_tp(self, leverage: int, fee_rate: float, planned_margin: float,
                     max_symbol_leverage: float | None = None) -> dict:
        """수동 모드: '진입 투자금(증거금) 기준' 익절/손절을 가격 변동 폭(%)으로 바꾼다.

        사용자가 정한 값은 수수료를 모두 뺀 순손익 기준이다.
          익절: 투자금의 tp% 순수익 -> 가격이 (tp / 레버리지 + 왕복 수수료)% 유리하게 움직일 때
          손절: 투자금의 sl% 순손실 -> 가격이 (sl / 레버리지 - 왕복 수수료)% 불리하게 움직일 때
        돌려주는 tp_margin_pct, sl_margin_pct는 제한을 적용한 뒤 실제로 걸리는 값이다.
        """
        leverage = max(1, int(leverage))
        tp_margin, sl_margin, notes = self.manual_targets(planned_margin)
        cost = breakeven_move_pct(fee_rate, fee_rate)          # 왕복 수수료(거래 금액 대비 %)
        sl = sl_margin / leverage - cost
        widest = max_sl_move(leverage, max_symbol_leverage)
        if sl > widest:        # 이 코인의 최대 레버리지에 가까우면 강제 청산이 더 가깝다
            sl = widest
            notes.append(f"{leverage}배에서는 강제 청산이 가까워 손절을 투자금의 {(sl + cost) * leverage:.0f}%로 좁혔습니다.")
        if sl < MIN_SL_MOVE_PCT:
            sl = MIN_SL_MOVE_PCT
            notes.append(f"{leverage}배에서는 수수료만으로 정해 둔 손절 금액을 넘습니다. 가장 좁은 폭으로 잡았고, "
                         f"손절되면 투자금의 약 {(sl + cost) * leverage:.1f}%를 잃습니다.")
        tp = tp_margin / leverage + cost
        if tp > MAX_TP_MOVE_PCT:
            tp = MAX_TP_MOVE_PCT
            notes.append(f"익절 목표가 가격이 {MAX_TP_MOVE_PCT:.0f}% 넘게 움직여야 하는 값이라 {MAX_TP_MOVE_PCT:.0f}%까지만 걸었습니다 "
                         f"(투자금의 약 {(tp - cost) * leverage:.0f}%).")
        return {"sl_pct": round(sl, 4), "tp_pct": round(tp, 4), "sl_margin_pct": round((sl + cost) * leverage, 2),
                "tp_margin_pct": round((tp - cost) * leverage, 2), "note": " ".join(notes)}

    def leverage_for_stop(self, sl_margin_pct: float, wanted_move_pct: float, fee_rate: float, max_symbol_leverage: float) -> int:
        """수동 손절(투자금의 sl%)이 '변동성에 맞는 가격 폭'에 놓이도록 하는 레버리지.

        레버리지 자동 + 익절/손절 수동이고 AI가 배율을 추천하지 않았을 때 쓴다.
        손절 금액은 사용자가 고정했으므로 레버리지는 손절까지의 거리만 바꾼다.
        """
        s = self.settings.all()
        cost = breakeven_move_pct(fee_rate, fee_rate)
        leverage = min(sl_margin_pct, MAX_SL_MARGIN_PCT) / max(wanted_move_pct + cost, 0.1)
        leverage = clamp(leverage, s["leverage_min"], s["leverage_max"])
        return int(max(1, min(leverage, max_symbol_leverage)))

    @staticmethod
    def sl_tp_prices(side: str, price: float, sl_pct: float, tp_pct: float, tick: float) -> tuple[float, float]:
        if side == "long":
            return round_step(price * (1 - sl_pct / 100), tick), round_step(price * (1 + tp_pct / 100), tick)
        return round_step(price * (1 + sl_pct / 100), tick, up=True), round_step(price * (1 - tp_pct / 100), tick, up=True)

    # ---------------------------------------------------------- 주문 수량
    def position_size(self, *, equity: float, start_equity: float, pnl_pct: float, price: float,
                      leverage: int, sl_pct: float, fee_rate: float, instrument: dict,
                      open_risk: float = 0.0, used_margin: float = 0.0, loss_limit_pct: float | None = None) -> dict:
        """주문 수량과 증거금.

        증거금 = 오늘 원금 x 증거금 투자율 / 동시 보유 한도
          - 오늘 원금(start_equity)은 그날 0시의 총자산이다. 하루 중에 수익이나 손실이 나도 투자 금액은 바뀌지 않고,
            다음 날에는 그 결과 금액이 새 원금이 된다.
          - 모든 포지션의 증거금 합계는 투자 한도(오늘 원금 x 투자율)를 넘지 않는다. used_margin은 이미 쓰고 있는 증거금.
          - 실제로 남아 있는 자산(총자산 - 이미 쓰고 있는 증거금)보다 많이 쓸 수도 없다.
        거래 금액 = 증거금 x 레버리지
        한 번의 손절로 '남은 일일 손실 한도'의 절반 이상을 잃지 않도록 필요하면 수량을 줄인다.
        open_risk: 이미 보유 중인 포지션들이 모두 손절될 때의 예상 손실(USDT). 남은 한도에서 먼저 뺀다.
        """
        s = self.settings.all()
        if equity <= 0 or price <= 0:
            return {"qty": 0.0, "margin": 0.0, "note": "자산 또는 가격 정보가 없습니다."}
        principal = start_equity if start_equity > 0 else equity
        limit = principal * s["invest_pct"] / 100          # 투자 한도: 모든 포지션의 증거금 합계가 넘으면 안 되는 금액
        margin = limit / self.slots()
        note = ""
        # 1) 이미 들어가 있는 투자금과 합쳐서 투자 한도를 넘지 않게 한다 (설정을 중간에 바꿔도 마찬가지)
        limit_left = limit - used_margin
        if limit_left <= 0:
            return {"qty": 0.0, "margin": 0.0,
                    "note": f"투자 한도({limit:,.2f} USDT)를 이미 모두 쓰고 있어 새로 사지 않았습니다."}
        if margin > limit_left:
            margin = limit_left
            note = "투자 한도(오늘 원금 x 투자율)를 넘지 않도록 투자 금액을 줄였습니다."
        # 2) 실제로 남아 있는 자산보다 많이 쓰지 않는다
        available = max(0.0, equity - used_margin) / (1 + fee_rate * leverage)    # 진입 수수료를 낼 몫은 남겨 둔다
        if margin > available:
            margin = available
            note = "손실로 줄어든 자산에 맞춰 투자 금액을 줄였습니다."
        notional = margin * leverage

        if loss_limit_pct is None:
            loss_limit_pct = daily_limits(s, start_equity)[1]
        remaining = (loss_limit_pct + pnl_pct) / 100 * start_equity - open_risk
        if remaining <= 0:
            return {"qty": 0.0, "margin": 0.0, "note": "일일 손실 한도가 남아 있지 않습니다."}
        loss_per_unit = sl_pct / 100 + fee_rate * 2
        max_notional = (remaining * 0.5) / loss_per_unit
        if notional > max_notional:
            notional = max_notional
            note = (note + " " if note else "") + "남은 일일 손실 한도에 맞춰 수량을 줄였습니다."

        qty = round_step(notional / price, instrument["qty_step"])
        if instrument.get("max_qty"):
            qty = min(qty, instrument["max_qty"])
        if qty < instrument["min_qty"] or qty * price < instrument.get("min_notional", 0):
            return {"qty": 0.0, "margin": 0.0,
                    "note": "주문 금액이 거래소 최소 주문 단위보다 작습니다. 증거금 투자율이나 레버리지를 올려야 합니다."}
        return {"qty": qty, "margin": qty * price / leverage, "note": note}

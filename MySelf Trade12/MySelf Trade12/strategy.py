"""Myself Trade - 규칙 기반 전략.

Gemini API 키가 없거나 AI 호출이 실패했을 때 대신 쓰는 판단 로직이다.
롱과 숏을 같은 기준(거울상)으로 채점해서 한쪽으로 치우치지 않게 한다.
  - 추세장: 1시간봉 추세 방향으로, 15분봉 지표가 같은 방향을 가리킬 때 진입 (상승이면 롱, 하락이면 숏)
  - 박스권: 박스 상단에서 숏, 하단에서 롱 (짧게)
  - 두 방향의 점수 차이가 작으면 관망
"""
from __future__ import annotations

from config import SYMBOLS, coin_meta
from utils import split_left, trade_context


MARGIN = 0.10        # 롱 점수와 숏 점수의 차이가 이보다 작으면 방향이 불분명하다고 보고 관망한다
ENTRY_SCORE = 0.5    # 규칙 전략이 진입을 제안하는 최소 점수
RANGE_TREND_CAP = 0.45   # 박스권일 때 추세 추종 점수의 상한 (진입 최소 점수보다 낮다)
TREND_KO = {"up": "상승", "down": "하락", "side": "횡보"}
REGIME_KO = {"up": "상승 추세", "down": "하락 추세", "range": "박스권"}


def _trend_score(up: bool, m15: dict, h1: dict, d1: dict, macro: dict) -> tuple[float, list[str]]:
    """추세를 따라가는 진입 점수. 롱과 숏을 완전히 같은 기준(거울상)으로 채점한다."""
    with_, against = ("up", "down") if up else ("down", "up")
    word = "상승" if up else "하락"
    score, why = 0.25, []
    if h1["trend"] == with_:
        score += 0.22
        why.append(f"1시간봉 {word} 추세(이동평균 정배열)" if up else f"1시간봉 {word} 추세(이동평균 역배열)")
    elif h1["trend"] == against:
        score -= 0.15
    if h1.get("regime") == with_ and h1["trend"] != with_:
        score += 0.10                      # 이동평균이 아직 완전히 돌아서지 않았지만 가격은 이미 그 방향으로 가고 있다
        why.append(f"1시간봉 {word} 전환 초기")
    elif h1.get("regime") == against:
        score -= 0.05
    slope = h1.get("slope_pct", 0.0)
    if (slope > 0.02) if up else (slope < -0.02):
        score += 0.05
        why.append("추세선 기울기 " + ("상승" if up else "하락"))
    if (m15["price"] > m15["ema20"]) == up:
        score += 0.10
        why.append("15분봉 가격이 20 이동평균 " + ("위" if up else "아래"))
    hist_side = (m15["macd_hist"] > 0) == up
    hist_move = m15["macd_rising"] == up
    if hist_side and hist_move:
        score += 0.12
        why.append("MACD " + word + " 방향으로 확대")
    elif hist_move:
        score += 0.04
    shape = m15.get("structure", "mixed")
    if shape == with_:
        score += 0.06
        why.append("고점과 저점이 함께 " + ("높아짐" if up else "낮아짐"))
    elif shape == against:
        score -= 0.05
    rsi = m15["rsi"]
    if (45 <= rsi <= 68) if up else (32 <= rsi <= 55):
        score += 0.07
        why.append(f"RSI {rsi:.0f}")
    if (rsi > 75 or m15["bb_pos"] > 1.0) if up else (rsi < 25 or m15["bb_pos"] < 0.0):
        score -= 0.15
        why.append("과열 구간이라 감점" if up else "과매도 구간이라 감점")
    if d1.get("ok"):
        if d1["trend"] == with_:
            score += 0.07
            why.append("일봉도 같은 방향")
        elif d1["trend"] == against:
            score -= 0.04
    if m15["vol_ratio"] > 1.3 and (m15["chg_4"] > 0) == up and m15["chg_4"] != 0:
        score += 0.04
        why.append("거래량이 붙은 " + ("상승" if up else "하락"))
    mood = macro.get("mood")
    if mood == ("risk_on" if up else "risk_off"):
        score += 0.05
        why.append("미국 증시 분위기 " + ("우호적" if up else "약세"))
    elif mood == ("risk_off" if up else "risk_on"):
        score -= 0.05
    score = max(0.0, min(0.95, score))
    if h1.get("regime") == "range":
        # 박스권에서는 추세를 따라가는 진입이 자주 속는다. 여기서는 박스 상단 숏 / 하단 롱(_range_score)만 진입 후보가 된다.
        score = min(score, RANGE_TREND_CAP)
    return score, why


def _range_score(up: bool, m15: dict, h1: dict) -> tuple[float, list[str]]:
    """박스권 진입 점수. 박스 상단에서는 숏, 하단에서는 롱 (되돌림을 노린다)."""
    if h1.get("regime") != "range" or h1["trend"] != "side":
        return 0.0, []
    pos, rsi = m15["bb_pos"], m15["rsi"]
    edge = pos <= 0.15 if up else pos >= 0.85            # 볼린저 밴드 하단 / 상단 근처
    stretched = rsi <= 40 if up else rsi >= 60
    if not (edge and stretched):
        return 0.0, []
    score = 0.25 + 0.27
    why = ["1시간봉 박스권", "15분봉 가격이 박스 " + ("하단" if up else "상단") + f" (RSI {rsi:.0f})"]
    if m15["macd_rising"] == up:                         # 밀던 힘이 줄어들고 있다
        score += 0.10
        why.append("MACD가 " + ("반등" if up else "꺾임"))
    width = h1["high_20"] - h1["low_20"]
    if width > 0:
        place = (m15["price"] - h1["low_20"]) / width    # 1시간봉 박스 안에서의 위치 (0=바닥, 1=천장)
        if (place <= 0.25) if up else (place >= 0.75):
            score += 0.08
            why.append("1시간봉 박스의 " + ("바닥권" if up else "천장권"))
    if (rsi < 22) if up else (rsi > 78):                 # 박스를 깨고 나가는 중일 수 있다
        score -= 0.12
    return max(0.0, min(0.9, score)), why


def _score(direction: str, m15: dict, h1: dict, d1: dict, macro: dict) -> tuple[float, list[str], str]:
    up = direction == "long"
    trend, trend_why = _trend_score(up, m15, h1, d1, macro)
    box, box_why = _range_score(up, m15, h1)
    if box > trend:
        return box, box_why, "range"
    return trend, trend_why, "trend"


def rule_scores(snap: dict, macro: dict) -> dict | None:
    """한 코인의 롱/숏 점수. Gemini 판단과 맞춰 보는 상호 검증에도 쓴다."""
    if not snap.get("ok"):
        return None
    m15, h1, d1 = snap["m15"], snap["h1"], snap.get("d1", {})
    long_score, long_why, long_setup = _score("long", m15, h1, d1, macro)
    short_score, short_why, short_setup = _score("short", m15, h1, d1, macro)
    return {"long": round(long_score, 2), "short": round(short_score, 2), "regime": h1.get("regime", "range"),
            "long_why": long_why, "short_why": short_why, "long_setup": long_setup, "short_setup": short_setup}


EXIT_SINGLE_SCORE = 0.72      # 규칙 점수가 이 값 이상이면 익절가까지 한 번에 가져간다 (일괄 매도)


def rule_decide(snapshots: dict, macro: dict, open_trades: list[dict], direction: str = "auto") -> list[dict]:
    """direction: auto(롱/숏 중 점수가 뚜렷하게 높은 쪽) | long(롱만 검토) | short(숏만 검토)"""
    open_by_symbol = {t["symbol"]: t for t in open_trades}
    decisions = []
    for symbol, snap in snapshots.items():
        name = coin_meta(symbol)["name"]
        view = rule_scores(snap, macro)
        if view is None:
            decisions.append({"symbol": symbol, "action": "hold", "confidence": 0.0, "source": "rule",
                              "reason": f"{name} 시세 데이터를 불러오지 못해 관망합니다."})
            continue
        m15, h1 = snap["m15"], snap["h1"]
        long_score, short_score = view["long"], view["short"]
        both = f"롱 {long_score:.2f} / 숏 {short_score:.2f}, {REGIME_KO[view['regime']]}"
        if direction == "long":
            side = "long"
        elif direction == "short":
            side = "short"
        elif abs(long_score - short_score) < MARGIN:
            side = None                    # 어느 쪽도 뚜렷하지 않다. 예전처럼 동점일 때 롱을 고르지 않는다.
        else:
            side = "long" if long_score > short_score else "short"
        score = view[side] if side else max(long_score, short_score)
        why = view[f"{side}_why"] if side else []
        setup = view[f"{side}_setup"] if side else "trend"

        held = open_by_symbol.get(symbol)
        if held:
            against = (held["side"] == "long" and h1["trend"] == "down" and m15["macd_hist"] < 0) or \
                      (held["side"] == "short" and h1["trend"] == "up" and m15["macd_hist"] > 0)
            if against:
                decisions.append({"symbol": symbol, "action": "close", "confidence": 0.7, "source": "rule",
                                  "reason": f"{name}: 보유 방향과 반대로 추세가 바뀌어 정리합니다."})
            else:
                same = long_score if held["side"] == "long" else short_score
                left = split_left(held)
                plan = trade_context(held).get("exit") or {}
                sell_left = int(plan.get("total") or 0) - int(plan.get("done") or 0)
                move = ((snap.get("price") or m15["price"]) / held["entry_price"] - 1) * 100 * (1 if held["side"] == "long" else -1)
                if sell_left > 1 and move >= 0.3 and same < 0.45:
                    # 분할 매도 중이고 수익이 난 상태에서 보유 방향의 힘이 약해졌다. 다음 회차를 먼저 판다.
                    decisions.append({"symbol": symbol, "action": "reduce", "confidence": round(1 - same, 2), "source": "rule",
                                      "reason": f"{name}: 수익이 난 상태({move:+.2f}%)에서 보유 방향 점수가 {same:.2f}로 약해져 "
                                                f"분할 매도 {int(plan.get('done') or 0) + 1}/{int(plan['total'])}회차를 먼저 팝니다."})
                elif left > 0 and same >= 0.55 and not int(plan.get("done") or 0):      # 분할 매수 중이고 방향이 유지되면 다음 회차를 산다
                    info = trade_context(held)["split"]
                    decisions.append({"symbol": symbol, "action": "add", "confidence": round(same, 2), "source": "rule",
                                      "reason": f"{name}: 분할 매수 {info['done'] + 1}/{info['total']}회차. "
                                                f"보유 방향의 근거가 유지되어 추가로 삽니다."})
                else:
                    decisions.append({"symbol": symbol, "action": "hold", "confidence": round(same, 2), "source": "rule",
                                      "reason": f"{name}: 포지션 유지. " + ("추가 매수는 방향이 더 뚜렷해질 때까지 기다립니다."
                                                                           if left > 0 else "추세가 아직 유효합니다.")})
            continue

        if side and score >= ENTRY_SCORE:
            atr = m15["atr_pct"]
            box = setup == "range"
            decisions.append({
                "symbol": symbol, "action": side, "confidence": round(score, 2), "source": "rule", "scores": view,
                "setup": setup,
                # 확신이 아주 높지 않거나 변동성이 크면 나눠서 산다. 박스권 되돌림은 자리가 중요해 한 번에 산다.
                "entry": "single" if box else ("split" if score < 0.7 or atr >= 0.5 else "single"),
                # 매도 방식: 박스권의 짧은 목표와 근거가 강한 추세는 익절가까지 한 번에 가져가고,
                # 근거가 보통인 추세는 나눠 팔아 수익을 차례로 확정한다
                "exit": "single" if box or score >= EXIT_SINGLE_SCORE else "split",
                # 박스권에서는 목표를 짧게(박스 중간까지), 추세에서는 길게 잡는다
                "sl_pct": round(max(atr * (1.2 if box else 1.5), 0.3), 3),
                "tp_pct": round(max(atr * (2.0 if box else 3.0), 0.6), 3),
                "reason": f"{name} {'롱(매수)' if side == 'long' else '숏(매도)'}: " + ", ".join(why) + f". ({both})",
            })
        else:
            if side is None:
                text = f"{name}: 롱과 숏의 근거가 비슷해 관망합니다 ({both})."
            else:
                text = (f"{name}: 진입 근거가 부족해 관망합니다 ({both}, "
                        f"1시간봉 {TREND_KO[h1['trend']]}, RSI {m15['rsi']:.0f}).")
            decisions.append({"symbol": symbol, "action": "hold", "confidence": round(score, 2), "source": "rule",
                              "scores": view, "reason": text})
    return decisions


def rule_plan(snapshots: dict, macro: dict, reason: str = "no_key") -> dict:
    """AI 없이 만드는 간단한 오늘의 계획.

    reason: 규칙으로 세운 까닭. no_key(Gemini 키 없음) | failed(키는 있지만 이번 호출이 실패)
    """
    caution = ("Gemini 호출이 실패해 이번 계획은 규칙 기반 전략으로 세웠습니다. Gemini가 다시 응답하면 자동으로 AI 계획으로 바꿉니다."
               if reason == "failed" else "Gemini API 키가 없어 규칙 기반 전략으로 동작 중입니다.")
    coins, ups, downs = {}, 0, 0
    for symbol, snap in snapshots.items():
        if not snap.get("ok"):
            coins[symbol] = {"bias": "neutral", "note": "데이터 없음"}
            continue
        trend = snap["h1"]["trend"]
        regime = snap["h1"].get("regime", "range")
        ups += trend == "up"
        downs += trend == "down"
        coins[symbol] = {"bias": {"up": "long", "down": "short"}.get(trend, "neutral"),
                         "note": f"1시간봉 {TREND_KO[trend]} ({REGIME_KO[regime]}), 24시간 {snap.get('chg_24h', 0):+.2f}%"}
    bias = "bullish" if ups > downs else "bearish" if downs > ups else "neutral"
    if macro.get("available"):
        us = ", ".join(f"{name} {v['chg_1d']:+.2f}%" for name, v in macro["indices"].items())
    else:
        us = "미국 증시 데이터를 불러오지 못했습니다."
    return {
        "source": "rule",
        "market_bias": bias,
        "us_market_summary": us,
        "crypto_summary": f"{len(snapshots)}개 코인 중 상승 추세 {ups}개, 하락 추세 {downs}개.",
        "recommended_leverage": None,
        "risk_level": "medium",
        "coins": coins,
        "strategy": "상승 추세에서는 롱, 하락 추세에서는 숏으로 진입하고, 박스권에서는 상단 숏과 하단 롱만 짧게 노립니다. "
                    "롱과 숏의 근거가 비슷하면 쉬어 갑니다.",
        "cautions": [caution],
        "fallback": reason == "failed",
    }


def rule_review(day: str, trades: list[dict], pnl_pct: float) -> dict:
    """AI 없이 만드는 하루 회고 (통계 요약)."""
    wins = [t for t in trades if (t["net_pnl"] or 0) > 0]
    losses = [t for t in trades if (t["net_pnl"] or 0) <= 0]
    lessons = []
    if trades:
        fee_total = sum(t["fee"] or 0 for t in trades)
        gross_total = sum(t["gross_pnl"] or 0 for t in trades)
        if gross_total > 0 and fee_total > gross_total * 0.3:
            lessons.append("수수료가 수익의 30%를 넘었다. 거래 횟수를 줄이거나 익절 폭을 넓힌다.")
        by_symbol: dict[str, float] = {}
        for t in trades:
            by_symbol[t["symbol"]] = by_symbol.get(t["symbol"], 0) + (t["net_pnl"] or 0)
        worst = min(by_symbol, key=by_symbol.get)
        if by_symbol[worst] < 0:
            lessons.append(f"{coin_meta(worst)['name']}에서 손실이 가장 컸다. 다음 날 이 코인은 진입 기준을 높인다.")
        if len(losses) > len(wins):
            lessons.append("손실 거래가 더 많았다. 추세가 뚜렷하지 않을 때는 관망한다.")
    return {
        "source": "rule",
        "summary": f"{day}: 거래 {len(trades)}회 (수익 {len(wins)}, 손실 {len(losses)}), 하루 수익률 {pnl_pct:+.2f}%.",
        "what_worked": [],
        "what_failed": [],
        "lessons": lessons,
        "next_day_plan": "1시간봉 추세 방향 진입 원칙을 유지한다.",
    }

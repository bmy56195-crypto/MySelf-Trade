"""Myself Trade 간단 버전 - autotrade.py

파일 하나로 도는 Bybit + Gemini 자동매매.
  1) .env에서 API 키를 불러온다
  2) Bybit에서 현재 가격, 최근 흐름, 계좌 잔고를 조회한다
  3) 그 데이터를 Gemini에 보내 [매수 / 매도 / 관망] 판단과 이유를 받는다
  4) 판단에 따라 Bybit에 주문한다 (TEST_MODE = True 이면 주문하지 않고 화면에만 출력)
  5) 정해진 주기(기본 1시간)마다 반복한다

실행:
  python autotrade.py          -> 바로 한 번 실행한 뒤, 주기마다 반복
  python autotrade.py --once   -> 한 번만 실행하고 끝냄 (처음 확인할 때 추천)
"""
import csv
import json
import os
import sys
import time
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

import schedule
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pybit.unified_trading import HTTP

# ======================================================================
#  설정 (여기만 바꾸면 됩니다)
# ======================================================================
TEST_MODE = True            # True: 실제 주문을 넣지 않고 화면에 출력만 함 (가상 매매)
                            # False: 실제 주문이 나감. 충분히 지켜본 뒤에만 바꾸세요.

MARKET_TYPE = "linear"      # "linear" = USDT 무기한 선물 / "spot" = 현물
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]

ORDER_USDT = 30             # 한 번 주문에 쓸 금액(USDT). 선물에서는 증거금으로 쓰인다.
LEVERAGE = 5                # 선물 레버리지 (현물에서는 무시)
STOP_LOSS_PCT = 1.5         # 손절 폭(%) - 선물 주문에 함께 걸린다
TAKE_PROFIT_PCT = 3.0       # 익절 폭(%)
MIN_CONFIDENCE = 0.6        # Gemini의 확신도가 이 값보다 낮으면 주문하지 않는다 (0~1)

INTERVAL_MINUTES = 60       # 실행 주기(분). 60 = 1시간마다
GEMINI_MODEL = "gemini-3.8-flash"
USE_DEMO_ACCOUNT = False    # True: Bybit '데모 트레이딩' 계정의 API 키를 쓸 때
TEST_BALANCE = 1000.0       # TEST_MODE에서 API 키가 없을 때 가정할 잔고(USDT)
LOG_FILE = "autotrade_log.csv"

# ======================================================================
#  1. 보안 키 불러오기 (.env)
# ======================================================================
load_dotenv()
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_SECRET_KEY = os.getenv("BYBIT_SECRET_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
HAS_BYBIT_KEYS = bool(BYBIT_API_KEY and BYBIT_SECRET_KEY)

if HAS_BYBIT_KEYS:
    bybit = HTTP(testnet=False, demo=USE_DEMO_ACCOUNT, api_key=BYBIT_API_KEY, api_secret=BYBIT_SECRET_KEY)
else:
    bybit = HTTP(testnet=False)   # 키가 없어도 가격 조회는 가능
gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}")


def floor_to_step(value, step):
    """거래소가 허용하는 단위(step)에 맞춰 내림한 문자열."""
    step = Decimal(str(step))
    units = (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN)
    return format((units * step).normalize(), "f")


# ======================================================================
#  2. Bybit 데이터 조회 (현재 가격, 최근 흐름, 잔고, 보유 포지션)
# ======================================================================
def get_market_data(symbol):
    """현재 가격과 최근 48시간(1시간봉) 흐름을 요약한다."""
    ticker = bybit.get_tickers(category=MARKET_TYPE, symbol=symbol)["result"]["list"][0]
    rows = bybit.get_kline(category=MARKET_TYPE, symbol=symbol, interval="60", limit=48)["result"]["list"]
    closes = [float(row[4]) for row in reversed(rows)]      # 과거 -> 최신 순서로 뒤집는다
    price = float(ticker["lastPrice"])

    # RSI(14): 최근 상승폭과 하락폭의 비율. 70 이상이면 과열, 30 이하면 침체로 본다.
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(len(closes) - 14, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(len(closes) - 14, len(closes))]
    avg_gain, avg_loss = sum(gains) / 14, sum(losses) / 14
    rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    return {
        "symbol": symbol,
        "price": price,
        "change_24h_pct": round(float(ticker["price24hPcnt"]) * 100, 2),
        "ma20": round(sum(closes[-20:]) / 20, 6),             # 20시간 이동평균
        "rsi14": round(rsi, 1),
        "high_48h": max(closes),
        "low_48h": min(closes),
        "recent_closes_1h": closes[-12:],                     # 최근 12시간 종가
    }


def get_balance():
    """계좌의 USDT 기준 총자산."""
    if not HAS_BYBIT_KEYS:
        return TEST_BALANCE
    account = bybit.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]
    total = account.get("totalEquity") or "0"
    return float(total)


def get_position(symbol):
    """보유 중인 수량. 선물: {"side": "Buy"|"Sell", "size": 수량}, 현물: {"side": "Buy", "size": 코인 수량}, 없으면 None."""
    if not HAS_BYBIT_KEYS:
        return None
    if MARKET_TYPE == "linear":
        for row in bybit.get_positions(category="linear", symbol=symbol)["result"]["list"]:
            if float(row.get("size") or 0) > 0:
                return {"side": row["side"], "size": float(row["size"]), "entry_price": float(row.get("avgPrice") or 0)}
        return None
    coin = symbol.replace("USDT", "")
    account = bybit.get_wallet_balance(accountType="UNIFIED", coin=coin)["result"]["list"][0]
    for row in account.get("coin", []):
        if row["coin"] == coin and float(row.get("walletBalance") or 0) > 0:
            return {"side": "Buy", "size": float(row["walletBalance"]), "entry_price": 0.0}
    return None


def get_order_rules(symbol):
    """주문 수량/가격의 최소 단위."""
    info = bybit.get_instruments_info(category=MARKET_TYPE, symbol=symbol)["result"]["list"][0]
    lot = info["lotSizeFilter"]
    return {
        "qty_step": lot.get("qtyStep") or lot.get("basePrecision"),
        "min_qty": float(lot.get("minOrderQty") or 0),
        "tick": info["priceFilter"]["tickSize"],
    }


# ======================================================================
#  3. Gemini에게 판단 요청 (매수 / 매도 / 관망 + 이유)
# ======================================================================
def ask_gemini(market, balance, positions):
    if gemini is None:
        log("GEMINI_API_KEY가 없어 판단을 받을 수 없습니다. 모두 관망으로 처리합니다.")
        return []
    kind = "USDT 무기한 선물" if MARKET_TYPE == "linear" else "현물"
    meaning = ("매수 = 롱 진입(보유 중인 숏이 있으면 청산), 매도 = 숏 진입(보유 중인 롱이 있으면 청산)"
               if MARKET_TYPE == "linear" else "매수 = 코인 구매, 매도 = 보유 코인 판매(보유가 없으면 관망)")
    prompt = f"""너는 신중한 암호화폐 단기 매매 분석가다. 아래 수치만 근거로 코인별로 판단해라.
근거가 약하거나 방향이 엇갈리면 '관망'을 택한다.

거래 종류: {kind} ({meaning})
계좌 총자산: {balance:.2f} USDT, 한 번 주문 금액: {ORDER_USDT} USDT
왕복 수수료는 거래 금액의 약 0.11%다. 기대 수익이 이보다 충분히 클 때만 진입한다.

시장 데이터 (ma20 = 20시간 이동평균, rsi14 = RSI):
{json.dumps(market, ensure_ascii=False)}

현재 보유: {json.dumps(positions, ensure_ascii=False)}

반드시 아래 JSON 형식으로만 답해라. reason은 초보자도 이해할 수 있는 한국어 2~3문장.
{{"decisions": [{{"symbol": "BTCUSDT", "decision": "매수 | 매도 | 관망", "confidence": 0.0, "reason": "..."}}]}}"""
    response = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.3),
    )
    decisions = json.loads(response.text).get("decisions", [])
    return [d for d in decisions if d.get("symbol") in SYMBOLS and d.get("decision") in ("매수", "매도", "관망")]


# ======================================================================
#  4. 주문 실행
# ======================================================================
def place(order, description):
    """TEST_MODE면 출력만, 아니면 실제 주문."""
    if TEST_MODE:
        log(f"  [테스트] 주문하지 않음: {description}")
        log(f"           실제였다면 보낼 내용: {json.dumps(order, ensure_ascii=False)}")
        return True
    if not HAS_BYBIT_KEYS:
        log("  Bybit API 키가 없어 주문할 수 없습니다. .env 파일을 확인하세요.")
        return False
    result = bybit.place_order(**order)
    log(f"  주문 완료: {description} (주문 번호 {result['result'].get('orderId')})")
    return True


def execute_order(symbol, decision, price, position):
    """Gemini의 판단(매수/매도)을 주문으로 바꾼다. 주문을 냈으면 True."""
    side = "Buy" if decision == "매수" else "Sell"
    rules = get_order_rules(symbol)

    if MARKET_TYPE == "spot":
        if side == "Buy":
            order = {"category": "spot", "symbol": symbol, "side": "Buy", "orderType": "Market",
                     "qty": str(ORDER_USDT), "marketUnit": "quoteCoin"}      # USDT 금액 기준으로 매수
            return place(order, f"{symbol} 현물 {ORDER_USDT} USDT어치 매수")
        if not position:
            log("  팔 수 있는 보유 수량이 없어 건너뜁니다.")
            return False
        qty = floor_to_step(position["size"], rules["qty_step"])
        if float(qty) < rules["min_qty"]:
            log("  보유 수량이 최소 주문 수량보다 적어 건너뜁니다.")
            return False
        order = {"category": "spot", "symbol": symbol, "side": "Sell", "orderType": "Market", "qty": qty}
        return place(order, f"{symbol} 현물 {qty}개 매도")

    # ----- 선물 -----
    if position and position["side"] == side:
        log("  이미 같은 방향의 포지션을 보유 중이라 추가로 진입하지 않습니다.")
        return False
    if position:   # 반대 방향 보유 -> 이번에는 청산만 하고, 새 진입은 다음 주기에 다시 판단
        qty = floor_to_step(position["size"], rules["qty_step"])
        order = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": qty,
                 "reduceOnly": True, "positionIdx": 0}
        return place(order, f"{symbol} 보유 포지션 {qty}개 청산")

    qty = floor_to_step(ORDER_USDT * LEVERAGE / price, rules["qty_step"])
    if float(qty) < rules["min_qty"] or float(qty) <= 0:
        log(f"  주문 수량이 최소 단위({rules['min_qty']})보다 작습니다. ORDER_USDT나 LEVERAGE를 올려야 합니다.")
        return False
    direction = 1 if side == "Buy" else -1
    stop_loss = floor_to_step(price * (1 - direction * STOP_LOSS_PCT / 100), rules["tick"])
    take_profit = floor_to_step(price * (1 + direction * TAKE_PROFIT_PCT / 100), rules["tick"])
    order = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": qty,
             "stopLoss": stop_loss, "takeProfit": take_profit, "tpslMode": "Full", "positionIdx": 0}
    if not TEST_MODE and HAS_BYBIT_KEYS:
        try:
            bybit.set_leverage(category="linear", symbol=symbol, buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE))
        except Exception as error:      # 이미 같은 레버리지면 Bybit가 오류(110043)를 돌려준다. 무시해도 된다.
            if "110043" not in str(error):
                raise
    name = "롱(매수)" if side == "Buy" else "숏(매도)"
    return place(order, f"{symbol} {name} {qty}개, 레버리지 {LEVERAGE}배, 손절 {stop_loss}, 익절 {take_profit}")


def save_log(row):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        if is_new:
            writer.writerow(["시각", "모드", "코인", "판단", "확신도", "가격", "주문 여부", "이유"])
        writer.writerow(row)


# ======================================================================
#  한 번의 실행 주기
# ======================================================================
def job():
    log("=" * 60)
    log(f"자동매매 실행 ({'테스트 모드 - 실제 주문 없음' if TEST_MODE else '실제 주문 모드'})")
    try:
        market = [get_market_data(symbol) for symbol in SYMBOLS]
        balance = get_balance()
        positions = {symbol: get_position(symbol) for symbol in SYMBOLS}
    except Exception as error:
        log(f"Bybit 데이터 조회 실패: {error}")
        return
    log(f"계좌 총자산: {balance:,.2f} USDT" + ("" if HAS_BYBIT_KEYS else " (가정한 값)"))
    for item in market:
        log(f"  {item['symbol']}: {item['price']} (24시간 {item['change_24h_pct']:+.2f}%, RSI {item['rsi14']})")

    try:
        decisions = ask_gemini(market, balance, positions)
    except Exception as error:
        log(f"Gemini 판단 실패: {error}")
        return

    prices = {item["symbol"]: item["price"] for item in market}
    for d in decisions:
        symbol, decision = d["symbol"], d["decision"]
        confidence = float(d.get("confidence") or 0)
        log(f"{symbol} -> {decision} (확신도 {confidence:.2f})")
        log(f"  이유: {d.get('reason', '')}")
        ordered = False
        if decision == "관망":
            pass
        elif confidence < MIN_CONFIDENCE:
            log(f"  확신도가 기준({MIN_CONFIDENCE})보다 낮아 주문하지 않습니다.")
        else:
            try:
                ordered = execute_order(symbol, decision, prices[symbol], positions.get(symbol))
            except Exception as error:
                log(f"  주문 실패: {error}")
        save_log([f"{datetime.now():%Y-%m-%d %H:%M:%S}", "테스트" if TEST_MODE else "실전", symbol, decision,
                  confidence, prices[symbol], "예" if ordered else "아니오", d.get("reason", "")])


# ======================================================================
#  5. 스케줄러 (정해진 주기마다 자동 실행)
# ======================================================================
def main():
    if not TEST_MODE:
        log("주의: TEST_MODE = False 입니다. 실제 자금으로 주문이 나갑니다.")
    job()                                                # 시작하자마자 한 번 실행
    if "--once" in sys.argv:
        return
    schedule.every(INTERVAL_MINUTES).minutes.do(job)     # 이후 INTERVAL_MINUTES 분마다 실행
    # 다른 예: schedule.every().hour.at(":01").do(job)        매시 1분에 실행
    #          schedule.every().day.at("09:00").do(job)       매일 오전 9시에 실행
    log(f"{INTERVAL_MINUTES}분마다 자동으로 실행합니다. 끝내려면 Ctrl+C 를 누르세요.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        log("자동매매를 종료했습니다.")


if __name__ == "__main__":
    main()

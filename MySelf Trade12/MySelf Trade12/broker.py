"""Myself Trade - 주문 실행 계층.

PaperBroker : 모의 투자. 실제 시세를 쓰되 주문은 프로그램 안에서만 체결한다.
LiveBroker  : Bybit에 실제 주문을 낸다 (데모 트레이딩 / 테스트넷 / 실전).

두 브로커는 같은 메서드를 가지므로 매매 엔진은 어느 쪽인지 신경 쓰지 않는다.
"""
from __future__ import annotations

import time

from bybit_api import BybitClient, BybitError
from utils import calc_trade_pnl, fmt_step, log, unrealized_pnl

SLIPPAGE = 0.0002  # 모의 투자 시장가 체결 미끄러짐 가정 (0.02%)


class PaperBroker:
    name = "paper"

    def __init__(self, market, storage, settings):
        self.market = market
        self.storage = storage
        self.settings = settings

    # -- 잔고: 실현 손익이 반영된 현금. 자산 = 잔고 + 미실현 손익
    def _balance(self) -> float:
        value = self.storage.kv_get("paper_balance")
        if value is None:
            value = float(self.settings.get("paper_start_balance"))
            self.storage.kv_set("paper_balance", value)
        return float(value)

    def _set_balance(self, value: float) -> None:
        self.storage.kv_set("paper_balance", value)

    def fee_rates(self) -> dict:
        return {"taker": self.settings.get("taker_fee_pct") / 100, "maker": self.settings.get("maker_fee_pct") / 100}

    @staticmethod
    def _exit_fill(side: str, price: float) -> float:
        """시장가로 청산할 때 체결될 가격 (미끄러짐 반영)."""
        return price * (1 - SLIPPAGE) if side == "long" else price * (1 + SLIPPAGE)

    def get_equity(self) -> float:
        equity = self._balance()
        for trade in self.storage.open_trades("paper"):
            fill = self._exit_fill(trade["side"], self.market.get_price(trade["symbol"]))
            equity += unrealized_pnl(trade["side"], trade["entry_price"], fill, trade["qty"])
        return equity

    def open_position(self, symbol: str, side: str, qty: float, leverage: int,
                      sl_price: float, tp_price: float, instrument: dict) -> dict:
        price = self.market.get_price(symbol)
        fill = price * (1 + SLIPPAGE) if side == "long" else price * (1 - SLIPPAGE)
        fee = fill * qty * self.fee_rates()["taker"]
        self._set_balance(self._balance() - fee)
        return {"entry_price": fill, "qty": qty, "fee": fee}

    def add_position(self, trade: dict, qty: float, sl_price: float, tp_price: float, instrument: dict) -> dict:
        """분할 매수: 보유 중인 포지션에 같은 방향으로 더 산다. 새 평균 단가와 전체 수량을 돌려준다."""
        price = self.market.get_price(trade["symbol"])
        fill = price * (1 + SLIPPAGE) if trade["side"] == "long" else price * (1 - SLIPPAGE)
        fee = fill * qty * self.fee_rates()["taker"]
        self._set_balance(self._balance() - fee)
        total = trade["qty"] + qty
        return {"avg_price": (trade["entry_price"] * trade["qty"] + fill * qty) / total, "total_qty": total, "fee": fee}

    def set_stops(self, trade: dict, sl_price: float, tp_price: float, instrument: dict, strict: bool = False) -> None:
        pass   # 모의 투자의 손절/익절은 거래 기록의 값으로 확인하므로 따로 할 일이 없다

    def _close(self, trade: dict, price: float, reason: str) -> dict:
        fill = self._exit_fill(trade["side"], price)
        taker = self.fee_rates()["taker"]
        pnl = calc_trade_pnl(trade["side"], trade["entry_price"], fill, trade["qty"], taker, taker)
        # 진입 수수료는 진입할 때 이미 잔고에서 뺐으므로 여기서는 청산분만 반영
        self._set_balance(self._balance() + pnl["gross_pnl"] - pnl["fee_out"])
        return {"exit_price": fill, "gross_pnl": pnl["gross_pnl"], "fee": pnl["fee"],
                "net_pnl": pnl["net_pnl"], "reason": reason}

    def close_position(self, trade: dict, reason: str) -> dict:
        return self._close(trade, self.market.get_price(trade["symbol"]), reason)

    def reduce_position(self, piece: dict, reason: str, instrument: dict) -> dict:
        """분할 매도: 포지션의 일부(piece["qty"])만 시장가로 정리한다. piece는 팔 몫만 담은 거래 기록."""
        return self._close(piece, self.market.get_price(piece["symbol"]), reason)

    def check_closed(self, trade: dict) -> dict | None:
        """손절/익절 가격에 닿았으면 청산 처리하고 결과를 돌려준다."""
        price = self.market.get_price(trade["symbol"])
        sl, tp = trade.get("sl_price"), trade.get("tp_price")
        if trade["side"] == "long":
            if sl and price <= sl:
                return self._close(trade, price, "손절")
            if tp and price >= tp:
                return self._close(trade, price, "익절")
        else:
            if sl and price >= sl:
                return self._close(trade, price, "손절")
            if tp and price <= tp:
                return self._close(trade, price, "익절")
        return None

    def position_pnl(self, trade: dict) -> float:
        fill = self._exit_fill(trade["side"], self.market.get_price(trade["symbol"]))
        return unrealized_pnl(trade["side"], trade["entry_price"], fill, trade["qty"])


class LiveBroker:
    """Bybit 실주문 브로커. env: live | demo | testnet"""

    def __init__(self, client: BybitClient, market, settings):
        self.client = client
        self.market = market
        self.settings = settings
        self.name = client.env
        self._fees: dict | None = None

    def fee_rates(self) -> dict:
        if self._fees is None:
            self._fees = self.client.get_fee_rates("BTCUSDT") or {
                "taker": self.settings.get("taker_fee_pct") / 100,
                "maker": self.settings.get("maker_fee_pct") / 100,
            }
        return self._fees

    def get_equity(self) -> float:
        return self.client.get_equity()

    def open_position(self, symbol: str, side: str, qty: float, leverage: int,
                      sl_price: float, tp_price: float, instrument: dict) -> dict:
        self.client.set_leverage(symbol, leverage)
        self.client.place_market_order(
            symbol, "Buy" if side == "long" else "Sell", fmt_step(qty, instrument["qty_step"]),
            stop_loss=fmt_step(sl_price, instrument["tick_size"]),
            take_profit=fmt_step(tp_price, instrument["tick_size"]))
        # 주문은 이미 나갔다. 이 뒤의 조회가 실패해도 거래 기록은 반드시 남겨야 하므로(안 그러면 거래소에만
        # 포지션이 남는다) 오류를 밖으로 던지지 않고, 확인하지 못하면 주문한 값으로 기록한다.
        try:
            entry = self.market.get_price(symbol)
        except Exception:
            entry = (sl_price + tp_price) / 2
        filled = qty
        for _ in range(5):  # 체결 반영을 잠시 기다린다
            time.sleep(0.6)
            try:
                position = self.client.get_position(symbol)
            except Exception as exc:
                log.warning("%s 체결 확인 실패, 다시 확인합니다: %s", symbol, exc)
                continue
            if position:
                entry, filled = position["entry_price"], position["qty"]
                break
        return {"entry_price": entry, "qty": filled, "fee": entry * filled * self.fee_rates()["taker"]}

    def add_position(self, trade: dict, qty: float, sl_price: float, tp_price: float, instrument: dict) -> dict:
        """분할 매수: 같은 방향으로 시장가 주문을 한 번 더 낸다. Bybit가 한 포지션으로 합쳐 준다."""
        symbol = trade["symbol"]
        self.client.place_market_order(
            symbol, "Buy" if trade["side"] == "long" else "Sell", fmt_step(qty, instrument["qty_step"]),
            stop_loss=fmt_step(sl_price, instrument["tick_size"]), take_profit=fmt_step(tp_price, instrument["tick_size"]))
        try:
            price = self.market.get_price(symbol)
        except Exception:
            price = trade["entry_price"]
        total = trade["qty"] + qty
        average = (trade["entry_price"] * trade["qty"] + price * qty) / total
        for _ in range(5):  # 체결 반영을 잠시 기다린다
            time.sleep(0.6)
            try:
                position = self.client.get_position(symbol)
            except Exception as exc:      # 주문은 이미 나갔으므로 기록은 남긴다
                log.warning("%s 추가 매수 체결 확인 실패, 다시 확인합니다: %s", symbol, exc)
                continue
            if position and position["qty"] >= total * 0.999:
                average, total = position["entry_price"], position["qty"]
                break
        return {"avg_price": average, "total_qty": total, "fee": price * qty * self.fee_rates()["taker"]}

    def set_stops(self, trade: dict, sl_price: float, tp_price: float, instrument: dict, strict: bool = False) -> None:
        """strict: 사용자가 화면에서 직접 고친 경우. 거래소가 받지 않으면 오류를 그대로 올려 화면에 알린다."""
        try:
            self.client.set_trading_stop(trade["symbol"], fmt_step(sl_price, instrument["tick_size"]),
                                         fmt_step(tp_price, instrument["tick_size"]))
        except BybitError as exc:
            log.warning("%s 손절/익절 가격 변경 실패: %s", trade["symbol"], exc)
            if strict:
                raise

    def _closure_from_exchange(self, trade: dict, reason: str, after_ms: int | None = None) -> dict:
        """after_ms: 이 시각(거래소 기록의 갱신 시각)보다 뒤에 생긴 청산 기록만 받는다. 분할 매도에서 앞 회차의 기록을 다시 읽지 않게 한다."""
        record = None
        for _ in range(4):
            try:
                record = self.client.get_last_closed_pnl(trade["symbol"])
            except BybitError as exc:
                log.warning("청산 기록 조회 실패: %s", exc)
            if record and after_ms is not None and (record.get("updated_ms") or 0) <= after_ms:
                record = None                      # 방금 낸 주문의 기록이 아직 올라오지 않았다
            if record and record["exit_price"]:
                break
            time.sleep(0.7)
        taker = self.fee_rates()["taker"]
        if record and record["exit_price"]:
            exit_price = record["exit_price"]
            fee = record["open_fee"] + record["close_fee"]
            if fee <= 0:
                fee = (trade["entry_price"] + exit_price) * trade["qty"] * taker
            net = record["closed_pnl"]
            gross = net + fee
        else:  # 기록을 못 받으면 현재가로 추정
            exit_price = self.market.get_price(trade["symbol"])
            pnl = calc_trade_pnl(trade["side"], trade["entry_price"], exit_price, trade["qty"], taker, taker)
            gross, fee, net = pnl["gross_pnl"], pnl["fee"], pnl["net_pnl"]
            reason += " (추정치)"
        return {"exit_price": exit_price, "gross_pnl": gross, "fee": fee, "net_pnl": net, "reason": reason}

    def close_position(self, trade: dict, reason: str) -> dict:
        position = self.client.get_position(trade["symbol"])
        if position:
            instrument = self.market.get_instrument(trade["symbol"])
            self.client.place_market_order(
                trade["symbol"], "Sell" if position["side"] == "long" else "Buy",
                fmt_step(position["qty"], instrument["qty_step"]), reduce_only=True)
            time.sleep(0.8)
        return self._closure_from_exchange(trade, reason)

    def reduce_position(self, piece: dict, reason: str, instrument: dict) -> dict:
        """분할 매도: 포지션의 일부(piece["qty"])만 reduce-only 시장가로 정리한다.

        남은 수량에는 거래소에 걸어 둔 손절/익절이 그대로 적용된다 (포지션 전체에 거는 방식).
        """
        symbol = piece["symbol"]
        try:
            before = self.client.get_last_closed_pnl(symbol)
        except BybitError:
            before = None
        after_ms = (before or {}).get("updated_ms") or 0
        self.client.place_market_order(
            symbol, "Sell" if piece["side"] == "long" else "Buy",
            fmt_step(piece["qty"], instrument["qty_step"]), reduce_only=True)
        time.sleep(0.8)
        return self._closure_from_exchange(piece, reason, after_ms=after_ms)

    def check_closed(self, trade: dict) -> dict | None:
        """거래소에 걸어 둔 손절/익절이 체결되어 포지션이 사라졌는지 확인."""
        if self.client.get_position(trade["symbol"]) is not None:
            return None
        closure = self._closure_from_exchange(trade, "거래소 청산")
        sl, tp, exit_price = trade.get("sl_price"), trade.get("tp_price"), closure["exit_price"]
        if tp and abs(exit_price - tp) / tp < 0.002:
            closure["reason"] = "익절"
        elif sl and abs(exit_price - sl) / sl < 0.002:
            closure["reason"] = "손절"
        return closure

    def position_pnl(self, trade: dict) -> float:
        position = self.client.get_position(trade["symbol"])
        return position["unrealized"] if position else 0.0

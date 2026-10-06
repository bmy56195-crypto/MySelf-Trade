"""Myself Trade - 실행 파일 (전체 버전: 자동 매매 엔진 + 대시보드).

실행:  python main.py
브라우저에서 http://127.0.0.1:8000 이 열린다.
"""
from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import config
from config import APP_NAME, DB_PATH, LOG_PATH, MODES, Settings
from dashboard import create_app
from storage import Storage
from trade_engine import TradeEngine
from utils import log, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - Bybit AI 자동 매매")
    parser.add_argument("--port", type=int, default=8000, help="대시보드 포트 (기본 8000)")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = parser.parse_args()

    setup_logging(LOG_PATH)
    config.load_env()
    settings = Settings()
    storage = Storage(DB_PATH)

    # 안전 장치 1: 프로그램을 켤 때 자동 매매는 항상 Off. 화면에서 직접 켜야 한다.
    changes = {"trading_enabled": False}
    # 안전 장치 2: API 키가 없으면 실제 주문 모드로 시작하지 않는다.
    if settings.get("mode") != "paper" and not config.has_bybit_keys():
        log.warning("Bybit API 키가 없어 모의 투자 모드로 시작합니다.")
        changes["mode"] = "paper"
    settings.update(changes)

    engine = TradeEngine(settings, storage)
    engine.start()

    app = create_app(engine, settings, storage)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    url = f"http://127.0.0.1:{args.port}"
    log.info("%s 시작. 거래 모드: %s", APP_NAME, MODES[settings.get("mode")])
    log.info("대시보드: %s  (끝내려면 이 창에서 Ctrl+C)", url)
    if not config.has_gemini_key():
        log.info("Gemini API 키가 없어 규칙 기반 전략으로 동작합니다. 대시보드의 API 연결 화면에서 키를 넣으면 AI 분석이 켜집니다.")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    finally:
        engine.stop()
        log.info("%s 종료. 거래소에 걸어 둔 손절/익절 주문은 그대로 남아 있습니다.", APP_NAME)


if __name__ == "__main__":
    main()

"""Myself Trade - 저장소 (SQLite).

거래 내역, 자산 변화, 일별 상태(시작 자산/손실 록/계획/회고), AI 판단 기록, 학습 노트, 학습 자료를 보관한다.
모의 투자와 실전 기록이 섞이지 않도록 대부분의 표에 mode 열이 있다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',      -- open | closed
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                       -- long | short
    qty REAL NOT NULL,
    leverage INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    sl_price REAL,
    tp_price REAL,
    entry_time TEXT NOT NULL,                 -- UTC ISO
    exit_time TEXT,
    day TEXT,                                 -- 청산 시점의 현지 날짜
    margin REAL NOT NULL,                     -- 사용한 증거금(USDT)
    equity_at_entry REAL NOT NULL,
    invest_pct REAL NOT NULL,                 -- 사용 원금 비율(%) = 증거금 / 진입 시 자산
    gross_pnl REAL,
    fee REAL DEFAULT 0,                       -- 진입 + 청산 수수료 합계
    net_pnl REAL,
    return_pct REAL,                          -- 증거금 대비 수익률(%)
    equity_return_pct REAL,                   -- 자산 대비 수익률(%)
    confidence REAL,
    source TEXT,                              -- gemini | rule
    entry_reason TEXT,
    exit_reason TEXT,
    context TEXT                              -- 진입 당시 시장 상황(JSON). 과거 유사 거래를 찾는 데 쓴다.
);
CREATE INDEX IF NOT EXISTS idx_trades_mode_exit ON trades(mode, status, exit_time);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    equity REAL NOT NULL,
    pnl_pct REAL NOT NULL                     -- 당일 수익률(%)
);
CREATE INDEX IF NOT EXISTS idx_equity_mode_ts ON equity(mode, ts);

CREATE TABLE IF NOT EXISTS days (
    mode TEXT NOT NULL,
    day TEXT NOT NULL,
    start_equity REAL NOT NULL,
    end_equity REAL,
    locked INTEGER NOT NULL DEFAULT 0,
    lock_reason TEXT,
    target_hit INTEGER NOT NULL DEFAULT 0,
    plan TEXT,                                -- JSON
    review TEXT,                              -- JSON
    PRIMARY KEY (mode, day)
);

CREATE TABLE IF NOT EXISTS analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,                     -- long | short | hold | close
    confidence REAL,
    source TEXT,
    reason TEXT,
    executed INTEGER NOT NULL DEFAULT 0,
    note TEXT                                 -- 실행하지 않았다면 그 이유
);
CREATE INDEX IF NOT EXISTS idx_analysis_mode_ts ON analysis(mode, ts);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    mode TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,                       -- text | youtube | image
    title TEXT NOT NULL,
    source TEXT,                              -- 유튜브 주소 또는 원래 파일 이름
    content TEXT,                             -- 사용자가 적은 글 / 메모, 이미지면 저장한 파일 이름은 file 열
    file TEXT,
    summary TEXT,                             -- Gemini가 뽑은 요약과 규칙(JSON)
    status TEXT NOT NULL DEFAULT 'pending',   -- pending(분석 대기) | ok | error
    error TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


# 분할 매도로 먼저 판 몫은 따로 한 줄씩 기록한다 (context에 part_of). 거래 횟수를 셀 때는 그 줄을 뺀다.
NOT_PART = "(context IS NULL OR context NOT LIKE '%\"part_of\"%')"


class Storage:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.folder = Path(path).parent if str(path) != ":memory:" else Path(".")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(trades)").fetchall()}
            if "context" not in columns:      # 예전 버전에서 만든 DB에 새 열 추가
                self._db.execute("ALTER TABLE trades ADD COLUMN context TEXT")
            self._db.commit()

    # ---------------------------------------------------------- 내부 도구
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def _row(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------- key-value
    def kv_get(self, key: str, default=None):
        row = self._row("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return default

    def kv_set(self, key: str, value) -> None:
        self._exec("INSERT INTO kv(key, value) VALUES(?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def kv_delete(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    # ---------------------------------------------------------- 거래
    def open_trade(self, **fields) -> int:
        keys = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self._exec(f"INSERT INTO trades({keys}) VALUES({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, **fields) -> None:
        fields["status"] = "closed"
        sets = ", ".join(f"{key}=?" for key in fields)
        self._exec(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), trade_id))

    def update_trade(self, trade_id: int, **fields) -> None:
        """보유 중인 거래를 고친다 (분할 매수로 수량, 평균 단가가 바뀔 때)."""
        sets = ", ".join(f"{key}=?" for key in fields)
        self._exec(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), trade_id))

    def open_trades(self, mode: str) -> list[dict]:
        return self._rows("SELECT * FROM trades WHERE mode=? AND status='open' ORDER BY id", (mode,))

    def closed_trades(self, mode: str, start_iso: str | None = None, end_iso: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE mode=? AND status='closed'"
        params: list = [mode]
        if start_iso:
            sql += " AND exit_time>=?"
            params.append(start_iso)
        if end_iso:
            sql += " AND exit_time<?"
            params.append(end_iso)
        return self._rows(sql + " ORDER BY exit_time", tuple(params))

    def recent_closed(self, mode: str, limit: int = 200) -> list[dict]:
        return self._rows("SELECT * FROM trades WHERE mode=? AND status='closed' ORDER BY exit_time DESC LIMIT ?",
                          (mode, limit))

    def closed_page(self, mode: str, limit: int = 10, offset: int = 0) -> list[dict]:
        """청산이 끝난 거래를 최신순으로 limit개씩 (대시보드의 '더보기')."""
        return self._rows("SELECT * FROM trades WHERE mode=? AND status='closed' ORDER BY exit_time DESC, id DESC "
                          "LIMIT ? OFFSET ?", (mode, limit, offset))

    def count_closed(self, mode: str) -> int:
        row = self._row("SELECT COUNT(*) AS n FROM trades WHERE mode=? AND status='closed'", (mode,))
        return int(row["n"]) if row else 0

    def recent_entries(self, mode: str, since_iso: str) -> list[dict]:
        """since 이후에 진입한 거래(보유 중 포함)의 방향. 롱/숏 쏠림을 확인하는 데 쓴다."""
        return self._rows("SELECT symbol, side, entry_time FROM trades WHERE mode=? AND entry_time>=? AND " + NOT_PART +
                          " ORDER BY entry_time", (mode, since_iso))

    def trades_on_day(self, mode: str, day: str) -> list[dict]:
        return self._rows("SELECT * FROM trades WHERE mode=? AND status='closed' AND day=? ORDER BY exit_time",
                          (mode, day))

    def count_trades_on_day(self, mode: str, day: str) -> int:
        """그날 끝난 거래 수. 분할 매도로 나눠 판 포지션은 마지막으로 전부 정리된 때 한 번만 센다."""
        row = self._row("SELECT COUNT(*) AS n FROM trades WHERE mode=? AND status='closed' AND day=? AND " + NOT_PART,
                        (mode, day))
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------- 자산 기록
    def add_equity(self, mode: str, ts: str, day: str, equity: float, pnl_pct: float) -> None:
        self._exec("INSERT INTO equity(mode, ts, day, equity, pnl_pct) VALUES(?,?,?,?,?)",
                   (mode, ts, day, equity, pnl_pct))

    def equity_series(self, mode: str, start_iso: str | None = None, day: str | None = None) -> list[dict]:
        sql = "SELECT ts, day, equity, pnl_pct FROM equity WHERE mode=?"
        params: list = [mode]
        if start_iso:
            sql += " AND ts>=?"
            params.append(start_iso)
        if day:
            sql += " AND day=?"
            params.append(day)
        return self._rows(sql + " ORDER BY ts", tuple(params))

    def first_equity_after(self, mode: str, start_iso: str) -> dict | None:
        return self._row("SELECT * FROM equity WHERE mode=? AND ts>=? ORDER BY ts LIMIT 1", (mode, start_iso))

    def prune_equity(self, mode: str, keep_after_iso: str) -> None:
        """오래된 기록은 하루에 마지막 한 점만 남겨 DB가 커지지 않게 한다."""
        self._exec("DELETE FROM equity WHERE mode=? AND ts<? AND id NOT IN "
                   "(SELECT MAX(id) FROM equity WHERE mode=? AND ts<? GROUP BY day)",
                   (mode, keep_after_iso, mode, keep_after_iso))

    # ---------------------------------------------------------- 일별 상태
    def get_day(self, mode: str, day: str) -> dict | None:
        row = self._row("SELECT * FROM days WHERE mode=? AND day=?", (mode, day))
        if row:
            for key in ("plan", "review"):
                try:
                    row[key] = json.loads(row[key]) if row[key] else None
                except json.JSONDecodeError:
                    row[key] = None
        return row

    def create_day(self, mode: str, day: str, start_equity: float) -> None:
        self._exec("INSERT OR IGNORE INTO days(mode, day, start_equity) VALUES(?,?,?)", (mode, day, start_equity))

    def update_day(self, mode: str, day: str, **fields) -> None:
        for key in ("plan", "review"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        sets = ", ".join(f"{key}=?" for key in fields)
        self._exec(f"UPDATE days SET {sets} WHERE mode=? AND day=?", (*fields.values(), mode, day))

    def days_between(self, mode: str, start_day: str, end_day: str) -> list[dict]:
        return self._rows("SELECT * FROM days WHERE mode=? AND day>=? AND day<=? ORDER BY day",
                          (mode, start_day, end_day))

    def last_day_before(self, mode: str, day: str) -> dict | None:
        row = self._row("SELECT day FROM days WHERE mode=? AND day<? ORDER BY day DESC LIMIT 1", (mode, day))
        return self.get_day(mode, row["day"]) if row else None

    # ---------------------------------------------------------- AI 판단 기록
    def add_analysis(self, **fields) -> None:
        keys = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self._exec(f"INSERT INTO analysis({keys}) VALUES({marks})", tuple(fields.values()))

    def recent_analysis(self, mode: str, limit: int = 60, executed_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM analysis WHERE mode=?"
        if executed_only:
            sql += " AND executed=1"
        return self._rows(sql + " ORDER BY id DESC LIMIT ?", (mode, limit))

    def analysis_on_day(self, mode: str, day: str, limit: int = 15, offset: int = 0) -> list[dict]:
        """그날의 판단 기록을 최신순으로 (기록 화면의 달력에서 날짜를 골랐을 때)."""
        return self._rows("SELECT * FROM analysis WHERE mode=? AND day=? ORDER BY id DESC LIMIT ? OFFSET ?",
                          (mode, day, limit, offset))

    def count_analysis_on_day(self, mode: str, day: str) -> int:
        row = self._row("SELECT COUNT(*) AS n FROM analysis WHERE mode=? AND day=?", (mode, day))
        return int(row["n"]) if row else 0

    def month_summary(self, mode: str, start_day: str, end_day: str) -> dict[str, dict]:
        """달력용: 날짜별 거래 수, 수익 합계, 손실 합계, 판단 기록 수."""
        days: dict[str, dict] = {}
        for row in self._rows(
                "SELECT day, COUNT(*) AS trades, SUM(net_pnl) AS net, "
                "SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END) AS profit, "
                "SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END) AS loss "
                "FROM trades WHERE mode=? AND status='closed' AND day>=? AND day<=? GROUP BY day", (mode, start_day, end_day)):
            days[row["day"]] = {"day": row["day"], "trades": int(row["trades"] or 0), "net": row["net"] or 0.0,
                                "profit": row["profit"] or 0.0, "loss": row["loss"] or 0.0, "analyses": 0}
        for row in self._rows("SELECT day, COUNT(*) AS n FROM analysis WHERE mode=? AND day>=? AND day<=? GROUP BY day",
                              (mode, start_day, end_day)):
            days.setdefault(row["day"], {"day": row["day"], "trades": 0, "net": 0.0, "profit": 0.0, "loss": 0.0,
                                         "analyses": 0})["analyses"] = int(row["n"] or 0)
        return days

    def first_day(self, mode: str) -> str | None:
        """기록이 있는 가장 이른 날짜."""
        row = self._row("SELECT MIN(day) AS day FROM days WHERE mode=?", (mode,))
        return row["day"] if row and row["day"] else None

    def prune_analysis(self, mode: str, keep: int = 100_000) -> None:
        """아주 오래된 판단 기록만 지운다. 달력에서 지난 날짜의 분석을 볼 수 있도록 넉넉히(약 1년치) 남긴다."""
        self._exec("DELETE FROM analysis WHERE mode=? AND id NOT IN "
                   "(SELECT id FROM analysis WHERE mode=? ORDER BY id DESC LIMIT ?)", (mode, mode, keep))

    # ---------------------------------------------------------- 학습 노트
    def add_lessons(self, mode: str, day: str, lessons: list[str]) -> None:
        known = {row["text"] for row in self._rows("SELECT text FROM lessons ORDER BY id DESC LIMIT 60")}
        for text in lessons:
            text = str(text).strip()
            if text and text not in known:   # 같은 교훈을 반복해서 쌓지 않는다
                known.add(text)
                self._exec("INSERT INTO lessons(day, mode, text) VALUES(?,?,?)", (day, mode, text))

    def recent_lessons(self, limit: int = 15) -> list[dict]:
        rows = self._rows("SELECT day, mode, text FROM lessons ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    # ---------------------------------------------------------- 학습 자료
    @staticmethod
    def _material(row: dict | None) -> dict | None:
        if row:
            try:
                row["summary"] = json.loads(row["summary"]) if row["summary"] else None
            except json.JSONDecodeError:
                row["summary"] = None
        return row

    def add_material(self, **fields) -> int:
        keys = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return int(self._exec(f"INSERT INTO materials({keys}) VALUES({marks})", tuple(fields.values())).lastrowid)

    def update_material(self, material_id: int, **fields) -> None:
        if "summary" in fields and fields["summary"] is not None and not isinstance(fields["summary"], str):
            fields["summary"] = json.dumps(fields["summary"], ensure_ascii=False)
        sets = ", ".join(f"{key}=?" for key in fields)
        self._exec(f"UPDATE materials SET {sets} WHERE id=?", (*fields.values(), material_id))

    def get_material(self, material_id: int) -> dict | None:
        return self._material(self._row("SELECT * FROM materials WHERE id=?", (material_id,)))

    def materials(self, only_enabled: bool = False) -> list[dict]:
        sql = "SELECT * FROM materials" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id DESC"
        return [self._material(row) for row in self._rows(sql)]

    def delete_material(self, material_id: int) -> None:
        self._exec("DELETE FROM materials WHERE id=?", (material_id,))

    # ---------------------------------------------------------- 초기화
    def reset_mode(self, mode: str) -> None:
        """한 모드의 기록을 모두 지운다 (모의 투자 다시 시작용)."""
        for table in ("trades", "equity", "days", "analysis", "lessons"):
            self._exec(f"DELETE FROM {table} WHERE mode=?", (mode,))

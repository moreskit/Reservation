"""SQLite storage for accounts, watches and the activity log.

Account credentials are encrypted at rest with a Fernet key stored next to the
database (``secret.key``). Keep that file private and back it up together with
``joshy.db``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    platform    TEXT PRIMARY KEY,
    data_enc    TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    venue_id        TEXT NOT NULL,
    venue_name      TEXT NOT NULL,
    venue_url       TEXT NOT NULL DEFAULT '',
    party_size      INTEGER NOT NULL,
    dates           TEXT NOT NULL,
    earliest        TEXT NOT NULL,
    latest          TEXT NOT NULL,
    preferred       TEXT NOT NULL DEFAULT '',
    auto_book       INTEGER NOT NULL DEFAULT 1,
    interval_secs   INTEGER NOT NULL DEFAULT 60,
    status          TEXT NOT NULL DEFAULT 'active',
    last_checked    TEXT,
    last_result     TEXT NOT NULL DEFAULT '',
    booking         TEXT NOT NULL DEFAULT '',
    notified        TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    watch_id  INTEGER,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Watch:
    id: int
    platform: str
    venue_id: str
    venue_name: str
    venue_url: str
    party_size: int
    dates: list[str]
    earliest: str
    latest: str
    preferred: str
    auto_book: bool
    interval_secs: int
    status: str
    last_checked: str | None
    last_result: str
    booking: dict[str, Any] = field(default_factory=dict)
    notified: list[str] = field(default_factory=list)
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Watch":
        return cls(
            id=row["id"],
            platform=row["platform"],
            venue_id=row["venue_id"],
            venue_name=row["venue_name"],
            venue_url=row["venue_url"],
            party_size=row["party_size"],
            dates=json.loads(row["dates"]),
            earliest=row["earliest"],
            latest=row["latest"],
            preferred=row["preferred"],
            auto_book=bool(row["auto_book"]),
            interval_secs=row["interval_secs"],
            status=row["status"],
            last_checked=row["last_checked"],
            last_result=row["last_result"],
            booking=json.loads(row["booking"]) if row["booking"] else {},
            notified=json.loads(row["notified"] or "[]"),
            created_at=row["created_at"],
        )


class Database:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_key(data_dir / "secret.key"))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(data_dir / "joshy.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @staticmethod
    def _load_key(path: Path) -> bytes:
        if path.exists():
            return path.read_bytes().strip()
        key = Fernet.generate_key()
        path.write_bytes(key)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- accounts -------------------------------------------------------
    def save_account(self, platform: str, data: dict[str, Any], label: str = "") -> None:
        enc = self._fernet.encrypt(json.dumps(data).encode()).decode()
        self._exec(
            "INSERT INTO accounts(platform, data_enc, label, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(platform) DO UPDATE SET data_enc=excluded.data_enc, "
            "label=excluded.label, updated_at=excluded.updated_at",
            (platform, enc, label, now_iso()),
        )

    def get_account(self, platform: str) -> dict[str, Any] | None:
        rows = self._query("SELECT data_enc FROM accounts WHERE platform=?", (platform,))
        if not rows:
            return None
        return json.loads(self._fernet.decrypt(rows[0]["data_enc"].encode()))

    def account_labels(self) -> dict[str, dict[str, str]]:
        rows = self._query("SELECT platform, label, updated_at FROM accounts")
        return {r["platform"]: {"label": r["label"], "updated_at": r["updated_at"]} for r in rows}

    def delete_account(self, platform: str) -> None:
        self._exec("DELETE FROM accounts WHERE platform=?", (platform,))

    # ---- watches --------------------------------------------------------
    def add_watch(
        self,
        *,
        platform: str,
        venue_id: str,
        venue_name: str,
        venue_url: str,
        party_size: int,
        dates: list[str],
        earliest: str,
        latest: str,
        preferred: str = "",
        auto_book: bool = True,
        interval_secs: int = 60,
    ) -> int:
        cur = self._exec(
            "INSERT INTO watches(platform, venue_id, venue_name, venue_url, party_size, dates, "
            "earliest, latest, preferred, auto_book, interval_secs, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                platform,
                venue_id,
                venue_name,
                venue_url,
                party_size,
                json.dumps(sorted(set(dates))),
                earliest,
                latest,
                preferred,
                int(auto_book),
                interval_secs,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def get_watch(self, watch_id: int) -> Watch | None:
        rows = self._query("SELECT * FROM watches WHERE id=?", (watch_id,))
        return Watch.from_row(rows[0]) if rows else None

    def list_watches(self, status: str | None = None) -> list[Watch]:
        if status:
            rows = self._query("SELECT * FROM watches WHERE status=? ORDER BY id DESC", (status,))
        else:
            rows = self._query("SELECT * FROM watches ORDER BY id DESC")
        return [Watch.from_row(r) for r in rows]

    def update_watch(self, watch_id: int, **fields: Any) -> None:
        if not fields:
            return
        encoded = {}
        for key, value in fields.items():
            if key in ("dates", "notified", "booking"):
                value = json.dumps(value) if value else ("[]" if key != "booking" else "")
            elif key == "auto_book":
                value = int(value)
            encoded[key] = value
        cols = ", ".join(f"{k}=?" for k in encoded)
        self._exec(f"UPDATE watches SET {cols} WHERE id=?", (*encoded.values(), watch_id))

    def delete_watch(self, watch_id: int) -> None:
        self._exec("DELETE FROM watches WHERE id=?", (watch_id,))

    # ---- events ---------------------------------------------------------
    def log(self, message: str, *, watch_id: int | None = None, level: str = "info") -> None:
        self._exec(
            "INSERT INTO events(ts, watch_id, level, message) VALUES(?,?,?,?)",
            (now_iso(), watch_id, level, message),
        )

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

"""SQLite WAL outbox. Rows are pseudonymised request bodies; a row leaves only when the server acknowledged its id.

One row per (kind, id): a second write with the same id is a no-op, so retries and duplicate captures dedupe here
and again on the server (ingest is keyed by interaction_id, verdict_id and outcome_id).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal

Kind = Literal["interaction", "verdict", "outcome"]
KINDS: tuple[Kind, ...] = ("interaction", "verdict", "outcome")
MAX_BACKOFF_S = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT    NOT NULL,
    id       TEXT    NOT NULL,
    body     TEXT    NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at  REAL    NOT NULL DEFAULT 0,
    dead     TEXT,
    UNIQUE (kind, id)
)
"""


def _private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)  # the mode in os.open applies only on create: narrow a file that already existed


class Spool:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        _private_file(self.path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(_SCHEMA)
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.path) + suffix)
            if side.exists():
                os.chmod(side, 0o600)

    def put(self, kind: Kind, id: str, body: str) -> bool:
        """Store a body. False when (kind, id) is already spooled."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO outbox (kind, id, body) VALUES (?, ?, ?)", (kind, id, body))
            return cur.rowcount == 1

    def due(self, kind: Kind, limit: int, *, now: float | None = None, force: bool = False) -> list[tuple[str, str]]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT id, body FROM outbox WHERE kind = ? AND dead IS NULL AND (? OR next_at <= ?) ORDER BY seq LIMIT ?",
                (kind, int(force), now, limit),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def ack(self, kind: Kind, ids: list[str]) -> None:
        if ids:
            with self._lock:
                self._db.executemany("DELETE FROM outbox WHERE kind = ? AND id = ?", [(kind, i) for i in ids])

    def retry(self, kind: Kind, ids: list[str], *, now: float | None = None) -> None:
        """Keep the rows and back off: 0.5 s doubling per attempt, at most 60 s."""
        if ids:
            now = time.time() if now is None else now
            with self._lock:
                self._db.executemany(
                    "UPDATE outbox SET attempts = attempts + 1, next_at = ? + min(?, 0.5 * (1 << min(attempts, 10))) WHERE kind = ? AND id = ?",
                    [(now, MAX_BACKOFF_S, kind, i) for i in ids],
                )

    def reject(self, kind: Kind, ids: list[str], reason: str) -> None:
        """Keep a row the server refused for good, out of the send loop. reason is a code, never data."""
        if ids:
            with self._lock:
                self._db.executemany("UPDATE outbox SET dead = ? WHERE kind = ? AND id = ?", [(reason, kind, i) for i in ids])

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT kind, dead IS NOT NULL, count(*) FROM outbox GROUP BY 1, 2").fetchall()
        out = {f"{k}{'_rejected' if d else ''}": 0 for k in KINDS for d in (0, 1)}
        for kind, dead, n in rows:
            out[f"{kind}{'_rejected' if dead else ''}"] = n
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._db.close()

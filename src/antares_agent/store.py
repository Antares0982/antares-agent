"""sqlite persistence for thread metadata and the event log.

Writes are synchronous. Each one is a single small row on a local disk with
WAL on, and this is a single-user service -- an async wrapper would buy
microseconds and cost a whole class of ordering bugs.

Conversation state itself is *not* stored here: the CLI owns its own session
files, and a thread is revived by handing its `session_id` back to `resume`.
What this holds is the mapping needed to find that session again, plus enough
event history to answer `?after=` after a restart.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .events import Event, EventType

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, thread TEXT NOT NULL, fingerprint TEXT NOT NULL,
    state TEXT NOT NULL, result TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id      TEXT PRIMARY KEY,
    profile        TEXT NOT NULL,
    session_id     TEXT,
    summary        TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    permission_mode TEXT NOT NULL DEFAULT '',
    model          TEXT,
    deleted        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    thread_id TEXT    NOT NULL,
    event_id  INTEGER NOT NULL,
    type      TEXT    NOT NULL,
    ts        TEXT    NOT NULL,
    payload   TEXT    NOT NULL,
    PRIMARY KEY (thread_id, event_id)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    profile: str
    session_id: str | None
    summary: str
    created_at: str
    last_active_at: str
    permission_mode: str = ""
    model: str | None = None


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(threads)")}
        if "permission_mode" not in columns:
            self._db.execute(
                "ALTER TABLE threads ADD COLUMN permission_mode TEXT NOT NULL DEFAULT ''"
            )
        if "model" not in columns:
            self._db.execute("ALTER TABLE threads ADD COLUMN model TEXT")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def has_artifact(self, artifact_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM events WHERE type='file' "
                "AND json_extract(payload,'$.artifact_id')=?",
                (artifact_id,),
            ).fetchone()
            is not None
        )

    def request(self, request_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def begin_request(self, request_id: str, thread: str, fingerprint: str) -> None:
        self._db.execute(
            "INSERT INTO requests VALUES(?,?,?,'dispatching',NULL)",
            (request_id, thread, fingerprint),
        )
        self._db.commit()

    def finish_request(self, request_id: str, result: dict) -> None:
        self._db.execute(
            "UPDATE requests SET state='accepted',result=? WHERE id=?",
            (json.dumps(result), request_id),
        )
        self._db.commit()

    def interrupt_requests(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT id,thread FROM requests WHERE state='dispatching' OR "
            "(state='accepted' AND json_extract(result,'$.status')='queued')"
        ).fetchall()
        self._db.executemany(
            "UPDATE requests SET state='uncertain' WHERE id=?", [(r["id"],) for r in rows]
        )
        self._db.commit()
        return [dict(row) for row in rows]

    # -- threads ---------------------------------------------------------

    def create_thread(self, thread_id: str, profile: str) -> ThreadRow:
        now = _now()
        self._db.execute(
            "INSERT INTO threads (thread_id, profile, session_id, summary, created_at, "
            "last_active_at) VALUES (?, ?, NULL, '', ?, ?)",
            (thread_id, profile, now, now),
        )
        self._db.commit()
        return ThreadRow(thread_id, profile, None, "", now, now)

    def get_thread(self, thread_id: str) -> ThreadRow | None:
        row = self._db.execute(
            "SELECT * FROM threads WHERE thread_id = ? AND deleted = 0", (thread_id,)
        ).fetchone()
        return _row(row) if row else None

    def list_threads(self, limit: int = 100) -> list[ThreadRow]:
        rows = self._db.execute(
            "SELECT * FROM threads WHERE deleted = 0 ORDER BY last_active_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row(r) for r in rows]

    def touch(
        self,
        thread_id: str,
        *,
        session_id: str | None = None,
        summary: str = "",
        permission_mode: str | None = None,
        model: str | None = None,
    ) -> None:
        """Bump activity, and record whatever new identity the thread gained.

        `session_id` and `summary` only ever move from unset to set, so a
        later call with nothing new must not blank what is already there.
        """
        self._db.execute(
            "UPDATE threads SET last_active_at = ?, "
            "session_id = COALESCE(?, session_id), "
            "summary = CASE WHEN summary = '' THEN ? ELSE summary END, "
            "permission_mode = COALESCE(?, permission_mode), "
            "model = COALESCE(?, model) "
            "WHERE thread_id = ?",
            (_now(), session_id, summary, permission_mode, model, thread_id),
        )
        self._db.commit()

    def delete_thread(self, thread_id: str) -> None:
        """Soft delete: the row stays so a stale id keeps 404-ing after a restart."""
        self._db.execute("UPDATE threads SET deleted = 1 WHERE thread_id = ?", (thread_id,))
        self._db.commit()

    # -- events ----------------------------------------------------------

    def append_event(self, event: Event) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO events (thread_id, event_id, type, ts, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.thread_id,
                event.id,
                str(event.type),
                event.ts,
                json.dumps(event.payload(), ensure_ascii=False),
            ),
        )
        if event.type == EventType.THREAD_STATUS and event.data.get("status") == "idle":
            self._db.execute(
                "UPDATE requests SET state='settled' WHERE thread=? AND state='accepted'",
                (event.thread_id,),
            )
        self._db.commit()

    def events_since(self, thread_id: str, after: int, limit: int = 2000) -> list[dict]:
        rows = self._db.execute(
            "SELECT type, payload FROM events WHERE thread_id = ? AND event_id > ? "
            "ORDER BY event_id LIMIT ?",
            (thread_id, after, limit),
        ).fetchall()
        return [{"type": r["type"], "payload": json.loads(r["payload"])} for r in rows]

    def last_status(self, thread_id: str) -> str | None:
        """The status the thread last announced, or None if it never has."""
        row = self._db.execute(
            "SELECT payload FROM events WHERE thread_id = ? AND type = ? "
            "ORDER BY event_id DESC LIMIT 1",
            (thread_id, str(EventType.THREAD_STATUS)),
        ).fetchone()
        return json.loads(row["payload"]).get("status") if row else None

    def last_event_id(self, thread_id: str) -> int:
        row = self._db.execute(
            "SELECT MAX(event_id) AS m FROM events WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return int(row["m"] or 0)


def _row(row: sqlite3.Row) -> ThreadRow:
    return ThreadRow(
        thread_id=row["thread_id"],
        profile=row["profile"],
        session_id=row["session_id"],
        summary=row["summary"],
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
        permission_mode=row["permission_mode"],
        model=row["model"],
    )

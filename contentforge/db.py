"""Persistence layer: jobs, step state, analytics metrics, hashtag history.

The default backend is SQLite via the standard library (zero dependencies,
safe for a single machine). PostgreSQL is supported through the same
interface by setting ``CONTENTFORGE_DATABASE_URL`` and installing
``psycopg2-binary`` - the SQL used here is deliberately kept to the common
subset supported by both engines.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contentforge.log import get_logger

log = get_logger("db")

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        source_hash TEXT,
        slug TEXT,
        title TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        current_step TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        work_dir TEXT,
        output_dir TEXT,
        duration_seconds REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        meta TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_steps (
        job_id TEXT NOT NULL,
        step TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        started_at TEXT,
        finished_at TEXT,
        duration_seconds REAL,
        error TEXT,
        outputs TEXT,
        PRIMARY KEY (job_id, step)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT,
        platform TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        comments INTEGER DEFAULT 0,
        shares INTEGER DEFAULT 0,
        saves INTEGER DEFAULT 0,
        watch_time_seconds REAL DEFAULT 0,
        avg_watch_seconds REAL DEFAULT 0,
        completion_rate REAL DEFAULT 0,
        follower_growth INTEGER DEFAULT 0,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hashtag_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT,
        created_at TEXT NOT NULL,
        hashtags TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        level TEXT NOT NULL,
        job_id TEXT,
        message TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_job ON metrics(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)",
]

JOB_STATUSES = ("queued", "processing", "completed", "failed", "archived", "cancelled")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Small DAO around SQLite (or PostgreSQL through a compatible driver)."""

    def __init__(self, path: str | Path | None = None, url: str | None = None):
        self.url = url or os.environ.get("CONTENTFORGE_DATABASE_URL") or ""
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._pg = None
        if self.url.startswith("postgres"):
            try:
                import psycopg2  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "PostgreSQL requested but psycopg2 is not installed "
                    "(pip install psycopg2-binary)"
                ) from exc
            self._pg = psycopg2.connect(self.url)
            self._pg.autocommit = True
            self.placeholder = "%s"
        else:
            if self.path is None:
                self.path = Path("data/db/contentforge.sqlite3")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._sqlite = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None, timeout=30
            )
            self._sqlite.row_factory = sqlite3.Row
            self._sqlite.execute("PRAGMA journal_mode=WAL")
            self._sqlite.execute("PRAGMA busy_timeout=30000")
            self.placeholder = "?"
        self._init_schema()

    # ------------------------------------------------------------ plumbing
    def _init_schema(self) -> None:
        for stmt in SCHEMA:
            if self._pg is not None:
                stmt = stmt.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            self.execute(stmt)

    def _q(self, sql: str) -> str:
        return sql if self.placeholder == "?" else sql.replace("?", "%s")

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        with self._lock:
            if self._pg is not None:
                cur = self._pg.cursor()
                try:
                    yield cur
                finally:
                    cur.close()
            else:
                cur = self._sqlite.cursor()
                try:
                    yield cur
                finally:
                    cur.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.cursor() as cur:
            cur.execute(self._q(sql), params)

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(self._q(sql), params)
            rows = cur.fetchall()
            if self._pg is not None:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            return [dict(r) for r in rows]

    def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.fetchall(sql + " LIMIT 1", params)
        return rows[0] if rows else None

    def close(self) -> None:
        if self._pg is not None:
            self._pg.close()
        else:
            self._sqlite.close()

    # ---------------------------------------------------------------- jobs
    def create_job(
        self,
        job_id: str,
        source_path: str,
        *,
        source_hash: str = "",
        slug: str = "",
        work_dir: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        self.execute(
            """INSERT INTO jobs (id, source_path, source_hash, slug, status, work_dir,
                                 created_at, updated_at, meta)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (job_id, source_path, source_hash, slug, work_dir, now, now, json.dumps(meta or {})),
        )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return _decode_job(row) if row else None

    def find_job_by_hash(self, source_hash: str) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT * FROM jobs WHERE source_hash = ? ORDER BY created_at DESC", (source_hash,)
        )
        return _decode_job(row) if row else None

    def find_job_by_source(self, source_path: str) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT * FROM jobs WHERE source_path = ? ORDER BY created_at DESC", (source_path,)
        )
        return _decode_job(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "meta" in fields and isinstance(fields["meta"], dict):
            fields["meta"] = json.dumps(fields["meta"], default=str)
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def list_jobs(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if status:
            rows = self.fetchall(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.fetchall("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_decode_job(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self.fetchall("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    def delete_job(self, job_id: str) -> None:
        self.execute("DELETE FROM job_steps WHERE job_id = ?", (job_id,))
        self.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # --------------------------------------------------------------- steps
    def get_steps(self, job_id: str) -> dict[str, dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM job_steps WHERE job_id = ?", (job_id,))
        out = {}
        for r in rows:
            r["outputs"] = json.loads(r["outputs"]) if r.get("outputs") else {}
            out[r["step"]] = r
        return out

    def step_started(self, job_id: str, step: str) -> None:
        existing = self.fetchone(
            "SELECT attempts FROM job_steps WHERE job_id = ? AND step = ?", (job_id, step)
        )
        now = utcnow()
        if existing:
            self.execute(
                """UPDATE job_steps SET status='running', attempts=attempts+1, started_at=?,
                   finished_at=NULL, error=NULL WHERE job_id=? AND step=?""",
                (now, job_id, step),
            )
        else:
            self.execute(
                """INSERT INTO job_steps (job_id, step, status, attempts, started_at)
                   VALUES (?, ?, 'running', 1, ?)""",
                (job_id, step, now),
            )
        self.update_job(job_id, current_step=step)

    def step_finished(
        self, job_id: str, step: str, *, outputs: dict[str, Any] | None = None, duration: float = 0
    ) -> None:
        self.execute(
            """UPDATE job_steps SET status='done', finished_at=?, duration_seconds=?, outputs=?
               WHERE job_id=? AND step=?""",
            (utcnow(), duration, json.dumps(outputs or {}, default=str), job_id, step),
        )

    def step_skipped(self, job_id: str, step: str) -> None:
        now = utcnow()
        self.execute("DELETE FROM job_steps WHERE job_id=? AND step=?", (job_id, step))
        self.execute(
            """INSERT INTO job_steps (job_id, step, status, attempts, started_at, finished_at)
               VALUES (?, ?, 'skipped', 0, ?, ?)""",
            (job_id, step, now, now),
        )

    def step_failed(self, job_id: str, step: str, error: str) -> None:
        self.execute(
            """UPDATE job_steps SET status='failed', finished_at=?, error=?
               WHERE job_id=? AND step=?""",
            (utcnow(), error[:4000], job_id, step),
        )

    # ------------------------------------------------------------- metrics
    def add_metrics(self, job_id: str | None, platform: str, **values: Any) -> int:
        allowed = {
            "views",
            "likes",
            "comments",
            "shares",
            "saves",
            "watch_time_seconds",
            "avg_watch_seconds",
            "completion_rate",
            "follower_growth",
            "notes",
            "recorded_at",
        }
        data = {k: v for k, v in values.items() if k in allowed}
        data.setdefault("recorded_at", utcnow())
        cols = ["job_id", "platform", *data.keys()]
        placeholders = ", ".join("?" for _ in cols)
        self.execute(
            f"INSERT INTO metrics ({', '.join(cols)}) VALUES ({placeholders})",
            (job_id, platform, *data.values()),
        )
        row = self.fetchone("SELECT MAX(id) AS id FROM metrics")
        return int(row["id"]) if row and row["id"] is not None else 0

    def list_metrics(self, job_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        if job_id:
            return self.fetchall(
                "SELECT * FROM metrics WHERE job_id = ? ORDER BY recorded_at DESC LIMIT ?",
                (job_id, limit),
            )
        return self.fetchall("SELECT * FROM metrics ORDER BY recorded_at DESC LIMIT ?", (limit,))

    def latest_metrics_per_job(self) -> list[dict[str, Any]]:
        """Most recent metrics row for every (job, platform) pair."""
        return self.fetchall(
            """SELECT m.* FROM metrics m
               JOIN (SELECT COALESCE(job_id, '') AS jid, platform, MAX(id) AS latest_id
                     FROM metrics GROUP BY COALESCE(job_id, ''), platform) l
                 ON m.id = l.latest_id
               ORDER BY m.recorded_at DESC, m.id DESC"""
        )

    # ------------------------------------------------------------ hashtags
    def add_hashtag_set(self, job_id: str | None, hashtags: list[str]) -> None:
        self.execute(
            "INSERT INTO hashtag_history (job_id, created_at, hashtags) VALUES (?, ?, ?)",
            (job_id, utcnow(), json.dumps(hashtags)),
        )

    def recent_hashtag_sets(self, limit: int = 5) -> list[list[str]]:
        rows = self.fetchall(
            "SELECT hashtags FROM hashtag_history ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [json.loads(r["hashtags"]) for r in rows]

    # -------------------------------------------------------------- events
    def add_event(self, level: str, message: str, job_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO events (created_at, level, job_id, message) VALUES (?, ?, ?, ?)",
            (utcnow(), level.upper(), job_id, message[:2000]),
        )

    def recent_events(self, limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
        if level:
            return self.fetchall(
                "SELECT * FROM events WHERE level = ? ORDER BY id DESC LIMIT ?",
                (level.upper(), limit),
            )
        return self.fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


def _decode_job(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    try:
        row["meta"] = json.loads(row.get("meta") or "{}")
    except json.JSONDecodeError:
        row["meta"] = {}
    return row

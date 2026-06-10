from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_sub TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    safe INTEGER NOT NULL DEFAULT 0,
                    risky INTEGER NOT NULL DEFAULT 0,
                    invalid INTEGER NOT NULL DEFAULT 0,
                    unknown INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subjobs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    server_name TEXT NOT NULL,
                    server_url TEXT NOT NULL,
                    batch_number INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    remote_job_id INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_sub, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_subjobs_job ON subjobs(job_id, server_name, batch_number);
                CREATE INDEX IF NOT EXISTS idx_results_job_status ON results(job_id, status);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "retry_delay_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN retry_delay_seconds INTEGER NOT NULL DEFAULT 300"
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE jobs
                SET status='failed', error='Coordinator restarted before completion', updated_at=?
                WHERE status IN ('queued', 'running', 'retrying')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE subjobs
                SET status='failed', error='Coordinator restarted before completion', updated_at=?
                WHERE status IN ('submitting', 'running')
                """,
                (now,),
            )

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(query, parameters)
            connection.commit()

    def executemany(self, query: str, parameters: list[tuple[Any, ...]]) -> None:
        if not parameters:
            return
        with self._lock, self._connect() as connection:
            connection.executemany(query, parameters)
            connection.commit()

    def fetchone(self, query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
            return dict(row) if row else None

    def fetchall(self, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def insert_results(self, job_id: str, results: list[dict[str, Any]]) -> None:
        rows = [
            (
                job_id,
                str(result.get("input", "")),
                str(result.get("is_reachable", "unknown")).lower(),
                json.dumps(result, separators=(",", ":")),
            )
            for result in results
        ]
        if not rows:
            return
        with self._lock, self._connect() as connection:
            # Retried emails replace their previous result so each email keeps
            # exactly one row per job.
            connection.executemany(
                "DELETE FROM results WHERE job_id=? AND email=?",
                [(job_id, row[1]) for row in rows],
            )
            connection.executemany(
                "INSERT INTO results (job_id, email, status, result_json) VALUES (?, ?, ?, ?)",
                rows,
            )
            connection.commit()

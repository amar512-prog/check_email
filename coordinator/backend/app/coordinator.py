from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from time import monotonic
from typing import Any

import httpx

from .config import ReacherServer, Settings
from .database import Database, utc_now


FINAL_REMOTE_STATUSES = {"completed", "failed"}


class Coordinator:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def server_health(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=5) as client:
            async def inspect(server: ReacherServer) -> dict[str, Any]:
                try:
                    response = await client.get(f"{server.url}/version")
                    response.raise_for_status()
                    return {
                        "name": server.name,
                        "status": "online",
                        "version": response.json().get("version"),
                        "emails_per_minute": server.emails_per_minute,
                    }
                except (httpx.HTTPError, ValueError):
                    return {
                        "name": server.name,
                        "status": "offline",
                        "version": None,
                        "emails_per_minute": server.emails_per_minute,
                    }

            return await asyncio.gather(*(inspect(server) for server in self.settings.servers))

    def create_job(self, user: dict[str, str], filename: str, emails: list[str]) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now()
        self.database.execute(
            """
            INSERT INTO jobs
                (id, user_sub, user_email, filename, status, total, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, user["sub"], user["email"], filename, len(emails), now, now),
        )
        self._tasks[job_id] = asyncio.create_task(self._run_job(job_id, emails))
        return job_id

    async def _run_job(self, job_id: str, emails: list[str]) -> None:
        self.database.execute(
            "UPDATE jobs SET status='running', updated_at=? WHERE id=?",
            (utc_now(), job_id),
        )
        allocations: dict[ReacherServer, list[list[str]]] = defaultdict(list)
        server_count = len(self.settings.servers)
        offsets = [0] * server_count

        for index, email in enumerate(emails):
            server_index = index % server_count
            server = self.settings.servers[server_index]
            batch_index = offsets[server_index] // server.emails_per_minute
            if len(allocations[server]) <= batch_index:
                allocations[server].append([])
            allocations[server][batch_index].append(email)
            offsets[server_index] += 1

        try:
            outcomes = await asyncio.gather(
                *(
                    self._run_server_batches(job_id, server, batches)
                    for server, batches in allocations.items()
                ),
                return_exceptions=True,
            )
            errors = [str(outcome) for outcome in outcomes if isinstance(outcome, Exception)]
            final_status = "failed" if errors else "completed"
            self.database.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                (final_status, "; ".join(errors)[:500] or None, utc_now(), job_id),
            )
        except Exception as exc:
            self.database.execute(
                "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(exc)[:500], utc_now(), job_id),
            )
        finally:
            self._tasks.pop(job_id, None)

    async def _run_server_batches(
        self,
        job_id: str,
        server: ReacherServer,
        batches: list[list[str]],
    ) -> None:
        for batch_number, batch in enumerate(batches, start=1):
            started = monotonic()
            await self._run_batch(job_id, server, batch_number, batch)
            if batch_number < len(batches) and self.settings.pacing_seconds > 0:
                elapsed = monotonic() - started
                await asyncio.sleep(max(0, self.settings.pacing_seconds - elapsed))

    async def _run_batch(
        self,
        job_id: str,
        server: ReacherServer,
        batch_number: int,
        emails: list[str],
    ) -> None:
        subjob_id = str(uuid.uuid4())
        now = utc_now()
        self.database.execute(
            """
            INSERT INTO subjobs
                (id, job_id, server_name, server_url, batch_number, total, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'submitting', ?, ?)
            """,
            (subjob_id, job_id, server.name, server.url, batch_number, len(emails), now, now),
        )

        headers = {"x-reacher-secret": server.secret}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                submit = await client.post(
                    f"{server.url}/v1/bulk",
                    headers=headers,
                    json={"input": emails},
                )
                submit.raise_for_status()
                remote_job_id = int(submit.json()["job_id"])
                self.database.execute(
                    "UPDATE subjobs SET remote_job_id=?, status='running', updated_at=? WHERE id=?",
                    (remote_job_id, utc_now(), subjob_id),
                )

                deadline = monotonic() + self.settings.job_timeout_seconds
                while True:
                    if monotonic() >= deadline:
                        raise TimeoutError(
                            f"{server.name} bulk job {remote_job_id} exceeded the configured timeout"
                        )
                    await asyncio.sleep(0.5)
                    progress = await client.get(
                        f"{server.url}/v1/bulk/{remote_job_id}",
                        headers=headers,
                    )
                    progress.raise_for_status()
                    remote = progress.json()
                    remote_status = str(remote.get("job_status", "running")).lower()
                    processed = int(remote.get("total_processed", 0))
                    self.database.execute(
                        "UPDATE subjobs SET processed=?, status=?, updated_at=? WHERE id=?",
                        (processed, remote_status, utc_now(), subjob_id),
                    )
                    self._refresh_job_counts(job_id)
                    if remote_status in FINAL_REMOTE_STATUSES:
                        if remote_status == "failed":
                            raise RuntimeError(f"{server.name} failed bulk job {remote_job_id}")
                        break

                results = await self._fetch_results(client, server, remote_job_id, headers)
                self.database.insert_results(job_id, results)
                self.database.execute(
                    "UPDATE subjobs SET processed=total, status='completed', updated_at=? WHERE id=?",
                    (utc_now(), subjob_id),
                )
                self._refresh_job_counts(job_id)
        except Exception as exc:
            self.database.execute(
                "UPDATE subjobs SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(exc)[:500], utc_now(), subjob_id),
            )
            self._refresh_job_counts(job_id)
            raise

    async def _fetch_results(
        self,
        client: httpx.AsyncClient,
        server: ReacherServer,
        remote_job_id: int,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        limit = 1000
        while True:
            response = await client.get(
                f"{server.url}/v1/bulk/{remote_job_id}/results",
                headers=headers,
                params={"format": "json", "limit": limit, "offset": offset},
            )
            response.raise_for_status()
            page = response.json().get("results", [])
            results.extend(page)
            if len(page) < limit:
                return results
            offset += limit

    def _refresh_job_counts(self, job_id: str) -> None:
        processed = self.database.fetchone(
            "SELECT COALESCE(SUM(processed), 0) AS count FROM subjobs WHERE job_id=?",
            (job_id,),
        )
        counts = self.database.fetchone(
            """
            SELECT
                SUM(CASE WHEN status='safe' THEN 1 ELSE 0 END) AS safe,
                SUM(CASE WHEN status='risky' THEN 1 ELSE 0 END) AS risky,
                SUM(CASE WHEN status='invalid' THEN 1 ELSE 0 END) AS invalid,
                SUM(CASE WHEN status='unknown' THEN 1 ELSE 0 END) AS unknown
            FROM results WHERE job_id=?
            """,
            (job_id,),
        ) or {}
        self.database.execute(
            """
            UPDATE jobs SET processed=?, safe=?, risky=?, invalid=?, unknown=?, updated_at=?
            WHERE id=?
            """,
            (
                int((processed or {}).get("count") or 0),
                int(counts.get("safe") or 0),
                int(counts.get("risky") or 0),
                int(counts.get("invalid") or 0),
                int(counts.get("unknown") or 0),
                utc_now(),
                job_id,
            ),
        )

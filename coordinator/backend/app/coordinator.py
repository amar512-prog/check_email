from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from time import monotonic
from typing import Any

import httpx

from .config import ReacherServer, Settings
from .database import Database, utc_now


logger = logging.getLogger("mailcheck.coordinator")


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

    def create_job(
        self,
        user: dict[str, str],
        filename: str,
        emails: list[str],
        retry_delay_seconds: int,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now()
        self.database.execute(
            """
            INSERT INTO jobs
                (id, user_sub, user_email, filename, status, total, retry_delay_seconds,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (job_id, user["sub"], user["email"], filename, len(emails), retry_delay_seconds, now, now),
        )
        self._tasks[job_id] = asyncio.create_task(self._run_job(job_id, emails, retry_delay_seconds))
        return job_id

    async def _run_job(self, job_id: str, emails: list[str], retry_delay_seconds: int) -> None:
        self.database.execute(
            "UPDATE jobs SET status='running', updated_at=? WHERE id=?",
            (utc_now(), job_id),
        )
        server_count = len(self.settings.servers)
        try:
            errors = await self._dispatch_round(
                job_id,
                [(email, index % server_count) for index, email in enumerate(emails)],
            )
            if not errors and self.settings.apify_token:
                # Hand the unknown tail to Apify (MillionVerifier) instead of
                # retrying on the Reacher servers. If Apify fails (e.g. credit
                # exhausted), fall back to the Reacher retry loop.
                if not await self._resolve_unknowns_with_apify(job_id):
                    errors = await self._run_reacher_retries(job_id, retry_delay_seconds)
            elif not errors:
                errors = await self._run_reacher_retries(job_id, retry_delay_seconds)
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

    async def _run_reacher_retries(self, job_id: str, retry_delay_seconds: int) -> list[str]:
        """Re-verify the job's unknown tail on the Reacher servers, up to
        UNKNOWN_RETRY_ATTEMPTS rounds. Returns transport errors, if any."""
        errors: list[str] = []
        for attempt in range(1, self.settings.unknown_retry_attempts + 1):
            unknowns = self._unknown_assignments(job_id, attempt)
            if not unknowns:
                break
            self.database.execute(
                "UPDATE jobs SET status='retrying', updated_at=? WHERE id=?",
                (utc_now(), job_id),
            )
            await asyncio.sleep(retry_delay_seconds)
            errors = await self._dispatch_round(job_id, unknowns)
            if errors:
                break
        return errors

    def _unknown_assignments(self, job_id: str, attempt: int) -> list[tuple[str, int]]:
        """Assign each unknown email to a server other than the one that
        produced its current result (different IP), stepping further around
        the ring on every retry round."""
        server_index_by_name = {server.name: i for i, server in enumerate(self.settings.servers)}
        server_count = len(self.settings.servers)
        assignments: list[tuple[str, int]] = []
        rows = self.database.fetchall(
            "SELECT email, result_json FROM results WHERE job_id=? AND status='unknown' ORDER BY id",
            (job_id,),
        )
        for position, row in enumerate(rows):
            backend = json.loads(row["result_json"]).get("debug", {}).get("backend_name")
            last_index = server_index_by_name.get(backend, position)
            assignments.append((row["email"], (last_index + attempt) % server_count))
        return assignments

    # Apify (MillionVerifier) result -> coordinator reachability.
    APIFY_REACHABILITY = {
        "ok": "safe",
        "invalid": "invalid",
        "disposable": "risky",
        "catch_all": "risky",
        "unknown": "unknown",
        "error": "unknown",
    }

    async def _resolve_unknowns_with_apify(self, job_id: str) -> bool:
        """Resolve the job's unknown tail via Apify. Returns True on success
        (including 'nothing to do'), False if the Apify call failed so the
        caller can fall back to Reacher retries."""
        unknowns = [
            row["email"]
            for row in self.database.fetchall(
                "SELECT email FROM results WHERE job_id=? AND status='unknown' ORDER BY id",
                (job_id,),
            )
        ]
        if not unknowns:
            return True
        self.database.execute(
            "UPDATE jobs SET status='retrying', updated_at=? WHERE id=?",
            (utc_now(), job_id),
        )
        try:
            items = await self._apify_verify(unknowns)
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            logger.warning(
                "Apify verification failed for job %s: HTTP %s %s — falling back to Reacher retries",
                job_id, exc.response.status_code, body,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Apify verification failed for job %s: %s — falling back to Reacher retries",
                job_id, exc,
            )
            return False
        results = [self._apify_to_result(item) for item in items if item.get("email")]
        if results:
            self.database.insert_results(job_id, results)
            self._refresh_job_counts(job_id)
        return True

    async def _apify_verify(self, emails: list[str]) -> list[dict[str, Any]]:
        actor = self.settings.apify_actor_id
        url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        headers = {"Authorization": f"Bearer {self.settings.apify_token}"}
        collected: list[dict[str, Any]] = []
        # run-sync caps at 300s; chunk so a large unknown tail stays well under it.
        chunk_size = 500
        async with httpx.AsyncClient(timeout=310) as client:
            for start in range(0, len(emails), chunk_size):
                chunk = emails[start : start + chunk_size]
                response = await client.post(url, headers=headers, json={"emails": chunk})
                response.raise_for_status()
                collected.extend(response.json())
        return collected

    def _apify_to_result(self, item: dict[str, Any]) -> dict[str, Any]:
        """Shape an Apify/MillionVerifier item like a Reacher result so the
        results table and CSV export keep working."""
        verdict = str(item.get("result", "unknown")).lower()
        reachable = self.APIFY_REACHABILITY.get(verdict, "unknown")
        return {
            "input": item.get("email", ""),
            "is_reachable": reachable,
            "mx": {"accepts_mail": reachable in {"safe", "risky"}, "records": []},
            "smtp": {
                "is_deliverable": reachable == "safe",
                "is_catch_all": verdict == "catch_all",
            },
            "debug": {"backend_name": "apify/millionverifier", "duration": {"secs": 0, "nanos": 0}},
            "millionverifier": item,
        }

    async def _dispatch_round(self, job_id: str, assignments: list[tuple[str, int]]) -> list[str]:
        allocations: dict[ReacherServer, list[list[str]]] = defaultdict(list)
        server_count = len(self.settings.servers)
        offsets = [0] * server_count

        for email, server_index in assignments:
            server_index %= server_count
            server = self.settings.servers[server_index]
            batch_index = offsets[server_index] // server.emails_per_minute
            if len(allocations[server]) <= batch_index:
                allocations[server].append([])
            allocations[server][batch_index].append(email)
            offsets[server_index] += 1

        outcomes = await asyncio.gather(
            *(
                self._run_server_batches(job_id, server, batches)
                for server, batches in allocations.items()
            ),
            return_exceptions=True,
        )
        return [str(outcome) for outcome in outcomes if isinstance(outcome, Exception)]

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
        # Live ticks come from subjob progress, but retry batches re-process
        # emails that already have results, so cap at the stored result count
        # ceiling: every email has at most one results row.
        processed = self.database.fetchone(
            """
            SELECT MIN(
                (SELECT total FROM jobs WHERE id=?),
                MAX(
                    (SELECT COALESCE(SUM(processed), 0) FROM subjobs WHERE job_id=?),
                    (SELECT COUNT(*) FROM results WHERE job_id=?)
                )
            ) AS count
            """,
            (job_id, job_id, job_id),
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

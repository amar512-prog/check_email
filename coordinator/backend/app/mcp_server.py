"""MCP server for the Mailcheck Coordinator.

Exposes the coordinator's verification workflow as MCP tools so agents (Claude
Code CLI + web app, Codex) can drive it directly. The server is served over
streamable-HTTP and mounted onto the coordinator's FastAPI app at ``/mcp`` (see
``main.py``); there is no standalone/stdio distribution.

The tools call the ``Coordinator`` / ``Database`` singletons **in-process** — the
same code path the REST routes use (via ``services.py``) — so a job created here
is identical to one created from the browser. Client requests are authenticated
by OAuth (see ``mcp_auth.py``); the tools do not need the coordinator's
``X-API-Key``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import HTTPException
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from . import services
from .mcp_auth import build_auth
from .services import coordinator, database, get_job_or_404, query_results, results_csv

# Build the FastMCP instance with OAuth enabled. ``build_auth`` returns the
# provider + AuthSettings (or (None, None) when auth is disabled for local
# testing via MCP_DISABLE_AUTH=1).
_auth_provider, _auth_settings = build_auth()
mcp = FastMCP(
    "mailcheck",
    instructions=(
        "Verify whether email addresses exist without sending mail. Submit a job "
        "with verify_emails (or verify_csv), then poll get_job until it is "
        "completed, and read get_results. For a single blocking call use "
        "verify_and_wait. Each address resolves to safe / risky / invalid / unknown."
    ),
    auth_server_provider=_auth_provider,
    auth=_auth_settings,
)


def _current_user() -> dict[str, str]:
    """Identity for job attribution, derived from the OAuth access token.

    Jobs are shared across all users in this app, so attribution is cosmetic;
    we still record who created the job when a token carries an email.
    """
    token = get_access_token()
    if token is None:
        return {"sub": "mcp", "email": "mcp", "name": "MCP", "picture": ""}
    email = getattr(token, "user_email", None) or f"{token.client_id}"
    sub = getattr(token, "user_sub", None) or f"mcp:{token.client_id}"
    return {"sub": sub, "email": email, "name": email, "picture": ""}


def _clean(message: HTTPException) -> ValueError:
    """Translate the coordinator's HTTPException into a plain, agent-readable error."""
    return ValueError(str(message.detail))


def _summary(job: dict[str, Any]) -> dict[str, Any]:
    """Compact per-category view of a job row."""
    return {
        "job_id": job["id"],
        "status": job["status"],
        "total": job["total"],
        "processed": job["processed"],
        "safe": job["safe"],
        "risky": job["risky"],
        "invalid": job["invalid"],
        "unknown": job["unknown"],
        "error": job.get("error"),
    }


def _trim(result: dict[str, Any]) -> dict[str, Any]:
    """Trim a raw Reacher result to the fields agents usually want."""
    return {
        "email": result.get("input"),
        "is_reachable": result.get("is_reachable"),
        "accepts_mail": result.get("mx", {}).get("accepts_mail"),
        "smtp_deliverable": result.get("smtp", {}).get("is_deliverable"),
        "catch_all": result.get("smtp", {}).get("is_catch_all"),
    }


@mcp.tool()
def verify_emails(emails: list[str], retry_delay_minutes: int = 1) -> dict[str, Any]:
    """Create a verification job from a list of email addresses.

    Duplicates and syntactically invalid addresses are dropped. Returns the
    ``job_id`` plus how many addresses were ``accepted`` / ``rejected``. Poll
    ``get_job`` until it is ``completed``, then read ``get_results`` — or use
    ``verify_and_wait`` to do both in one call.

    Args:
        emails: Email addresses to verify.
        retry_delay_minutes: Minutes to wait before re-verifying any ``unknown``
            results on a different server (1–15, default 1).
    """
    try:
        normalized, rejected = services.normalize_emails(emails)
    except HTTPException as exc:
        raise _clean(exc) from exc
    label = normalized[0] if len(normalized) == 1 else f"Manual entry ({len(normalized)} emails)"
    job_id = coordinator.create_job(
        _current_user(), label, normalized, services.retry_delay_to_seconds(retry_delay_minutes)
    )
    return {"job_id": job_id, "accepted": len(normalized), "rejected": rejected}


@mcp.tool()
def verify_csv(path: str, retry_delay_minutes: int = 1) -> dict[str, Any]:
    """Create a verification job from a local CSV file.

    The CSV's first column must hold email addresses (a header row named
    email/email_address/to_email is skipped). Max 10 MB and
    ``max_upload_emails`` unique addresses. Returns the ``job_id`` with
    ``accepted`` / ``rejected`` counts.

    Args:
        path: Absolute path to a CSV file on the machine running the coordinator.
        retry_delay_minutes: Minutes between retry rounds for ``unknown`` results (1–15).
    """
    try:
        with open(path, "rb") as handle:
            content = handle.read()
    except OSError as exc:
        raise ValueError(f"Could not read CSV file: {exc}") from exc
    try:
        normalized, rejected = services.parse_email_csv(content)
    except HTTPException as exc:
        raise _clean(exc) from exc
    filename = path.rsplit("/", 1)[-1] or "emails.csv"
    job_id = coordinator.create_job(
        _current_user(), filename, normalized, services.retry_delay_to_seconds(retry_delay_minutes)
    )
    return {"job_id": job_id, "accepted": len(normalized), "rejected": rejected}


@mcp.tool()
def list_jobs(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List verification jobs, newest first (paginated).

    Args:
        limit: Page size, clamped to 1–100.
        offset: Number of jobs to skip.
    """
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    total = database.fetchone("SELECT COUNT(*) AS count FROM jobs")
    jobs = database.fetchall(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return {
        "total": int((total or {}).get("count") or 0),
        "jobs": [_summary(job) for job in jobs],
    }


@mcp.tool()
def get_job(job_id: str) -> dict[str, Any]:
    """Get a job's status, counts, and per-server progress.

    ``status`` is one of ``queued`` / ``running`` / ``retrying`` / ``completed`` /
    ``failed``. Poll until ``completed`` or ``failed``.
    """
    try:
        job = get_job_or_404(job_id)
    except HTTPException as exc:
        raise _clean(exc) from exc
    view = _summary(job)
    view["servers"] = job.get("servers", [])
    view["created_at"] = job.get("created_at")
    view["updated_at"] = job.get("updated_at")
    return view


@mcp.tool()
def get_results(
    job_id: str,
    status: str = "all",
    sort: str = "default",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Read verified results for a job (paginated), trimmed to the common fields.

    Each row has ``email``, ``is_reachable`` (safe/risky/invalid/unknown),
    ``accepts_mail``, ``smtp_deliverable``, and ``catch_all``. Use ``download_csv``
    or the REST API for the full raw Reacher objects.

    Args:
        job_id: The job to read.
        status: Filter: ``all``, ``safe``, ``risky``, ``invalid``, or ``unknown``.
        sort: Order: ``default`` (insertion), ``email_asc``, or ``email_desc``.
        limit: Page size, clamped to 1–500.
        offset: Number of rows to skip.
    """
    try:
        page = query_results(job_id, status=status, sort=sort, limit=limit, offset=offset)
    except HTTPException as exc:
        raise _clean(exc) from exc
    return {
        "total": page["total"],
        "results": [_trim(result) for result in page["results"]],
    }


@mcp.tool()
def download_csv(job_id: str, out_path: str) -> dict[str, Any]:
    """Write a job's results to a flat CSV file and return the path.

    Columns: email, status, accepts_mail, smtp_deliverable, catch_all,
    duration_seconds (escaped against spreadsheet formula injection).

    Args:
        job_id: The job to export.
        out_path: Absolute path to write the CSV to.
    """
    try:
        csv_text = results_csv(job_id)
    except HTTPException as exc:
        raise _clean(exc) from exc
    try:
        with open(out_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(csv_text)
    except OSError as exc:
        raise ValueError(f"Could not write CSV file: {exc}") from exc
    return {"path": out_path, "bytes": len(csv_text.encode("utf-8"))}


@mcp.tool()
async def verify_and_wait(
    emails: list[str],
    retry_delay_minutes: int = 1,
    poll_interval_seconds: int = 5,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Verify a list of emails and block until the job finishes (or times out).

    Creates a job, polls until it is ``completed`` or ``failed``, then returns a
    per-category summary plus the first page (up to 200) of trimmed results. This
    is the most convenient entry point for one-shot verification.

    Args:
        emails: Email addresses to verify.
        retry_delay_minutes: Minutes between retry rounds for ``unknown`` results (1–15).
        poll_interval_seconds: How often to re-check job status (clamped to 1–60).
        timeout_seconds: Give up waiting after this many seconds (clamped to 10–7200);
            the job keeps running server-side and can still be polled with get_job.
    """
    created = verify_emails(emails, retry_delay_minutes=retry_delay_minutes)
    job_id = created["job_id"]
    poll_interval_seconds = min(max(poll_interval_seconds, 1), 60)
    timeout_seconds = min(max(timeout_seconds, 10), 7200)

    deadline = time.monotonic() + timeout_seconds
    job = get_job_or_404(job_id)
    while job["status"] not in {"completed", "failed"}:
        if time.monotonic() >= deadline:
            summary = _summary(job)
            summary["timed_out"] = True
            summary["accepted"] = created["accepted"]
            summary["rejected"] = created["rejected"]
            return summary
        await asyncio.sleep(poll_interval_seconds)
        job = get_job_or_404(job_id)

    summary = _summary(job)
    summary["accepted"] = created["accepted"]
    summary["rejected"] = created["rejected"]
    summary["timed_out"] = False
    summary["results"] = get_results(job_id, limit=200)["results"]
    return summary

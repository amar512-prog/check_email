"""Shared singletons and request helpers.

These are used by both the HTTP routes in ``main.py`` and the MCP tools in
``mcp_server.py`` so the two entry points produce identical behavior. Keeping
the ``settings`` / ``database`` / ``coordinator`` singletons here (rather than in
``main.py``) also avoids an import cycle: ``mcp_server`` imports from here, and
``main`` imports from both.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from fastapi import HTTPException

from .config import Settings
from .coordinator import Coordinator
from .database import Database


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

settings = Settings.from_env()
database = Database(settings.database_path)
coordinator = Coordinator(settings, database)


def retry_delay_to_seconds(minutes: int) -> int:
    return max(1, min(15, minutes)) * 60


def normalize_emails(values: list[str]) -> tuple[list[str], int]:
    emails: list[str] = []
    seen: set[str] = set()
    rejected = 0
    for raw in values:
        value = raw.strip().lower()
        if not value:
            continue
        if not EMAIL_PATTERN.match(value):
            rejected += 1
            continue
        if value not in seen:
            seen.add(value)
            emails.append(value)

    if not emails:
        raise HTTPException(status_code=400, detail="No valid email addresses found")
    if len(emails) > settings.max_upload_emails:
        raise HTTPException(
            status_code=400,
            detail=f"More than {settings.max_upload_emails} unique emails were provided",
        )
    return emails, rejected


def parse_email_csv(content: bytes) -> tuple[list[str], int]:
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV file is larger than 10 MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must use UTF-8 encoding") from exc

    values = [row[0] for row in csv.reader(io.StringIO(text)) if row]
    if values and values[0].strip().lower() in {"email", "email_address", "to_email"}:
        values = values[1:]
    return normalize_emails(values)


def spreadsheet_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def get_job_or_404(job_id: str) -> dict[str, Any]:
    # Jobs are shared across all signed-in users.
    job = database.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["servers"] = database.fetchall(
        """
        SELECT server_name, SUM(total) AS total, SUM(processed) AS processed,
               COUNT(*) AS batches,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_batches,
               MAX(CASE WHEN status='failed' THEN error END) AS error
        FROM subjobs WHERE job_id=? GROUP BY server_name ORDER BY server_name
        """,
        (job_id,),
    )
    return job


RESULTS_ORDER_BY = {
    "default": "id",
    "email_asc": "email ASC, id",
    "email_desc": "email DESC, id",
}


def query_results(
    job_id: str,
    status: str = "all",
    sort: str = "default",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Return ``{"total", "results"}`` — the raw Reacher result objects for a job.

    Shared by ``GET /api/jobs/{id}/results`` and the ``get_results`` MCP tool.
    """
    get_job_or_404(job_id)
    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    order_by = RESULTS_ORDER_BY.get(sort, "id")
    where = "job_id=?"
    parameters: list[Any] = [job_id]
    if status != "all":
        where += " AND status=?"
        parameters.append(status)
    total = database.fetchone(f"SELECT COUNT(*) AS count FROM results WHERE {where}", tuple(parameters))
    rows = database.fetchall(
        f"SELECT result_json FROM results WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        tuple(parameters + [limit, offset]),
    )
    return {
        "total": int((total or {}).get("count") or 0),
        "results": [json.loads(row["result_json"]) for row in rows],
    }


def results_csv(job_id: str) -> str:
    """Build the flat results CSV for a job (same columns as the download route)."""
    get_job_or_404(job_id)
    rows = database.fetchall(
        "SELECT email, status, result_json FROM results WHERE job_id=? ORDER BY id",
        (job_id,),
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "status", "accepts_mail", "smtp_deliverable", "catch_all", "duration_seconds"])
    for row in rows:
        result = json.loads(row["result_json"])
        duration = result.get("debug", {}).get("duration", {})
        seconds = float(duration.get("secs", 0)) + float(duration.get("nanos", 0)) / 1_000_000_000
        writer.writerow(
            [
                spreadsheet_safe(row["email"]),
                row["status"],
                result.get("mx", {}).get("accepts_mail", False),
                result.get("smtp", {}).get("is_deliverable", False),
                result.get("smtp", {}).get("is_catch_all", False),
                f"{seconds:.3f}",
            ]
        )
    return output.getvalue()

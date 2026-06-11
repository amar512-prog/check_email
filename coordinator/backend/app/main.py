from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, validate_basic_credentials, verify_google_credential
from .config import Settings
from .coordinator import Coordinator
from .database import Database


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
settings = Settings.from_env()
database = Database(settings.database_path)
coordinator = Coordinator(settings, database)

API_DESCRIPTION = """
**Mailcheck** distributes email-list verification across one or more Reacher
workers and exposes the results through this API.

### Workflow

1. **Create a job** — upload a CSV (`POST /api/jobs`) or send a JSON list
   (`POST /api/jobs/emails`). You get back a `job_id`.
2. **Poll the job** — `GET /api/jobs/{job_id}` until `status` is `completed`
   or `failed` (`running` / `retrying` while in progress).
3. **Read results** — page through `GET /api/jobs/{job_id}/results`, or
   download a flat CSV from `GET /api/jobs/{job_id}/download`.

### Result categories

Each address resolves to one of **`safe`**, **`risky`**, **`invalid`**, or
**`unknown`**. `unknown` results (often greylisting or provider blocks) are
automatically re-verified up to a few times on a *different* server; the wait
between rounds is the job's `retry_delay_minutes` (1–15, default 1).

### Authentication

- **Browser** — sign in with Google or username/password; the session cookie
  authorizes every call automatically.
- **Machine / API** — send an `X-API-Key: <key>` header. In this page click
  **Authorize**, paste the key once, then use **Try it out**.

### Quick start

```bash
curl -X POST https://email-verifier.revengineer.ai/api/jobs/emails \\
  -H "X-API-Key: <your-key>" -H "Content-Type: application/json" \\
  -d '{"emails":["amar@basisvps.com","jane@example.com"],"retry_delay_minutes":1}'
```
"""

OPENAPI_TAGS = [
    {"name": "Jobs", "description": "Create and track verification jobs."},
    {"name": "Results", "description": "Read or download verified results."},
]

app = FastAPI(
    title="Mailcheck Coordinator",
    version="0.1.0",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    swagger_ui_parameters={"defaultModelsExpandDepth": 0},
)
app.state.settings = settings
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="mailcheck_session",
    same_site="lax",
    https_only=settings.session_secure,
    max_age=60 * 60 * 12,
)


class GoogleLogin(BaseModel):
    credential: str = Field(
        ...,
        description="The Google ID-token (a JWT, starts with `eyJ…`) returned by the "
        "Sign-in-with-Google flow in the browser.",
    )


class EmailList(BaseModel):
    emails: list[str] = Field(
        ...,
        description="Email addresses to verify. Duplicates and invalid syntax are dropped.",
        examples=[["amar@basisvps.com", "jane@example.com"]],
    )
    retry_delay_minutes: int = Field(
        1,
        ge=1,
        le=15,
        description="Minutes to wait before re-verifying any 'unknown' results on a "
        "different server (1–15, default 1).",
    )


class PasswordLogin(BaseModel):
    username: str = Field(..., description="The configured AUTH_USERNAME.")
    password: str = Field(..., description="The configured AUTH_PASSWORD.")

    model_config = {"json_schema_extra": {"example": {"username": "admin", "password": "your-password"}}}


def retry_delay_to_seconds(minutes: int) -> int:
    return max(1, min(15, minutes)) * 60


@app.get("/api/config", include_in_schema=False)
async def public_config() -> dict[str, Any]:
    return {
        "auth_mode": settings.auth_mode,
        "google_client_id": settings.google_client_id,
        "password_enabled": settings.password_enabled,
        "max_upload_emails": settings.max_upload_emails,
        "servers": await coordinator.server_health(),
    }


@app.get("/api/auth/me", include_in_schema=False)
async def auth_me(request: Request) -> dict[str, Any]:
    return {"user": request.session.get("user")}


@app.post("/api/auth/google", include_in_schema=False)
async def auth_google(payload: GoogleLogin, request: Request) -> dict[str, Any]:
    if settings.auth_mode != "google":
        raise HTTPException(status_code=404, detail="Google login is not enabled")
    user = verify_google_credential(payload.credential, settings)
    request.session.clear()
    request.session["user"] = user
    return {"user": user}


@app.post("/api/auth/password", include_in_schema=False)
async def auth_password(payload: PasswordLogin, request: Request) -> dict[str, Any]:
    if not settings.password_enabled:
        raise HTTPException(status_code=404, detail="Password login is not enabled")
    if not validate_basic_credentials(payload.username, payload.password, settings):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    user = {
        "sub": f"user:{payload.username}",
        "email": payload.username,
        "name": payload.username,
        "picture": "",
    }
    request.session.clear()
    request.session["user"] = user
    return {"user": user}


@app.post("/api/auth/development", include_in_schema=False)
async def auth_development(request: Request) -> dict[str, Any]:
    if settings.auth_mode != "development":
        raise HTTPException(status_code=404, detail="Development login is disabled")
    user = {
        "sub": "local-development-user",
        "email": "developer@localhost",
        "name": "Local Developer",
        "picture": "",
    }
    request.session.clear()
    request.session["user"] = user
    return {"user": user}


@app.post("/api/auth/logout", include_in_schema=False)
async def auth_logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


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


@app.post(
    "/api/jobs",
    tags=["Jobs"],
    summary="Create a job from a CSV upload",
    description="Upload a CSV whose first column holds email addresses (a header row "
    "named email/email_address/to_email is skipped). Max 10 MB and "
    "`max_upload_emails` unique addresses.",
    response_description="The created job id with accepted and rejected counts.",
    responses={
        400: {"description": "Not a CSV / no valid emails / over the limit"},
        401: {"description": "Missing or invalid authentication"},
        413: {"description": "File larger than 10 MB"},
    },
)
async def create_job(
    file: UploadFile = File(..., description="CSV file; email addresses in the first column."),
    retry_delay_minutes: int = Form(
        1, description="Minutes between retry rounds for 'unknown' results (1–15)."
    ),
    user: dict[str, str] = Depends(current_user),
) -> dict[str, Any]:
    filename = file.filename or "emails.csv"
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a CSV file")
    emails, rejected = parse_email_csv(await file.read())
    job_id = coordinator.create_job(
        user, filename, emails, retry_delay_to_seconds(retry_delay_minutes)
    )
    return {"job_id": job_id, "accepted": len(emails), "rejected": rejected}


@app.post(
    "/api/jobs/emails",
    tags=["Jobs"],
    summary="Create a job from a list of emails",
    description="Submit one or many addresses as JSON. Duplicates and invalid syntax "
    "are dropped before the job is created.",
    response_description="The created job id with accepted and rejected counts.",
    responses={
        400: {"description": "No valid emails / over the limit"},
        401: {"description": "Missing or invalid authentication"},
        413: {"description": "Too many email addresses in one request"},
    },
)
async def create_job_from_emails(
    payload: EmailList,
    user: dict[str, str] = Depends(current_user),
) -> dict[str, Any]:
    if len(payload.emails) > settings.max_upload_emails * 2:
        raise HTTPException(status_code=413, detail="Too many email addresses in one request")
    emails, rejected = normalize_emails(payload.emails)
    label = emails[0] if len(emails) == 1 else f"Manual entry ({len(emails)} emails)"
    job_id = coordinator.create_job(
        user, label, emails, retry_delay_to_seconds(payload.retry_delay_minutes)
    )
    return {"job_id": job_id, "accepted": len(emails), "rejected": rejected}


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


@app.get(
    "/api/jobs",
    tags=["Jobs"],
    summary="List jobs (paginated)",
    description="Jobs across all users, newest first. Use `limit` (1–100) and `offset` "
    "to page; `total` is the full count.",
)
async def list_jobs(
    limit: int = Query(50, description="Page size, clamped to 1–100."),
    offset: int = Query(0, description="Number of jobs to skip."),
    user: dict[str, str] = Depends(current_user),
) -> dict[str, Any]:
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    total = database.fetchone("SELECT COUNT(*) AS count FROM jobs")
    return {
        "total": int((total or {}).get("count") or 0),
        "jobs": database.fetchall(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ),
    }


@app.get(
    "/api/jobs/{job_id}",
    tags=["Jobs"],
    summary="Job status and progress",
    description="Counts and per-server progress for one job. Poll until `status` is "
    "`completed` or `failed` (`running`/`retrying` while in progress).",
    responses={404: {"description": "Job not found"}},
)
async def job_status(job_id: str, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    return get_job_or_404(job_id)


RESULTS_ORDER_BY = {
    "default": "id",
    "email_asc": "email ASC, id",
    "email_desc": "email DESC, id",
}


@app.get(
    "/api/jobs/{job_id}/results",
    tags=["Results"],
    summary="Read verified results (paginated)",
    description="Returns the raw Reacher result objects. Each has an `is_reachable` of "
    "`safe`/`risky`/`invalid`/`unknown`, plus `mx`, `smtp`, and `debug` details. "
    "Use `sort` to order by email.",
    response_description="`total` matching count and a page of `results`.",
    responses={404: {"description": "Job not found"}},
)
async def job_results(
    job_id: str,
    status: str = Query("all", description="Filter: `all`, `safe`, `risky`, `invalid`, or `unknown`."),
    sort: str = Query("default", description="Order: `default` (insertion), `email_asc`, or `email_desc`."),
    limit: int = Query(200, description="Page size, clamped to 1–500."),
    offset: int = Query(0, description="Number of rows to skip."),
    user: dict[str, str] = Depends(current_user),
) -> dict[str, Any]:
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


@app.get(
    "/api/jobs/{job_id}/download",
    tags=["Results"],
    summary="Download results as CSV",
    description="A flat CSV with columns: email, status, accepts_mail, smtp_deliverable, "
    "catch_all, duration_seconds. Values are escaped against spreadsheet formula injection.",
    response_description="`text/csv` attachment.",
    responses={404: {"description": "Job not found"}},
)
async def download_results(job_id: str, user: dict[str, str] = Depends(current_user)) -> StreamingResponse:
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
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="mailcheck-{job_id}.csv"'
    return response


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        requested = (frontend_dist / path).resolve()
        try:
            requested.relative_to(frontend_dist.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="File not found")
        if path and requested.is_file():
            return FileResponse(requested)
        return FileResponse(frontend_dist / "index.html")

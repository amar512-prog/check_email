from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, verify_google_credential
from .config import Settings
from .coordinator import Coordinator
from .database import Database


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
settings = Settings.from_env()
database = Database(settings.database_path)
coordinator = Coordinator(settings, database)

app = FastAPI(title="Mailcheck Coordinator", version="0.1.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="mailcheck_session",
    same_site="lax",
    https_only=settings.session_secure,
    max_age=60 * 60 * 12,
)


class GoogleLogin(BaseModel):
    credential: str


class EmailList(BaseModel):
    emails: list[str]


@app.get("/api/config")
async def public_config() -> dict[str, Any]:
    return {
        "auth_mode": settings.auth_mode,
        "google_client_id": settings.google_client_id,
        "max_upload_emails": settings.max_upload_emails,
        "servers": await coordinator.server_health(),
    }


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    return {"user": request.session.get("user")}


@app.post("/api/auth/google")
async def auth_google(payload: GoogleLogin, request: Request) -> dict[str, Any]:
    if settings.auth_mode != "google":
        raise HTTPException(status_code=404, detail="Google login is not enabled")
    user = verify_google_credential(payload.credential, settings)
    request.session.clear()
    request.session["user"] = user
    return {"user": user}


@app.post("/api/auth/development")
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


@app.post("/api/auth/logout")
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


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    user: dict[str, str] = Depends(current_user),
) -> dict[str, Any]:
    filename = file.filename or "emails.csv"
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a CSV file")
    emails, rejected = parse_email_csv(await file.read())
    job_id = coordinator.create_job(user, filename, emails)
    return {"job_id": job_id, "accepted": len(emails), "rejected": rejected}


@app.post("/api/jobs/emails")
async def create_job_from_emails(
    payload: EmailList,
    user: dict[str, str] = Depends(current_user),
) -> dict[str, Any]:
    if len(payload.emails) > settings.max_upload_emails * 2:
        raise HTTPException(status_code=413, detail="Too many email addresses in one request")
    emails, rejected = normalize_emails(payload.emails)
    label = emails[0] if len(emails) == 1 else f"Manual entry ({len(emails)} emails)"
    job_id = coordinator.create_job(user, label, emails)
    return {"job_id": job_id, "accepted": len(emails), "rejected": rejected}


def get_owned_job(job_id: str, user: dict[str, str]) -> dict[str, Any]:
    job = database.fetchone("SELECT * FROM jobs WHERE id=? AND user_sub=?", (job_id, user["sub"]))
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


@app.get("/api/jobs")
async def list_jobs(user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    return {
        "jobs": database.fetchall(
            "SELECT * FROM jobs WHERE user_sub=? ORDER BY created_at DESC LIMIT 20",
            (user["sub"],),
        )
    }


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    return get_owned_job(job_id, user)


@app.get("/api/jobs/{job_id}/results")
async def job_results(
    job_id: str,
    status: str = "all",
    limit: int = 200,
    offset: int = 0,
    user: dict[str, str] = Depends(current_user),
) -> dict[str, Any]:
    get_owned_job(job_id, user)
    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    where = "job_id=?"
    parameters: list[Any] = [job_id]
    if status != "all":
        where += " AND status=?"
        parameters.append(status)
    total = database.fetchone(f"SELECT COUNT(*) AS count FROM results WHERE {where}", tuple(parameters))
    rows = database.fetchall(
        f"SELECT result_json FROM results WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
        tuple(parameters + [limit, offset]),
    )
    return {
        "total": int((total or {}).get("count") or 0),
        "results": [json.loads(row["result_json"]) for row in rows],
    }


@app.get("/api/jobs/{job_id}/download")
async def download_results(job_id: str, user: dict[str, str] = Depends(current_user)) -> StreamingResponse:
    get_owned_job(job_id, user)
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

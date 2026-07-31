from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from pydantic import BaseModel, Field
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, validate_basic_credentials, verify_google_credential
from .mcp_auth import oauth_router
from .mcp_server import mcp
from .services import (
    coordinator,
    database,
    get_job_or_404,
    normalize_emails,
    parse_email_csv,
    query_results,
    results_csv,
    retry_delay_to_seconds,
    settings,
)

# Build the MCP Starlette app once. This lazily creates the streamable-HTTP
# session manager and the OAuth + /mcp routes; we then lift those routes onto
# the FastAPI app below so OAuth discovery lives at the site root.
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The MCP endpoint needs its session manager running for the app's lifetime.
    async with mcp.session_manager.run():
        yield

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
    lifespan=lifespan,
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

# --- MCP server wiring -------------------------------------------------------
# Lift the MCP + OAuth routes (built above) onto this app so /mcp and the OAuth
# discovery documents are served from the site root. Added before the frontend
# catch-all further down, so they take precedence. When auth is enabled we also
# install the bearer middleware (so /mcp validates tokens and tools can read the
# caller's identity) and the Google-backed login routes.
app.router.routes.extend(mcp_app.routes)
if mcp._token_verifier is not None:
    app.add_middleware(AuthContextMiddleware)
    app.add_middleware(AuthenticationMiddleware, backend=BearerAuthBackend(mcp._token_verifier))
    app.include_router(oauth_router)


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
    return query_results(job_id, status=status, sort=sort, limit=limit, offset=offset)


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
    response = StreamingResponse(iter([results_csv(job_id)]), media_type="text/csv")
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

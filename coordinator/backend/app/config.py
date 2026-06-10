from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ReacherServer:
    name: str
    url: str
    secret: str
    emails_per_minute: int = 50


@dataclass(frozen=True)
class Settings:
    auth_mode: str
    google_client_id: str
    google_allowed_domains: tuple[str, ...]
    session_secret: str
    session_secure: bool
    database_path: str
    max_upload_emails: int
    pacing_seconds: float
    job_timeout_seconds: float
    servers: tuple[ReacherServer, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        raw_servers = os.environ.get("REACHER_SERVERS_JSON", "[]")
        try:
            server_items = json.loads(raw_servers)
        except json.JSONDecodeError as exc:
            raise RuntimeError("REACHER_SERVERS_JSON must be valid JSON") from exc

        servers = tuple(
            ReacherServer(
                name=item["name"],
                url=item["url"].rstrip("/"),
                secret=item["secret"],
                emails_per_minute=max(1, int(item.get("emails_per_minute", 50))),
            )
            for item in server_items
        )
        if not servers:
            raise RuntimeError("At least one Reacher server must be configured")

        auth_mode = os.environ.get("AUTH_MODE", "google").lower()
        if auth_mode not in {"development", "google"}:
            raise RuntimeError("AUTH_MODE must be development or google")
        google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        if auth_mode == "google" and not google_client_id:
            raise RuntimeError("GOOGLE_CLIENT_ID is required when AUTH_MODE=google")

        session_secret = os.environ.get("SESSION_SECRET", "")
        if len(session_secret) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters")

        domains = tuple(
            domain.strip().lower()
            for domain in os.environ.get("GOOGLE_ALLOWED_DOMAINS", "").split(",")
            if domain.strip()
        )
        return cls(
            auth_mode=auth_mode,
            google_client_id=google_client_id,
            google_allowed_domains=domains,
            session_secret=session_secret,
            session_secure=os.environ.get("SESSION_SECURE", "true").lower() == "true",
            database_path=os.environ.get("COORDINATOR_DB", "./data/coordinator.db"),
            max_upload_emails=int(os.environ.get("MAX_UPLOAD_EMAILS", "10000")),
            pacing_seconds=float(os.environ.get("REACHER_PACING_SECONDS", "60")),
            job_timeout_seconds=float(os.environ.get("REACHER_JOB_TIMEOUT_SECONDS", "900")),
            servers=servers,
        )

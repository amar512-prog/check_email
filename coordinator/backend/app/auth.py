from __future__ import annotations

import secrets
from typing import Any, Optional

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from .config import Settings


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def current_user(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
) -> dict[str, Any]:
    user = request.session.get("user")
    if user:
        return user
    settings: Settings = request.app.state.settings
    if api_key and settings.api_key and secrets.compare_digest(api_key, settings.api_key):
        return {"sub": "api-key", "email": "api-key", "name": "API key", "picture": ""}
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def validate_basic_credentials(username: str, password: str, settings: Settings) -> bool:
    if not settings.password_enabled:
        return False
    return secrets.compare_digest(username, settings.basic_auth_username) and secrets.compare_digest(
        password, settings.basic_auth_password
    )


def verify_google_credential(credential: str, settings: Settings) -> dict[str, str]:
    try:
        claims = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            settings.google_client_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Google credential") from exc

    email = str(claims.get("email", "")).lower()
    if not claims.get("email_verified") or not email:
        raise HTTPException(status_code=403, detail="A verified Google email is required")

    if settings.google_allowed_domains:
        domain = email.rsplit("@", 1)[-1]
        if domain not in settings.google_allowed_domains:
            raise HTTPException(status_code=403, detail="This Google account is not allowed")

    return {
        "sub": str(claims["sub"]),
        "email": email,
        "name": str(claims.get("name") or email.split("@", 1)[0]),
        "picture": str(claims.get("picture", "")),
    }


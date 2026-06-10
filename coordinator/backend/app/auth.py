from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from .config import Settings


def current_user(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


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


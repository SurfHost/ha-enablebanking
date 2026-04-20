"""JWT minting for Enable Banking API authentication.

Enable Banking caps JWTs at 86400 s (24 h). This module generates
short-lived tokens (23 h) and is used by both the config flow and the
coordinator for silent renewal.
"""

from __future__ import annotations

import base64
import json
import time

import jwt as _jwt

JWT_TTL_SECONDS: int = 82800  # 23 h — safely below the 86400 s hard cap


def mint_jwt(private_key_pem: str, app_id: str) -> str:
    """Sign and return a fresh Enable Banking RS256 JWT."""
    now = int(time.time())
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
    }
    token: str = _jwt.encode(
        payload,
        private_key_pem.encode(),
        algorithm="RS256",
        headers={"typ": "JWT", "kid": app_id},
    )
    return token


def jwt_seconds_remaining(token: str) -> int:
    """Return seconds until the JWT expires (negative if already expired)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0)) - int(time.time())
    except Exception:  # noqa: BLE001
        return -1

"""Tests for `jwt_helper`.

The coordinator re-mints a JWT every 23 hours without anyone watching, so the
claims have to be right unattended: Enable Banking rejects a token whose `aud`,
`iss` or `kid` is wrong, and rejects any TTL over 24 hours outright.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from jwt.exceptions import PyJWTError

from custom_components.enablebanking.jwt_helper import (
    JWT_TTL_SECONDS,
    jwt_seconds_remaining,
    mint_jwt,
)

APP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _public_key_of(private_key_pem: str) -> str:
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def test_token_verifies_against_the_public_key(rsa_private_key_pem: str) -> None:
    """A real RS256 verification, not just a claim that encode() returned."""
    token = mint_jwt(rsa_private_key_pem, APP_ID)

    claims = pyjwt.decode(
        token,
        _public_key_of(rsa_private_key_pem),
        algorithms=["RS256"],
        audience="api.enablebanking.com",
    )

    assert claims["iss"] == "enablebanking.com"
    assert claims["aud"] == "api.enablebanking.com"


def test_headers_carry_the_application_id(rsa_private_key_pem: str) -> None:
    """`kid` is how Enable Banking knows which application signed the token."""
    token = mint_jwt(rsa_private_key_pem, APP_ID)

    headers = pyjwt.get_unverified_header(token)

    assert headers["kid"] == APP_ID
    assert headers["alg"] == "RS256"
    assert headers["typ"] == "JWT"


def test_ttl_stays_under_the_api_hard_cap(rsa_private_key_pem: str) -> None:
    """Enable Banking refuses any token whose lifetime exceeds 86400 s."""
    token = mint_jwt(rsa_private_key_pem, APP_ID)
    claims = pyjwt.decode(token, options={"verify_signature": False}, audience=None)

    assert claims["exp"] - claims["iat"] == JWT_TTL_SECONDS
    assert JWT_TTL_SECONDS < 86400


def test_seconds_remaining_tracks_the_expiry(rsa_private_key_pem: str) -> None:
    token = mint_jwt(rsa_private_key_pem, APP_ID)

    remaining = jwt_seconds_remaining(token)

    # Generous window: this asserts the value is the real expiry rather than
    # a placeholder, without becoming flaky on a slow runner.
    assert JWT_TTL_SECONDS - 60 <= remaining <= JWT_TTL_SECONDS


def test_seconds_remaining_is_negative_for_an_expired_token(rsa_private_key_pem: str) -> None:
    """The renewal check treats <= 1800 as "renew now", so this must go negative."""
    past = int(time.time()) - 10_000
    token = pyjwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": past, "exp": past + 60},
        rsa_private_key_pem.encode(),
        algorithm="RS256",
        headers={"typ": "JWT", "kid": APP_ID},
    )

    assert jwt_seconds_remaining(token) < 0


@pytest.mark.parametrize(
    "token", ["", "not-a-jwt", "a.b", "a.b.c"], ids=["empty", "garbage", "two-parts", "bad-payload"]
)
def test_seconds_remaining_survives_a_malformed_token(token: str) -> None:
    """Returns a negative number rather than raising mid-poll."""
    assert jwt_seconds_remaining(token) < 0


def test_minting_with_a_bad_key_raises(rsa_private_key_pem: str) -> None:
    """The config flow relies on this to report `invalid_auth`."""
    with pytest.raises(PyJWTError):
        mint_jwt("-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----", APP_ID)

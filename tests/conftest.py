"""Shared fixtures for the Enable Banking test suite."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Let Home Assistant load `custom_components/enablebanking` in every test.

    Without this the config flow is simply not registered and every flow test
    fails with `Invalid handler specified`.
    """
    yield


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> str:
    """A throwaway RSA private key in PEM form.

    Generated rather than committed: a PEM in the repository is the kind of
    thing that gets copied into a real config by mistake, and secret scanners
    flag it. 2048 bits keeps generation fast while still being a key RS256
    actually accepts.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def session_payload() -> dict[str, Any]:
    """A GET /sessions/{id} body in the shape most ASPSPs return.

    Bare uid strings in ``accounts`` with the metadata alongside in
    ``accounts_data`` — the N26 / de Volksbank shape.
    """
    return {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "status": "AUTHORIZED",
        "accounts": ["uid-one", "uid-two"],
        "accounts_data": [
            {
                "uid": "uid-one",
                "identification_hash": "hash-one",
                "account_id": {"iban": "NL91ABNA0417164300"},
                "name": "Betaalrekening",
                "product": "Current Account",
                "currency": "EUR",
            },
            {
                "uid": "uid-two",
                "identification_hash": "hash-two",
                "iban": "SE4550000000058398257466",
                "name": "Sparkonto",
                "currency": "SEK",
            },
        ],
        "aspsp": {"name": "ASN Bank", "country": "NL"},
        "access": {"valid_until": "2026-12-01T00:00:00+00:00"},
    }

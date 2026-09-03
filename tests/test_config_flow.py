"""Tests for the four-step setup flow.

Covers the path a user actually walks — credentials, country, bank, bank
authorisation — and the recoveries from each way it can fail. A config flow
that shows an error but cannot then succeed is a common and miserable bug, so
every error case here goes on to complete the flow.

Reauth is deliberately not covered here: it is being restructured separately,
and tests pinning its current step ids would only have to be rewritten.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.enablebanking.const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_AUTH_CODE,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_BUSINESS,
    PSU_PERSONAL,
)
from custom_components.enablebanking.errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
)

APP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AUTH_URL = "https://auth.enablebanking.com/ais/start?sessionid=abc"
SESSION_ID = "11111111-2222-3333-4444-555555555555"

ASPSPS = [
    {"name": "ASN Bank", "country": "NL"},
    {"name": "N26", "country": "DE"},
    {"name": "Revolut", "country": "LT"},
]


@pytest.fixture
def client() -> MagicMock:
    """A stand-in Enable Banking client with every call succeeding."""
    mock = MagicMock()
    mock.async_get_aspsps = AsyncMock(return_value=ASPSPS)
    mock.async_start_auth = AsyncMock(return_value=AUTH_URL)
    mock.async_create_session = AsyncMock(
        return_value={
            "session_id": SESSION_ID,
            "accounts": [{"uid": "uid-one"}],
            "access": {"valid_until": "2026-12-01T00:00:00+00:00"},
        }
    )
    mock.async_validate = AsyncMock(return_value=True)
    return mock


async def _run_to_auth_step(hass: HomeAssistant, client: MagicMock, private_key: str) -> str:
    """Walk credentials -> country -> bank, returning the auth step's flow id."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRIVATE_KEY: private_key, CONF_APP_ID: APP_ID}
    )
    assert result["step_id"] == "country"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ASPSP_COUNTRY: "NL"}
    )
    assert result["step_id"] == "aspsp"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ASPSP_NAME: "ASN Bank", CONF_PSU_TYPE: PSU_PERSONAL}
    )
    assert result["step_id"] == "auth"
    flow_id: str = result["flow_id"]
    return flow_id


async def test_full_setup_flow_creates_the_entry(
    hass: HomeAssistant, client: MagicMock, rsa_private_key_pem: str
) -> None:
    """The happy path, end to end."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client
        client_cls.return_value = client

        flow_id = await _run_to_auth_step(hass, client, rsa_private_key_pem)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_AUTH_CODE: "the-auth-code"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ASN Bank"
    assert result["data"][CONF_SESSION_ID] == SESSION_ID
    assert result["data"][CONF_APP_ID] == APP_ID
    assert result["data"][CONF_ASPSP_NAME] == "ASN Bank"
    assert result["data"][CONF_ASPSP_COUNTRY] == "NL"
    # The key is stored so the coordinator can re-mint a JWT unattended, with
    # surrounding whitespace trimmed off the paste.
    assert result["data"][CONF_PRIVATE_KEY] == rsa_private_key_pem.strip()
    client.async_create_session.assert_awaited_once_with("the-auth-code")


async def test_business_account_is_labelled_in_the_title(
    hass: HomeAssistant, client: MagicMock, rsa_private_key_pem: str
) -> None:
    """Revolut personal and Revolut business are separate entries; the title says which."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client
        client_cls.return_value = client

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRIVATE_KEY: rsa_private_key_pem, CONF_APP_ID: APP_ID},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ASPSP_COUNTRY: "LT"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ASPSP_NAME: "Revolut", CONF_PSU_TYPE: PSU_BUSINESS},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AUTH_CODE: "the-auth-code"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Revolut (business)"


async def test_unparseable_private_key_is_reported_and_recoverable(
    hass: HomeAssistant, client: MagicMock, rsa_private_key_pem: str
) -> None:
    """A truncated PEM is the single most common setup mistake."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client
        client_cls.return_value = client

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRIVATE_KEY: "-----BEGIN PRIVATE KEY-----\ntruncated\n", CONF_APP_ID: APP_ID},
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "invalid_auth"}

        # ... and the same flow still completes once the real key is pasted.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRIVATE_KEY: rsa_private_key_pem, CONF_APP_ID: APP_ID},
        )

    assert result["step_id"] == "country"


@pytest.mark.parametrize(
    ("raised", "expected_error"),
    [
        (EnableBankingAuthenticationError("rejected"), "invalid_auth"),
        (EnableBankingConnectionError("offline"), "cannot_connect"),
        (RuntimeError("something else"), "unknown"),
    ],
    ids=["rejected", "offline", "unexpected"],
)
async def test_credential_validation_failures_are_reported_and_recoverable(
    hass: HomeAssistant,
    client: MagicMock,
    rsa_private_key_pem: str,
    raised: Exception,
    expected_error: str,
) -> None:
    """Each failure mode maps to its own message, and none of them dead-ends."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client
        client_cls.return_value = client
        client.async_get_aspsps.side_effect = raised

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRIVATE_KEY: rsa_private_key_pem, CONF_APP_ID: APP_ID},
        )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected_error}

        client.async_get_aspsps.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRIVATE_KEY: rsa_private_key_pem, CONF_APP_ID: APP_ID},
        )

    assert result["step_id"] == "country"


@pytest.mark.parametrize(
    ("raised", "expected_error"),
    [
        (EnableBankingAPIError("bad code"), "invalid_auth_code"),
        (EnableBankingAuthenticationError("rejected"), "invalid_auth_code"),
        (EnableBankingConnectionError("offline"), "cannot_connect"),
    ],
    ids=["bad-code", "rejected", "offline"],
)
async def test_bad_authorisation_code_is_reported_and_recoverable(
    hass: HomeAssistant,
    client: MagicMock,
    rsa_private_key_pem: str,
    raised: Exception,
    expected_error: str,
) -> None:
    """Copying the wrong query parameter must not mean starting the flow over."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client
        client_cls.return_value = client

        flow_id = await _run_to_auth_step(hass, client, rsa_private_key_pem)

        client.async_create_session.side_effect = raised
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_AUTH_CODE: "wrong-parameter"}
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "auth"
        assert result["errors"] == {"base": expected_error}

        client.async_create_session.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_AUTH_CODE: "the-right-code"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_auth_step_offers_the_bank_link(
    hass: HomeAssistant, client: MagicMock, rsa_private_key_pem: str
) -> None:
    """The URL from POST /auth has to reach the user or the flow is a dead end."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client
        client_cls.return_value = client

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRIVATE_KEY: rsa_private_key_pem, CONF_APP_ID: APP_ID},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ASPSP_COUNTRY: "NL"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ASPSP_NAME: "ASN Bank", CONF_PSU_TYPE: PSU_PERSONAL},
        )

    assert result["step_id"] == "auth"
    assert result["description_placeholders"] == {"auth_url": AUTH_URL}
    client.async_start_auth.assert_awaited_once_with("ASN Bank", "NL", PSU_PERSONAL)


async def test_country_step_lists_only_countries_with_banks(
    hass: HomeAssistant, client: MagicMock, rsa_private_key_pem: str
) -> None:
    """The ASPSP list is long; the country step is what makes it navigable."""
    with (
        patch("custom_components.enablebanking.config_flow.EnableBankingClient") as client_cls,
        patch("custom_components.enablebanking.async_setup_entry", return_value=True),
    ):
        client_cls.for_config_flow.return_value = client
        client_cls.return_value = client

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PRIVATE_KEY: rsa_private_key_pem, CONF_APP_ID: APP_ID},
        )

    assert result["step_id"] == "country"
    options = result["data_schema"].schema[CONF_ASPSP_COUNTRY].config["options"]
    values = {option["value"] for option in options}
    assert values == {"NL", "DE", "LT"}
    # Rendered with a readable name, not a bare code.
    assert any(option["label"] == "Netherlands (NL)" for option in options)

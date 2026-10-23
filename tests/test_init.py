"""Setup, teardown and the sensor a successful poll produces.

The narrowest thing worth asserting about an integration is that it loads at
all, and that a poll turns into an entity someone can put on a dashboard.
Everything else in the suite is detail underneath these two.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enablebanking.const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_CONSENT_EXPIRES_AT,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_PERSONAL,
)
from custom_components.enablebanking.models import AccountBalance

IBAN = "NL91ABNA0417164300"


def _balance_entity_id(hass: HomeAssistant) -> str:
    """The balance sensor's entity_id, looked up rather than hardcoded.

    Resolved through the registry on purpose. The generated entity_id depends
    on the device name and on `has_entity_name`, so writing it out here would
    turn an unrelated naming change into a puzzling failure in tests that are
    really about setup and polling.
    """
    registry = er.async_get(hass)
    entities = [
        entry.entity_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.unique_id.endswith("_balance")
    ]
    assert len(entities) == 1, f"expected exactly one balance sensor, got {entities}"
    return entities[0]


@pytest.fixture
def account() -> AccountBalance:
    return AccountBalance(
        account_id="uid-one",
        stable_id="hash-one",
        iban=IBAN,
        name="Betaalrekening",
        product="Current Account",
        currency="EUR",
        balance=1234.56,
        balance_type="CLBD",
        reference_date="2026-09-01",
        last_polled_at=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
    )


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        title="ASN Bank",
        unique_id="abc123",
        data={
            CONF_JWT: "a.b.c",
            CONF_PRIVATE_KEY: "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
            CONF_APP_ID: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            CONF_SESSION_ID: "11111111-2222-3333-4444-555555555555",
            CONF_ASPSP_NAME: "ASN Bank",
            CONF_ASPSP_COUNTRY: "NL",
            CONF_PSU_TYPE: PSU_PERSONAL,
            CONF_CONSENT_EXPIRES_AT: "2026-12-01T00:00:00+00:00",
        },
    )
    mock_entry.add_to_hass(hass)
    return mock_entry


@pytest.fixture
def client(account: AccountBalance) -> MagicMock:
    mock = MagicMock()
    mock.async_get_all_balances = AsyncMock(return_value=({"hash-one": account}, set()))
    return mock


async def test_entry_sets_up_and_unloads(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """Load, then unload, leaving nothing behind."""
    with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_spends_no_quota_when_nothing_is_cached(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """Setup itself must not poll — PSD2 allows only four a day.

    The catch-up poll is scheduled behind a jittered timer instead, so that a
    restart loop cannot burn the day's allowance.
    """
    with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    client.async_get_all_balances.assert_not_awaited()


async def test_refresh_creates_a_balance_sensor(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """One poll, one sensor, carrying the figures a dashboard needs."""
    with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get(_balance_entity_id(hass))
    assert state is not None
    assert state.state == "1234.56"
    # friendly_name is deliberately not asserted: Home Assistant composes it
    # from the device and entity names and the rules have shifted between
    # releases, so pinning it here would make this test about HA rather than
    # about the integration.
    assert state.attributes["iban"] == IBAN
    assert state.attributes["account_name"] == "Betaalrekening"
    assert state.attributes["currency"] == "EUR"
    assert state.attributes["balance_type"] == "CLBD"
    assert state.attributes["aspsp"] == "ASN Bank"
    assert state.attributes["last_error"] == ""
    assert state.attributes["consent_days_remaining"] >= 0


async def test_sensor_keeps_its_value_when_a_poll_fails(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """The whole point of the disk cache: a blip must not blank the balance.

    A rate limit or a network drop leaves the last known figure on screen with
    `last_error` explaining itself, rather than the sensor going unavailable
    and putting a hole in the history graph.
    """
    from custom_components.enablebanking.errors import EnableBankingConnectionError

    with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(_balance_entity_id(hass)).state == "1234.56"

        client.async_get_all_balances.side_effect = EnableBankingConnectionError("offline")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get(_balance_entity_id(hass))
    assert state is not None
    assert state.state == "1234.56"
    assert state.attributes["last_error"] == "network"


async def test_refresh_service_polls_every_entry(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """`enablebanking.refresh` is the documented way to force a poll."""
    with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, "refresh")

        await hass.services.async_call(DOMAIN, "refresh", blocking=True)
        await hass.async_block_till_done()

    client.async_get_all_balances.assert_awaited()

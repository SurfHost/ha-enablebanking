"""ASN Bank Balance integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnableBankingClient
from .const import CONF_JWT, CONF_SCAN_INTERVAL, CONF_SESSION_ID, DEFAULT_SCAN_INTERVAL
from .coordinator import AsnBankConfigEntry, AsnBankCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: AsnBankConfigEntry
) -> bool:
    """Set up ASN Bank Balance from a config entry."""
    session = async_get_clientsession(hass)
    client = EnableBankingClient(
        session,
        jwt=entry.data[CONF_JWT],
        session_id=entry.data[CONF_SESSION_ID],
    )

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = AsnBankCoordinator(hass, client, scan_interval)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AsnBankConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: AsnBankConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)

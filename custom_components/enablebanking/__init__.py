"""Enable Banking integration for Home Assistant."""

from __future__ import annotations

import logging
import random
from datetime import datetime

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from .api import EnableBankingClient
from .const import CONF_JWT, CONF_SESSION_ID, DOMAIN, STARTUP_JITTER_SECONDS
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .services import register_transaction_service

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_REFRESH = "refresh"


def _register_services(hass: HomeAssistant) -> None:
    """Register the domain-wide ``enablebanking.refresh`` service once.

    Forces an immediate balance poll for every configured entry > handy for
    debugging (you don't need an existing sensor to trigger it) and still
    subject to the bank's PSD2 rate limit.
    """
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def _handle_refresh(_call: ServiceCall) -> None:
        entries: list[EnableBankingConfigEntry] = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            _LOGGER.debug("enablebanking.refresh: forcing poll for entry %s", entry.entry_id)
            await coordinator.async_refresh()

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _handle_refresh)
    register_transaction_service(hass)


async def async_setup_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> bool:
    """Set up Enable Banking from a config entry.

    Startup flow:
    1. Build client + coordinator.
    2. Hydrate coordinator from disk cache > sensors come up showing their
       last known balance, zero API calls.
    3. Forward platforms.
    4. Register scheduled polls at POLL_HOURS (10/14/18/22 local) with
       per-entry minute jitter.
    5. If the cache is older than the most recent scheduled slot that has
       already passed, trigger one catch-up refresh (with 0-60 s jitter to
       stagger multiple entries). Otherwise do nothing > the next scheduled
       poll handles it.
    """
    http = async_get_clientsession(hass)
    client = EnableBankingClient(
        http,
        jwt=entry.data[CONF_JWT],
        session_id=entry.data[CONF_SESSION_ID],
    )

    coordinator = EnableBankingCoordinator(hass, entry, client)
    await coordinator.async_load_cache()
    entry.runtime_data = coordinator

    _register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the four daily scheduled polls.
    for unsub in coordinator.register_scheduled_polls():
        entry.async_on_unload(unsub)

    # Catch up if we missed a scheduled slot while HA was down.
    if coordinator.needs_catchup():
        delay = random.uniform(0, STARTUP_JITTER_SECONDS)
        _LOGGER.debug(
            "Catch-up refresh for entry %s scheduled in %.0f s (last_refresh=%s)",
            entry.entry_id,
            delay,
            coordinator.last_refresh,
        )

        async def _catchup(_now: datetime) -> None:
            await coordinator.async_refresh()

        entry.async_on_unload(async_call_later(hass, delay, _catchup))
    else:
        _LOGGER.debug(
            "Cache for entry %s is fresh (last_refresh=%s); waiting for next scheduled slot",
            entry.entry_id,
            coordinator.last_refresh,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

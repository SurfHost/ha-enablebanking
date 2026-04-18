"""Enable Banking integration for Home Assistant."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.util.dt import utcnow

from .api import EnableBankingClient
from .const import (
    CONF_JWT,
    CONF_SCAN_INTERVAL,
    CONF_SESSION_ID,
    DEFAULT_SCAN_INTERVAL,
    STARTUP_JITTER_SECONDS,
)
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: EnableBankingConfigEntry
) -> bool:
    """Set up Enable Banking from a config entry.

    The startup path is quota-aware:

    1. Build the client and coordinator as usual.
    2. ``coordinator.async_load_cache()`` hydrates the coordinator from the
       on-disk balance cache and seeds ``coordinator.data`` — sensors come
       up showing their last known value, no API call made.
    3. We compute when the *next* scheduled poll is due based on the cached
       ``last_polled_at`` and the configured interval. If that moment is in
       the past (or there's no cache), we poll soon (after a 0–60 s random
       jitter to stagger restarts across multiple entries). Otherwise we
       wait.
    4. After that first scheduled refresh fires, the coordinator's own
       timer takes over with normal cadence.
    """
    http = async_get_clientsession(hass)
    client = EnableBankingClient(
        http,
        jwt=entry.data[CONF_JWT],
        session_id=entry.data[CONF_SESSION_ID],
    )

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = EnableBankingCoordinator(hass, entry, client, scan_interval)

    await coordinator.async_load_cache()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    delay = _compute_first_refresh_delay(coordinator.last_refresh, scan_interval)
    _LOGGER.debug(
        "Scheduling first post-restart refresh for entry %s in %.0f s "
        "(last_polled_at=%s, interval=%ds)",
        entry.entry_id,
        delay,
        coordinator.last_refresh,
        scan_interval,
    )

    async def _first_refresh(_now: datetime) -> None:
        await coordinator.async_refresh()

    entry.async_on_unload(async_call_later(hass, delay, _first_refresh))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EnableBankingConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: EnableBankingConfigEntry
) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _compute_first_refresh_delay(
    last_polled_at: datetime | None, scan_interval: int
) -> float:
    """Seconds to wait before the first post-startup refresh.

    - If we've never polled, poll shortly (jitter only).
    - If the next scheduled poll is in the past, poll shortly (jitter only).
    - Otherwise wait until the scheduled moment, plus jitter.
    """
    jitter = random.uniform(0, STARTUP_JITTER_SECONDS)
    if last_polled_at is None:
        return jitter
    next_poll = last_polled_at + timedelta(seconds=scan_interval)
    base = max(0.0, (next_poll - utcnow()).total_seconds())
    return base + jitter

"""Enable Banking integration for Home Assistant."""

from __future__ import annotations

import logging
import random
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_update_statistics_metadata,
    get_metadata,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .api import EnableBankingClient
from .const import CONF_JWT, CONF_SESSION_ID, DOMAIN, STARTUP_JITTER_SECONDS, STORAGE_VERSION
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import account_unique_id

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


async def async_migrate_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> bool:
    """Migrate an entry to the current version.

    v1 -> v2: balance sensors reported the euro *symbol* as their unit. Home
    Assistant documents `SensorDeviceClass.MONETARY` as taking an ISO 4217
    code, so they now report one. Changing a sensor's unit is normally what
    parks its long-term statistics behind a repair the user has to resolve by
    hand, so the existing statistics metadata is rewritten here instead.
    """
    if entry.version < 2:
        await _async_migrate_statistic_units(hass, entry)
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def _async_migrate_statistic_units(
    hass: HomeAssistant, entry: EnableBankingConfigEntry
) -> None:
    """Point each balance sensor's statistics at its account's ISO currency.

    Best effort throughout. A migration that cannot find the recorder, the
    cache or an entity has nothing to fix; failing setup over it would be a
    far worse outcome than a unit the user corrects once themselves.
    """
    if "recorder" not in hass.config.components:
        return

    stored = await Store[dict[str, Any]](
        hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cache"
    ).async_load()
    accounts = (stored or {}).get("accounts") or {}
    if not accounts:
        return

    registry = er.async_get(hass)
    # stable_id is not the entity_id: resolve through the unique_id scheme the
    # sensor platform actually registers under.
    wanted: dict[str, str] = {}
    for stable_id, raw in accounts.items():
        if not isinstance(raw, dict):
            continue
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, account_unique_id(entry.entry_id, stable_id, "balance")
        )
        currency = raw.get("currency")
        if entity_id and isinstance(currency, str) and currency:
            wanted[entity_id] = currency.upper()

    if not wanted:
        return

    try:
        recorder = get_instance(hass)
        metadata = await recorder.async_add_executor_job(
            partial(get_metadata, hass, statistic_ids=set(wanted))
        )
    except Exception:
        _LOGGER.exception("Could not read statistics metadata; leaving units alone")
        return

    for statistic_id, target in wanted.items():
        entry_meta = metadata.get(statistic_id)
        if entry_meta is None:
            continue
        current = entry_meta[1].get("unit_of_measurement")
        if current == target:
            continue
        _LOGGER.info(
            "Enable Banking: migrating statistics unit for %s from %s to %s",
            statistic_id,
            current,
            target,
        )
        async_update_statistics_metadata(
            hass,
            statistic_id,
            new_unit_of_measurement=target,
            # Monetary values have no conversion class; passing it explicitly
            # avoids the deprecation report for omitting it.
            new_unit_class=None,
        )

"""Enable Banking integration for Home Assistant."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import cast

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType

from .api import EnableBankingClient
from .const import (
    CONF_ASPSP_NAME,
    CONF_JWT,
    CONF_SESSION_ID,
    DEFAULT_TRANSACTION_HISTORY_DAYS,
    DOMAIN,
    MAX_TRANSACTION_HISTORY_DAYS,
    STARTUP_JITTER_SECONDS,
)
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.EVENT, Platform.SENSOR]

SERVICE_REFRESH = "refresh"
SERVICE_GET_TRANSACTIONS = "get_transactions"

ATTR_DAYS = "days"
ATTR_ACCOUNT = "account"


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

    async def _handle_get_transactions(call: ServiceCall) -> ServiceResponse:
        """Return the stored transaction window.

        A service rather than an entity on purpose. The list is far too big for
        entity attributes -- the recorder rewrites an entity's whole attribute
        set on every state change -- and Home Assistant has no way to backdate
        states, so past transactions cannot be replayed onto the event entity
        either. Handing them back on request is what makes the history that
        predates setup reachable at all.
        """
        days: int = call.data.get(ATTR_DAYS, DEFAULT_TRANSACTION_HISTORY_DAYS)
        wanted_account: str | None = call.data.get(ATTR_ACCOUNT)
        cutoff = (dt_util.now().date() - timedelta(days=days - 1)).isoformat()

        rows: list[dict[str, JsonValueType]] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            for stable_id, account in (
                coordinator.data.accounts if coordinator.data else {}
            ).items():
                if wanted_account and wanted_account not in (account.iban, account.name):
                    continue
                for transaction in coordinator.transactions_for(stable_id):
                    day = transaction.booking_date or transaction.value_date or ""
                    if day and day < cutoff:
                        continue
                    rows.append(
                        {
                            "bank": entry.data.get(CONF_ASPSP_NAME),
                            "iban": account.iban,
                            "account": account.name,
                            **transaction.as_event_payload(),
                        }
                    )

        # Newest first, which is the order anyone reading a statement expects.
        rows.sort(key=lambda row: str(row.get("booking_date") or ""), reverse=True)
        # cast because list is invariant: list[dict[str, JsonValueType]] is not
        # a list[JsonValueType] to the type checker, though every element is one.
        return {"transactions": cast(list[JsonValueType], rows), "count": len(rows)}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TRANSACTIONS,
        _handle_get_transactions,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_DAYS, default=DEFAULT_TRANSACTION_HISTORY_DAYS): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=MAX_TRANSACTION_HISTORY_DAYS)
                ),
                vol.Optional(ATTR_ACCOUNT): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )


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

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> None:
    """Reload when the options change.

    Turning transactions on or off adds or removes the event entities, which
    only happens on a platform setup, so the entry has to come back up.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

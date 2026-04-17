"""DataUpdateCoordinator for the ASN Bank Balance integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnableBankingClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingSessionError,
)
from .models import AsnBankData

_LOGGER = logging.getLogger(__name__)

type AsnBankConfigEntry = ConfigEntry[AsnBankCoordinator]


class AsnBankCoordinator(DataUpdateCoordinator[AsnBankData]):
    """Coordinator to fetch ASN Bank balances via Enable Banking."""

    config_entry: AsnBankConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: EnableBankingClient,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client

    async def _async_update_data(self) -> AsnBankData:
        """Fetch all account balances through Enable Banking."""
        data = self.data or AsnBankData()
        try:
            data.accounts = await self.client.async_get_all_balances()
        except EnableBankingAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnableBankingSessionError as err:
            # A stale session means the PSU has to re-authorise at their bank.
            # Signalling ConfigEntryAuthFailed gives the user a "Re-authenticate"
            # button in the UI.
            raise ConfigEntryAuthFailed(str(err)) from err
        except (EnableBankingConnectionError, EnableBankingAPIError) as err:
            raise UpdateFailed(str(err)) from err
        return data

"""DataUpdateCoordinator for the Enable Banking integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import utcnow

from .api import EnableBankingClient
from .const import (
    CONF_ASPSP_NAME,
    CONF_CONSENT_EXPIRES_AT,
    CONSENT_WARNING_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingRateLimitError,
    EnableBankingSessionError,
)
from .models import EnableBankingData

_LOGGER = logging.getLogger(__name__)

type EnableBankingConfigEntry = ConfigEntry[EnableBankingCoordinator]


class EnableBankingCoordinator(DataUpdateCoordinator[EnableBankingData]):
    """Coordinator to fetch balances via Enable Banking."""

    config_entry: EnableBankingConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: EnableBankingClient,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self._warned_expiry = False
        self.last_refresh: datetime | None = None

    async def _async_update_data(self) -> EnableBankingData:
        """Fetch all account balances through Enable Banking.

        Passes the previous poll's balances into ``async_get_all_balances``
        as a fallback so per-account 429s keep the sensor populated rather
        than flipping to unavailable. A whole-session 429 preserves the
        whole previous snapshot.
        """
        prev_accounts = self.data.accounts if self.data is not None else {}
        try:
            accounts = await self.client.async_get_all_balances(fallback=prev_accounts)
        except EnableBankingAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnableBankingSessionError as err:
            # Consent has expired or been revoked — surface the reauth UI.
            self.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnableBankingRateLimitError as err:
            if prev_accounts:
                _LOGGER.warning(
                    "Session-level PSD2 rate limit; keeping previous snapshot. %s",
                    err,
                )
                accounts = prev_accounts
            else:
                raise UpdateFailed(str(err)) from err
        except (EnableBankingConnectionError, EnableBankingAPIError) as err:
            raise UpdateFailed(str(err)) from err

        consent_expires_at = self._parse_consent_expires()
        self._maybe_warn_expiry(consent_expires_at)
        self.last_refresh = utcnow()

        return EnableBankingData(
            accounts=accounts,
            consent_expires_at=consent_expires_at,
        )

    def _parse_consent_expires(self) -> datetime | None:
        raw = self.config_entry.data.get(CONF_CONSENT_EXPIRES_AT)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None

    def _maybe_warn_expiry(self, consent_expires_at: datetime | None) -> None:
        if consent_expires_at is None or self._warned_expiry:
            return
        days_remaining = (consent_expires_at - utcnow()).days
        if days_remaining > CONSENT_WARNING_DAYS:
            return
        aspsp_name = self.config_entry.data.get(CONF_ASPSP_NAME, "your bank")
        persistent_notification.async_create(
            self.hass,
            message=(
                f"Your {aspsp_name} Enable Banking consent expires in "
                f"{days_remaining} day(s). Open **Settings → Devices & Services → "
                f"Enable Banking ({aspsp_name})** and click **Reconfigure** to renew "
                "before it expires and sensors go unavailable."
            ),
            title="Enable Banking consent expiring soon",
            notification_id=f"{DOMAIN}_expiry_{self.config_entry.entry_id}",
        )
        self._warned_expiry = True

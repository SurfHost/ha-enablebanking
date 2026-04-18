"""DataUpdateCoordinator for the Enable Banking integration.

Design notes (v0.4.0):

- Balances are persisted to ``.storage/enablebanking.<entry_id>.cache`` via
  ``Store``. On startup the coordinator hydrates its state from that cache
  before platforms are forwarded, so sensors come up immediately showing
  the last known balance — no API call burned at boot.

- ``_async_update_data`` NEVER raises. On any failure (rate limit, network,
  consent expiry, unexpected API error) it sets ``self.last_error`` to a
  short tag (``rate_limited`` / ``network`` / ``consent_expired`` / ``auth``
  / ``api``) and returns the cached snapshot so sensors keep displaying
  their last good value. The reauth card is still triggered directly via
  ``config_entry.async_start_reauth`` when we detect session or auth
  failures — we just don't degrade the sensor state to surface it.

- Per-account 429 back-off: when the API reports a UID was rate limited
  this cycle, we stamp ``rate_limited_until = now + update_interval`` on
  that cached entry. The next scheduled poll checks this stamp and tells
  the API to skip the UID entirely, saving a quota slot. On the subsequent
  poll the stamp has elapsed and normal cadence resumes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.dt import utcnow

from .api import EnableBankingClient
from .const import (
    CONF_ASPSP_NAME,
    CONF_CONSENT_EXPIRES_AT,
    CONSENT_WARNING_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
)
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingRateLimitError,
    EnableBankingSessionError,
)
from .models import AccountBalance, EnableBankingData

_LOGGER = logging.getLogger(__name__)

type EnableBankingConfigEntry = ConfigEntry[EnableBankingCoordinator]


class EnableBankingCoordinator(DataUpdateCoordinator[EnableBankingData]):
    """Coordinator to fetch balances via Enable Banking."""

    config_entry: EnableBankingConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EnableBankingConfigEntry,
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
        self.last_refresh: datetime | None = None
        self.last_error: str = ""
        self._warned_expiry = False
        self._cached: dict[str, AccountBalance] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cache"
        )

    # ------------------------------------------------------------------ #
    # Cache                                                                #
    # ------------------------------------------------------------------ #

    async def async_load_cache(self) -> None:
        """Hydrate ``self._cached`` from disk and seed ``coordinator.data``.

        Call this once in ``async_setup_entry`` before forwarding platforms.
        """
        stored = await self._store.async_load() or {}
        for uid, raw in (stored.get("accounts") or {}).items():
            if not isinstance(raw, dict):
                continue
            ab = _balance_from_stored(raw)
            if ab is not None:
                self._cached[uid] = ab

        self.last_refresh = _parse_iso(stored.get("last_polled_at"))

        if self._cached:
            _LOGGER.debug(
                "Hydrated %d account(s) from cache for entry %s",
                len(self._cached),
                self.config_entry.entry_id,
            )
            self.async_set_updated_data(
                EnableBankingData(
                    accounts=dict(self._cached),
                    consent_expires_at=self._parse_consent_expires(),
                )
            )

    async def _save_cache(self) -> None:
        await self._store.async_save(
            {
                "last_polled_at": self.last_refresh.isoformat()
                if self.last_refresh
                else None,
                "accounts": {
                    uid: _balance_to_stored(ab) for uid, ab in self._cached.items()
                },
            }
        )

    def cached_account(self, uid: str) -> AccountBalance | None:
        """Return the cached AccountBalance for ``uid``, or None."""
        return self._cached.get(uid)

    # ------------------------------------------------------------------ #
    # Refresh                                                              #
    # ------------------------------------------------------------------ #

    async def _async_update_data(self) -> EnableBankingData:
        """Fetch balances. NEVER raises — always returns cached data on error."""
        now = utcnow()
        skip_uids = {
            uid
            for uid, ab in self._cached.items()
            if ab.rate_limited_until is not None and ab.rate_limited_until > now
        }
        if skip_uids:
            _LOGGER.debug(
                "Skipping %d rate-limited account(s) this cycle: %s",
                len(skip_uids),
                sorted(u[:8] for u in skip_uids),
            )

        try:
            fresh, rate_limited_uids = await self.client.async_get_all_balances(
                fallback=self._cached,
                skip_uids=skip_uids,
            )
        except EnableBankingAuthenticationError as err:
            self.last_error = "auth"
            _LOGGER.warning("JWT rejected: %s — triggering reauth", err)
            self.config_entry.async_start_reauth(self.hass)
            return self._cached_snapshot()
        except EnableBankingSessionError as err:
            self.last_error = "consent_expired"
            _LOGGER.warning("Session expired: %s — triggering reauth", err)
            self.config_entry.async_start_reauth(self.hass)
            return self._cached_snapshot()
        except EnableBankingRateLimitError as err:
            self.last_error = "rate_limited"
            _LOGGER.warning(
                "Session-level PSD2 rate limit; keeping cached balances: %s", err
            )
            return self._cached_snapshot()
        except EnableBankingConnectionError as err:
            self.last_error = "network"
            _LOGGER.warning("Network error; keeping cached balances: %s", err)
            return self._cached_snapshot()
        except EnableBankingAPIError as err:
            self.last_error = "api"
            _LOGGER.warning("API error; keeping cached balances: %s", err)
            return self._cached_snapshot()

        self.last_error = ""
        self.last_refresh = now
        back_off_until = now + (self.update_interval or timedelta(hours=8))

        for uid, ab in fresh.items():
            if uid in rate_limited_uids:
                # The client returned the fallback entry; mark it for one
                # cycle of back-off and keep its prior last_polled_at.
                ab.rate_limited_until = back_off_until
            else:
                ab.last_polled_at = now
                ab.rate_limited_until = None
            self._cached[uid] = ab

        await self._save_cache()

        consent_expires_at = self._parse_consent_expires()
        self._maybe_warn_expiry(consent_expires_at)

        return EnableBankingData(
            accounts=dict(self._cached),
            consent_expires_at=consent_expires_at,
        )

    def _cached_snapshot(self) -> EnableBankingData:
        """Produce an EnableBankingData from the current cache."""
        return EnableBankingData(
            accounts=dict(self._cached),
            consent_expires_at=self._parse_consent_expires(),
        )

    # ------------------------------------------------------------------ #
    # Consent expiry                                                       #
    # ------------------------------------------------------------------ #

    def _parse_consent_expires(self) -> datetime | None:
        return _parse_iso(self.config_entry.data.get(CONF_CONSENT_EXPIRES_AT))

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
                "before it expires and balances go stale."
            ),
            title="Enable Banking consent expiring soon",
            notification_id=f"{DOMAIN}_expiry_{self.config_entry.entry_id}",
        )
        self._warned_expiry = True


# ---------------------------------------------------------------------- #
# Cache serialisation helpers                                             #
# ---------------------------------------------------------------------- #


def _balance_from_stored(data: dict[str, Any]) -> AccountBalance | None:
    try:
        return AccountBalance(
            account_id=str(data["account_id"]),
            iban=str(data.get("iban", "")),
            name=str(data.get("name", "")),
            product=data.get("product") if isinstance(data.get("product"), str) else None,
            currency=str(data.get("currency", "EUR")),
            balance=float(data["balance"]),
            balance_type=data.get("balance_type")
            if isinstance(data.get("balance_type"), str)
            else None,
            reference_date=data.get("reference_date")
            if isinstance(data.get("reference_date"), str)
            else None,
            last_polled_at=_parse_iso(data.get("last_polled_at")),
            rate_limited_until=_parse_iso(data.get("rate_limited_until")),
        )
    except (KeyError, TypeError, ValueError):
        _LOGGER.debug("Skipping malformed cached entry: %r", data)
        return None


def _balance_to_stored(ab: AccountBalance) -> dict[str, Any]:
    return {
        "account_id": ab.account_id,
        "iban": ab.iban,
        "name": ab.name,
        "product": ab.product,
        "currency": ab.currency,
        "balance": ab.balance,
        "balance_type": ab.balance_type,
        "reference_date": ab.reference_date,
        "last_polled_at": ab.last_polled_at.isoformat()
        if ab.last_polled_at
        else None,
        "rate_limited_until": ab.rate_limited_until.isoformat()
        if ab.rate_limited_until
        else None,
    }


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None

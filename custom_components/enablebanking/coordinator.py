"""DataUpdateCoordinator for the Enable Banking integration.

Design notes (v0.5.0):

- **Fixed-schedule polling**: polls fire at ``POLL_HOURS`` local time
  (10:00, 14:00, 18:00, 22:00) with a per-entry minute jitter so multiple
  banks don't burst at ``HH:00:00``. The minute offset is a deterministic
  hash of ``entry_id`` so it's stable across HA restarts.

- **``update_interval = None``**: we opt out of ``DataUpdateCoordinator``'s
  built-in interval scheduler entirely. All polls come from our
  ``async_track_time_change`` listeners or the one-shot catch-up.

- **Catch-up on startup**: if cache's ``last_polled_at`` is older than the
  most recent scheduled time that has passed, we trigger one refresh
  (with 0–60 s jitter). Otherwise we just wait for the next slot. This is
  what keeps HA restarts from burning PSD2 quota.

- **``_async_update_data`` NEVER raises.** On any failure (rate limit,
  network, consent expiry, auth) it sets ``self.last_error`` and returns
  the cached snapshot so sensors keep displaying their last good value.
  Reauth UI is triggered directly via ``config_entry.async_start_reauth``.

- **Per-account 429 back-off**: a rate-limited UID gets
  ``rate_limited_until = now + 4 hours`` stamped on its cached entry. The
  next scheduled poll skips it; the one after resumes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import EnableBankingClient
from .const import (
    CONF_APP_ID,
    CONF_ASPSP_NAME,
    CONF_CONSENT_EXPIRES_AT,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONSENT_WARNING_DAYS,
    DOMAIN,
    POLL_HOURS,
    STALE_THRESHOLD_HOURS,
    STORAGE_VERSION,
)
from .jwt_helper import JWT_TTL_SECONDS, jwt_seconds_remaining, mint_jwt
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

# Back-off one scheduled cycle on a 429. With 4-hour gaps between polls
# this effectively retries at the next slot.
_BACK_OFF = timedelta(hours=4)


class EnableBankingCoordinator(DataUpdateCoordinator[EnableBankingData]):
    """Coordinator to fetch balances via Enable Banking on a fixed schedule."""

    config_entry: EnableBankingConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EnableBankingConfigEntry,
        client: EnableBankingClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # scheduled polling — we drive refresh ourselves
        )
        self.client = client
        self.last_refresh: datetime | None = None
        self.last_error: str = ""
        self._warned_expiry = False
        self._cached: dict[str, AccountBalance] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cache"
        )
        # Deterministic per-entry minute offset in [0, 59] so multiple
        # banks don't all poll at xx:00:00. Hash of entry_id stays stable
        # across HA restarts.
        self._minute_offset: int = abs(hash(entry.entry_id)) % 60

    @property
    def minute_offset(self) -> int:
        return self._minute_offset

    # ------------------------------------------------------------------ #
    # Scheduling                                                           #
    # ------------------------------------------------------------------ #

    def register_scheduled_polls(self) -> list:
        """Register an ``async_track_time_change`` per POLL_HOUR.

        Returns the unsub callbacks — caller should attach them to
        ``entry.async_on_unload``.
        """
        async def _on_scheduled(now: datetime) -> None:
            _LOGGER.debug(
                "Scheduled poll fired for entry %s at %s (minute_offset=%d)",
                self.config_entry.entry_id,
                now.isoformat(),
                self._minute_offset,
            )
            await self.async_refresh()

        unsubs = []
        for hour in POLL_HOURS:
            unsubs.append(
                async_track_time_change(
                    self.hass,
                    _on_scheduled,
                    hour=hour,
                    minute=self._minute_offset,
                    second=0,
                )
            )
        _LOGGER.debug(
            "Registered %d scheduled polls for entry %s at %s local time, minute %02d",
            len(unsubs),
            self.config_entry.entry_id,
            ", ".join(f"{h:02d}:00" for h in POLL_HOURS),
            self._minute_offset,
        )
        return unsubs

    def most_recent_scheduled_time(self, now: datetime) -> datetime:
        """The most recent of the POLL_HOURS slots at or before ``now`` (UTC)."""
        local_now = dt_util.as_local(now)
        today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        candidates = [
            today.replace(hour=h, minute=self._minute_offset)
            for h in POLL_HOURS
        ]
        past = [c for c in candidates if c <= local_now]
        if past:
            return dt_util.as_utc(max(past))
        # Before today's first slot — most recent is yesterday's last slot
        yesterday_last = (today - timedelta(days=1)).replace(
            hour=POLL_HOURS[-1], minute=self._minute_offset
        )
        return dt_util.as_utc(yesterday_last)

    def needs_catchup(self) -> bool:
        """True if we should poll now rather than wait for the next slot.

        We catch up if the cache has never been populated, OR the most
        recent scheduled slot already passed and the cache is older than it.
        """
        now = dt_util.utcnow()
        if self.last_refresh is None:
            return True
        return self.last_refresh < self.most_recent_scheduled_time(now)

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
        return self._cached.get(uid)

    # ------------------------------------------------------------------ #
    # Refresh                                                              #
    # ------------------------------------------------------------------ #

    async def _async_maybe_renew_jwt(self) -> None:
        """Silently regenerate the JWT if it expires within 30 minutes.

        Only runs when a private key is stored in the config entry (new-style
        setup). Old entries without a private key are unaffected.
        """
        private_key = self.config_entry.data.get(CONF_PRIVATE_KEY)
        app_id = self.config_entry.data.get(CONF_APP_ID)
        if not private_key or not app_id:
            return

        remaining = jwt_seconds_remaining(self.client._jwt)
        if remaining > 1800:  # more than 30 min left — nothing to do
            return

        _LOGGER.debug(
            "JWT for entry %s expires in %ds — auto-renewing",
            self.config_entry.entry_id,
            remaining,
        )
        try:
            new_jwt = mint_jwt(private_key, app_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to auto-renew JWT: %s", err)
            return

        self.client.update_jwt(new_jwt)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_JWT: new_jwt},
        )
        _LOGGER.debug("JWT auto-renewed for entry %s", self.config_entry.entry_id)

    async def _async_update_data(self) -> EnableBankingData:
        """Fetch balances. NEVER raises — always returns cached data on error."""
        await self._async_maybe_renew_jwt()
        now = dt_util.utcnow()
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
        back_off_until = now + _BACK_OFF

        for uid, ab in fresh.items():
            if uid in rate_limited_uids:
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
        days_remaining = (consent_expires_at - dt_util.utcnow()).days
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

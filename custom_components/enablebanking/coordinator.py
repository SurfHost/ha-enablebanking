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
  (with 0-60 s jitter). Otherwise we just wait for the next slot. This is
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
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import EnableBankingClient
from .const import (
    CONF_APP_ID,
    CONF_ASPSP_NAME,
    CONF_CONSENT_EXPIRES_AT,
    CONF_FETCH_TRANSACTIONS,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONF_TRANSACTION_HISTORY_DAYS,
    CONSENT_WARNING_DAYS,
    DEFAULT_FETCH_TRANSACTIONS,
    DEFAULT_TRANSACTION_HISTORY_DAYS,
    DOMAIN,
    MAX_REMEMBERED_TRANSACTIONS,
    MAX_STORED_TRANSACTIONS,
    POLL_HOURS,
    STATISTIC_SPENDING,
    STORAGE_VERSION,
)
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingError,
    EnableBankingRateLimitError,
    EnableBankingSessionError,
)
from .jwt_helper import jwt_seconds_remaining, mint_jwt
from .models import AccountBalance, EnableBankingData, Transaction, transaction_from_raw
from .statistics import (
    async_import_statistics,
    daily_totals,
    merge_daily_totals,
    window_start_for,
)

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
            update_interval=None,  # scheduled polling > we drive refresh ourselves
        )
        self.client = client
        self.last_refresh: datetime | None = None
        self.last_error: str = ""
        self._warned_expiry = False
        self._cached: dict[str, AccountBalance] = {}
        # Pre-0.6.5 cache entries were keyed by the session uid and carry no
        # stable_id, so they can't seed a (stable_id-keyed) sensor directly.
        # We hold them here and adopt them on the first poll > which knows the
        # uid-to-stable_id mapping > so last-known balances survive the upgrade
        # instead of the sensors going unavailable until a fresh poll succeeds.
        self._legacy_by_uid: dict[str, AccountBalance] = {}
        #: Dedup keys of transactions already turned into events, per account.
        #: Persisted, so a restart does not refire the whole window.
        self._seen_transactions: dict[str, set[str]] = {}
        #: Our own per-day spending/income totals, per account. The statistics
        #: import computes its running sum from these rather than reading the
        #: recorder back, which is what makes a repeated import correct itself
        #: instead of doubling.
        self._daily_totals: dict[str, dict[str, dict[str, float]]] = {}
        #: The most recent window of booked transactions per account, newest
        #: first. Persisted so the list survives a restart without spending a
        #: poll to rebuild it.
        self._transactions: dict[str, list[Transaction]] = {}
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

    def register_scheduled_polls(self) -> list[CALLBACK_TYPE]:
        """Register an ``async_track_time_change`` per POLL_HOUR.

        Returns the unsub callbacks > caller should attach them to
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

        unsubs: list[CALLBACK_TYPE] = []
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
        candidates = [today.replace(hour=h, minute=self._minute_offset) for h in POLL_HOURS]
        past = [c for c in candidates if c <= local_now]
        if past:
            return dt_util.as_utc(max(past))
        # Before today's first slot > most recent is yesterday's last slot
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
        # The stored key is ignored on purpose: which key an entry belongs
        # under is re-derived below (stable_id, or uid for pre-0.6.5 files).
        for raw in (stored.get("accounts") or {}).values():
            if not isinstance(raw, dict):
                continue
            ab = _balance_from_stored(raw)
            if ab is None:
                continue
            if ab.stable_id:
                self._cached[ab.stable_id] = ab
            elif ab.account_id:
                # Pre-0.6.5 entry: keep by uid and adopt on the first poll.
                self._legacy_by_uid[ab.account_id] = ab

        for stable_id, keys in (stored.get("seen_transactions") or {}).items():
            if isinstance(stable_id, str) and isinstance(keys, list):
                self._seen_transactions[stable_id] = {k for k in keys if isinstance(k, str)}

        for stable_id, days in (stored.get("daily_totals") or {}).items():
            if not isinstance(stable_id, str) or not isinstance(days, dict):
                continue
            self._daily_totals[stable_id] = {
                day: {k: float(v) for k, v in values.items() if isinstance(v, int | float)}
                for day, values in days.items()
                if isinstance(day, str) and isinstance(values, dict)
            }

        for stable_id, raw_list in (stored.get("transactions") or {}).items():
            if not isinstance(stable_id, str) or not isinstance(raw_list, list):
                continue
            restored: list[Transaction] = []
            for raw in raw_list:
                if isinstance(raw, dict):
                    try:
                        restored.append(Transaction(**raw))
                    except TypeError:
                        # A cache written by a different version of the model.
                        # Dropping it costs one poll to rebuild, which is far
                        # better than failing setup over a stale file.
                        _LOGGER.debug("Skipping unreadable cached transaction: %r", raw)
            if restored:
                self._transactions[stable_id] = restored

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
                "last_polled_at": self.last_refresh.isoformat() if self.last_refresh else None,
                "accounts": {
                    stable_id: _balance_to_stored(ab) for stable_id, ab in self._cached.items()
                },
                # Sorted so the file is stable between saves and diffs cleanly
                # when someone goes looking in .storage.
                "seen_transactions": {
                    stable_id: sorted(keys)
                    for stable_id, keys in self._seen_transactions.items()
                    if keys
                },
                "daily_totals": self._daily_totals,
                "transactions": {
                    stable_id: [asdict(tx) for tx in txs]
                    for stable_id, txs in self._transactions.items()
                    if txs
                },
            }
        )

    def cached_account(self, stable_id: str) -> AccountBalance | None:
        return self._cached.get(stable_id)

    def cached_stable_ids(self) -> set[str]:
        """Every account we hold a cached balance for.

        The sensor platform needs this at boot: ``self.data`` only reflects the
        latest poll, while the cache also covers accounts whose last poll
        happened before the current HA run.
        """
        return set(self._cached)

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
        if remaining > 1800:  # more than 30 min left > nothing to do
            return

        _LOGGER.debug(
            "JWT for entry %s expires in %ds > auto-renewing",
            self.config_entry.entry_id,
            remaining,
        )
        try:
            new_jwt = mint_jwt(private_key, app_id)
        except Exception as err:
            _LOGGER.warning("Failed to auto-renew JWT: %s", err)
            return

        self.client.update_jwt(new_jwt)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_JWT: new_jwt},
        )
        _LOGGER.debug("JWT auto-renewed for entry %s", self.config_entry.entry_id)

    async def _async_update_data(self) -> EnableBankingData:
        """Fetch balances. NEVER raises > always returns cached data on error."""
        await self._async_maybe_renew_jwt()
        now = dt_util.utcnow()
        skip_ids = {
            stable_id
            for stable_id, ab in self._cached.items()
            if ab.rate_limited_until is not None and ab.rate_limited_until > now
        }
        if skip_ids:
            _LOGGER.debug(
                "Skipping %d rate-limited account(s) this cycle: %s",
                len(skip_ids),
                sorted(s[:8] for s in skip_ids),
            )

        try:
            fresh, rate_limited_ids = await self.client.async_get_all_balances(
                fallback=self._cached,
                skip_ids=skip_ids,
                legacy_by_uid=self._legacy_by_uid or None,
            )
        except EnableBankingAuthenticationError as err:
            self.last_error = "auth"
            _LOGGER.warning("JWT rejected: %s > triggering reauth", err)
            self.config_entry.async_start_reauth(self.hass)
            return self._cached_snapshot()
        except EnableBankingSessionError as err:
            self.last_error = "consent_expired"
            _LOGGER.warning("Session expired: %s > triggering reauth", err)
            self.config_entry.async_start_reauth(self.hass)
            return self._cached_snapshot()
        except EnableBankingRateLimitError as err:
            self.last_error = "rate_limited"
            _LOGGER.warning("Session-level PSD2 rate limit; keeping cached balances: %s", err)
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

        for stable_id, ab in fresh.items():
            if stable_id in rate_limited_ids:
                ab.rate_limited_until = back_off_until
            else:
                ab.last_polled_at = now
                ab.rate_limited_until = None
            self._cached[stable_id] = ab

        # Drop legacy (pre-0.6.5) entries we've now adopted into the stable
        # cache so they aren't re-considered on later polls.
        if self._legacy_by_uid:
            for adopted in {ab.account_id for ab in fresh.values() if ab.account_id}:
                self._legacy_by_uid.pop(adopted, None)

        new_transactions = await self._async_fetch_transactions(fresh, skip_ids)

        await self._save_cache()

        consent_expires_at = self._parse_consent_expires()
        self._maybe_warn_expiry(consent_expires_at)

        return EnableBankingData(
            accounts=dict(self._cached),
            consent_expires_at=consent_expires_at,
            new_transactions=new_transactions,
        )

    # ------------------------------------------------------------------ #
    # Transactions                                                         #
    # ------------------------------------------------------------------ #

    @property
    def transactions_enabled(self) -> bool:
        """Whether the user opted in to fetching transactions."""
        return bool(
            self.config_entry.options.get(CONF_FETCH_TRANSACTIONS, DEFAULT_FETCH_TRANSACTIONS)
        )

    @property
    def _history_days(self) -> int:
        value = self.config_entry.options.get(
            CONF_TRANSACTION_HISTORY_DAYS, DEFAULT_TRANSACTION_HISTORY_DAYS
        )
        return value if isinstance(value, int) and value > 0 else DEFAULT_TRANSACTION_HISTORY_DAYS

    def spend_over(self, stable_id: str, days: int) -> float | None:
        """Total debits for an account over the last ``days``, today included.

        None when there is nothing recorded for the account, which keeps the
        sensor unknown rather than asserting a confident zero for an account
        whose transactions have never been fetched.
        """
        totals = self._daily_totals.get(stable_id)
        if not totals:
            return None
        today = dt_util.now().date()
        cutoff = today - timedelta(days=days - 1)
        total = 0.0
        for day, values in totals.items():
            try:
                parsed = date.fromisoformat(day)
            except (ValueError, TypeError):
                continue
            if cutoff <= parsed <= today:
                total += values.get(STATISTIC_SPENDING, 0.0)
        return round(total, 2)

    def transactions_for(self, stable_id: str) -> list[Transaction]:
        """The stored window for one account, newest first."""
        return list(self._transactions.get(stable_id, []))

    async def _async_fetch_transactions(
        self, accounts: dict[str, AccountBalance], skip_ids: set[str]
    ) -> dict[str, list[Transaction]]:
        """Fetch, dedup and file transactions for every account.

        Every failure here is swallowed per account. Balances are the reason
        this integration exists and transactions are an extra; a bank that
        rate-limits or 500s on ``/transactions`` must not cost the user their
        balance sensors, and must not mark the whole poll failed.
        """
        if not self.transactions_enabled:
            return {}

        window_start = window_start_for(self._history_days)
        new_by_account: dict[str, list[Transaction]] = {}

        for stable_id, account in accounts.items():
            if stable_id in skip_ids or account.rate_limited_until is not None:
                _LOGGER.debug(
                    "Skipping transactions for %s > rate-limit back-off active",
                    stable_id[:8],
                )
                continue

            try:
                raw = await self.client.async_get_transactions(account.account_id, window_start)
            except EnableBankingRateLimitError as err:
                _LOGGER.warning(
                    "Rate limited fetching transactions for %s; balances are unaffected: %s",
                    account.name or stable_id[:8],
                    err,
                )
                continue
            except EnableBankingError as err:
                _LOGGER.warning(
                    "Could not fetch transactions for %s; balances are unaffected: %s",
                    account.name or stable_id[:8],
                    err,
                )
                continue

            parsed = [tx for item in raw if (tx := transaction_from_raw(item)) is not None]

            # The first poll for an account backfills the whole history window.
            # Those are events only in the sense that they happened; firing
            # them would replay months of transactions into every automation
            # the moment the option is switched on. Seed the seen-set silently
            # and start firing from the next poll.
            seeding = stable_id not in self._seen_transactions
            seen = self._seen_transactions.setdefault(stable_id, set())
            fresh = [tx for tx in parsed if tx.booked and tx.key not in seen]
            seen.update(tx.key for tx in fresh)
            if seeding:
                _LOGGER.debug(
                    "Seeding %d transaction key(s) for %s without firing events",
                    len(fresh),
                    stable_id[:8],
                )
                fresh = []
            if len(seen) > MAX_REMEMBERED_TRANSACTIONS:
                # Bounded, oldest-arbitrary eviction. The window is what
                # actually protects against refiring; this only stops the
                # cache growing without limit on a very busy account.
                self._seen_transactions[stable_id] = set(list(seen)[-MAX_REMEMBERED_TRANSACTIONS:])
            if fresh:
                new_by_account[stable_id] = fresh

            merged = merge_daily_totals(
                self._daily_totals.get(stable_id, {}),
                daily_totals(parsed),
                window_start,
            )
            self._daily_totals[stable_id] = merged
            # Keep the window itself, not just its daily totals. It is already
            # fetched and would otherwise be discarded, and it is what the
            # get_transactions service hands back -- HA can backdate statistics
            # but not states, so a queryable list is the only way to reach the
            # history that predates the integration being switched on.
            self._transactions[stable_id] = sorted(
                (tx for tx in parsed if tx.booked),
                key=lambda tx: (tx.booking_date or tx.value_date or "", tx.key),
                reverse=True,
            )[:MAX_STORED_TRANSACTIONS]
            try:
                async_import_statistics(self.hass, account, merged, window_start)
            except Exception:
                # A rejected statistic must not lose the transactions we just
                # parsed, nor the balances alongside them.
                _LOGGER.exception("Could not import statistics for %s", stable_id[:8])

        return new_by_account

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
                f"{days_remaining} day(s). Open **Settings > Devices & Services > "
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
        # ``stable_id`` may be absent in pre-0.6.5 cache files; such entries
        # get an empty stable_id and are held as legacy (adopted on first poll).
        stable_id = data.get("stable_id", "")
        if not isinstance(stable_id, str):
            stable_id = ""
        return AccountBalance(
            account_id=str(data["account_id"]),
            stable_id=stable_id,
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
        "stable_id": ab.stable_id,
        "iban": ab.iban,
        "name": ab.name,
        "product": ab.product,
        "currency": ab.currency,
        "balance": ab.balance,
        "balance_type": ab.balance_type,
        "reference_date": ab.reference_date,
        "last_polled_at": ab.last_polled_at.isoformat() if ab.last_polled_at else None,
        "rate_limited_until": ab.rate_limited_until.isoformat() if ab.rate_limited_until else None,
    }


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None

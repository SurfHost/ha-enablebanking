"""Constants for the Enable Banking integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "enablebanking"

CONF_JWT: Final = "jwt"
CONF_PRIVATE_KEY: Final = "private_key"
CONF_APP_ID: Final = "app_id"
CONF_SESSION_ID: Final = "session_id"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ASPSP_NAME: Final = "aspsp_name"
CONF_ASPSP_COUNTRY: Final = "aspsp_country"
CONF_PSU_TYPE: Final = "psu_type"
CONF_AUTH_CODE: Final = "auth_code"
CONF_CONSENT_EXPIRES_AT: Final = "consent_expires_at"

# Fixed scheduled polling at these local hours > four polls/day, aligned
# with typical waking life, sitting exactly at the PSD2 4/day cap with
# regular 4-hour gaps (plus one 12-hour overnight gap).
POLL_HOURS: Final = (10, 14, 18, 22)

# Legacy / unused constants kept to avoid breaking imports in older
# user automations referencing scan_interval. Scheduled polling ignores
# these; the only live use is as the nominal interval that the sensor's
# `stale` attribute is measured against (2x this, so 16 h).
DEFAULT_SCAN_INTERVAL: Final = 8 * 60 * 60

# Sensor staleness threshold as originally designed: flag `stale: true` if the
# last successful poll is more than this many hours old, which allows for the
# 12-hour overnight gap plus slack. Currently NOT wired up: sensor._is_stale
# compares against 2x DEFAULT_SCAN_INTERVAL (16 h) instead. Kept so the intended
# figure is not lost; deciding between 16 h and 24 h is a behaviour change.
STALE_THRESHOLD_HOURS: Final = 24

# Storage (persistent on-disk balance cache, one file per config entry).
STORAGE_VERSION: Final = 1

# Max jitter added to the catch-up poll on HA startup, seconds.
STARTUP_JITTER_SECONDS: Final = 60

ENABLE_BANKING_API_URL: Final = "https://api.enablebanking.com"

# Redirect URL used during the OAuth consent flow.
# After authorising at the bank the user is sent here; they copy the
# ?code= query parameter and paste it into the config flow.
REDIRECT_URL: Final = "https://enablebanking.com/"

PSU_PERSONAL: Final = "personal"
PSU_BUSINESS: Final = "business"

CONSENT_WARNING_DAYS: Final = 14

# --- Transactions (opt-in) ------------------------------------------------ #

#: Options-flow keys. Transactions are off by default: fetching them costs one
#: extra API call per account per poll against the PSD2 4/day cap, and nobody
#: should start spending that budget — or recorder space — without asking.
CONF_FETCH_TRANSACTIONS: Final = "fetch_transactions"
CONF_TRANSACTION_HISTORY_DAYS: Final = "transaction_history_days"

DEFAULT_FETCH_TRANSACTIONS: Final = False

#: How far back to ask for on the first poll. 90 days is what PSD2 guarantees
#: without a fresh SCA; asking for more generally returns nothing extra.
DEFAULT_TRANSACTION_HISTORY_DAYS: Final = 90
MIN_TRANSACTION_HISTORY_DAYS: Final = 1
MAX_TRANSACTION_HISTORY_DAYS: Final = 365

#: Event entity type fired for each newly-seen booked transaction.
EVENT_TYPE_TRANSACTION: Final = "transaction"

#: Suffixes for the two external statistic series kept per account.
STATISTIC_SPENDING: Final = "spending"
STATISTIC_INCOME: Final = "income"

#: Cap on remembered dedup keys per account in the on-disk cache. Bounds the
#: cache file on a busy account; well above the number of transactions a
#: 90-day window can hold, so it never truncates a live window in practice.
MAX_REMEMBERED_TRANSACTIONS: Final = 2000

#: Transactions retained per account for the get_transactions service. Bounds
#: the cache file; comfortably more than a 90-day window holds for a normal
#: account, so it only bites on a very busy one.
MAX_STORED_TRANSACTIONS: Final = 1000

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

# Fixed scheduled polling at these local hours — four polls/day, aligned
# with typical waking life, sitting exactly at the PSD2 4/day cap with
# regular 4-hour gaps (plus one 12-hour overnight gap).
POLL_HOURS: Final = (10, 14, 18, 22)

# Legacy / unused constants kept to avoid breaking imports in older
# user automations referencing scan_interval. Scheduled polling ignores
# these; they're only used by the staleness threshold calculation.
DEFAULT_SCAN_INTERVAL: Final = 8 * 60 * 60

# Sensor staleness: flag `stale: true` if the last successful poll is
# more than this many hours old. Accounts for the 12-hour overnight gap
# plus some slack for the occasional missed poll.
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

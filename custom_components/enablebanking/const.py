"""Constants for the Enable Banking integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "enablebanking"

CONF_JWT: Final = "jwt"
CONF_SESSION_ID: Final = "session_id"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ASPSP_NAME: Final = "aspsp_name"
CONF_ASPSP_COUNTRY: Final = "aspsp_country"
CONF_PSU_TYPE: Final = "psu_type"
CONF_AUTH_CODE: Final = "auth_code"
CONF_CONSENT_EXPIRES_AT: Final = "consent_expires_at"

# PSD2 caps unattended AIS polling at 4/day per consent.
# 6 hours = 4 polls/day — stay at or above this default.
DEFAULT_SCAN_INTERVAL: Final = 6 * 60 * 60
MIN_SCAN_INTERVAL: Final = 60 * 60
MAX_SCAN_INTERVAL: Final = 24 * 60 * 60

ENABLE_BANKING_API_URL: Final = "https://api.enablebanking.com"

# Redirect URL used during the OAuth consent flow.
# After authorising at the bank the user is sent here; they copy the
# ?code= query parameter and paste it into the config flow.
REDIRECT_URL: Final = "https://enablebanking.com/"

PSU_PERSONAL: Final = "personal"
PSU_BUSINESS: Final = "business"

CONSENT_WARNING_DAYS: Final = 14

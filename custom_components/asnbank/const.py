"""Constants for the ASN Bank Balance integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "asnbank"

CONF_JWT: Final = "jwt"
CONF_SESSION_ID: Final = "session_id"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# Enable Banking's free personal tier permits polling well above this rate,
# but PSD2 itself caps unattended AIS polling at 4 per day per consent.
# 6 hours = 4 polls/day, which keeps us safely within both budgets.
DEFAULT_SCAN_INTERVAL: Final = 6 * 60 * 60  # seconds
MIN_SCAN_INTERVAL: Final = 60 * 60  # 1 hour
MAX_SCAN_INTERVAL: Final = 24 * 60 * 60  # 24 hours

ENABLE_BANKING_API_URL: Final = "https://api.enablebanking.com"

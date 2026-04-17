"""Exceptions for the Enable Banking integration."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class EnableBankingError(HomeAssistantError):
    """Base exception for Enable Banking."""


class EnableBankingConnectionError(EnableBankingError):
    """Raised when unable to connect to Enable Banking."""


class EnableBankingAuthenticationError(EnableBankingError):
    """Raised when the JWT is invalid or expired."""


class EnableBankingSessionError(EnableBankingError):
    """Raised when the session id is invalid, revoked, or expired."""


class EnableBankingAPIError(EnableBankingError):
    """Raised when Enable Banking returns an unexpected error."""

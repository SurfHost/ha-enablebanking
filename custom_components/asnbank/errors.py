"""Exceptions for the ASN Bank Balance integration."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class AsnBankError(HomeAssistantError):
    """Base exception for ASN Bank Balance."""


class EnableBankingConnectionError(AsnBankError):
    """Raised when unable to connect to Enable Banking."""


class EnableBankingAuthenticationError(AsnBankError):
    """Raised when the JWT is invalid or expired."""


class EnableBankingSessionError(AsnBankError):
    """Raised when the session id is invalid, revoked, or expired."""


class EnableBankingAPIError(AsnBankError):
    """Raised when Enable Banking returns an unexpected error."""

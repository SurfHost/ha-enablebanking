"""Config flow for the ASN Bank Balance integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnableBankingClient
from .const import (
    CONF_JWT,
    CONF_SCAN_INTERVAL,
    CONF_SESSION_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import AsnBankConfigEntry
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingSessionError,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_JWT): str,
        vol.Required(CONF_SESSION_ID): str,
    }
)


class AsnBankConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ASN Bank Balance."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect JWT + session id and validate them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            jwt = user_input[CONF_JWT].strip()
            session_id = user_input[CONF_SESSION_ID].strip()
            session = async_get_clientsession(self.hass)
            client = EnableBankingClient(session, jwt, session_id)

            try:
                await client.async_validate()
            except EnableBankingAuthenticationError:
                errors["base"] = "invalid_auth"
            except EnableBankingSessionError:
                errors["base"] = "invalid_session"
            except EnableBankingConnectionError:
                errors["base"] = "cannot_connect"
            except EnableBankingAPIError:
                errors["base"] = "unknown"
            except (TimeoutError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception validating Enable Banking")
                errors["base"] = "unknown"
            else:
                unique_id = hashlib.sha256(session_id.encode()).hexdigest()[:12]
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="ASN Bank",
                    data={
                        CONF_JWT: jwt,
                        CONF_SESSION_ID: session_id,
                    },
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth flow — e.g. the session was revoked or JWT expired."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user paste a fresh JWT + session id."""
        errors: dict[str, str] = {}

        if user_input is not None:
            jwt = user_input[CONF_JWT].strip()
            session_id = user_input[CONF_SESSION_ID].strip()
            session = async_get_clientsession(self.hass)
            client = EnableBankingClient(session, jwt, session_id)

            try:
                await client.async_validate()
            except EnableBankingAuthenticationError:
                errors["base"] = "invalid_auth"
            except EnableBankingSessionError:
                errors["base"] = "invalid_session"
            except EnableBankingConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"
            else:
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_JWT: jwt,
                        CONF_SESSION_ID: session_id,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: AsnBankConfigEntry,
    ) -> AsnBankOptionsFlow:
        """Get the options flow for this handler."""
        return AsnBankOptionsFlow()


class AsnBankOptionsFlow(OptionsFlow):
    """Handle ASN Bank Balance options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options — currently just the poll interval."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        current_interval: int = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=current_interval,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                }
            ),
        )

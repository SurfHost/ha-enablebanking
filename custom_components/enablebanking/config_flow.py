"""Config flow for the Enable Banking integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .api import EnableBankingClient
from .const import (
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_AUTH_CODE,
    CONF_CONSENT_EXPIRES_AT,
    CONF_JWT,
    CONF_PSU_TYPE,
    CONF_SCAN_INTERVAL,
    CONF_SESSION_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    PSU_BUSINESS,
    PSU_PERSONAL,
)
from .coordinator import EnableBankingConfigEntry
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingSessionError,
)

_LOGGER = logging.getLogger(__name__)


class EnableBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Enable Banking."""

    VERSION = 1

    def __init__(self) -> None:
        self._jwt: str = ""
        self._aspsps: list[dict[str, Any]] = []
        self._aspsp_name: str = ""
        self._aspsp_country: str = ""
        self._psu_type: str = PSU_PERSONAL
        self._auth_url: str = ""

    # ------------------------------------------------------------------ #
    # Step 1: JWT                                                          #
    # ------------------------------------------------------------------ #

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the Enable Banking application JWT and validate it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            jwt = user_input[CONF_JWT].strip()
            http = async_get_clientsession(self.hass)
            client = EnableBankingClient.for_config_flow(http, jwt)
            try:
                self._aspsps = await client.async_get_aspsps()
            except EnableBankingAuthenticationError:
                errors["base"] = "invalid_auth"
            except EnableBankingConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating JWT")
                errors["base"] = "unknown"
            else:
                self._jwt = jwt
                return await self.async_step_aspsp()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_JWT): str}),
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Step 2: ASPSP + PSU type                                             #
    # ------------------------------------------------------------------ #

    async def async_step_aspsp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a bank and PSU type, then initiate the consent flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            raw = user_input[CONF_ASPSP_NAME]
            # Value is "Name|COUNTRY" to keep name + country together.
            aspsp_name, _, aspsp_country = raw.partition("|")
            psu_type = user_input[CONF_PSU_TYPE]

            http = async_get_clientsession(self.hass)
            client = EnableBankingClient.for_config_flow(http, self._jwt)
            try:
                auth_url = await client.async_start_auth(
                    aspsp_name, aspsp_country, psu_type
                )
            except EnableBankingAuthenticationError:
                errors["base"] = "invalid_auth"
            except EnableBankingConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error starting auth")
                errors["base"] = "unknown"
            else:
                self._aspsp_name = aspsp_name
                self._aspsp_country = aspsp_country
                self._psu_type = psu_type
                self._auth_url = auth_url
                return await self.async_step_auth()

        aspsp_options = _build_aspsp_options(self._aspsps)
        psu_options = {PSU_PERSONAL: "Personal", PSU_BUSINESS: "Business"}

        return self.async_show_form(
            step_id="aspsp",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ASPSP_NAME): SelectSelector(
                        SelectSelectorConfig(options=aspsp_options)
                    ),
                    vol.Required(CONF_PSU_TYPE, default=PSU_PERSONAL): vol.In(
                        psu_options
                    ),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Step 3: bank OAuth + auth code → session_id                          #
    # ------------------------------------------------------------------ #

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the bank's auth URL and collect the returned auth code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            auth_code = user_input[CONF_AUTH_CODE].strip()
            http = async_get_clientsession(self.hass)
            client = EnableBankingClient.for_config_flow(http, self._jwt)
            try:
                session_data = await client.async_create_session(auth_code)
            except (EnableBankingAuthenticationError, EnableBankingAPIError):
                errors["base"] = "invalid_auth_code"
            except EnableBankingConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error creating session")
                errors["base"] = "unknown"
            else:
                return await self._async_finish_session(session_data)

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema({vol.Required(CONF_AUTH_CODE): str}),
            description_placeholders={"auth_url": self._auth_url},
            errors=errors,
        )

    async def _async_finish_session(
        self, session_data: dict[str, Any]
    ) -> ConfigFlowResult:
        session_id = session_data.get("session_id") or session_data.get("uid", "")
        consent_expires_at: str | None = (session_data.get("access") or {}).get(
            "valid_until"
        )

        # Sanity-check the new session before saving.
        http = async_get_clientsession(self.hass)
        client = EnableBankingClient(http, self._jwt, session_id)
        try:
            await client.async_validate()
        except (
            EnableBankingAuthenticationError,
            EnableBankingSessionError,
            EnableBankingAPIError,
        ):
            return self.async_show_form(
                step_id="auth",
                data_schema=vol.Schema({vol.Required(CONF_AUTH_CODE): str}),
                description_placeholders={"auth_url": self._auth_url},
                errors={"base": "invalid_session"},
            )
        except EnableBankingConnectionError:
            return self.async_show_form(
                step_id="auth",
                data_schema=vol.Schema({vol.Required(CONF_AUTH_CODE): str}),
                description_placeholders={"auth_url": self._auth_url},
                errors={"base": "cannot_connect"},
            )

        unique_id = hashlib.sha256(session_id.encode()).hexdigest()[:12]
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        title = self._aspsp_name
        if self._psu_type == PSU_BUSINESS:
            title = f"{title} (business)"

        return self.async_create_entry(
            title=title,
            data={
                CONF_JWT: self._jwt,
                CONF_SESSION_ID: session_id,
                CONF_ASPSP_NAME: self._aspsp_name,
                CONF_ASPSP_COUNTRY: self._aspsp_country,
                CONF_PSU_TYPE: self._psu_type,
                CONF_CONSENT_EXPIRES_AT: consent_expires_at,
            },
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
        )

    # ------------------------------------------------------------------ #
    # Reauth flow                                                          #
    # ------------------------------------------------------------------ #

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_jwt()

    async def async_step_reauth_jwt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter JWT (may still be valid) and start a new consent."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            jwt = user_input[CONF_JWT].strip()
            http = async_get_clientsession(self.hass)
            client = EnableBankingClient.for_config_flow(http, jwt)
            try:
                await client.async_get_aspsps()
            except EnableBankingAuthenticationError:
                errors["base"] = "invalid_auth"
            except EnableBankingConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating JWT during reauth")
                errors["base"] = "unknown"

            if not errors:
                self._jwt = jwt
                self._aspsp_name = entry.data.get(CONF_ASPSP_NAME, "")
                self._aspsp_country = entry.data.get(CONF_ASPSP_COUNTRY, "")
                self._psu_type = entry.data.get(CONF_PSU_TYPE, PSU_PERSONAL)
                try:
                    self._auth_url = await client.async_start_auth(
                        self._aspsp_name, self._aspsp_country, self._psu_type
                    )
                except EnableBankingConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error starting reauth")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_reauth_auth()

        aspsp_name = entry.data.get(CONF_ASPSP_NAME, "your bank")
        return self.async_show_form(
            step_id="reauth_jwt",
            data_schema=vol.Schema(
                {vol.Required(CONF_JWT, default=entry.data.get(CONF_JWT, "")): str}
            ),
            description_placeholders={"aspsp_name": aspsp_name},
            errors=errors,
        )

    async def async_step_reauth_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect new auth code and update the existing entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            auth_code = user_input[CONF_AUTH_CODE].strip()
            http = async_get_clientsession(self.hass)
            client = EnableBankingClient.for_config_flow(http, self._jwt)
            try:
                session_data = await client.async_create_session(auth_code)
            except (EnableBankingAuthenticationError, EnableBankingAPIError):
                errors["base"] = "invalid_auth_code"
            except EnableBankingConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error creating session during reauth")
                errors["base"] = "unknown"
            else:
                session_id = session_data.get("session_id") or session_data.get(
                    "uid", ""
                )
                consent_expires_at: str | None = (session_data.get("access") or {}).get(
                    "valid_until"
                )
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_JWT: self._jwt,
                        CONF_SESSION_ID: session_id,
                        CONF_CONSENT_EXPIRES_AT: consent_expires_at,
                    },
                )

        return self.async_show_form(
            step_id="reauth_auth",
            data_schema=vol.Schema({vol.Required(CONF_AUTH_CODE): str}),
            description_placeholders={"auth_url": self._auth_url},
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Options flow                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: EnableBankingConfigEntry,
    ) -> EnableBankingOptionsFlow:
        return EnableBankingOptionsFlow()


class EnableBankingOptionsFlow(OptionsFlow):
    """Handle Enable Banking options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    )
                },
            )

        current: int = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #


def _build_aspsp_options(aspsps: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build SelectSelector options from the /aspsps response.

    Value is "Name|COUNTRY" so we can round-trip both pieces without a
    separate field. Label is "Name (COUNTRY)" for readability.
    """
    seen: set[str] = set()
    options: list[dict[str, str]] = []
    for aspsp in sorted(
        aspsps,
        key=lambda a: (a.get("country", ""), a.get("name", "")),
    ):
        name = aspsp.get("name", "")
        country = aspsp.get("country", "")
        if not name or not country:
            continue
        key = f"{name}|{country}"
        if key in seen:
            continue
        seen.add(key)
        options.append({"value": key, "label": f"{name} ({country})"})
    return options

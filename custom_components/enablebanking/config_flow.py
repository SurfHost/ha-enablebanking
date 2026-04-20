"""Config flow for the Enable Banking integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .api import EnableBankingClient
from .const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_AUTH_CODE,
    CONF_CONSENT_EXPIRES_AT,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_BUSINESS,
    PSU_PERSONAL,
)
from .jwt_helper import mint_jwt
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
        self._private_key: str = ""
        self._app_id: str = ""
        self._aspsps: list[dict[str, Any]] = []
        self._aspsp_name: str = ""
        self._aspsp_country: str = ""
        self._psu_type: str = PSU_PERSONAL
        self._auth_url: str = ""

    # ------------------------------------------------------------------ #
    # Step 1: private key + app ID                                         #
    # ------------------------------------------------------------------ #

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the private key and app ID, then validate by minting a JWT."""
        errors: dict[str, str] = {}

        if user_input is None:
            # Reuse credentials from an existing entry if available.
            existing = self._credentials_from_existing_entries()
            if existing:
                pk, app_id = existing
                jwt = self._mint_jwt_or_none(pk, app_id)
                if jwt and await self._try_load_aspsps(jwt):
                    self._private_key, self._app_id, self._jwt = pk, app_id, jwt
                    return await self.async_step_country()
        else:
            pk = user_input[CONF_PRIVATE_KEY].strip()
            app_id = user_input[CONF_APP_ID].strip()
            jwt = self._mint_jwt_or_none(pk, app_id, errors)
            if jwt and await self._try_load_aspsps(jwt, errors=errors):
                self._private_key, self._app_id, self._jwt = pk, app_id, jwt
                return await self.async_step_country()

        existing = self._credentials_from_existing_entries()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PRIVATE_KEY,
                        default=existing[0] if existing else vol.UNDEFINED,
                    ): str,
                    vol.Required(
                        CONF_APP_ID,
                        default=existing[1] if existing else vol.UNDEFINED,
                    ): str,
                }
            ),
            errors=errors,
        )

    def _mint_jwt_or_none(
        self,
        private_key: str,
        app_id: str,
        errors: dict[str, str] | None = None,
    ) -> str | None:
        try:
            return mint_jwt(private_key, app_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not mint JWT: %s", err)
            if errors is not None:
                errors["base"] = "invalid_auth"
            return None

    async def _try_load_aspsps(
        self, jwt: str, errors: dict[str, str] | None = None
    ) -> bool:
        http = async_get_clientsession(self.hass)
        client = EnableBankingClient.for_config_flow(http, jwt)
        try:
            self._aspsps = await client.async_get_aspsps()
        except EnableBankingAuthenticationError:
            if errors is not None:
                errors["base"] = "invalid_auth"
            return False
        except EnableBankingConnectionError:
            if errors is not None:
                errors["base"] = "cannot_connect"
            return False
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating credentials")
            if errors is not None:
                errors["base"] = "unknown"
            return False
        return True

    def _credentials_from_existing_entries(
        self, exclude_entry=None
    ) -> tuple[str, str] | None:
        """Return (private_key, app_id) from any existing entry, newest first."""
        for entry in reversed(list(self._async_current_entries())):
            if exclude_entry is not None and entry.entry_id == exclude_entry.entry_id:
                continue
            pk = entry.data.get(CONF_PRIVATE_KEY)
            app_id = entry.data.get(CONF_APP_ID)
            if isinstance(pk, str) and pk and isinstance(app_id, str) and app_id:
                return pk, app_id
        return None

    # ------------------------------------------------------------------ #
    # Step 2a: country                                                     #
    # ------------------------------------------------------------------ #

    async def async_step_country(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a country to filter the bank list."""
        if user_input is not None:
            self._aspsp_country = user_input[CONF_ASPSP_COUNTRY]
            return await self.async_step_aspsp()

        country_options = _build_country_options(self._aspsps)
        return self.async_show_form(
            step_id="country",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ASPSP_COUNTRY): SelectSelector(
                        SelectSelectorConfig(options=country_options)
                    ),
                }
            ),
        )

    # ------------------------------------------------------------------ #
    # Step 2b: ASPSP (filtered by country) + PSU type                      #
    # ------------------------------------------------------------------ #

    async def async_step_aspsp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a bank within the chosen country and its PSU type."""
        errors: dict[str, str] = {}

        if user_input is not None:
            aspsp_name = user_input[CONF_ASPSP_NAME]
            psu_type = user_input[CONF_PSU_TYPE]

            http = async_get_clientsession(self.hass)
            client = EnableBankingClient.for_config_flow(http, self._jwt)
            try:
                auth_url = await client.async_start_auth(
                    aspsp_name, self._aspsp_country, psu_type
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
                self._psu_type = psu_type
                self._auth_url = auth_url
                return await self.async_step_auth()

        in_country = [
            a for a in self._aspsps if a.get("country") == self._aspsp_country
        ]
        aspsp_options = _build_aspsp_options_for_country(in_country)
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
            description_placeholders={"country": _country_name(self._aspsp_country)},
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
                CONF_PRIVATE_KEY: self._private_key,
                CONF_APP_ID: self._app_id,
                CONF_SESSION_ID: session_id,
                CONF_ASPSP_NAME: self._aspsp_name,
                CONF_ASPSP_COUNTRY: self._aspsp_country,
                CONF_PSU_TYPE: self._psu_type,
                CONF_CONSENT_EXPIRES_AT: consent_expires_at,
            },
        )

    # ------------------------------------------------------------------ #
    # Reauth flow                                                          #
    # ------------------------------------------------------------------ #

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_jwt()

    async def async_step_reauth_jwt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-authenticate using the private key.

        Fast-path: mint a JWT and validate it against the existing session —
        if the session is still alive this finishes in one click with no bank
        round-trip. Slow-path: session is dead, do full bank reauth.
        """
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            pk = user_input[CONF_PRIVATE_KEY].strip()
            app_id = user_input[CONF_APP_ID].strip()
            http = async_get_clientsession(self.hass)

            jwt = self._mint_jwt_or_none(pk, app_id, errors)

            if jwt:
                client = EnableBankingClient.for_config_flow(http, jwt)
                try:
                    self._aspsps = await client.async_get_aspsps()
                except EnableBankingAuthenticationError:
                    errors["base"] = "invalid_auth"
                except EnableBankingConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error validating credentials during reauth")
                    errors["base"] = "unknown"

            if not errors and jwt:
                # Fast-path: check if existing session is still alive.
                existing_session_id = entry.data.get(CONF_SESSION_ID, "")
                if existing_session_id:
                    session_client = EnableBankingClient(http, jwt, existing_session_id)
                    try:
                        await session_client.async_validate()
                    except (EnableBankingAuthenticationError, EnableBankingSessionError):
                        pass  # session dead or different app — fall through
                    except EnableBankingConnectionError:
                        errors["base"] = "cannot_connect"
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Unexpected error during smart reauth")
                        errors["base"] = "unknown"
                    else:
                        _LOGGER.debug(
                            "Smart reauth: credentials validate against existing "
                            "session %s — skipping bank authorisation",
                            existing_session_id[:8],
                        )
                        return self.async_update_reload_and_abort(
                            entry,
                            data_updates={
                                CONF_JWT: jwt,
                                CONF_PRIVATE_KEY: pk,
                                CONF_APP_ID: app_id,
                            },
                        )

            if not errors and jwt:
                # Session dead — full bank reauth.
                self._jwt, self._private_key, self._app_id = jwt, pk, app_id
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

        existing = self._credentials_from_existing_entries(exclude_entry=entry)
        # Fall back to this entry's own stored credentials if no other entry has them.
        if not existing:
            pk_fb = entry.data.get(CONF_PRIVATE_KEY, "")
            app_id_fb = entry.data.get(CONF_APP_ID, "")
            if pk_fb and app_id_fb:
                existing = (pk_fb, app_id_fb)

        aspsp_name = entry.data.get(CONF_ASPSP_NAME, "your bank")
        return self.async_show_form(
            step_id="reauth_jwt",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PRIVATE_KEY,
                        default=existing[0] if existing else vol.UNDEFINED,
                    ): str,
                    vol.Required(
                        CONF_APP_ID,
                        default=existing[1] if existing else vol.UNDEFINED,
                    ): str,
                }
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
                        CONF_PRIVATE_KEY: self._private_key,
                        CONF_APP_ID: self._app_id,
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
# Helpers                                                             #
# ------------------------------------------------------------------ #


# ISO 3166-1 alpha-2 → human name for the EU/EEA + UK + CH.
# Unknown codes fall back to the raw two-letter code.
_COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "CH": "Switzerland",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HR": "Croatia",
    "HU": "Hungary",
    "IE": "Ireland",
    "IS": "Iceland",
    "IT": "Italy",
    "LI": "Liechtenstein",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MT": "Malta",
    "NL": "Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SE": "Sweden",
    "SI": "Slovenia",
    "SK": "Slovakia",
}


def _country_name(code: str) -> str:
    return _COUNTRY_NAMES.get(code, code)


def _build_country_options(
    aspsps: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """One option per country present in the ASPSP list, sorted by display name."""
    countries = {a["country"] for a in aspsps if a.get("country")}
    return [
        {"value": code, "label": f"{_country_name(code)} ({code})"}
        for code in sorted(countries, key=lambda c: _country_name(c).lower())
    ]


def _build_aspsp_options_for_country(
    aspsps: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Bank options for a single country, alphabetical, dedup on name."""
    seen: set[str] = set()
    options: list[dict[str, str]] = []
    for aspsp in sorted(aspsps, key=lambda a: a.get("name", "").lower()):
        name = aspsp.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        options.append({"value": name, "label": name})
    return options

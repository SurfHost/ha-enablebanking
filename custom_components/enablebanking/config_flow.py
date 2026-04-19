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
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_AUTH_CODE,
    CONF_CONSENT_EXPIRES_AT,
    CONF_JWT,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_BUSINESS,
    PSU_PERSONAL,
)
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
        """Collect the Enable Banking application JWT and validate it.

        If a previously-added entry still has a valid JWT we silently reuse
        it and skip to the country step — adding the 2nd/3rd/4th bank is a
        one-click affair rather than a re-paste of the same token.
        """
        errors: dict[str, str] = {}

        if user_input is None:
            # Try to reuse a JWT from an existing entry for this domain.
            reuse_jwt = self._jwt_from_existing_entries()
            if reuse_jwt and await self._try_load_aspsps(reuse_jwt):
                _LOGGER.debug("Reusing JWT from an existing entry; skipping JWT step")
                self._jwt = reuse_jwt
                return await self.async_step_country()
        else:
            jwt = user_input[CONF_JWT].strip()
            if await self._try_load_aspsps(jwt, errors=errors):
                self._jwt = jwt
                return await self.async_step_country()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_JWT,
                        default=self._jwt_from_existing_entries() or vol.UNDEFINED,
                    ): str,
                }
            ),
            errors=errors,
        )

    async def _try_load_aspsps(
        self, jwt: str, errors: dict[str, str] | None = None
    ) -> bool:
        """Fetch the ASPSP list with ``jwt``; cache on success.

        If ``errors`` is provided, populates it on failure for form display.
        """
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
            _LOGGER.exception("Unexpected error validating JWT")
            if errors is not None:
                errors["base"] = "unknown"
            return False
        return True

    def _jwt_from_existing_entries(self, exclude_entry=None) -> str | None:
        """Return a JWT from any existing config entry, most recent first.

        ``exclude_entry`` — skip this entry (used during reauth so we don't
        just hand the user back the entry's own expired JWT).
        """
        for entry in reversed(list(self._async_current_entries())):
            if exclude_entry is not None and entry.entry_id == exclude_entry.entry_id:
                continue
            jwt = entry.data.get(CONF_JWT)
            if isinstance(jwt, str) and jwt:
                return jwt
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
        """Smart reauth.

        Most of the time the JWT expired but the session is still valid —
        we detect that by testing the submitted JWT against the stored
        ``session_id`` directly. If it works, update the JWT and finish
        immediately (no bank authorisation round trip). Only if the session
        is actually dead do we fall through to a full bank-reauth flow.

        The JWT field is pre-filled with the most recently-updated JWT
        across all entries, so once the user fixes the first bank the
        remaining three are essentially one-click.
        """
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            jwt = user_input[CONF_JWT].strip()
            http = async_get_clientsession(self.hass)

            # Fast-path: try the new JWT against the existing session.
            existing_session_id = entry.data.get(CONF_SESSION_ID, "")
            if existing_session_id:
                session_client = EnableBankingClient(http, jwt, existing_session_id)
                try:
                    await session_client.async_validate()
                except EnableBankingAuthenticationError:
                    errors["base"] = "invalid_auth"
                except EnableBankingSessionError:
                    # Session itself is dead — fall through to full reauth
                    errors.pop("base", None)
                except EnableBankingConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error during smart reauth")
                    errors["base"] = "unknown"
                else:
                    _LOGGER.debug(
                        "Smart reauth: new JWT validates against existing "
                        "session %s — skipping bank authorisation",
                        existing_session_id[:8],
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={CONF_JWT: jwt},
                    )

            # Either the session is dead or we have no existing session:
            # validate JWT alone and proceed to full bank-reauth.
            if "base" not in errors:
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
                            self._aspsp_name,
                            self._aspsp_country,
                            self._psu_type,
                        )
                    except EnableBankingConnectionError:
                        errors["base"] = "cannot_connect"
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Unexpected error starting reauth")
                        errors["base"] = "unknown"
                    else:
                        return await self.async_step_reauth_auth()

        # Pre-fill with any fresher JWT we have from another entry; fall
        # back to this entry's stored (possibly expired) JWT.
        pre_fill = (
            self._jwt_from_existing_entries(exclude_entry=entry)
            or entry.data.get(CONF_JWT, "")
        )
        aspsp_name = entry.data.get(CONF_ASPSP_NAME, "your bank")
        return self.async_show_form(
            step_id="reauth_jwt",
            data_schema=vol.Schema(
                {vol.Required(CONF_JWT, default=pre_fill or vol.UNDEFINED): str}
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

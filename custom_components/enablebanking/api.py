"""Enable Banking API client.

Wraps the Enable Banking aggregator API which acts as the licensed TPP and
front-ends ASN Bank, N26, Revolut, Openbank, and many other ASPSPs.

Auth model: a user-signed JWT is used as a bearer token; per-account calls
are scoped by the Enable Banking session id obtained after the PSU completes
the bank's redirect-based consent flow.

Endpoints implemented:

    GET  /aspsps                          -> supported bank list
    POST /auth                            -> initiate consent, get redirect URL
    POST /sessions                        -> exchange auth code for session_id
    GET  /sessions/{session_id}           -> account list and session status
    GET  /accounts/{account_id}/balances  -> balance objects for one account

See https://enablebanking.com/docs/api/reference/ for the full surface.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .const import ENABLE_BANKING_API_URL, REDIRECT_URL
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingSessionError,
)
from .models import AccountBalance

_LOGGER = logging.getLogger(__name__)

_BALANCE_TYPE_PREFERENCE: tuple[str, ...] = (
    "CLBD",  # closing booked
    "ITAV",  # interim available
    "XPCD",  # expected
    "ITBD",  # interim booked
    "OPBD",  # opening booked
)


class EnableBankingClient:
    """Async client for the Enable Banking AIS endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        jwt: str,
        session_id: str,
    ) -> None:
        self._session = session
        self._jwt = jwt
        self._session_id = session_id

    @classmethod
    def for_config_flow(
        cls, session: aiohttp.ClientSession, jwt: str
    ) -> EnableBankingClient:
        """Create a client for config-flow steps that precede session creation."""
        return cls(session, jwt, "")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{ENABLE_BANKING_API_URL}{path}"
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                params=params,
                json=json,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                text = await response.text()
                if response.status in (401, 403):
                    raise EnableBankingAuthenticationError(
                        f"Enable Banking rejected the JWT (HTTP {response.status})"
                    )
                if response.status == 404:
                    raise EnableBankingSessionError(
                        f"Session not found or expired: {text}"
                    )
                if response.status >= 400:
                    raise EnableBankingAPIError(
                        f"Enable Banking HTTP {response.status}: {text[:200]}"
                    )
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise EnableBankingAPIError(
                        f"Invalid JSON from Enable Banking: {text[:200]}"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnableBankingConnectionError(
                f"Cannot connect to Enable Banking: {err}"
            ) from err

    # ------------------------------------------------------------------ #
    # ASPSP discovery                                                      #
    # ------------------------------------------------------------------ #

    async def async_get_aspsps(
        self,
        country: str | None = None,
        psu_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the list of ASPSPs available under the current application."""
        params: dict[str, str] = {}
        if country:
            params["country"] = country
        if psu_type:
            params["psu_type"] = psu_type
        result = await self._request("GET", "/aspsps", params=params or None)
        if isinstance(result, list):
            return result
        return result.get("aspsps", [])

    # ------------------------------------------------------------------ #
    # Auth / session creation                                              #
    # ------------------------------------------------------------------ #

    async def async_start_auth(
        self,
        aspsp_name: str,
        aspsp_country: str,
        psu_type: str,
    ) -> str:
        """Initiate a consent request and return the bank's OAuth redirect URL."""
        valid_until = (datetime.now(UTC) + timedelta(days=180)).strftime(
            "%Y-%m-%dT%H:%M:%S.000000+00:00"
        )
        payload: dict[str, Any] = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": aspsp_country},
            "psu_type": psu_type,
            "state": secrets.token_urlsafe(16),
            "redirect_url": REDIRECT_URL,
        }
        result = await self._request("POST", "/auth", json=payload)
        url: str = result["url"]
        return url

    async def async_create_session(self, auth_code: str) -> dict[str, Any]:
        """Exchange a bank auth code for an Enable Banking session.

        Returns the full session object; ``session_id`` (or ``uid``) and
        ``access.valid_until`` are the fields we store.
        """
        payload: dict[str, Any] = {
            "code": auth_code,
            "redirect_url": REDIRECT_URL,
        }
        result: dict[str, Any] = await self._request("POST", "/sessions", json=payload)
        return result

    # ------------------------------------------------------------------ #
    # Session / balance fetching                                           #
    # ------------------------------------------------------------------ #

    async def async_validate(self) -> bool:
        """Check that the JWT and session id are both usable."""
        await self.async_get_session()
        return True

    async def async_get_session(self) -> dict[str, Any]:
        """Return the session object (includes the account list)."""
        data = await self._request("GET", f"/sessions/{self._session_id}")
        if not isinstance(data, dict):
            raise EnableBankingAPIError(
                f"Unexpected session payload type: {type(data).__name__}"
            )
        return data

    async def async_get_account_balances(self, account_id: str) -> list[dict[str, Any]]:
        """Return the list of balance objects for a single account."""
        data = await self._request("GET", f"/accounts/{account_id}/balances")
        if not isinstance(data, dict):
            raise EnableBankingAPIError(
                f"Unexpected balances payload type: {type(data).__name__}"
            )
        balances = data.get("balances", [])
        if not isinstance(balances, list):
            return []
        return balances

    async def async_get_all_balances(self) -> dict[str, AccountBalance]:
        """Return a snapshot of every account in the session.

        The mapping key is the Enable Banking account UID (UUID), stable
        across account renames. Accounts without a usable balance type are
        silently skipped.

        Session payload shape (observed for N26 and similar ASPSPs):
            {
              "accounts": ["<uid>", "<uid>", ...],
              "accounts_data": [{"uid": "<uid>", "account_id": {"iban": ...}, ...}, ...],
              ...
            }
        Some ASPSPs instead return rich dicts in ``accounts`` directly — this
        implementation handles both.
        """
        session = await self.async_get_session()
        _LOGGER.debug(
            "Session keys=%s status=%s",
            sorted(session.keys()),
            session.get("status"),
        )

        uids, metadata = _collect_accounts(session)
        _LOGGER.debug(
            "Resolved %d account uid(s); metadata entries: %d",
            len(uids),
            len(metadata),
        )
        if not uids:
            return {}

        out: dict[str, AccountBalance] = {}
        for uid in uids:
            meta = metadata.get(uid, {})
            iban = _account_iban(meta)
            name = (
                meta.get("name")
                or meta.get("account_name")
                or meta.get("product")
                or iban
                or uid[:8]
            )
            product = meta.get("product")

            try:
                balances = await self.async_get_account_balances(uid)
            except EnableBankingSessionError:
                raise
            except EnableBankingAuthenticationError:
                raise
            except EnableBankingConnectionError:
                raise
            except EnableBankingAPIError as err:
                _LOGGER.warning("Skipping account %s (%s): %s", name, uid, err)
                continue

            _LOGGER.debug(
                "account %s (%s) → %d balance object(s), types=%s",
                uid[:8],
                iban or name,
                len(balances),
                [b.get("balance_type") for b in balances if isinstance(b, dict)],
            )

            picked = _pick_preferred_balance(balances)
            if picked is None:
                _LOGGER.warning(
                    "No usable balance for %s (%s); raw balances=%r",
                    name,
                    uid,
                    balances,
                )
                continue

            amount_obj = picked.get("balance_amount") or picked.get("amount") or {}
            try:
                amount = float(amount_obj.get("amount"))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Could not parse amount for %s; picked=%r", uid, picked
                )
                continue

            out[uid] = AccountBalance(
                account_id=uid,
                iban=iban,
                name=str(name),
                product=product if isinstance(product, str) else None,
                currency=str(amount_obj.get("currency", "EUR")),
                balance=amount,
                balance_type=picked.get("balance_type"),
                reference_date=picked.get("reference_date"),
            )

        _LOGGER.debug("async_get_all_balances produced %d account balance(s)", len(out))
        return out


def _collect_accounts(
    session: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Normalise the session payload into (uids, metadata-by-uid).

    Enable Banking ASPSPs differ in shape:
      - Most (e.g. N26) put bare UID strings in ``accounts`` and the rich
        metadata in ``accounts_data``.
      - A few older/alternative shapes put the full dicts in ``accounts``
        directly.
    ``accounts_data`` may itself be a list of dicts (each keyed by ``uid``)
    or a dict keyed by uid — handle both.
    """
    metadata: dict[str, dict[str, Any]] = {}

    accounts_data = session.get("accounts_data")
    if isinstance(accounts_data, list):
        for item in accounts_data:
            if not isinstance(item, dict):
                continue
            uid = item.get("uid") or item.get("account_uid") or item.get("id")
            if isinstance(uid, str) and uid:
                metadata[uid] = item
    elif isinstance(accounts_data, dict):
        for uid, item in accounts_data.items():
            if isinstance(uid, str) and isinstance(item, dict):
                metadata[uid] = item

    uids: list[str] = []
    accounts = session.get("accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if isinstance(item, str) and item:
                uids.append(item)
            elif isinstance(item, dict):
                uid = item.get("uid") or item.get("id")
                if isinstance(uid, str) and uid:
                    uids.append(uid)
                    metadata.setdefault(uid, item)

    # De-duplicate while preserving order.
    seen: set[str] = set()
    uids = [u for u in uids if not (u in seen or seen.add(u))]
    return uids, metadata


def _account_iban(meta: dict[str, Any]) -> str:
    """Extract an IBAN from the account-metadata dict, tolerating nesting."""
    account_id = meta.get("account_id")
    if isinstance(account_id, dict):
        iban = account_id.get("iban")
        if isinstance(iban, str) and iban:
            return iban
    iban = meta.get("iban")
    if isinstance(iban, str) and iban:
        return iban
    return ""


def _pick_preferred_balance(
    balances: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Choose the most useful balance from the Enable Banking response."""
    if not balances:
        return None
    by_type: dict[str, dict[str, Any]] = {}
    for bal in balances:
        if not isinstance(bal, dict):
            continue
        btype = bal.get("balance_type")
        if isinstance(btype, str):
            by_type.setdefault(btype, bal)
    for preferred in _BALANCE_TYPE_PREFERENCE:
        if preferred in by_type:
            return by_type[preferred]
    for bal in balances:
        if isinstance(bal, dict):
            return bal
    return None

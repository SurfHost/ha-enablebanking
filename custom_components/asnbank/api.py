"""Enable Banking API client for the ASN Bank Balance integration.

The integration wraps Enable Banking's "aggregator" API, which acts as the
licensed TPP and front-ends de Volksbank (ASN/SNS/RegioBank) among many
other ASPSPs. Auth model: a user-signed JWT is used as a bearer token, and
all per-account calls are scoped by the Enable Banking *session id* that
the PSU received after completing the redirect authorisation at ASN.

This module intentionally only implements the two read endpoints we need:

    GET /sessions/{session_id}         -> account list and status
    GET /accounts/{account_id}/balances

See https://enablebanking.com/docs/api/reference/ for the full surface.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import ENABLE_BANKING_API_URL
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingSessionError,
)
from .models import AccountBalance

_LOGGER = logging.getLogger(__name__)

# Balance type preference: closing booked gives the "end of day" balance
# everyone reads as "my balance". Falling back to interim available keeps
# the sensor alive if a bank only exposes real-time availability.
_BALANCE_TYPE_PREFERENCE: tuple[str, ...] = (
    "CLBD",  # closing booked
    "ITAV",  # interim available
    "XPCD",  # expected
    "ITBD",  # interim booked
    "OPBD",  # opening booked
)


class EnableBankingClient:
    """Small async client for the Enable Banking AIS endpoints we need."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        jwt: str,
        session_id: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._jwt = jwt
        self._session_id = session_id

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt}",
            "Accept": "application/json",
        }

    async def _request_json(self, path: str) -> Any:
        url = f"{ENABLE_BANKING_API_URL}{path}"
        try:
            async with self._session.get(
                url,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                text = await response.text()
                if response.status == 401:
                    raise EnableBankingAuthenticationError(
                        f"Enable Banking rejected the JWT: {text}"
                    )
                if response.status == 403:
                    raise EnableBankingAuthenticationError(
                        f"Enable Banking forbade the request: {text}"
                    )
                if response.status == 404:
                    # /sessions/{id} returns 404 once the session is revoked,
                    # expired, or the id is wrong.
                    raise EnableBankingSessionError(
                        f"Session not found — it may have expired: {text}"
                    )
                if response.status >= 400:
                    raise EnableBankingAPIError(
                        f"Enable Banking HTTP {response.status}: {text}"
                    )
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise EnableBankingAPIError(
                        f"Invalid JSON from Enable Banking: {text}"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnableBankingConnectionError(
                f"Cannot connect to Enable Banking: {err}"
            ) from err

    async def async_validate(self) -> bool:
        """Check that the JWT and session id are both usable."""
        await self.async_get_session()
        return True

    async def async_get_session(self) -> dict[str, Any]:
        """Return the session object (includes the account list)."""
        data = await self._request_json(f"/sessions/{self._session_id}")
        if not isinstance(data, dict):
            raise EnableBankingAPIError(
                f"Unexpected session payload type: {type(data).__name__}"
            )
        return data

    async def async_get_account_balances(
        self, account_id: str
    ) -> list[dict[str, Any]]:
        """Return the list of balance objects for a single account."""
        data = await self._request_json(f"/accounts/{account_id}/balances")
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

        The mapping key is the Enable Banking account_id (uuid) so it is
        stable across renames. Accounts without a matching preferred
        balance type are skipped rather than surfaced as ``None``.
        """
        session = await self.async_get_session()

        # Enable Banking's session object contains "accounts" — a list
        # of account summaries with account_id, account_name, product
        # and identification (IBAN).
        account_summaries = session.get("accounts", [])
        if not isinstance(account_summaries, list):
            return {}

        out: dict[str, AccountBalance] = {}
        for summary in account_summaries:
            if not isinstance(summary, dict):
                continue
            account_id = summary.get("uid") or summary.get("account_id")
            if not account_id:
                continue

            iban = (
                (summary.get("account_id") or {}).get("iban")
                if isinstance(summary.get("account_id"), dict)
                else summary.get("iban", "")
            ) or ""

            name = (
                summary.get("name")
                or summary.get("account_name")
                or summary.get("product")
                or iban
            )
            product = summary.get("product")

            try:
                balances = await self.async_get_account_balances(account_id)
            except EnableBankingSessionError:
                raise
            except EnableBankingAuthenticationError:
                raise
            except EnableBankingConnectionError:
                raise
            except EnableBankingAPIError as err:
                _LOGGER.warning(
                    "Skipping account %s (%s): %s", name, account_id, err
                )
                continue

            picked = _pick_preferred_balance(balances)
            if picked is None:
                _LOGGER.debug(
                    "No usable balance for account %s (%s)", name, account_id
                )
                continue

            amount_obj = picked.get("balance_amount") or picked.get("amount") or {}
            try:
                amount = float(amount_obj.get("amount"))
            except (TypeError, ValueError):
                continue

            out[account_id] = AccountBalance(
                account_id=account_id,
                iban=iban,
                name=str(name),
                product=product if isinstance(product, str) else None,
                currency=str(amount_obj.get("currency", "EUR")),
                balance=amount,
                balance_type=picked.get("balance_type"),
                reference_date=picked.get("reference_date"),
            )

        return out


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
    # Fall back to whichever the bank returned first.
    for bal in balances:
        if isinstance(bal, dict):
            return bal
    return None

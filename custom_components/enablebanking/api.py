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
    GET  /accounts/{account_id}/transactions -> paged transaction history

See https://enablebanking.com/docs/api/reference/ for the full surface.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp

from .const import ENABLE_BANKING_API_URL, REDIRECT_URL
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingRateLimitError,
    EnableBankingSessionError,
)
from .models import AccountBalance

_LOGGER = logging.getLogger(__name__)

#: Upper bound on continuation-key pages per account per poll. 90 days of a
#: busy current account is comfortably under this; the cap exists so a server
#: that keeps handing back a key cannot spin the poll forever.
MAX_TRANSACTION_PAGES: int = 20

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
    def for_config_flow(cls, session: aiohttp.ClientSession, jwt: str) -> EnableBankingClient:
        """Create a client for config-flow steps that precede session creation."""
        return cls(session, jwt, "")

    def update_jwt(self, new_jwt: str) -> None:
        """Replace the active JWT (called by coordinator on auto-renewal)."""
        self._jwt = new_jwt

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt}",
            "Accept": "application/json",
        }

    def _jwt_debug_info(self) -> str:
        """Return non-secret JWT header claims for debug logging."""
        try:
            import base64
            import json as _json

            header_b64 = self._jwt.split(".")[0]
            # add padding
            header_b64 += "=" * (-len(header_b64) % 4)
            header = _json.loads(base64.urlsafe_b64decode(header_b64))
            payload_b64 = self._jwt.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
            import time

            exp = payload.get("exp", 0)
            remaining = exp - int(time.time())
            return (
                f"kid={header.get('kid', '?')!r} "
                f"alg={header.get('alg', '?')!r} "
                f"exp={exp} (expires in {remaining}s)"
            )
        except Exception:
            return "<could not decode JWT>"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{ENABLE_BANKING_API_URL}{path}"
        _LOGGER.debug("Enable Banking request: %s %s", method, url)
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
                _LOGGER.debug(
                    "Enable Banking response: HTTP %s for %s %s > body: %s",
                    response.status,
                    method,
                    url,
                    text[:3000],
                )
                if response.status in (401, 403):
                    # An expired/revoked consent also surfaces as 401, but with
                    # an EXPIRED_SESSION body > that's a session problem, not a
                    # bad JWT. Classify it as such for accurate logs (both still
                    # trigger reauth in the coordinator).
                    if response.status == 401 and "EXPIRED_SESSION" in text:
                        raise EnableBankingSessionError(f"Session expired (HTTP 401): {text[:200]}")
                    _LOGGER.error(
                        "Enable Banking JWT rejected (HTTP %s). JWT info: %s. Response: %s",
                        response.status,
                        self._jwt_debug_info(),
                        text[:500],
                    )
                    raise EnableBankingAuthenticationError(
                        f"Enable Banking rejected the JWT (HTTP {response.status}): {text[:200]}"
                    )
                if response.status == 404:
                    raise EnableBankingSessionError(f"Session not found or expired: {text}")
                if response.status == 429:
                    raise EnableBankingRateLimitError(
                        f"PSD2 rate limit exceeded at ASPSP: {text[:200]}"
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
            raise EnableBankingConnectionError(f"Cannot connect to Enable Banking: {err}") from err

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
            aspsps: list[dict[str, Any]] = result
            return aspsps
        nested = result.get("aspsps", []) if isinstance(result, dict) else []
        return nested if isinstance(nested, list) else []

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
        """Check that the JWT works AND the session is genuinely usable.

        Enable Banking returns HTTP 200 for ``GET /sessions/{id}`` even when the
        underlying consent has expired or been revoked > the session object is
        still readable, it just carries a non-``AUTHORIZED`` ``status`` and every
        ``/accounts/{uid}/balances`` call under it returns 401. Only treating a
        200 as "alive" makes the reauth fast-path keep a dead session forever, so
        require ``status == AUTHORIZED`` here.
        """
        session = await self.async_get_session()
        status = session.get("status")
        # If an ASPSP omits ``status`` we stay backward-compatible and assume OK.
        if status is not None and status != "AUTHORIZED":
            raise EnableBankingSessionError(f"Session status is {status!r}, not AUTHORIZED")
        return True

    async def async_get_session(self) -> dict[str, Any]:
        """Return the session object (includes the account list)."""
        data = await self._request("GET", f"/sessions/{self._session_id}")
        if not isinstance(data, dict):
            raise EnableBankingAPIError(f"Unexpected session payload type: {type(data).__name__}")
        return data

    async def async_get_account_balances(self, account_id: str) -> list[dict[str, Any]]:
        """Return the list of balance objects for a single account."""
        data = await self._request("GET", f"/accounts/{account_id}/balances")
        if not isinstance(data, dict):
            raise EnableBankingAPIError(f"Unexpected balances payload type: {type(data).__name__}")
        balances = data.get("balances", [])
        if not isinstance(balances, list):
            return []
        return balances

    async def async_get_transactions(
        self,
        account_id: str,
        date_from: date,
        max_pages: int = MAX_TRANSACTION_PAGES,
    ) -> list[dict[str, Any]]:
        """Return raw transaction objects for one account since ``date_from``.

        Enable Banking pages with an opaque ``continuation_key``: a response
        carrying one has more behind it. The loop is bounded by ``max_pages``
        rather than trusting the server to stop, because a key that never
        clears would otherwise spin inside a single coordinator poll, holding
        the update lock and spending rate-limit budget until the account is
        locked out for the day.

        Raises the same exceptions as every other call here, including
        ``EnableBankingRateLimitError`` on a 429 so the coordinator can apply
        its per-account back-off.
        """
        params: dict[str, str] = {"date_from": date_from.isoformat()}
        transactions: list[dict[str, Any]] = []

        for page in range(max_pages):
            data = await self._request("GET", f"/accounts/{account_id}/transactions", params=params)
            if not isinstance(data, dict):
                raise EnableBankingAPIError(
                    f"Unexpected transactions payload type: {type(data).__name__}"
                )
            batch = data.get("transactions")
            if isinstance(batch, list):
                transactions.extend(item for item in batch if isinstance(item, dict))

            continuation_key = data.get("continuation_key")
            if not isinstance(continuation_key, str) or not continuation_key:
                break
            params["continuation_key"] = continuation_key
            if page == max_pages - 1:
                _LOGGER.warning(
                    "Stopped paging transactions for %s after %d pages with a "
                    "continuation key still set; some history is missing",
                    account_id[:8],
                    max_pages,
                )

        return transactions

    async def async_get_account_details(self, account_id: str) -> dict[str, Any]:
        """Return the account-details object for a single account.

        The balances endpoint carries no account identification, and several
        ASPSPs (e.g. N26, SNS Bank) omit the IBAN from the session payload too.
        This endpoint returns it (``account_id.iban``) plus ``name``/``product``/
        ``currency``. It has its own per-account rate-limit budget separate from
        ``/balances``, and the IBAN is immutable, so callers fetch it once per
        account and cache the result.
        """
        data = await self._request("GET", f"/accounts/{account_id}/details")
        if not isinstance(data, dict):
            raise EnableBankingAPIError(
                f"Unexpected account-details payload type: {type(data).__name__}"
            )
        return data

    async def async_get_all_balances(
        self,
        fallback: dict[str, AccountBalance] | None = None,
        skip_ids: set[str] | None = None,
        fetch_details: bool = True,
        legacy_by_uid: dict[str, AccountBalance] | None = None,
    ) -> tuple[dict[str, AccountBalance], set[str]]:
        """Return (accounts, rate_limited_ids) for the current session.

        Accounts are keyed by ``stable_id`` (Enable Banking's
        ``identification_hash``), not the session ``uid`` > the uid changes on
        every reauth, the stable_id does not.

        ``fallback`` is the coordinator's previous per-stable_id data. If an
        account's balance fetch hits a 429 (or is in ``skip_ids`` for
        back-off), we return its previous ``AccountBalance`` rather than
        dropping the sensor. The returned ``rate_limited_ids`` set tells
        the coordinator which accounts need a back-off flag set on their
        cached entry.

        Session payload shape (observed for N26 and similar ASPSPs):
            {
              "accounts": ["<uid>", "<uid>", ...],
              "accounts_data": [{"uid": "<uid>", "account_id": {"iban": ...}, ...}, ...],
              ...
            }
        Some ASPSPs instead return rich dicts in ``accounts`` directly > this
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
            # Visible at INFO/WARNING (not just DEBUG) because this is the
            # usual reason an entry loads but never gets sensors: the consent
            # was granted but the session exposes no accounts.
            aspsp_raw = session.get("aspsp")
            aspsp: dict[str, Any] = aspsp_raw if isinstance(aspsp_raw, dict) else {}
            _LOGGER.warning(
                "Enable Banking: session for %s returned no accounts "
                "(status=%s, session keys=%s). If the bank consent was just "
                "granted, check in the Enable Banking dashboard that the "
                "account is linked/authorised and that the app's Account "
                "Information service is not 'Restricted'.",
                aspsp.get("name", "?"),
                session.get("status"),
                sorted(session.keys()),
            )
            return {}, set()

        out: dict[str, AccountBalance] = {}
        rate_limited: set[str] = set()
        for uid in uids:
            meta = metadata.get(uid, {})
            stable_id = _account_stable_id(meta, uid)
            _LOGGER.debug(
                "metadata for %s (stable %s): keys=%s",
                uid[:8],
                _short_token(stable_id),
                sorted(meta.keys()) if meta else "<missing>",
            )

            # Respect the coordinator's back-off: don't spend a poll on
            # an account we already know is rate-limited this cycle.
            if skip_ids and stable_id in skip_ids:
                if fallback and stable_id in fallback:
                    _LOGGER.debug("Skipping %s > rate-limit back-off active", uid[:8])
                    out[stable_id] = fallback[stable_id]
                continue

            # Resolve the account's IBAN / display fields. Order of preference:
            #   1. the session payload (some ASPSPs include it there),
            #   2. the previous cache entry (already resolved on an earlier poll),
            #   3. a one-time GET /accounts/{uid}/details call (N26, SNS Bank …
            #      omit the IBAN from the session, and balances carry none).
            prev = fallback.get(stable_id) if fallback else None
            if prev is None and legacy_by_uid:
                # Pre-0.6.5 last-known balance, keyed by the (current) uid.
                prev = legacy_by_uid.get(uid)
            iban = _account_iban(meta) or (prev.iban if prev else "")
            name = _account_display_name(meta) or (prev.name if prev else "")
            product = meta.get("product") if isinstance(meta.get("product"), str) else None
            if not product and prev and prev.product:
                product = prev.product

            if not iban and fetch_details:
                # Separate per-account rate-limit budget from /balances, and the
                # IBAN never changes, so this runs at most once per account.
                # Let session/auth/connection errors propagate (they mean the
                # whole poll should fail/trigger reauth); only shrug off a
                # details-specific rate limit or API hiccup.
                try:
                    details = await self.async_get_account_details(uid)
                except EnableBankingRateLimitError:
                    _LOGGER.debug(
                        "Account details rate-limited for %s; will retry next poll",
                        uid[:8],
                    )
                    details = None
                except EnableBankingAPIError as err:
                    _LOGGER.debug("Could not fetch account details for %s: %s", uid[:8], err)
                    details = None
                if details:
                    iban = _account_iban(details) or iban
                    name = name or _account_display_name(details)
                    detail_product = details.get("product")
                    if not product and isinstance(detail_product, str):
                        product = detail_product

            # Name must never depend on the session uid (it churns on reauth);
            # fall back to a stable short token derived from stable_id.
            name = name or iban or _short_token(stable_id)

            try:
                balances = await self.async_get_account_balances(uid)
            except EnableBankingSessionError:
                raise
            except EnableBankingAuthenticationError:
                raise
            except EnableBankingConnectionError:
                raise
            except EnableBankingRateLimitError as err:
                rate_limited.add(stable_id)
                if prev is not None:
                    _LOGGER.warning(
                        "Rate limited on %s > keeping previous balance "
                        "(PSD2 caps AIS polling at 4/day). Error: %s",
                        name,
                        err,
                    )
                    # Adopt the previous value under the resolved stable_id
                    # (prev may be a legacy uid-keyed entry from before 0.6.5).
                    prev.stable_id = stable_id
                    prev.account_id = uid
                    out[stable_id] = prev
                else:
                    _LOGGER.warning(
                        "Rate limited on %s and no previous balance to fall back on. Error: %s",
                        name,
                        err,
                    )
                continue
            except EnableBankingAPIError as err:
                _LOGGER.warning("Skipping account %s (%s): %s", name, uid, err)
                continue

            _LOGGER.debug(
                "account %s (%s) > %d balance object(s), types=%s",
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

            amount_raw = picked.get("balance_amount") or picked.get("amount") or {}
            amount_obj: dict[str, Any] = amount_raw if isinstance(amount_raw, dict) else {}
            # A missing key and an unparseable value are the same failure to us
            # (no usable number), so funnel both into one warning-and-skip.
            amount: float | None = None
            if (raw_value := amount_obj.get("amount")) is not None:
                try:
                    amount = float(raw_value)
                except (TypeError, ValueError):
                    amount = None
            if amount is None:
                _LOGGER.warning("Could not parse amount for %s; picked=%r", uid, picked)
                continue

            out[stable_id] = AccountBalance(
                account_id=uid,
                stable_id=stable_id,
                iban=iban,
                name=str(name),
                product=product,
                currency=str(amount_obj.get("currency", "EUR")),
                balance=amount,
                balance_type=picked.get("balance_type"),
                reference_date=picked.get("reference_date"),
            )

        _LOGGER.debug(
            "async_get_all_balances produced %d account balance(s); %d rate-limited",
            len(out),
            len(rate_limited),
        )
        return out, rate_limited


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
    or a dict keyed by uid > handle both.
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
    unique_uids: list[str] = []
    for uid_value in uids:
        if uid_value in seen:
            continue
        seen.add(uid_value)
        unique_uids.append(uid_value)
    return unique_uids, metadata


def _account_stable_id(meta: dict[str, Any], uid: str) -> str:
    """Return a session-independent identifier for an account.

    Enable Banking regenerates the account ``uid`` on every session, so it is
    useless as a persistent key. ``identification_hash`` is account-intrinsic
    (a hash over IBAN+currency, or bank+country+resource_id for IBAN-less
    accounts) and stays constant across sessions > that's what we key on.
    Falls back to ``uid`` only in the unlikely event the hash is absent.
    """
    hash_id = meta.get("identification_hash")
    if isinstance(hash_id, str) and hash_id:
        return hash_id
    return uid


def _short_token(value: str) -> str:
    """A short, stable, filesystem/name-safe token derived from ``value``."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _account_iban(meta: dict[str, Any]) -> str:
    """Extract an IBAN from the account-metadata dict.

    ASPSPs vary: some put it top-level as ``iban``, some nest under
    ``account_id.iban`` (Berlin Group style), others use ``identification``,
    ``details``, or ``account``. Walk the likely paths and return the first
    string hit.
    """
    for key in ("iban", "IBAN"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            return val
    for container_key in (
        "account_id",
        "identification",
        "identifications",
        "details",
        "account",
    ):
        container = meta.get(container_key)
        if isinstance(container, dict):
            for key in ("iban", "IBAN"):
                val = container.get(key)
                if isinstance(val, str) and val:
                    return val
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, dict):
                    for key in ("iban", "IBAN"):
                        val = item.get(key)
                        if isinstance(val, str) and val:
                            return val
    return ""


def _account_display_name(meta: dict[str, Any]) -> str:
    """Best human-readable name for an account, across ASPSP variations."""
    for key in (
        "name",
        "displayName",
        "display_name",
        "account_name",
        "ownerName",
        "owner_name",
        "product",
        "cash_account_type",
    ):
        val = meta.get(key)
        if isinstance(val, str) and val:
            return val
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

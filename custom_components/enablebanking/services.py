"""The ``enablebanking.get_transactions`` service (issue #2).

Transactions are fetched only when an automation or the user asks for them,
never on the balance schedule: each call spends the account's PSD2 quota for
the transactions endpoint (typically 4 per day without the user present).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnableBankingCoordinator
from .entity import account_unique_id
from .errors import EnableBankingError, EnableBankingRateLimitError
from .models import AccountBalance

# Same import dance as config_flow.py: probatio from HA 2026.9, voluptuous before.
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol  # type: ignore[no-redef]

SERVICE_GET_TRANSACTIONS = "get_transactions"

ATTR_DATE_FROM = "date_from"
ATTR_DATE_TO = "date_to"
ATTR_STATUS = "status"

# Default look-back when date_from is left out.
DEFAULT_DAYS_BACK = 30

# Statuses Enable Banking can filter on (ISO 20022 transaction status codes).
TRANSACTION_STATUSES = ("BOOK", "PDNG", "SCHD", "HOLD", "CNCL", "RJCT", "OTHR")

GET_TRANSACTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_DATE_FROM): cv.date,
        vol.Optional(ATTR_DATE_TO): cv.date,
        vol.Optional(ATTR_STATUS): vol.In(TRANSACTION_STATUSES),
    }
)


def register_transaction_service(hass: HomeAssistant) -> None:
    """Register ``enablebanking.get_transactions`` once for the domain."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_TRANSACTIONS):
        return

    async def _handle(call: ServiceCall) -> ServiceResponse:
        return await _async_get_transactions(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TRANSACTIONS,
        _handle,
        schema=GET_TRANSACTIONS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _resolve_account(
    hass: HomeAssistant, entity_id: str
) -> tuple[EnableBankingCoordinator, AccountBalance]:
    """Map a balance sensor's entity_id to its coordinator and account."""
    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry is None or reg_entry.platform != DOMAIN or not reg_entry.config_entry_id:
        raise ServiceValidationError(f"{entity_id} is not an Enable Banking balance sensor")
    entry = hass.config_entries.async_get_entry(reg_entry.config_entry_id)
    coordinator: EnableBankingCoordinator | None = getattr(entry, "runtime_data", None)
    if entry is None or coordinator is None:
        raise ServiceValidationError(f"The bank entry behind {entity_id} is not loaded")

    # Latest poll first, the on-disk cache as a fallback (e.g. right after a restart).
    candidates: dict[str, AccountBalance] = {}
    for stable_id in coordinator.cached_stable_ids():
        if (cached := coordinator.cached_account(stable_id)) is not None:
            candidates[stable_id] = cached
    if coordinator.data is not None:
        candidates.update(coordinator.data.accounts)
    for stable_id, account in candidates.items():
        if account_unique_id(entry.entry_id, stable_id, "balance") == reg_entry.unique_id:
            return coordinator, account
    raise ServiceValidationError(
        f"No account data for {entity_id} yet; wait for the first balance poll"
    )


def summarise_transaction(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Enable Banking transaction into automation-friendly fields.

    ``amount`` is signed, negative for money leaving the account (DBIT), so a
    template can simply sum it. The bank's own payload stays under ``raw``.
    """
    amount_raw = raw.get("transaction_amount")
    amount_obj: dict[str, Any] = amount_raw if isinstance(amount_raw, dict) else {}
    amount: float | None
    try:
        amount = float(amount_obj["amount"])
    except (KeyError, TypeError, ValueError):
        amount = None
    indicator = raw.get("credit_debit_indicator")
    if amount is not None:
        amount = -abs(amount) if indicator == "DBIT" else abs(amount)

    # The other party: who got paid on a debit, who paid on a credit.
    party = raw.get("creditor" if indicator == "DBIT" else "debtor")
    counterparty = party.get("name") if isinstance(party, dict) else None

    remittance = raw.get("remittance_information")
    if isinstance(remittance, list):
        description = " ".join(str(r) for r in remittance if r)
    else:
        description = str(remittance) if remittance else ""

    return {
        "date": raw.get("booking_date") or raw.get("value_date") or raw.get("transaction_date"),
        "booking_date": raw.get("booking_date"),
        "value_date": raw.get("value_date"),
        "amount": amount,
        "currency": amount_obj.get("currency"),
        "status": raw.get("status"),
        "credit_debit_indicator": indicator,
        "counterparty": counterparty,
        "description": description,
        "raw": raw,
    }


async def _async_get_transactions(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entity_id: str = call.data[ATTR_ENTITY_ID]
    today = dt_util.now().date()
    date_from: date = call.data.get(ATTR_DATE_FROM) or today - timedelta(days=DEFAULT_DAYS_BACK)
    date_to: date | None = call.data.get(ATTR_DATE_TO)
    if date_from > today:
        raise ServiceValidationError(
            "date_from cannot be in the future (Enable Banking rejects it). Use today "
            "as date_from and a future date_to to ask for scheduled payments."
        )
    if date_to is not None and date_to < date_from:
        raise ServiceValidationError("date_to must be on or after date_from")

    coordinator, account = _resolve_account(hass, entity_id)
    await coordinator.async_renew_jwt_if_needed()
    try:
        raw = await coordinator.client.async_get_transactions(
            account.account_id, date_from, date_to, call.data.get(ATTR_STATUS)
        )
    except EnableBankingRateLimitError as err:
        raise HomeAssistantError(
            f"The bank's PSD2 rate limit was hit for {entity_id}, try again later: {err}"
        ) from err
    except EnableBankingError as err:
        raise HomeAssistantError(f"Could not fetch transactions for {entity_id}: {err}") from err

    transactions = [summarise_transaction(t) for t in raw]
    total = round(sum(t["amount"] for t in transactions if t["amount"] is not None), 2)
    return {
        "entity_id": entity_id,
        "iban": account.iban,
        "currency": account.currency,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat() if date_to else None,
        "count": len(transactions),
        "total": total,
        "transactions": transactions,
    }

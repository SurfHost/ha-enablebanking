"""Data models for the Enable Banking integration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

#: ``credit_debit_indicator`` values that mean money left the account.
#: ISO 20022 spells debit ``DBIT``; Enable Banking's published reference renders
#: it ``DRBT`` in one place. Accept both rather than silently booking every
#: debit as income, which would invert the sign of the whole spending series.
DEBIT_INDICATORS: frozenset[str] = frozenset({"DBIT", "DRBT"})


@dataclass(slots=True)
class AccountBalance:
    """Balance snapshot for a single account.

    Mutable (not frozen) because the coordinator updates ``last_polled_at``
    and ``rate_limited_until`` in place as polls complete or back-offs
    trigger. The cache round-trip (disk to coordinator) relies on these
    fields being persisted alongside the balance itself so that, after an
    HA restart, the sensor can show exactly how old the displayed value
    is and whether a back-off is still in force.

    ``stable_id`` is Enable Banking's ``identification_hash`` > an
    account-intrinsic value (derived from IBAN+currency, or resource_id for
    IBAN-less accounts) that stays constant across sessions. It is the key we
    use for entity identity and the cache. ``account_id`` is the session-scoped
    ``uid`` which Enable Banking regenerates on every reauth; it is only used
    to call ``GET /accounts/{uid}/balances`` and to migrate old entity ids.
    """

    account_id: str
    stable_id: str
    iban: str
    name: str
    product: str | None
    currency: str
    balance: float
    balance_type: str | None
    reference_date: str | None
    last_polled_at: datetime | None = None
    rate_limited_until: datetime | None = None


@dataclass(slots=True)
class EnableBankingData:
    """Container for all Enable Banking data from one coordinator poll."""

    accounts: dict[str, AccountBalance] = field(default_factory=dict)
    consent_expires_at: datetime | None = None
    #: Booked transactions seen for the first time on *this* poll, keyed by
    #: account stable_id. Only what is new: the event platform fires one event
    #: per entry here, so carrying the whole window would refire the entire
    #: history on every poll.
    new_transactions: dict[str, list[Transaction]] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Transaction:
    """One booked or pending entry on an account.

    Frozen, unlike ``AccountBalance``: a transaction is a historical fact. If
    the bank restates one it arrives as a different object, and the dedup key
    below is what decides whether that counts as the same entry.

    Only the fields the integration actually surfaces are kept. The raw object
    also carries postal addresses, agents and exchange rates; those would end
    up in recorded event payloads for no benefit.
    """

    key: str
    amount: float
    currency: str
    is_debit: bool
    booked: bool
    booking_date: str | None
    value_date: str | None
    counterparty: str | None
    reference: str | None
    bank_transaction_code: str | None

    @property
    def signed_amount(self) -> float:
        """Negative when money left the account, for arithmetic on a mixed list."""
        return -self.amount if self.is_debit else self.amount

    def as_event_payload(self) -> dict[str, str | float | None]:
        """The flat, trimmed dict fired on the event entity.

        Flat on purpose: event payloads are written to the recorder with every
        state change, and nested bank objects make both the database rows and
        the automation templates that read them larger than they need to be.
        """
        return {
            "amount": self.amount,
            "currency": self.currency,
            "direction": "debit" if self.is_debit else "credit",
            "counterparty": self.counterparty,
            "reference": self.reference,
            "booking_date": self.booking_date,
            "value_date": self.value_date,
            "bank_transaction_code": self.bank_transaction_code,
        }


def transaction_dedup_key(raw: dict[str, object]) -> str:
    """A stable identity for one transaction, across polls and restarts.

    ``entry_reference`` is the right answer and most ASPSPs send it.
    ``transaction_id`` is the fallback. Both are optional in the schema
    though, and an account whose bank sends neither would otherwise refire
    every event and re-import every statistic on every poll — so the last
    resort is a hash of the fields that identify the entry in practice.

    The hash deliberately excludes ``status``: a pending entry that later
    books must map to the same key, or it would be counted twice.
    """
    for field_name in ("entry_reference", "transaction_id"):
        value = raw.get(field_name)
        if isinstance(value, str) and value:
            return value

    amount_obj = raw.get("transaction_amount")
    amount = amount_obj.get("amount") if isinstance(amount_obj, dict) else None
    currency = amount_obj.get("currency") if isinstance(amount_obj, dict) else None
    remittance = raw.get("remittance_information")
    if isinstance(remittance, list):
        remittance_text = "|".join(str(part) for part in remittance)
    else:
        remittance_text = str(remittance or "")

    parts = [
        str(raw.get("booking_date") or ""),
        str(raw.get("value_date") or ""),
        str(amount or ""),
        str(currency or ""),
        str(raw.get("credit_debit_indicator") or ""),
        _counterparty_name(raw) or "",
        remittance_text,
    ]
    return "sha256:" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _counterparty_name(raw: dict[str, object]) -> str | None:
    """The other party's name, whichever side of the transaction they are on."""
    for field_name in ("creditor", "debtor"):
        party = raw.get(field_name)
        if isinstance(party, dict):
            name = party.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def transaction_from_raw(raw: dict[str, object]) -> Transaction | None:
    """Build a `Transaction`, or None when the entry carries no usable amount."""
    amount_obj = raw.get("transaction_amount")
    if not isinstance(amount_obj, dict):
        return None
    try:
        amount = abs(float(amount_obj.get("amount")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

    remittance = raw.get("remittance_information")
    if isinstance(remittance, list) and remittance:
        reference: str | None = " ".join(str(part) for part in remittance if part)
    elif isinstance(remittance, str) and remittance:
        reference = remittance
    else:
        reference = None

    code_obj = raw.get("bank_transaction_code")
    code = code_obj.get("description") if isinstance(code_obj, dict) else None

    indicator = str(raw.get("credit_debit_indicator") or "").upper()

    return Transaction(
        key=transaction_dedup_key(raw),
        amount=amount,
        currency=str(amount_obj.get("currency") or "EUR"),
        is_debit=indicator in DEBIT_INDICATORS,
        booked=str(raw.get("status") or "").upper() == "BOOK",
        booking_date=_as_str(raw.get("booking_date")),
        value_date=_as_str(raw.get("value_date")),
        counterparty=_counterparty_name(raw),
        reference=reference,
        bank_transaction_code=code if isinstance(code, str) and code else None,
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

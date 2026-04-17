"""Data models for the ASN Bank Balance integration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """Represents a single account balance snapshot from Enable Banking."""

    account_id: str
    iban: str
    name: str
    product: str | None
    currency: str
    balance: float
    balance_type: str | None
    reference_date: str | None


@dataclass(slots=True)
class AsnBankData:
    """Container for all ASN Bank data from the coordinator."""

    accounts: dict[str, AccountBalance] = field(default_factory=dict)

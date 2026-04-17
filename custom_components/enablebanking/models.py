"""Data models for the Enable Banking integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
class EnableBankingData:
    """Container for all Enable Banking data from one coordinator poll."""

    accounts: dict[str, AccountBalance] = field(default_factory=dict)
    consent_expires_at: datetime | None = None

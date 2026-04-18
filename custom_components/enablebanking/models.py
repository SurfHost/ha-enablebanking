"""Data models for the Enable Banking integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class AccountBalance:
    """Balance snapshot for a single account.

    Mutable (not frozen) because the coordinator updates ``last_polled_at``
    and ``rate_limited_until`` in place as polls complete or back-offs
    trigger. The cache round-trip (disk ↔ coordinator) relies on these
    fields being persisted alongside the balance itself so that, after an
    HA restart, the sensor can show exactly how old the displayed value
    is and whether a back-off is still in force.
    """

    account_id: str
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

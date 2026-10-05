"""Tests for the on-disk balance cache serialisation in `coordinator.py`.

This cache is what makes sensors survive a restart without spending PSD2
quota, and what carried balances through the 0.6.5 stable-id migration. A
round-trip that quietly drops a field shows up as a sensor that reads
`unknown` after a restart, which is exactly the failure the cache exists to
prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from custom_components.enablebanking.coordinator import (
    _balance_from_stored,
    _balance_to_stored,
    _parse_iso,
)
from custom_components.enablebanking.models import AccountBalance

POLLED_AT = datetime(2026, 9, 1, 10, 30, tzinfo=UTC)
LIMITED_UNTIL = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def _balance(**overrides: Any) -> AccountBalance:
    defaults: dict[str, Any] = {
        "account_id": "uid-one",
        "stable_id": "hash-one",
        "iban": "NL91ABNA0417164300",
        "name": "Betaalrekening",
        "product": "Current Account",
        "currency": "EUR",
        "balance": 1234.56,
        "balance_type": "CLBD",
        "reference_date": "2026-09-01",
        "last_polled_at": POLLED_AT,
        "rate_limited_until": LIMITED_UNTIL,
    }
    return AccountBalance(**{**defaults, **overrides})


def test_round_trip_preserves_every_field() -> None:
    """Written then read back, the account must be unchanged."""
    original = _balance()

    restored = _balance_from_stored(_balance_to_stored(original))

    assert restored == original


def test_round_trip_preserves_optional_nulls() -> None:
    original = _balance(
        product=None,
        balance_type=None,
        reference_date=None,
        last_polled_at=None,
        rate_limited_until=None,
    )

    restored = _balance_from_stored(_balance_to_stored(original))

    assert restored == original


def test_timestamps_survive_as_aware_datetimes() -> None:
    """Naive datetimes here would blow up the `rate_limited_until > now` compare."""
    restored = _balance_from_stored(_balance_to_stored(_balance()))

    assert restored is not None
    assert restored.last_polled_at == POLLED_AT
    assert restored.last_polled_at.tzinfo is not None
    assert restored.rate_limited_until == LIMITED_UNTIL


def test_pre_0_6_5_entry_has_no_stable_id() -> None:
    """Old cache files were keyed by uid; those get adopted on the first poll."""
    restored = _balance_from_stored(
        {"account_id": "uid-one", "balance": 10.0, "iban": "NL91ABNA0417164300"}
    )

    assert restored is not None
    assert restored.stable_id == ""
    assert restored.account_id == "uid-one"


def test_balance_is_coerced_to_float() -> None:
    """Enable Banking sends amounts as strings; the cache must not echo one back."""
    restored = _balance_from_stored(
        {"account_id": "uid-one", "stable_id": "hash-one", "balance": "10.50"}
    )

    assert restored is not None
    assert restored.balance == 10.50
    assert isinstance(restored.balance, float)


def test_currency_defaults_to_eur_when_absent() -> None:
    restored = _balance_from_stored({"account_id": "uid", "stable_id": "h", "balance": 1.0})

    assert restored is not None
    assert restored.currency == "EUR"


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {"stable_id": "hash-one"},
        {"account_id": "uid-one"},
        {"account_id": "uid-one", "balance": "not-a-number"},
        {"account_id": "uid-one", "balance": None},
    ],
    ids=["empty", "no-account-id", "no-balance", "unparseable-balance", "null-balance"],
)
def test_malformed_entries_are_skipped_not_raised(stored: dict[str, Any]) -> None:
    """One corrupt entry must not take the whole cache — and the entry — down."""
    assert _balance_from_stored(stored) is None


class TestParseIso:
    """`_parse_iso` guards every timestamp read back off disk."""

    def test_parses_an_aware_timestamp(self) -> None:
        assert _parse_iso("2026-09-01T10:30:00+00:00") == POLLED_AT

    def test_round_trips_isoformat(self) -> None:
        assert _parse_iso(POLLED_AT.isoformat()) == POLLED_AT

    @pytest.mark.parametrize(
        "value",
        [None, "", "not-a-date", 42, {}, "2026-13-45T99:99:99"],
        ids=["none", "empty", "garbage", "int", "dict", "impossible-date"],
    )
    def test_unusable_values_become_none(self, value: Any) -> None:
        assert _parse_iso(value) is None

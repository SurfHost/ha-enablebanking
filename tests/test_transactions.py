"""Tests for transaction parsing, dedup and the statistics series.

The statistics tests matter most. A cumulative sum that double-counts cannot
be repaired without deleting the statistic, and the bug only shows up on the
*second* import — which is exactly the case a hand-test skips.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.enablebanking.const import STATISTIC_INCOME, STATISTIC_SPENDING
from custom_components.enablebanking.models import (
    Transaction,
    transaction_dedup_key,
    transaction_from_raw,
)
from custom_components.enablebanking.statistics import (
    async_import_statistics,
    daily_totals,
    merge_daily_totals,
    statistic_id,
)


def raw(**overrides: Any) -> dict[str, Any]:
    """A booked debit, in the shape Enable Banking returns."""
    base: dict[str, Any] = {
        "entry_reference": "ref-1",
        "transaction_amount": {"currency": "EUR", "amount": "12.34"},
        "credit_debit_indicator": "DBIT",
        "status": "BOOK",
        "booking_date": "2026-09-01",
        "value_date": "2026-09-01",
        "creditor": {"name": "Albert Heijn"},
        "remittance_information": ["Groceries"],
        "bank_transaction_code": {"description": "Card payment"},
    }
    base.update(overrides)
    return base


class TestParsing:
    """`transaction_from_raw` flattens one entry."""

    def test_reads_the_fields_the_integration_surfaces(self) -> None:
        tx = transaction_from_raw(raw())

        assert tx is not None
        assert tx.amount == 12.34
        assert tx.currency == "EUR"
        assert tx.is_debit is True
        assert tx.booked is True
        assert tx.counterparty == "Albert Heijn"
        assert tx.reference == "Groceries"
        assert tx.bank_transaction_code == "Card payment"

    @pytest.mark.parametrize("indicator", ["DBIT", "DRBT", "dbit"])
    def test_both_debit_spellings_are_debits(self, indicator: str) -> None:
        """The published reference renders debit as `DRBT` in one place.

        Reading only `DBIT` would silently book every debit as income and
        invert the whole spending series, so both are accepted.
        """
        tx = transaction_from_raw(raw(credit_debit_indicator=indicator))

        assert tx is not None
        assert tx.is_debit is True

    def test_credit_is_not_a_debit(self) -> None:
        tx = transaction_from_raw(raw(credit_debit_indicator="CRDT"))

        assert tx is not None
        assert tx.is_debit is False
        assert tx.signed_amount == 12.34

    def test_debit_signs_negative(self) -> None:
        tx = transaction_from_raw(raw())

        assert tx is not None
        assert tx.signed_amount == -12.34

    @pytest.mark.parametrize("status", ["PEND", "OTHR", ""])
    def test_unbooked_entries_are_marked_unbooked(self, status: str) -> None:
        tx = transaction_from_raw(raw(status=status))

        assert tx is not None
        assert tx.booked is False

    def test_debtor_name_used_when_there_is_no_creditor(self) -> None:
        entry = raw(creditor=None, debtor={"name": "Employer BV"})
        tx = transaction_from_raw(entry)

        assert tx is not None
        assert tx.counterparty == "Employer BV"

    @pytest.mark.parametrize(
        "entry",
        [{}, {"transaction_amount": None}, {"transaction_amount": {"amount": "abc"}}],
        ids=["no-amount-object", "null-amount", "unparseable"],
    )
    def test_unusable_entry_is_skipped(self, entry: dict[str, Any]) -> None:
        assert transaction_from_raw(entry) is None

    def test_event_payload_is_flat(self) -> None:
        """Nested objects would be written into the recorder on every event."""
        tx = transaction_from_raw(raw())

        assert tx is not None
        payload = tx.as_event_payload()

        assert payload["direction"] == "debit"
        assert all(not isinstance(value, dict | list) for value in payload.values())


class TestDedupKey:
    """Identity across polls and restarts."""

    def test_entry_reference_wins(self) -> None:
        assert transaction_dedup_key(raw()) == "ref-1"

    def test_falls_back_to_transaction_id(self) -> None:
        entry = raw(entry_reference=None, transaction_id="tx-9")

        assert transaction_dedup_key(entry) == "tx-9"

    def test_hashes_when_the_bank_sends_neither(self) -> None:
        """Otherwise such an account refires every event on every poll."""
        entry = raw(entry_reference=None)

        key = transaction_dedup_key(entry)

        assert key.startswith("sha256:")
        assert transaction_dedup_key(raw(entry_reference=None)) == key

    def test_hash_ignores_status_so_pending_becomes_booked_cleanly(self) -> None:
        """A pending entry that later books is the same entry, not a new one."""
        pending = transaction_dedup_key(raw(entry_reference=None, status="PEND"))
        booked = transaction_dedup_key(raw(entry_reference=None, status="BOOK"))

        assert pending == booked

    def test_hash_separates_different_amounts(self) -> None:
        one = transaction_dedup_key(
            raw(entry_reference=None, transaction_amount={"currency": "EUR", "amount": "1.00"})
        )
        two = transaction_dedup_key(
            raw(entry_reference=None, transaction_amount={"currency": "EUR", "amount": "2.00"})
        )

        assert one != two


class TestDailyTotals:
    """Per-day rollup feeding the statistics."""

    def test_splits_spending_from_income(self) -> None:
        transactions = [
            transaction_from_raw(raw(entry_reference="a")),
            transaction_from_raw(raw(entry_reference="b", credit_debit_indicator="CRDT")),
        ]

        totals = daily_totals([t for t in transactions if t])

        assert totals["2026-09-01"][STATISTIC_SPENDING] == 12.34
        assert totals["2026-09-01"][STATISTIC_INCOME] == 12.34

    def test_pending_is_excluded(self) -> None:
        """A pending entry counted into a cumulative sum cannot be taken back."""
        transactions = [
            transaction_from_raw(raw(entry_reference="a", status="PEND")),
        ]

        assert daily_totals([t for t in transactions if t]) == {}

    def test_groups_by_day(self) -> None:
        transactions = [
            transaction_from_raw(raw(entry_reference="a", booking_date="2026-09-01")),
            transaction_from_raw(raw(entry_reference="b", booking_date="2026-09-02")),
            transaction_from_raw(raw(entry_reference="c", booking_date="2026-09-02")),
        ]

        totals = daily_totals([t for t in transactions if t])

        assert totals["2026-09-01"][STATISTIC_SPENDING] == 12.34
        assert round(totals["2026-09-02"][STATISTIC_SPENDING], 2) == 24.68


class TestMergeDailyTotals:
    """Folding a poll's window into the stored history."""

    def test_window_days_are_replaced_not_added(self) -> None:
        """A restated day must not accumulate on top of its previous value."""
        stored = {"2026-09-05": {STATISTIC_SPENDING: 100.0, STATISTIC_INCOME: 0.0}}
        fresh = {"2026-09-05": {STATISTIC_SPENDING: 40.0, STATISTIC_INCOME: 0.0}}

        merged = merge_daily_totals(stored, fresh, date(2026, 9, 1))

        assert merged["2026-09-05"][STATISTIC_SPENDING] == 40.0

    def test_days_before_the_window_are_kept(self) -> None:
        """They are outside what the poll asked about, so it says nothing about them."""
        stored = {"2026-08-01": {STATISTIC_SPENDING: 7.0, STATISTIC_INCOME: 0.0}}

        merged = merge_daily_totals(stored, {}, date(2026, 9, 1))

        assert merged["2026-08-01"][STATISTIC_SPENDING] == 7.0

    def test_emptied_day_inside_the_window_is_dropped(self) -> None:
        """The bank saying a day is empty differs from the day being unknown."""
        stored = {"2026-09-05": {STATISTIC_SPENDING: 100.0, STATISTIC_INCOME: 0.0}}

        merged = merge_daily_totals(stored, {}, date(2026, 9, 1))

        assert "2026-09-05" not in merged


class TestStatisticsImport:
    """The cumulative series, and the reason it is safe to repeat."""

    @staticmethod
    def _account() -> MagicMock:
        account = MagicMock()
        account.stable_id = "hash-one"
        account.iban = "NL91ABNA0417164300"
        account.name = "Betaalrekening"
        account.currency = "EUR"
        return account

    @staticmethod
    def _totals() -> dict[str, dict[str, float]]:
        return {
            "2026-09-01": {STATISTIC_SPENDING: 10.0, STATISTIC_INCOME: 0.0},
            "2026-09-02": {STATISTIC_SPENDING: 5.0, STATISTIC_INCOME: 100.0},
            "2026-09-03": {STATISTIC_SPENDING: 2.5, STATISTIC_INCOME: 0.0},
        }

    def _import(self, totals: dict[str, dict[str, float]], window_start: date) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def _capture(_hass: Any, metadata: Any, points: Any) -> None:
            captured[metadata["statistic_id"]] = (metadata, list(points))

        with patch(
            "custom_components.enablebanking.statistics.async_add_external_statistics",
            side_effect=_capture,
        ):
            async_import_statistics(MagicMock(), self._account(), totals, window_start)
        return captured

    def test_sum_is_cumulative(self) -> None:
        captured = self._import(self._totals(), date(2026, 9, 1))

        _meta, points = captured[statistic_id("hash-one", STATISTIC_SPENDING)]

        assert [p["state"] for p in points] == [10.0, 5.0, 2.5]
        assert [p["sum"] for p in points] == [10.0, 15.0, 17.5]

    def test_repeating_the_import_does_not_double_count(self) -> None:
        """The regression this design exists to prevent.

        Every poll re-reads a window it has already imported. If the running
        total restarted from the previous sum instead of being recomputed, the
        second import would report 35.0 and the history would be wrong forever.
        """
        first = self._import(self._totals(), date(2026, 9, 1))
        second = self._import(self._totals(), date(2026, 9, 1))

        key = statistic_id("hash-one", STATISTIC_SPENDING)
        assert [p["sum"] for p in first[key][1]] == [p["sum"] for p in second[key][1]]
        assert second[key][1][-1]["sum"] == 17.5

    def test_only_window_days_are_written_but_sum_includes_history(self) -> None:
        """Older days still count toward the running total they anchor."""
        captured = self._import(self._totals(), date(2026, 9, 3))

        _meta, points = captured[statistic_id("hash-one", STATISTIC_SPENDING)]

        assert len(points) == 1
        # 10.0 + 5.0 from the days before the window, plus 2.5 on the day itself.
        assert points[0]["sum"] == 17.5

    def test_income_is_a_separate_series(self) -> None:
        captured = self._import(self._totals(), date(2026, 9, 1))

        _meta, points = captured[statistic_id("hash-one", STATISTIC_INCOME)]

        assert [p["sum"] for p in points] == [0.0, 100.0, 100.0]

    def test_metadata_satisfies_the_recorder_contract(self) -> None:
        """`source` must equal the statistic_id domain or the recorder raises."""
        captured = self._import(self._totals(), date(2026, 9, 1))

        metadata, _points = captured[statistic_id("hash-one", STATISTIC_SPENDING)]

        assert metadata["statistic_id"].startswith("enablebanking:")
        assert metadata["source"] == "enablebanking"
        assert metadata["has_sum"] is True
        assert metadata["unit_of_measurement"] == "EUR"
        assert "NL91ABNA0417164300" in str(metadata["name"])

    def test_name_carries_the_currency(self) -> None:
        """Multi-currency accounts share one IBAN across sub-accounts.

        Revolut and Wise expose the same IBAN for a EUR and a USD balance. With
        the currency left out, both series read identically in the statistics
        picker, which does not show the unit column — so the only way to tell
        them apart would be to import one and see which graph moved.
        """
        captured = self._import(self._totals(), date(2026, 9, 1))

        metadata, _points = captured[statistic_id("hash-one", STATISTIC_SPENDING)]

        assert metadata["name"] == "NL91ABNA0417164300 EUR spending"

    def test_statistic_id_keeps_the_iban_out_of_the_identifier(self) -> None:
        """The id is visible in Developer tools and in every screenshot of it."""
        assert "NL91ABNA" not in statistic_id("NL91ABNA0417164300", STATISTIC_SPENDING)

    def test_no_days_writes_nothing(self) -> None:
        assert self._import({}, date(2026, 9, 1)) == {}


def test_transaction_is_immutable() -> None:
    """A transaction is a historical fact; a restatement is a different object."""
    tx = transaction_from_raw(raw())

    assert isinstance(tx, Transaction)
    with pytest.raises(AttributeError):
        tx.amount = 1.0  # type: ignore[misc]

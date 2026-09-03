"""Tests for the payload-normalising helpers in `api.py`.

These functions exist because ASPSPs disagree about the shape of an account:
where the IBAN lives, whether ``accounts`` holds uid strings or full dicts,
what a human-readable name is called. That variation is the part of this
integration most likely to break on a bank nobody has tried yet, and it is
pure enough to test without Home Assistant running at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.enablebanking.api import (
    _account_display_name,
    _account_iban,
    _account_stable_id,
    _collect_accounts,
    _pick_preferred_balance,
)


class TestCollectAccounts:
    """`_collect_accounts` normalises the session payload to (uids, metadata)."""

    def test_uid_list_with_accounts_data(self, session_payload: dict[str, Any]) -> None:
        """The common shape: bare uids in `accounts`, metadata in `accounts_data`."""
        uids, metadata = _collect_accounts(session_payload)

        assert uids == ["uid-one", "uid-two"]
        assert metadata["uid-one"]["identification_hash"] == "hash-one"
        assert metadata["uid-two"]["name"] == "Sparkonto"

    def test_dicts_inline_in_accounts(self) -> None:
        """Some ASPSPs put the whole account object straight into `accounts`."""
        uids, metadata = _collect_accounts(
            {"accounts": [{"uid": "uid-one", "iban": "NL91ABNA0417164300"}]}
        )

        assert uids == ["uid-one"]
        assert metadata["uid-one"]["iban"] == "NL91ABNA0417164300"

    def test_accounts_data_keyed_by_uid(self) -> None:
        """`accounts_data` may be a dict keyed by uid rather than a list."""
        uids, metadata = _collect_accounts(
            {
                "accounts": ["uid-one"],
                "accounts_data": {"uid-one": {"name": "Current"}},
            }
        )

        assert uids == ["uid-one"]
        assert metadata["uid-one"]["name"] == "Current"

    def test_duplicates_removed_and_order_kept(self) -> None:
        """Order matters: it decides the order sensors are created in."""
        uids, _ = _collect_accounts({"accounts": ["b", "a", "b", "c"]})

        assert uids == ["b", "a", "c"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"accounts": []},
            {"accounts": None},
            {"accounts": [None, 42, ""]},
        ],
        ids=["empty", "no-accounts", "null-accounts", "junk-entries"],
    )
    def test_no_accounts_is_not_an_error(self, payload: dict[str, Any]) -> None:
        """A session with nothing usable yields no uids rather than raising."""
        uids, metadata = _collect_accounts(payload)

        assert uids == []
        assert metadata == {}


class TestAccountIban:
    """`_account_iban` walks the containers different ASPSPs nest the IBAN in."""

    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            ({"iban": "NL91ABNA0417164300"}, "NL91ABNA0417164300"),
            ({"IBAN": "NL91ABNA0417164300"}, "NL91ABNA0417164300"),
            ({"account_id": {"iban": "DE89370400440532013000"}}, "DE89370400440532013000"),
            (
                {"identification": {"iban": "ES9121000418450200051332"}},
                "ES9121000418450200051332",
            ),
            ({"details": {"IBAN": "FR1420041010050500013M02606"}}, "FR1420041010050500013M02606"),
            (
                {"identifications": [{"iban": "IT60X0542811101000000123456"}]},
                "IT60X0542811101000000123456",
            ),
        ],
        ids=[
            "top-level",
            "uppercase",
            "account_id",
            "identification",
            "details",
            "list-container",
        ],
    )
    def test_extracts_from_each_known_shape(self, meta: dict[str, Any], expected: str) -> None:
        assert _account_iban(meta) == expected

    @pytest.mark.parametrize(
        "meta",
        [{}, {"iban": ""}, {"iban": None}, {"account_id": {}}, {"account_id": "not-a-dict"}],
        ids=["empty", "blank", "null", "empty-container", "wrong-type"],
    )
    def test_missing_iban_is_empty_string(self, meta: dict[str, Any]) -> None:
        """IBAN-less accounts are legitimate (some cards, some wallets)."""
        assert _account_iban(meta) == ""

    def test_top_level_wins_over_nested(self) -> None:
        assert _account_iban({"iban": "TOP", "account_id": {"iban": "NESTED"}}) == "TOP"


class TestAccountDisplayName:
    """`_account_display_name` picks the first populated name-ish field."""

    def test_name_preferred_over_later_candidates(self) -> None:
        meta = {"name": "Betaalrekening", "product": "Current", "ownerName": "J de Vries"}

        assert _account_display_name(meta) == "Betaalrekening"

    def test_falls_through_to_product(self) -> None:
        assert _account_display_name({"product": "Savings"}) == "Savings"

    def test_falls_through_to_cash_account_type(self) -> None:
        assert _account_display_name({"cash_account_type": "CACC"}) == "CACC"

    @pytest.mark.parametrize(
        "meta",
        [{}, {"name": ""}, {"name": None}, {"unrelated": "x"}],
        ids=["empty", "blank", "null", "no-match"],
    )
    def test_no_name_is_empty_string(self, meta: dict[str, Any]) -> None:
        assert _account_display_name(meta) == ""


class TestAccountStableId:
    """The stable id is what keeps entity history across a reauth."""

    def test_prefers_identification_hash(self) -> None:
        """`uid` is regenerated per session; `identification_hash` is not."""
        assert _account_stable_id({"identification_hash": "hash-one"}, "uid-one") == "hash-one"

    @pytest.mark.parametrize(
        "meta", [{}, {"identification_hash": ""}, {"identification_hash": None}]
    )
    def test_falls_back_to_uid(self, meta: dict[str, Any]) -> None:
        assert _account_stable_id(meta, "uid-one") == "uid-one"


class TestPickPreferredBalance:
    """Which of several balance objects becomes the sensor's state."""

    def test_closing_booked_beats_interim_available(self) -> None:
        """CLBD is the settled figure and heads the preference list."""
        picked = _pick_preferred_balance(
            [
                {"balance_type": "ITAV", "balance_amount": {"amount": "2.00"}},
                {"balance_type": "CLBD", "balance_amount": {"amount": "1.00"}},
            ]
        )

        assert picked is not None
        assert picked["balance_type"] == "CLBD"

    def test_walks_down_the_preference_list(self) -> None:
        """With no CLBD present the next preferred type wins, not the first item."""
        picked = _pick_preferred_balance(
            [
                {"balance_type": "OPBD", "balance_amount": {"amount": "3.00"}},
                {"balance_type": "ITAV", "balance_amount": {"amount": "2.00"}},
            ]
        )

        assert picked is not None
        assert picked["balance_type"] == "ITAV"

    def test_first_duplicate_of_a_type_is_kept(self) -> None:
        picked = _pick_preferred_balance(
            [
                {"balance_type": "CLBD", "balance_amount": {"amount": "1.00"}},
                {"balance_type": "CLBD", "balance_amount": {"amount": "9.99"}},
            ]
        )

        assert picked is not None
        assert picked["balance_amount"]["amount"] == "1.00"

    def test_unknown_type_still_yields_a_balance(self) -> None:
        """An unrecognised code is better than no sensor at all."""
        picked = _pick_preferred_balance([{"balance_type": "WEIRD", "balance_amount": {}}])

        assert picked is not None
        assert picked["balance_type"] == "WEIRD"

    @pytest.mark.parametrize(
        "balances", [[], [None], ["nonsense"]], ids=["empty", "null", "wrong-type"]
    )
    def test_nothing_usable_returns_none(self, balances: list[Any]) -> None:
        assert _pick_preferred_balance(balances) is None

"""Fetching transactions: pagination, and not taking balances down with it."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enablebanking.api import EnableBankingClient
from custom_components.enablebanking.const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_FETCH_TRANSACTIONS,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_PERSONAL,
)
from custom_components.enablebanking.errors import (
    EnableBankingAPIError,
    EnableBankingRateLimitError,
)
from custom_components.enablebanking.models import AccountBalance

WINDOW_START = date(2026, 6, 1)
IBAN = "NL91ABNA0417164300"


def _client(pages: list[dict[str, Any]]) -> EnableBankingClient:
    client = EnableBankingClient(MagicMock(), "a.b.c", "session-id")
    client._request = AsyncMock(side_effect=pages)  # type: ignore[method-assign]
    return client


class TestPagination:
    """`continuation_key` is how Enable Banking pages."""

    async def test_follows_the_continuation_key(self) -> None:
        client = _client(
            [
                {"transactions": [{"entry_reference": "a"}], "continuation_key": "k1"},
                {"transactions": [{"entry_reference": "b"}], "continuation_key": "k2"},
                {"transactions": [{"entry_reference": "c"}]},
            ]
        )

        result = await client.async_get_transactions("uid", WINDOW_START)

        assert [t["entry_reference"] for t in result] == ["a", "b", "c"]
        assert client._request.await_count == 3  # type: ignore[attr-defined]

    async def test_passes_the_key_back_on_the_next_request(self) -> None:
        client = _client(
            [
                {"transactions": [], "continuation_key": "k1"},
                {"transactions": []},
            ]
        )

        await client.async_get_transactions("uid", WINDOW_START)

        second_call = client._request.await_args_list[1]  # type: ignore[attr-defined]
        assert second_call.kwargs["params"]["continuation_key"] == "k1"
        assert second_call.kwargs["params"]["date_from"] == WINDOW_START.isoformat()

    async def test_single_page_makes_one_request(self) -> None:
        client = _client([{"transactions": [{"entry_reference": "a"}]}])

        result = await client.async_get_transactions("uid", WINDOW_START)

        assert len(result) == 1
        assert client._request.await_count == 1  # type: ignore[attr-defined]

    async def test_stops_at_the_page_cap(self) -> None:
        """A key that never clears must not spin inside one poll.

        It would hold the coordinator's update lock and keep spending
        rate-limit budget until the account is locked out for the day.
        """
        endless = [
            {"transactions": [{"entry_reference": str(i)}], "continuation_key": "k"}
            for i in range(50)
        ]
        client = _client(endless)

        result = await client.async_get_transactions("uid", WINDOW_START, max_pages=5)

        assert len(result) == 5
        assert client._request.await_count == 5  # type: ignore[attr-defined]

    async def test_non_dict_entries_are_ignored(self) -> None:
        client = _client([{"transactions": [{"entry_reference": "a"}, "junk", None]}])

        result = await client.async_get_transactions("uid", WINDOW_START)

        assert len(result) == 1

    async def test_rate_limit_propagates(self) -> None:
        """The coordinator needs to see this to apply its per-account back-off."""
        client = EnableBankingClient(MagicMock(), "a.b.c", "session-id")
        client._request = AsyncMock(side_effect=EnableBankingRateLimitError("429"))  # type: ignore[method-assign]

        with pytest.raises(EnableBankingRateLimitError):
            await client.async_get_transactions("uid", WINDOW_START)


@pytest.fixture
def account() -> AccountBalance:
    return AccountBalance(
        account_id="uid-one",
        stable_id="hash-one",
        iban=IBAN,
        name="Betaalrekening",
        product="Current",
        currency="EUR",
        balance=1234.56,
        balance_type="CLBD",
        reference_date="2026-09-01",
        last_polled_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        title="ASN Bank",
        unique_id="abc123",
        data={
            CONF_JWT: "a.b.c",
            CONF_PRIVATE_KEY: "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
            CONF_APP_ID: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            CONF_SESSION_ID: "11111111-2222-3333-4444-555555555555",
            CONF_ASPSP_NAME: "ASN Bank",
            CONF_ASPSP_COUNTRY: "NL",
            CONF_PSU_TYPE: PSU_PERSONAL,
        },
        options={CONF_FETCH_TRANSACTIONS: True},
    )
    mock_entry.add_to_hass(hass)
    return mock_entry


class TestCoordinatorResilience:
    """Transactions are an extra; balances are the product."""

    async def test_transaction_failure_leaves_balances_intact(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        account: AccountBalance,
        enable_custom_integrations: None,
    ) -> None:
        """A bank that 500s on /transactions must not cost the balance sensor."""
        client = MagicMock()
        client.async_get_all_balances = AsyncMock(return_value=({"hash-one": account}, set()))
        client.async_get_transactions = AsyncMock(
            side_effect=EnableBankingAPIError("upstream is having a day")
        )

        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert coordinator.last_error == ""
        assert coordinator.data is not None
        assert coordinator.data.accounts["hash-one"].balance == 1234.56
        assert coordinator.data.new_transactions == {}

    async def test_first_poll_seeds_without_firing_events(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        account: AccountBalance,
        enable_custom_integrations: None,
    ) -> None:
        """Switching the option on must not replay months into every automation."""
        client = MagicMock()
        client.async_get_all_balances = AsyncMock(return_value=({"hash-one": account}, set()))
        client.async_get_transactions = AsyncMock(
            return_value=[
                {
                    "entry_reference": f"old-{i}",
                    "transaction_amount": {"currency": "EUR", "amount": "5.00"},
                    "credit_debit_indicator": "DBIT",
                    "status": "BOOK",
                    "booking_date": "2026-08-15",
                }
                for i in range(40)
            ]
        )

        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data

            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert coordinator.data is not None
            assert coordinator.data.new_transactions == {}

            # A genuinely new entry on the next poll does fire.
            client.async_get_transactions.return_value = [
                {
                    "entry_reference": "brand-new",
                    "transaction_amount": {"currency": "EUR", "amount": "9.99"},
                    "credit_debit_indicator": "DBIT",
                    "status": "BOOK",
                    "booking_date": "2026-09-02",
                }
            ]
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        fresh = coordinator.data.new_transactions["hash-one"]
        assert [t.key for t in fresh] == ["brand-new"]

    async def test_disabled_option_fetches_nothing(
        self,
        hass: HomeAssistant,
        account: AccountBalance,
        enable_custom_integrations: None,
    ) -> None:
        """Nobody spends PSD2 quota on a feature they did not switch on."""
        off_entry = MockConfigEntry(
            domain=DOMAIN,
            title="ASN Bank",
            unique_id="off",
            data={
                CONF_JWT: "a.b.c",
                CONF_SESSION_ID: "sid",
                CONF_ASPSP_NAME: "ASN Bank",
                CONF_ASPSP_COUNTRY: "NL",
                CONF_PSU_TYPE: PSU_PERSONAL,
            },
        )
        off_entry.add_to_hass(hass)

        client = MagicMock()
        client.async_get_all_balances = AsyncMock(return_value=({"hash-one": account}, set()))
        client.async_get_transactions = AsyncMock(return_value=[])

        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(off_entry.entry_id)
            await hass.async_block_till_done()
            await off_entry.runtime_data.async_refresh()
            await hass.async_block_till_done()

        client.async_get_transactions.assert_not_awaited()


class TestGetTransactionsService:
    """The history that predates setup is only reachable through this."""

    @staticmethod
    def _raw(ref: str, day: str, amount: str = "5.00", indicator: str = "DBIT") -> dict[str, Any]:
        return {
            "entry_reference": ref,
            "transaction_amount": {"currency": "EUR", "amount": amount},
            "credit_debit_indicator": indicator,
            "status": "BOOK",
            "booking_date": day,
            "creditor": {"name": "Albert Heijn"},
        }

    async def _setup(
        self, hass: HomeAssistant, entry: MockConfigEntry, account: AccountBalance, raw: list[Any]
    ) -> MagicMock:
        client = MagicMock()
        client.async_get_all_balances = AsyncMock(return_value=({"hash-one": account}, set()))
        client.async_get_transactions = AsyncMock(return_value=raw)
        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
        return client

    async def test_returns_the_stored_window_newest_first(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        account: AccountBalance,
        enable_custom_integrations: None,
    ) -> None:
        """The seeding poll fires no events, so this is how that history is read."""
        today = dt_util.now().date()
        await self._setup(
            hass,
            entry,
            account,
            [
                self._raw("a", (today - timedelta(days=40)).isoformat()),
                self._raw("b", (today - timedelta(days=2)).isoformat()),
                self._raw("c", (today - timedelta(days=20)).isoformat()),
            ],
        )

        result = await hass.services.async_call(
            DOMAIN, "get_transactions", blocking=True, return_response=True
        )

        assert result is not None
        assert result["count"] == 3
        days = [row["booking_date"] for row in result["transactions"]]
        assert days == sorted(days, reverse=True)
        assert result["transactions"][0]["counterparty"] == "Albert Heijn"
        assert result["transactions"][0]["iban"] == IBAN

    async def test_days_filter_trims_the_window(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        account: AccountBalance,
        enable_custom_integrations: None,
    ) -> None:
        today = dt_util.now().date()
        await self._setup(
            hass,
            entry,
            account,
            [
                self._raw("old", (today - timedelta(days=60)).isoformat()),
                self._raw("recent", (today - timedelta(days=3)).isoformat()),
            ],
        )

        result = await hass.services.async_call(
            DOMAIN, "get_transactions", {"days": 7}, blocking=True, return_response=True
        )

        assert result is not None
        assert result["count"] == 1

    async def test_survives_a_restart(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        account: AccountBalance,
        enable_custom_integrations: None,
    ) -> None:
        """Reloading must not cost a poll to rebuild the list."""
        today = dt_util.now().date()
        await self._setup(
            hass, entry, account, [self._raw("a", (today - timedelta(days=1)).isoformat())]
        )

        client = MagicMock()
        client.async_get_all_balances = AsyncMock(return_value=({"hash-one": account}, set()))
        client.async_get_transactions = AsyncMock(return_value=[])
        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()

            result = await hass.services.async_call(
                DOMAIN, "get_transactions", blocking=True, return_response=True
            )

        assert result is not None
        assert result["count"] == 1
        client.async_get_transactions.assert_not_awaited()

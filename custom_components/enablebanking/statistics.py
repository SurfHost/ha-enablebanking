"""Long-term statistics for spending and income.

Balances are a state, so a sensor records them fine. Spending is not: it is a
stream of dated amounts that arrives in batches, gets restated by the bank, and
is interesting mostly as history. Home Assistant has a purpose-built home for
that — external statistics — which the Statistics card renders with day, week
and month bucketing, and which the recorder never purges. States are purged on
``purge_keep_days``; a year-old grocery total would otherwise simply be gone.

The shape here follows `homeassistant/components/opower/coordinator.py`, which
solves the same problem for utility bills.

Idempotency is the whole design problem. A poll re-reads a window that overlaps
what was already imported, and a bank may restate a day inside that window, so
the import has to be safe to repeat with different numbers. The approach: keep
our own per-day totals in the config entry's cache, recompute the cumulative
series from those, and rewrite only the buckets inside the current window.
``async_add_external_statistics`` overwrites a bucket that shares a start time,
so a repeated import corrects rather than doubles.

Deliberately no recorder reads. Deriving the running total by querying back the
last imported sum works until there is a gap in the series, at which point the
baseline silently reads as zero and the entire history restates downward. Our
own totals cannot develop that gap.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import date, timedelta

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STATISTIC_INCOME, STATISTIC_SPENDING
from .models import AccountBalance, Transaction

_LOGGER = logging.getLogger(__name__)

#: Per-day totals retained per account. Bounds the cache file while still
#: covering far more than the window any poll asks for, so the cumulative sum
#: is computed from a complete run of days rather than a truncated one.
MAX_RETAINED_DAYS: int = 800


def statistic_id(stable_id: str, suffix: str) -> str:
    """Build the external statistic id for one account and series.

    Hashed rather than readable: ``stable_id`` is Enable Banking's
    ``identification_hash``, which is derived from the IBAN and can contain
    ``/``, ``+`` and ``=`` — none of which are legal in a statistic id, whose
    object part follows entity-id rules. Hashing also keeps the IBAN out of a
    string that is visible in Developer tools and in every shared screenshot of
    the statistics list. The human-readable form lives in ``name`` instead.
    """
    token = hashlib.sha256(stable_id.encode()).hexdigest()[:16]
    return f"{DOMAIN}:{token}_{suffix}"


def daily_totals(transactions: Iterable[Transaction]) -> dict[str, dict[str, float]]:
    """Total booked spending and income per calendar day.

    Pending entries are excluded. They mutate and disappear, and a cumulative
    sum that counted one cannot be corrected without deleting the statistic —
    a booked entry that was previously pending is picked up on the poll that
    books it, keyed identically, so nothing is lost by waiting.
    """
    totals: dict[str, dict[str, float]] = {}
    for transaction in transactions:
        if not transaction.booked:
            continue
        day = transaction.booking_date or transaction.value_date
        if not day:
            continue
        try:
            day = date.fromisoformat(day[:10]).isoformat()
        except ValueError:
            continue
        bucket = totals.setdefault(day, {STATISTIC_SPENDING: 0.0, STATISTIC_INCOME: 0.0})
        if transaction.is_debit:
            bucket[STATISTIC_SPENDING] += transaction.amount
        else:
            bucket[STATISTIC_INCOME] += transaction.amount
    return totals


def merge_daily_totals(
    stored: dict[str, dict[str, float]],
    fresh: dict[str, dict[str, float]],
    window_start: date,
) -> dict[str, dict[str, float]]:
    """Fold a poll's totals into the stored history.

    Days inside the fetched window are *replaced*, not added to: the fetch is
    authoritative for that range, so a restated or reversed transaction lands
    correctly instead of accumulating. Days outside it are left alone, because
    this poll says nothing about them.

    A day inside the window with no transactions is intentionally dropped —
    that is the bank telling us the day is empty, which is different from the
    day being unknown.
    """
    merged: dict[str, dict[str, float]] = {}
    for day, values in stored.items():
        parsed = _parse_day(day)
        if parsed is None or parsed < window_start:
            merged[day] = values
    merged.update(fresh)

    if len(merged) > MAX_RETAINED_DAYS:
        for day in sorted(merged)[: len(merged) - MAX_RETAINED_DAYS]:
            del merged[day]
    return merged


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def async_import_statistics(
    hass: HomeAssistant,
    account: AccountBalance,
    totals: dict[str, dict[str, float]],
    window_start: date,
) -> None:
    """Write the spending and income series for one account.

    The cumulative sum is computed across *every* retained day so the running
    total is right, but only buckets from ``window_start`` onward are written,
    since those are the only ones this poll could have changed.
    """
    days = sorted(day for day in totals if _parse_day(day) is not None)
    if not days:
        return

    for suffix, label in ((STATISTIC_SPENDING, "spending"), (STATISTIC_INCOME, "income")):
        running = 0.0
        points: list[StatisticData] = []
        for day in days:
            value = totals[day].get(suffix, 0.0)
            running += value
            parsed = _parse_day(day)
            if parsed is None or parsed < window_start:
                continue
            # Local midnight is hour-aligned, which is what an hourly
            # statistic bucket requires; the daily rollup the UI shows is
            # derived from these.
            points.append(
                StatisticData(
                    start=dt_util.start_of_local_day(parsed),
                    state=value,
                    sum=running,
                )
            )

        if not points:
            continue

        # Currency is part of the name, not decoration: a multi-currency
        # account (Revolut, Wise) exposes one IBAN across several sub-accounts,
        # so without it two series read identically in the statistics picker
        # and differ only by a unit column that the picker does not show.
        identifier = account.iban or account.name or account.stable_id[:8]
        name_parts = [part for part in (identifier, account.currency, label) if part]
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=" ".join(name_parts),
                source=DOMAIN,
                statistic_id=statistic_id(account.stable_id, suffix),
                unit_class=None,
                unit_of_measurement=account.currency,
            ),
            points,
        )

    _LOGGER.debug(
        "Imported statistics for %s: %d day(s) from %s",
        account.stable_id[:8],
        len(days),
        window_start.isoformat(),
    )


def window_start_for(history_days: int) -> date:
    """First day a poll asks the bank about."""
    # Bound to a local name: homeassistant-stubs types dt_util.utcnow as
    # Incomplete, so returning the expression directly is an implicit Any.
    start: date = (dt_util.utcnow() - timedelta(days=history_days)).date()
    return start

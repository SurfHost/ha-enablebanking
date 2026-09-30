"""Event platform: one transaction feed per account.

A transaction is not a state, so it does not belong in a sensor. Home
Assistant's `event` entity is the domain built for "this happened", and it is
what Home Assistant core's own bank integration uses — see
`homeassistant/components/monzo/event.py`.

The difference is the trigger. Monzo receives webhooks and fires one event per
push; Enable Banking is poll-only under the PSD2 four-polls-a-day cap, so
events arrive in batches whenever a poll discovers entries it has not seen
before. The automation interface is identical, the timing is coarser, and the
README says so rather than leaving people to wonder why their notification is
three hours late.
"""

from __future__ import annotations

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPE_TRANSACTION
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntity

PARALLEL_UPDATES = 0

#: One feed per account. `key` also becomes the unique_id suffix via
#: `account_unique_id`, so it is as permanent as the entity's history.
TRANSACTION_EVENT = EventEntityDescription(
    key="transaction",
    translation_key="transaction",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one transaction event entity per account.

    Nothing is created when transactions are switched off, so the opt-in does
    not leave a permanently blank entity behind for people who only wanted
    balances.
    """
    coordinator = entry.runtime_data
    if not coordinator.transactions_enabled:
        return

    known: set[str] = set()

    @callback
    def _async_add_for_new_accounts() -> None:
        seen_ids: set[str] = set(coordinator.cached_stable_ids())
        if coordinator.data is not None:
            seen_ids.update(coordinator.data.accounts)

        new = [stable_id for stable_id in sorted(seen_ids) if stable_id not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(
            EnableBankingTransactionEvent(coordinator, stable_id) for stable_id in new
        )

    _async_add_for_new_accounts()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_for_new_accounts))


class EnableBankingTransactionEvent(EnableBankingEntity, EventEntity):
    """Fires once per newly-booked transaction on one account."""

    def __init__(self, coordinator: EnableBankingCoordinator, stable_id: str) -> None:
        """Bind to one account's transaction feed.

        `event_types` is set per instance rather than on the class: as a class
        attribute it is a shared mutable, which this repo's ruff config
        rightly rejects, and overriding the base class's instance attribute
        with a ClassVar would not type-check.
        """
        super().__init__(coordinator, TRANSACTION_EVENT, stable_id)
        self._attr_event_types = [EVENT_TYPE_TRANSACTION]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire an event per transaction this poll turned up.

        Each is written out separately rather than collapsed into one: an
        event entity carries a single payload, so batching would silently drop
        every transaction but the last, and a state write per event is what
        lets an automation act on each of them.
        """
        data = self.coordinator.data
        if data is not None:
            for transaction in data.new_transactions.get(self._stable_id, []):
                self._trigger_event(EVENT_TYPE_TRANSACTION, transaction.as_event_payload())
                self.async_write_ha_state()

        super()._handle_coordinator_update()

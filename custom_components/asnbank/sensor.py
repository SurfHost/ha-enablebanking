"""Sensor platform for the ASN Bank Balance integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import AsnBankConfigEntry, AsnBankCoordinator
from .entity import AsnBankEntity
from .models import AccountBalance, AsnBankData


@dataclass(frozen=True, kw_only=True)
class AsnBankSensorDescription(SensorEntityDescription):
    """Describes an ASN Bank Balance sensor entity."""

    value_fn: Callable[[AccountBalance], StateType] = lambda _: None
    extra_attrs_fn: Callable[[AccountBalance], dict[str, Any]] | None = None


BALANCE_SENSOR = AsnBankSensorDescription(
    key="balance",
    translation_key="balance",
    native_unit_of_measurement=CURRENCY_EURO,
    device_class=SensorDeviceClass.MONETARY,
    state_class=SensorStateClass.TOTAL,
    suggested_display_precision=2,
    icon="mdi:bank",
    value_fn=lambda acc: round(acc.balance, 2),
    extra_attrs_fn=lambda acc: {
        "iban": acc.iban,
        "account_name": acc.name,
        "product": acc.product,
        "currency": acc.currency,
        "balance_type": acc.balance_type,
        "reference_date": acc.reference_date,
    },
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AsnBankConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ASN Bank balance sensors.

    One balance entity is created per account the Enable Banking session
    exposes. If the session grows to include more accounts later (e.g. a
    second ASN account at the same bank), new entities will be added on the
    next reload.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_for_new_accounts() -> None:
        if coordinator.data is None:
            return
        new_entities: list[AsnBankBalanceSensor] = []
        for account_id in coordinator.data.accounts:
            if account_id in known:
                continue
            known.add(account_id)
            new_entities.append(
                AsnBankBalanceSensor(coordinator, BALANCE_SENSOR, account_id)
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_for_new_accounts()
    entry.async_on_unload(
        coordinator.async_add_listener(_async_add_for_new_accounts)
    )


class AsnBankBalanceSensor(AsnBankEntity, SensorEntity):
    """ASN Bank balance sensor entity."""

    entity_description: AsnBankSensorDescription

    @property
    def name(self) -> str | None:
        """Return a display name including the IBAN so multiple accounts can be told apart."""
        account = self._current_account
        if account is None:
            return "Balance"
        if account.iban:
            return f"Balance {account.iban}"
        return "Balance"

    @property
    def _current_account(self) -> AccountBalance | None:
        data: AsnBankData | None = self.coordinator.data
        if data is None:
            return None
        return data.accounts.get(self._account_id)

    @property
    def available(self) -> bool:
        """Return True only when the coordinator has data for this account."""
        return super().available and self._current_account is not None

    @property
    def native_value(self) -> StateType:
        """Return the account balance."""
        account = self._current_account
        if account is None:
            return None
        return self.entity_description.value_fn(account)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes (IBAN, account name, etc.)."""
        account = self._current_account
        if account is None or self.entity_description.extra_attrs_fn is None:
            return None
        attrs = self.entity_description.extra_attrs_fn(account)
        attrs["last_updated"] = self.coordinator.last_update_success_time
        return attrs

"""Sensor platform for the Enable Banking integration."""

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
from homeassistant.util.dt import utcnow

from .const import CONF_ASPSP_NAME
from .coordinator import EnableBankingConfigEntry
from .entity import EnableBankingEntity
from .models import AccountBalance, EnableBankingData


@dataclass(frozen=True, kw_only=True)
class EnableBankingSensorDescription(SensorEntityDescription):
    """Describes an Enable Banking sensor entity."""

    value_fn: Callable[[AccountBalance], StateType] = lambda _: None
    account_attrs_fn: Callable[[AccountBalance], dict[str, Any]] | None = None


BALANCE_SENSOR = EnableBankingSensorDescription(
    key="balance",
    translation_key="balance",
    native_unit_of_measurement=CURRENCY_EURO,
    device_class=SensorDeviceClass.MONETARY,
    state_class=SensorStateClass.TOTAL,
    suggested_display_precision=2,
    icon="mdi:bank",
    value_fn=lambda acc: round(acc.balance, 2),
    account_attrs_fn=lambda acc: {
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
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enable Banking balance sensors.

    One balance entity is created per account the session exposes. New
    accounts discovered on a later poll are added without a reload.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_for_new_accounts() -> None:
        if coordinator.data is None:
            return
        new_entities: list[EnableBankingBalanceSensor] = []
        for account_id in coordinator.data.accounts:
            if account_id in known:
                continue
            known.add(account_id)
            new_entities.append(
                EnableBankingBalanceSensor(coordinator, BALANCE_SENSOR, account_id)
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_for_new_accounts()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_for_new_accounts))


class EnableBankingBalanceSensor(EnableBankingEntity, SensorEntity):
    """Balance sensor for one Enable Banking account."""

    entity_description: EnableBankingSensorDescription

    @property
    def name(self) -> str | None:
        account = self._current_account
        if account is not None and account.iban:
            return f"Balance {account.iban}"
        return "Balance"

    @property
    def _current_account(self) -> AccountBalance | None:
        data: EnableBankingData | None = self.coordinator.data
        if data is None:
            return None
        return data.accounts.get(self._account_id)

    @property
    def available(self) -> bool:
        return super().available and self._current_account is not None

    @property
    def native_value(self) -> StateType:
        account = self._current_account
        if account is None:
            return None
        return self.entity_description.value_fn(account)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        account = self._current_account
        if account is None or self.entity_description.account_attrs_fn is None:
            return None

        attrs = self.entity_description.account_attrs_fn(account)
        attrs["last_updated"] = self.coordinator.last_update_success_time
        attrs["aspsp"] = self.coordinator.config_entry.data.get(CONF_ASPSP_NAME)

        data = self.coordinator.data
        if data is not None and data.consent_expires_at is not None:
            attrs["consent_expires_at"] = data.consent_expires_at.isoformat()
            attrs["consent_days_remaining"] = max(
                0, (data.consent_expires_at - utcnow()).days
            )

        return attrs

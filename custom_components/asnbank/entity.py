"""Base entity for the ASN Bank Balance integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AsnBankCoordinator


class AsnBankEntity(CoordinatorEntity[AsnBankCoordinator]):
    """Base entity for ASN Bank Balance sensors."""

    _attr_has_entity_name = True
    _attr_attribution = "Data via Enable Banking AIS (PSD2)"

    def __init__(
        self,
        coordinator: AsnBankCoordinator,
        description: EntityDescription,
        account_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{account_id}_{description.key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_{account_id}")},
            name=f"ASN Bank {account_id[:8]}",
            manufacturer="ASN Bank",
            model="Betaalrekening",
            entry_type=DeviceEntryType.SERVICE,
        )

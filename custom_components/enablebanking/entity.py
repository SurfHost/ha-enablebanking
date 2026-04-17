"""Base entity for the Enable Banking integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ASPSP_COUNTRY, CONF_ASPSP_NAME, CONF_PSU_TYPE, DOMAIN
from .coordinator import EnableBankingCoordinator


class EnableBankingEntity(CoordinatorEntity[EnableBankingCoordinator]):
    """Base entity for Enable Banking sensors."""

    _attr_has_entity_name = True
    _attr_attribution = "Data via Enable Banking AIS (PSD2)"

    def __init__(
        self,
        coordinator: EnableBankingCoordinator,
        description: EntityDescription,
        account_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{account_id}_{description.key}"
        )

        entry = coordinator.config_entry
        aspsp_name = entry.data.get(CONF_ASPSP_NAME, "Enable Banking")
        country = entry.data.get(CONF_ASPSP_COUNTRY, "")
        psu_type = entry.data.get(CONF_PSU_TYPE, "")
        model_parts = [p for p in (country, psu_type) if p]

        # Put the bank in `manufacturer` so the service-info card reads
        # "<country · psu_type> / door <Bank>" — that's the info users
        # actually care about on a balance card. The "data via Enable
        # Banking" provenance stays visible through the attribution on
        # each entity and the integration card title.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=aspsp_name,
            manufacturer=aspsp_name,
            model=" · ".join(model_parts) if model_parts else "Account",
            entry_type=DeviceEntryType.SERVICE,
        )

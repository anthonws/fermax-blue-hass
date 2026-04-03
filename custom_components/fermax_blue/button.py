"""Button platform for Fermax Blue."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FermaxBlueCoordinator
from .entity import FermaxBlueEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Fermax Blue buttons."""
    coordinators: list[FermaxBlueCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []

    for coordinator in coordinators:
        for door_name, door in coordinator.pairing.access_doors.items():
            if door.visible:
                entities.append(
                    FermaxOpenDoorButton(coordinator, door_name, door.title)
                )

    async_add_entities(entities)


class FermaxOpenDoorButton(FermaxBlueEntity, ButtonEntity):
    """Button to open a door."""

    _attr_translation_key = "open_door"

    def __init__(
        self,
        coordinator: FermaxBlueCoordinator,
        door_name: str,
        door_title: str,
    ) -> None:
        super().__init__(coordinator)
        self._door_name = door_name
        self._attr_unique_id = f"{self._device_id}_{door_name}_open"
        self._attr_name = f"Open {door_title or door_name}"

    async def async_press(self) -> None:
        """Open the door."""
        success = await self.coordinator.open_door(self._door_name)
        if success:
            _LOGGER.info("Door %s opened via button", self._door_name)
        else:
            _LOGGER.error("Failed to open door %s via button", self._door_name)

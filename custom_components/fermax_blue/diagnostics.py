"""Diagnostics support for Fermax Blue."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import FermaxBlueCoordinator

REDACT_KEYS = {
    "password",
    "username",
    "access_token",
    "fcm_token",
    "token",
    "fermax_auth_basic",
    "firebase_api_key",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinators: list[FermaxBlueCoordinator] = hass.data[DOMAIN][entry.entry_id]

    devices = []
    for coordinator in coordinators:
        device_data: dict[str, Any] = {
            "device_id": coordinator.pairing.device_id,
            "tag": coordinator.pairing.tag,
            "coordinator_data": coordinator.data,
        }
        if coordinator.device_info:
            device_data["device_info"] = {
                "connection_state": coordinator.device_info.connection_state,
                "status": coordinator.device_info.status,
                "family": coordinator.device_info.family,
                "device_type": coordinator.device_info.device_type,
                "subtype": coordinator.device_info.subtype,
                "wireless_signal": coordinator.device_info.wireless_signal,
                "photocaller": coordinator.device_info.photocaller,
            }
        devices.append(device_data)

    return async_redact_data(
        {
            "config_entry": async_redact_data(dict(entry.data), REDACT_KEYS),
            "devices": devices,
        },
        REDACT_KEYS,
    )

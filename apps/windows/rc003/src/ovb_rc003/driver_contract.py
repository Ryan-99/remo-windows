"""Decode the RC003 device filter through device-scoped Raw Input.

Receiving a reserved VK is not proof that the driver is installed or healthy.
Other keyboards can generate F13-F20. The selected device path remains required.
Native direction/OK keys are still passthrough, not exclusive custom mappings.
"""

from . import hid_identity

# Windows VK values, NOT the HID usages 0x68..0x6F in remap.c.
FILTER_VK_TO_BUTTON = {
    0x7C: "volume_up",
    0x7D: "volume_down",
    0x7E: "back",
    0x7F: "home",
    0x80: "menu",
    0x81: "tv",
    0x82: "power",
    0x83: "mic",
}

FILTER_BUTTONS = frozenset(FILTER_VK_TO_BUTTON.values())


def is_filter_event(vkey: int, device_path: str | None, selected_path: str | None) -> bool:
    return filtered_button(vkey, device_path, selected_path) is not None


def filtered_button(vkey: int, device_path: str | None, selected_path: str | None) -> str | None:
    if not device_path or not selected_path:
        return None
    if not hid_identity.device_path_matches_rc003(selected_path):
        return None
    if hid_identity.normalize_device_path(device_path) != hid_identity.normalize_device_path(selected_path):
        return None
    return FILTER_VK_TO_BUTTON.get(vkey)

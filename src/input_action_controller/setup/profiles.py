from __future__ import annotations

from collections.abc import Iterable

from ..devices.discovery import DeviceCandidate
from ..models import DeviceProfile, EvdevProfile, HidrawProfile
from .devices import SelectorDraft


def compatible_profiles(
    profiles: Iterable[DeviceProfile],
    candidate: DeviceCandidate,
) -> tuple[DeviceProfile, ...]:
    """Return profiles whose persisted identity accepts the selected node."""
    return tuple(profile for profile in profiles if _matches(profile, candidate))


def selector_draft_from_profile(profile: DeviceProfile) -> SelectorDraft:
    """Return the persisted identity used by an existing profile."""
    return SelectorDraft(
        transport="evdev" if isinstance(profile, EvdevProfile) else "hidraw",
        vendor_id=profile.vendor_id,
        product_id=profile.product_id,
        interface_number=profile.interface_number,
        serial=profile.serial,
        id_path=profile.id_path,
        classifier=(
            (profile.input_classifier, "1")
            if isinstance(profile, EvdevProfile)
            and profile.input_classifier is not None
            else None
        ),
    )


def _matches(profile: DeviceProfile, candidate: DeviceCandidate) -> bool:
    expected_subsystem = "hidraw" if isinstance(profile, HidrawProfile) else "input"
    if candidate.subsystem != expected_subsystem:
        return False
    properties = candidate.properties
    if properties.get("ID_VENDOR_ID", "").lower() != profile.vendor_id:
        return False
    if properties.get("ID_MODEL_ID", "").lower() != profile.product_id:
        return False
    interface = properties.get("ID_USB_INTERFACE_NUM")
    if profile.interface_number is not None and (
        interface is None or interface.lower().zfill(2) != profile.interface_number
    ):
        return False
    if (
        profile.serial is not None
        and properties.get("ID_SERIAL_SHORT") != profile.serial
    ):
        return False
    if profile.id_path is not None and properties.get("ID_PATH") != profile.id_path:
        return False
    if isinstance(profile, EvdevProfile) and profile.input_classifier is not None:
        if properties.get(profile.input_classifier) != "1":
            return False
    return True

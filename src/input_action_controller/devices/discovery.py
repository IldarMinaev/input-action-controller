from collections import Counter
from dataclasses import dataclass, replace
import os
from typing import Callable, Iterable, Mapping, Protocol

from evdev import InputDevice, ecodes
import pyudev

from ..models import DeviceProfile, EvdevProfile, HidrawProfile
from .classifiers import (
    INPUT_CLASSIFIER_NAMES,
    collect_input_classifiers,
    is_key_classifier,
)


AVAILABLE = "available"
UNAVAILABLE = "unavailable"
AMBIGUOUS_DEVICE = "ambiguous-device"
DEVICE_NODE_CONFLICT = "device-node-conflict"
PERMISSION_DENIED = "permission-denied"

_PROPERTY_NAMES = (
    "ID_VENDOR_ID",
    "ID_MODEL_ID",
    "ID_USB_INTERFACE_NUM",
    "ID_SERIAL_SHORT",
    "ID_PATH",
    "ID_MODEL_FROM_DATABASE",
    "ID_MODEL",
    "ID_VENDOR_FROM_DATABASE",
    "ID_VENDOR",
    "ID_INPUT_MOUSE",
    "ID_INPUT_KEYBOARD",
    "ID_INPUT_JOYSTICK",
    "DEVNAME",
) + INPUT_CLASSIFIER_NAMES


@dataclass(frozen=True)
class DeviceCandidate:
    node: str
    subsystem: str
    properties: Mapping[str, str]
    event_codes: frozenset[int] | None
    keyboard_class: bool
    display_name: str | None = None
    classifiers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProfileResolution:
    profile: DeviceProfile
    status: str
    node: str | None

    @property
    def is_available(self) -> bool:
        return self.status == AVAILABLE and self.node is not None


class UdevDevice(Protocol):
    subsystem: str
    device_node: str | None

    def get(self, key: str, default=None): ...


class UdevContext(Protocol):
    def list_devices(self, *, subsystem: str) -> Iterable[UdevDevice]: ...


class CapabilityDevice(Protocol):
    def capabilities(self) -> Mapping[int, Iterable[int]]: ...

    def close(self) -> None: ...


def resolve_profiles(
    profiles: Iterable[DeviceProfile],
    candidates: Iterable[DeviceCandidate],
) -> tuple[ProfileResolution, ...]:
    snapshot = tuple(candidates)
    resolutions = tuple(_resolve_profile(profile, snapshot) for profile in profiles)
    node_counts = Counter(item.node for item in resolutions if item.node is not None)
    return tuple(
        replace(item, status=DEVICE_NODE_CONFLICT, node=None)
        if item.node is not None and node_counts[item.node] > 1
        else item
        for item in resolutions
    )


def _resolve_profile(
    profile: DeviceProfile,
    candidates: tuple[DeviceCandidate, ...],
) -> ProfileResolution:
    identity_matches = tuple(
        item for item in candidates if _matches_identity(profile, item)
    )
    required_codes = _required_event_codes(profile)
    potential_matches = tuple(
        item
        for item in identity_matches
        if item.event_codes is None
        or not required_codes
        or required_codes.issubset(item.event_codes)
    )
    if not potential_matches:
        return ProfileResolution(profile, UNAVAILABLE, None)
    if len(potential_matches) > 1:
        return ProfileResolution(profile, AMBIGUOUS_DEVICE, None)
    match = potential_matches[0]
    if match.event_codes is None:
        return ProfileResolution(profile, PERMISSION_DENIED, match.node)
    return ProfileResolution(profile, AVAILABLE, match.node)


def _matches_identity(profile: DeviceProfile, candidate: DeviceCandidate) -> bool:
    expected_subsystem = "hidraw" if isinstance(profile, HidrawProfile) else "input"
    if candidate.subsystem != expected_subsystem:
        return False

    properties = candidate.properties
    if _hex_property(properties, "ID_VENDOR_ID") != profile.vendor_id:
        return False
    if _hex_property(properties, "ID_MODEL_ID") != profile.product_id:
        return False
    if (
        profile.interface_number is not None
        and _interface_property(properties) != profile.interface_number
    ):
        return False
    if (
        profile.serial is not None
        and properties.get("ID_SERIAL_SHORT") != profile.serial
    ):
        return False
    if profile.id_path is not None and properties.get("ID_PATH") != profile.id_path:
        return False
    return True


def _required_event_codes(profile: DeviceProfile) -> frozenset[int]:
    if not isinstance(profile, EvdevProfile):
        return frozenset()
    event_names = profile.on_events + profile.off_events + profile.toggle_events
    return frozenset(ecodes.ecodes[name] for name in event_names)


def _hex_property(properties: Mapping[str, str], name: str) -> str | None:
    value = properties.get(name)
    return value.lower() if value is not None else None


def _interface_property(properties: Mapping[str, str]) -> str | None:
    value = _hex_property(properties, "ID_USB_INTERFACE_NUM")
    return value.zfill(2) if value is not None else None


class DeviceDiscovery:
    def __init__(
        self,
        *,
        context: UdevContext | None = None,
        input_device_factory: Callable[[str], CapabilityDevice] = InputDevice,
        access_fn: Callable[[str, int], bool] = os.access,
    ):
        self._context = context if context is not None else pyudev.Context()
        self._input_device_factory = input_device_factory
        self._access = access_fn

    def enumerate(self) -> tuple[DeviceCandidate, ...]:
        candidates = []
        for subsystem in ("hidraw", "input"):
            for device in self._context.list_devices(subsystem=subsystem):
                candidate = self._candidate(device, subsystem)
                if candidate is not None:
                    candidates.append(candidate)
        return tuple(candidates)

    def resolve(
        self,
        profiles: Iterable[DeviceProfile],
    ) -> tuple[ProfileResolution, ...]:
        return resolve_profiles(profiles, self.enumerate())

    def _candidate(
        self,
        device: UdevDevice,
        subsystem: str,
    ) -> DeviceCandidate | None:
        raw_node = device.get("DEVNAME") or device.device_node
        if raw_node is None:
            return None
        node = str(raw_node)
        if subsystem == "input" and not node.startswith("/dev/input/event"):
            return None

        properties = self._properties(device, node)
        event_codes: frozenset[int] | None = frozenset()
        if not self._access(node, os.R_OK):
            event_codes = None
        elif subsystem == "input":
            try:
                event_codes = self._event_codes(node)
            except PermissionError:
                event_codes = None
            except OSError:
                return None

        classifiers = collect_input_classifiers(properties)
        return DeviceCandidate(
            node=node,
            subsystem=subsystem,
            properties=properties,
            event_codes=event_codes,
            keyboard_class=any(
                is_key_classifier(classifier) for classifier in classifiers
            ),
            display_name=(
                properties.get("ID_MODEL_FROM_DATABASE") or properties.get("ID_MODEL")
            ),
            classifiers=classifiers,
        )

    def _properties(self, device: UdevDevice, node: str) -> dict[str, str]:
        properties = {
            name: str(value)
            for name in _PROPERTY_NAMES
            if (value := device.get(name)) is not None
        }
        properties["DEVNAME"] = node
        for name in ("ID_VENDOR_ID", "ID_MODEL_ID"):
            if name in properties:
                properties[name] = properties[name].lower()
        if "ID_USB_INTERFACE_NUM" in properties:
            properties["ID_USB_INTERFACE_NUM"] = (
                properties["ID_USB_INTERFACE_NUM"].lower().zfill(2)
            )
        return properties

    def _event_codes(self, node: str) -> frozenset[int]:
        device = self._input_device_factory(node)
        try:
            capabilities = device.capabilities()
            return frozenset(capabilities.get(ecodes.EV_KEY, ()))
        finally:
            try:
                device.close()
            except OSError:
                pass

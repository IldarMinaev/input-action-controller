from collections.abc import Sequence
from dataclasses import dataclass

from ..devices.classifiers import INPUT_CLASSIFIER_NAMES
from ..devices.discovery import DeviceCandidate


class AmbiguousSelectorError(ValueError):
    """Raised when stable runtime selectors cannot identify one candidate."""


@dataclass(frozen=True)
class SelectorDraft:
    transport: str
    vendor_id: str
    product_id: str
    interface_number: str | None
    serial: str | None = None
    id_path: str | None = None
    classifier: tuple[str, str] | None = None


def propose_selectors(
    selected: DeviceCandidate,
    all_candidates: Sequence[DeviceCandidate],
    *,
    allow_port_binding: bool = False,
) -> SelectorDraft:
    """Build the narrowest runtime selector that identifies ``selected``."""
    transport = _transport(selected)
    vendor_id = _required_property(selected, "ID_VENDOR_ID")
    product_id = _required_property(selected, "ID_MODEL_ID")
    interface_number = selected.properties.get("ID_USB_INTERFACE_NUM")
    base_matches = tuple(
        candidate
        for candidate in all_candidates
        if _matches_base(
            candidate,
            transport=transport,
            vendor_id=vendor_id,
            product_id=product_id,
            interface_number=interface_number,
        )
    )
    if selected not in base_matches:
        raise ValueError("selected candidate is not present in discovery results")

    classifier = _narrowing_classifier(selected, base_matches)
    narrowed_matches = (
        tuple(
            candidate
            for candidate in base_matches
            if candidate.properties.get(classifier[0]) == classifier[1]
        )
        if classifier is not None
        else base_matches
    )
    serial = None
    id_path = None
    if len(narrowed_matches) > 1:
        selected_serial = selected.properties.get("ID_SERIAL_SHORT")
        if selected_serial and _unique_property_match(
            narrowed_matches,
            "ID_SERIAL_SHORT",
            selected_serial,
        ):
            serial = selected_serial
        else:
            selected_id_path = selected.properties.get("ID_PATH")
            if (
                allow_port_binding
                and selected_id_path
                and _unique_property_match(
                    narrowed_matches,
                    "ID_PATH",
                    selected_id_path,
                )
            ):
                id_path = selected_id_path
            else:
                raise AmbiguousSelectorError(
                    "multiple devices match the stable transport, USB ID, and interface selectors"
                )

    return SelectorDraft(
        transport=transport,
        vendor_id=vendor_id,
        product_id=product_id,
        interface_number=interface_number,
        serial=serial,
        id_path=id_path,
        classifier=classifier,
    )


def _transport(candidate: DeviceCandidate) -> str:
    if candidate.subsystem == "input":
        return "evdev"
    if candidate.subsystem == "hidraw":
        return "hidraw"
    raise ValueError(f"unsupported device subsystem: {candidate.subsystem}")


def _required_property(candidate: DeviceCandidate, name: str) -> str:
    value = candidate.properties.get(name)
    if not value:
        raise ValueError(f"selected candidate is missing {name}")
    return value


def _matches_base(
    candidate: DeviceCandidate,
    *,
    transport: str,
    vendor_id: str,
    product_id: str,
    interface_number: str | None,
) -> bool:
    return (
        _transport(candidate) == transport
        and candidate.properties.get("ID_VENDOR_ID") == vendor_id
        and candidate.properties.get("ID_MODEL_ID") == product_id
        and (
            interface_number is None
            or candidate.properties.get("ID_USB_INTERFACE_NUM") == interface_number
        )
    )


def _unique_property_match(
    candidates: Sequence[DeviceCandidate],
    name: str,
    value: str,
) -> bool:
    return sum(candidate.properties.get(name) == value for candidate in candidates) == 1


def _narrowing_classifier(
    selected: DeviceCandidate,
    base_matches: Sequence[DeviceCandidate],
) -> tuple[str, str] | None:
    if selected.subsystem != "input" or len(base_matches) < 2:
        return None
    ranked = []
    for index, name in enumerate(INPUT_CLASSIFIER_NAMES):
        if selected.properties.get(name) != "1":
            continue
        count = sum(candidate.properties.get(name) == "1" for candidate in base_matches)
        if count < len(base_matches):
            ranked.append((count, index, (name, "1")))
    return min(ranked)[2] if ranked else None

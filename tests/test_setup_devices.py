import unittest

from evdev import ecodes

from input_action_controller.devices.classifiers import INPUT_CLASSIFIER_NAMES
from input_action_controller.devices.discovery import DeviceCandidate, resolve_profiles
from input_action_controller.models import EvdevProfile
from input_action_controller.setup.devices import (
    AmbiguousSelectorError,
    propose_selectors,
)


def candidate(
    node: str,
    *,
    subsystem: str = "input",
    interface_number: str | None = "01",
    serial: str | None = None,
    id_path: str | None = None,
    event_codes: frozenset[int] | None = frozenset(),
    classifiers: tuple[tuple[str, str], ...] = (),
) -> DeviceCandidate:
    properties = {
        "ID_VENDOR_ID": "1234",
        "ID_MODEL_ID": "5678",
        "DEVNAME": node,
    }
    if interface_number is not None:
        properties["ID_USB_INTERFACE_NUM"] = interface_number
    if serial is not None:
        properties["ID_SERIAL_SHORT"] = serial
    if id_path is not None:
        properties["ID_PATH"] = id_path
    for name, value in classifiers:
        properties[name] = value
    return DeviceCandidate(
        node=node,
        subsystem=subsystem,
        properties=properties,
        event_codes=event_codes,
        keyboard_class=False,
        classifiers=classifiers,
    )


class SelectorProposalTests(unittest.TestCase):
    def test_uses_only_base_selectors_when_the_device_is_unique(self):
        selected = candidate(
            "/dev/input/event4",
            serial="desk-button",
            id_path="pci-0000:00:14.0-usb-0:2:1.3",
        )

        selectors = propose_selectors(selected, (selected,))

        self.assertEqual(selectors.transport, "evdev")
        self.assertEqual(selectors.vendor_id, "1234")
        self.assertEqual(selectors.product_id, "5678")
        self.assertEqual(selectors.interface_number, "01")
        self.assertIsNone(selectors.serial)
        self.assertIsNone(selectors.id_path)
        self.assertIsNone(selectors.classifier)

    def test_adds_serial_only_when_it_disambiguates_base_selectors(self):
        selected = candidate(
            "/dev/input/event4",
            serial="left",
            id_path="pci-0000:00:14.0-usb-0:2:1.3",
            classifiers=(("ID_INPUT_MOUSE", "1"),),
        )
        other = candidate(
            "/dev/input/event5",
            serial="right",
            classifiers=(("ID_INPUT_MOUSE", "1"),),
        )

        selectors = propose_selectors(selected, (selected, other))

        self.assertEqual(selectors.serial, "left")
        self.assertIsNone(selectors.id_path)
        self.assertIsNone(selectors.classifier)

    def test_classifier_narrows_before_serial_or_shared_id_path(self):
        shared_path = "pci-0000:00:14.0-usb-0:5:1.0"
        selected = candidate(
            "/dev/input/event4",
            serial="shared",
            id_path=shared_path,
            classifiers=(("ID_INPUT_MOUSE", "1"),),
        )
        sibling = candidate(
            "/dev/input/event5",
            serial="shared",
            id_path=shared_path,
            classifiers=(("ID_INPUT_KEY", "1"),),
        )

        selectors = propose_selectors(selected, (selected, sibling))

        self.assertEqual(selectors.classifier, ("ID_INPUT_MOUSE", "1"))
        self.assertIsNone(selectors.serial)
        self.assertIsNone(selectors.id_path)

    def test_most_selective_classifier_wins_with_registry_tie_breaking(self):
        selected = candidate(
            "/dev/input/event4",
            classifiers=(
                ("ID_INPUT_MOUSE", "1"),
                ("ID_INPUT_TOUCHPAD", "1"),
            ),
        )
        mouse_sibling = candidate(
            "/dev/input/event5",
            classifiers=(("ID_INPUT_MOUSE", "1"),),
        )
        unrelated = candidate(
            "/dev/input/event6",
            classifiers=(("ID_INPUT_KEY", "1"),),
        )

        selectors = propose_selectors(selected, (selected, mouse_sibling, unrelated))

        self.assertEqual(selectors.classifier, ("ID_INPUT_TOUCHPAD", "1"))

    def test_classifier_tie_uses_registry_order_not_candidate_order(self):
        self.assertLess(
            INPUT_CLASSIFIER_NAMES.index("ID_INPUT_TOUCHPAD"),
            INPUT_CLASSIFIER_NAMES.index("ID_INPUT_MOUSE"),
        )
        selected = candidate(
            "/dev/input/event4",
            classifiers=(
                ("ID_INPUT_MOUSE", "1"),
                ("ID_INPUT_TOUCHPAD", "1"),
            ),
        )
        sibling = candidate(
            "/dev/input/event5",
            classifiers=(("ID_INPUT_KEY", "1"),),
        )

        selectors = propose_selectors(selected, (selected, sibling))

        self.assertEqual(selectors.classifier, ("ID_INPUT_TOUCHPAD", "1"))

    def test_missing_interface_uses_runtime_wildcard_and_serial_resolves_one_node(self):
        selected = candidate(
            "/dev/input/event4",
            interface_number=None,
            serial="left",
            event_codes=frozenset({ecodes.BTN_SIDE}),
        )
        other = candidate(
            "/dev/input/event5",
            interface_number="02",
            serial="right",
            event_codes=frozenset({ecodes.BTN_SIDE}),
        )

        selectors = propose_selectors(selected, (selected, other))
        profile = EvdevProfile(
            name="Desk button",
            action="voice_input",
            vendor_id=selectors.vendor_id,
            product_id=selectors.product_id,
            interface_number=selectors.interface_number,
            serial=selectors.serial,
            id_path=selectors.id_path,
            mode="toggle",
            toggle_events=("BTN_SIDE",),
        )

        resolution = resolve_profiles((profile,), (selected, other))[0]

        self.assertIsNone(selectors.interface_number)
        self.assertEqual(selectors.serial, "left")
        self.assertTrue(resolution.is_available)
        self.assertEqual(resolution.node, "/dev/input/event4")

    def test_refuses_to_choose_between_identical_candidates_without_a_serial(self):
        selected = candidate("/dev/input/event4")
        other = candidate("/dev/input/event5")

        with self.assertRaises(AmbiguousSelectorError):
            propose_selectors(selected, (selected, other))

    def test_uses_id_path_only_after_explicit_port_binding_choice(self):
        selected = candidate(
            "/dev/input/event4",
            serial="shared",
            id_path="pci-0000:00:14.0-usb-0:2:1.3",
        )
        other = candidate(
            "/dev/input/event5",
            serial="shared",
            id_path="pci-0000:00:14.0-usb-0:3:1.3",
        )

        with self.assertRaises(AmbiguousSelectorError):
            propose_selectors(selected, (selected, other))

        selectors = propose_selectors(
            selected,
            (selected, other),
            allow_port_binding=True,
        )

        self.assertIsNone(selectors.serial)
        self.assertEqual(selectors.id_path, "pci-0000:00:14.0-usb-0:2:1.3")

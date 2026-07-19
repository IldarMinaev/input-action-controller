from dataclasses import replace
import os
import unittest

from evdev import ecodes

from input_action_controller.devices.classifiers import INPUT_CLASSIFIER_NAMES
from input_action_controller.devices.discovery import (
    DeviceCandidate,
    DeviceDiscovery,
    resolve_profiles,
)
from input_action_controller.models import EvdevProfile, HidrawProfile


def hidraw_profile(**overrides) -> HidrawProfile:
    profile = HidrawProfile(
        name="Desk headset",
        action="voice_input",
        vendor_id="047f",
        product_id="c056",
        interface_number="03",
        on_reports=(b"\x08\x02",),
        off_reports=(b"\x08\x00",),
    )
    return replace(profile, **overrides)


def evdev_profile(**overrides) -> EvdevProfile:
    profile = EvdevProfile(
        name="Desk button",
        action="voice_input",
        vendor_id="1234",
        product_id="5678",
        interface_number="01",
        mode="on-off",
        on_events=("KEY_F13",),
        off_events=("KEY_F14",),
    )
    return replace(profile, **overrides)


def candidate(
    node: str,
    subsystem: str,
    *,
    vendor_id: str,
    product_id: str,
    interface_number: str | None = None,
    serial: str | None = None,
    id_path: str | None = None,
    event_codes: frozenset[int] | None = frozenset(),
    keyboard_class: bool = False,
) -> DeviceCandidate:
    properties = {
        "ID_VENDOR_ID": vendor_id,
        "ID_MODEL_ID": product_id,
        "DEVNAME": node,
    }
    if interface_number is not None:
        properties["ID_USB_INTERFACE_NUM"] = interface_number
    if serial is not None:
        properties["ID_SERIAL_SHORT"] = serial
    if id_path is not None:
        properties["ID_PATH"] = id_path
    return DeviceCandidate(
        node,
        subsystem,
        properties,
        event_codes,
        keyboard_class,
    )


class ResolutionTests(unittest.TestCase):
    def test_matches_every_stable_selector_exactly(self):
        profile = hidraw_profile(
            serial="serial-B",
            id_path="pci-0000:00:14.0-usb-0:2:1.3",
        )
        exact = candidate(
            "/dev/hidraw7",
            "hidraw",
            vendor_id="047f",
            product_id="c056",
            interface_number="03",
            serial="serial-B",
            id_path="pci-0000:00:14.0-usb-0:2:1.3",
        )
        candidates = (
            replace(exact, subsystem="input", node="/dev/input/event1"),
            candidate(
                "/dev/hidraw2",
                "hidraw",
                vendor_id="147f",
                product_id="c056",
                interface_number="03",
                serial="serial-B",
                id_path="pci-0000:00:14.0-usb-0:2:1.3",
            ),
            candidate(
                "/dev/hidraw3",
                "hidraw",
                vendor_id="047f",
                product_id="d056",
                interface_number="03",
                serial="serial-B",
                id_path="pci-0000:00:14.0-usb-0:2:1.3",
            ),
            candidate(
                "/dev/hidraw4",
                "hidraw",
                vendor_id="047f",
                product_id="c056",
                interface_number="04",
                serial="serial-B",
                id_path="pci-0000:00:14.0-usb-0:2:1.3",
            ),
            candidate(
                "/dev/hidraw5",
                "hidraw",
                vendor_id="047f",
                product_id="c056",
                interface_number="03",
                serial="serial-A",
                id_path="pci-0000:00:14.0-usb-0:2:1.3",
            ),
            candidate(
                "/dev/hidraw6",
                "hidraw",
                vendor_id="047f",
                product_id="c056",
                interface_number="03",
                serial="serial-B",
                id_path="pci-0000:00:14.0-usb-0:3:1.3",
            ),
            exact,
        )

        resolution = resolve_profiles((profile,), candidates)[0]

        self.assertTrue(resolution.is_available)
        self.assertEqual(resolution.status, "available")
        self.assertEqual(resolution.node, "/dev/hidraw7")

    def test_reports_zero_matches_as_unavailable(self):
        resolution = resolve_profiles(
            (hidraw_profile(),),
            (
                candidate(
                    "/dev/hidraw1",
                    "hidraw",
                    vendor_id="9999",
                    product_id="0001",
                    interface_number="03",
                ),
            ),
        )[0]

        self.assertFalse(resolution.is_available)
        self.assertEqual(resolution.status, "unavailable")
        self.assertIsNone(resolution.node)

    def test_identical_devices_are_ambiguous_until_serial_selects_one(self):
        candidates = (
            candidate(
                "/dev/hidraw1",
                "hidraw",
                vendor_id="047f",
                product_id="c056",
                interface_number="03",
                serial="left",
            ),
            candidate(
                "/dev/hidraw2",
                "hidraw",
                vendor_id="047f",
                product_id="c056",
                interface_number="03",
                serial="right",
            ),
        )

        ambiguous = resolve_profiles((hidraw_profile(),), candidates)[0]
        selected = resolve_profiles(
            (hidraw_profile(serial="right"),),
            candidates,
        )[0]

        self.assertEqual(ambiguous.status, "ambiguous-device")
        self.assertIsNone(ambiguous.node)
        self.assertEqual(selected.status, "available")
        self.assertEqual(selected.node, "/dev/hidraw2")

    def test_evdev_requires_every_configured_key_capability(self):
        incomplete = candidate(
            "/dev/input/event3",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=frozenset({ecodes.KEY_F13}),
        )
        complete = candidate(
            "/dev/input/event4",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=frozenset({ecodes.KEY_F13, ecodes.KEY_F14, ecodes.KEY_F15}),
        )

        resolution = resolve_profiles((evdev_profile(),), (incomplete, complete))[0]

        self.assertEqual(resolution.status, "available")
        self.assertEqual(resolution.node, "/dev/input/event4")

    def test_evdev_readable_capability_mismatch_is_unavailable(self):
        mismatch = candidate(
            "/dev/input/event3",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=frozenset({ecodes.KEY_F13}),
        )

        resolution = resolve_profiles((evdev_profile(),), (mismatch,))[0]

        self.assertEqual(resolution.status, "unavailable")
        self.assertIsNone(resolution.node)

    def test_evdev_mismatch_is_excluded_before_single_unreadable_result(self):
        mismatch = candidate(
            "/dev/input/event3",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=frozenset({ecodes.KEY_F13}),
        )
        denied = candidate(
            "/dev/input/event8",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=None,
        )

        resolution = resolve_profiles((evdev_profile(),), (mismatch, denied))[0]

        self.assertEqual(resolution.status, "permission-denied")
        self.assertEqual(resolution.node, "/dev/input/event8")

    def test_evdev_readable_full_match_and_unreadable_match_are_ambiguous(self):
        complete = candidate(
            "/dev/input/event4",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=frozenset({ecodes.KEY_F13, ecodes.KEY_F14}),
        )
        denied = candidate(
            "/dev/input/event8",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=None,
        )

        resolution = resolve_profiles((evdev_profile(),), (complete, denied))[0]

        self.assertEqual(resolution.status, "ambiguous-device")
        self.assertIsNone(resolution.node)

    def test_evdev_multiple_unreadable_matches_are_ambiguous(self):
        first = candidate(
            "/dev/input/event8",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=None,
        )
        second = replace(first, node="/dev/input/event9")

        resolution = resolve_profiles((evdev_profile(),), (first, second))[0]

        self.assertEqual(resolution.status, "ambiguous-device")
        self.assertIsNone(resolution.node)

    def test_evdev_multiple_readable_full_matches_are_ambiguous(self):
        first = candidate(
            "/dev/input/event4",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=frozenset({ecodes.KEY_F13, ecodes.KEY_F14}),
        )
        second = replace(first, node="/dev/input/event5")

        resolution = resolve_profiles((evdev_profile(),), (first, second))[0]

        self.assertEqual(resolution.status, "ambiguous-device")
        self.assertIsNone(resolution.node)

    def test_reports_matching_unreadable_node_as_permission_denied(self):
        denied = candidate(
            "/dev/input/event8",
            "input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            event_codes=None,
        )

        resolution = resolve_profiles((evdev_profile(),), (denied,))[0]

        self.assertEqual(resolution.status, "permission-denied")
        self.assertEqual(resolution.node, "/dev/input/event8")

    def test_marks_every_cross_profile_same_node_collision_inactive(self):
        first = hidraw_profile(name="Primary headset")
        second = hidraw_profile(name="Backup headset")
        shared = candidate(
            "/dev/hidraw7",
            "hidraw",
            vendor_id="047f",
            product_id="c056",
            interface_number="03",
        )

        resolutions = resolve_profiles((first, second), (shared,))

        self.assertEqual(
            [(item.profile.name, item.status, item.node) for item in resolutions],
            [
                ("Primary headset", "device-node-conflict", None),
                ("Backup headset", "device-node-conflict", None),
            ],
        )

    def test_node_collision_overrides_permission_denied_for_every_profile(self):
        first = hidraw_profile(name="Primary headset")
        second = hidraw_profile(name="Backup headset")
        shared = candidate(
            "/dev/hidraw7",
            "hidraw",
            vendor_id="047f",
            product_id="c056",
            interface_number="03",
            event_codes=None,
        )

        resolutions = resolve_profiles((first, second), (shared,))

        self.assertEqual(
            [(item.status, item.node) for item in resolutions],
            [
                ("device-node-conflict", None),
                ("device-node-conflict", None),
            ],
        )


class FakeUdevDevice(dict):
    def __init__(self, subsystem: str, node: str | None, **properties: str):
        super().__init__(properties)
        self.subsystem = subsystem
        self.device_node = node


class FakeUdevContext:
    def __init__(self, devices_by_subsystem):
        self.devices_by_subsystem = devices_by_subsystem
        self.calls = []

    def list_devices(self, *, subsystem: str):
        self.calls.append(subsystem)
        return tuple(self.devices_by_subsystem.get(subsystem, ()))


class FakeEvdevDevice:
    def __init__(self, event_codes):
        self.event_codes = event_codes
        self.closed = False

    def capabilities(self):
        return {ecodes.EV_KEY: list(self.event_codes), ecodes.EV_REL: [ecodes.REL_X]}

    def close(self):
        self.closed = True


class EnumerationTests(unittest.TestCase):
    def test_enumerates_hidraw_and_event_nodes_with_normalized_properties(self):
        hidraw = FakeUdevDevice(
            "hidraw",
            "/dev/hidraw7",
            ID_VENDOR_ID="047F",
            ID_MODEL_ID="C056",
            ID_USB_INTERFACE_NUM="3",
            ID_SERIAL_SHORT="ABC-123",
            ID_PATH="pci-0000:00:14.0-usb-0:2:1.3",
        )
        event = FakeUdevDevice(
            "input",
            "/dev/input/event4",
            DEVNAME="/dev/input/event4",
            ID_VENDOR_ID="1234",
            ID_MODEL_ID="ABCD",
            ID_USB_INTERFACE_NUM="01",
            ID_MODEL_FROM_DATABASE="Desk mouse",
            **{name: "1" for name in INPUT_CLASSIFIER_NAMES},
        )
        no_node = FakeUdevDevice("input", None, ID_VENDOR_ID="1234")
        denied = FakeUdevDevice(
            "input",
            "/dev/input/event5",
            ID_VENDOR_ID="1234",
            ID_MODEL_ID="ABCD",
            ID_USB_INTERFACE_NUM="01",
        )
        context = FakeUdevContext(
            {"hidraw": (hidraw,), "input": (event, no_node, denied)}
        )
        opened = []
        fake_evdev = FakeEvdevDevice({ecodes.KEY_F13, ecodes.KEY_F14})

        def input_device_factory(node):
            opened.append(node)
            return fake_evdev

        discovery = DeviceDiscovery(
            context=context,
            input_device_factory=input_device_factory,
            access_fn=lambda node, mode: (
                node != "/dev/input/event5" and mode == os.R_OK
            ),
        )

        candidates = discovery.enumerate()

        self.assertEqual(context.calls, ["hidraw", "input"])
        self.assertEqual(opened, ["/dev/input/event4"])
        self.assertTrue(fake_evdev.closed)
        self.assertEqual(
            [item.node for item in candidates],
            [
                "/dev/hidraw7",
                "/dev/input/event4",
                "/dev/input/event5",
            ],
        )
        self.assertEqual(candidates[0].properties["DEVNAME"], "/dev/hidraw7")
        self.assertEqual(candidates[0].properties["ID_VENDOR_ID"], "047f")
        self.assertEqual(candidates[0].properties["ID_MODEL_ID"], "c056")
        self.assertEqual(candidates[0].properties["ID_USB_INTERFACE_NUM"], "03")
        self.assertEqual(
            candidates[1].event_codes,
            frozenset({ecodes.KEY_F13, ecodes.KEY_F14}),
        )
        self.assertTrue(candidates[1].keyboard_class)
        self.assertEqual(candidates[1].display_name, "Desk mouse")
        self.assertEqual(
            candidates[1].classifiers,
            tuple((name, "1") for name in INPUT_CLASSIFIER_NAMES),
        )
        self.assertEqual(candidates[1].properties["ID_INPUT_MOUSE"], "1")
        self.assertIsNone(candidates[2].event_codes)

    def test_id_input_key_is_treated_as_key_class(self):
        device = FakeUdevDevice(
            "input",
            "/dev/input/event4",
            ID_VENDOR_ID="1234",
            ID_MODEL_ID="5678",
            ID_INPUT_KEY="1",
        )
        discovery = DeviceDiscovery(
            context=FakeUdevContext({"hidraw": (), "input": (device,)}),
            input_device_factory=lambda _node: FakeEvdevDevice({ecodes.KEY_F13}),
            access_fn=lambda _node, _mode: True,
        )

        candidate = discovery.enumerate()[0]

        self.assertEqual(candidate.classifiers, (("ID_INPUT_KEY", "1"),))
        self.assertTrue(candidate.keyboard_class)


if __name__ == "__main__":
    unittest.main()

from dataclasses import dataclass
import os
from threading import Event
import unittest

from evdev import ecodes

from input_action_controller.devices.base import DeviceReadError, MatchedInput
from input_action_controller.models import ActionRequest, EvdevProfile, HidrawProfile


HIDRAW_NODE = "/dev/hidraw7"
EVDEV_NODE = "/dev/input/event4"


def hidraw_profile() -> HidrawProfile:
    return HidrawProfile(
        name="Desk headset",
        action="voice_input",
        vendor_id="047f",
        product_id="c056",
        on_reports=(bytes.fromhex("08 02"),),
        off_reports=(bytes.fromhex("08 00"),),
    )


def evdev_profile() -> EvdevProfile:
    return EvdevProfile(
        name="Desk button",
        action="voice_input",
        vendor_id="1234",
        product_id="5678",
        mode="toggle",
        toggle_events=("BTN_SIDE",),
        toggle_off_timeout_seconds=30.0,
    )


class FakeHidrawApi:
    def __init__(self, reports=(), open_error: OSError | None = None):
        self._reports = iter(reports)
        self._open_error = open_error
        self.open_calls = []
        self.read_calls = []
        self.close_calls = []

    def open(self, node, flags):
        self.open_calls.append((node, flags))
        if self._open_error is not None:
            raise self._open_error
        return 71

    def read(self, descriptor, size):
        self.read_calls.append((descriptor, size))
        return next(self._reports)[:size]

    def close(self, descriptor):
        self.close_calls.append(descriptor)


class FakePoll:
    def __init__(self, results, stop: Event | None = None):
        self._results = iter(results)
        self._stop = stop
        self.calls = []

    def __call__(self, readable, writable, exceptional, timeout):
        self.calls.append((readable, writable, exceptional, timeout))
        result = next(self._results)
        if self._stop is not None and not result[0]:
            self._stop.set()
        return result


@dataclass(frozen=True)
class FakeInputEvent:
    type: int
    code: int
    value: int


class FakeInputDevice:
    fd = 44

    def __init__(self, events=(), read_error: OSError | None = None):
        self._events = tuple(events)
        self._read_error = read_error
        self.read_calls = 0
        self.close_calls = 0
        self.grab_calls = 0

    def read(self):
        self.read_calls += 1
        if self._read_error is not None:
            raise self._read_error
        return iter(self._events)

    def close(self):
        self.close_calls += 1

    def grab(self):
        self.grab_calls += 1


class HidrawEventSourceTests(unittest.TestCase):
    def test_opens_nonblocking_reads_matches_and_closes(self):
        from input_action_controller.devices.hidraw import (
            HID_MAX_BUFFER_SIZE,
            HidrawEventSource,
        )

        api = FakeHidrawApi(reports=(bytes.fromhex("08 02"),))
        stop = Event()
        poll = FakePoll((([71], [], []), ([], [], [])), stop)
        emitted = []

        HidrawEventSource(
            hidraw_profile(),
            HIDRAW_NODE,
            open_fn=api.open,
            read_fn=api.read,
            close_fn=api.close,
            select_fn=poll,
            poll_interval_seconds=0.01,
        ).run(stop, emitted.append)

        self.assertEqual(api.open_calls, [(HIDRAW_NODE, os.O_RDONLY | os.O_NONBLOCK)])
        self.assertEqual(HID_MAX_BUFFER_SIZE, 16 * 1024)
        self.assertEqual(api.read_calls, [(71, HID_MAX_BUFFER_SIZE)])
        self.assertEqual(api.close_calls, [71])
        self.assertEqual(
            emitted,
            [MatchedInput(ActionRequest.ON, "Desk headset", None)],
        )
        self.assertLessEqual(poll.calls[0][3], 0.25)

    def test_stops_after_a_bounded_empty_poll(self):
        from input_action_controller.devices.hidraw import HidrawEventSource

        api = FakeHidrawApi()
        stop = Event()
        poll = FakePoll((([], [], []),), stop)

        HidrawEventSource(
            hidraw_profile(),
            HIDRAW_NODE,
            open_fn=api.open,
            read_fn=api.read,
            close_fn=api.close,
            select_fn=poll,
            poll_interval_seconds=0.01,
        ).run(stop, lambda matched: self.fail(f"unexpected match: {matched}"))

        self.assertEqual(api.read_calls, [])
        self.assertEqual(api.close_calls, [71])
        self.assertEqual(len(poll.calls), 1)

    def test_rejects_an_unknown_report_that_starts_with_a_configured_report(self):
        from input_action_controller.devices.hidraw import (
            HID_MAX_BUFFER_SIZE,
            HidrawEventSource,
        )

        configured_report = bytes(range(256)) * 16
        profile = HidrawProfile(
            name="Desk headset",
            action="voice_input",
            vendor_id="047f",
            product_id="c056",
            on_reports=(configured_report,),
            off_reports=(bytes.fromhex("08 00"),),
        )
        api = FakeHidrawApi(reports=(configured_report + b"\x00",))
        stop = Event()
        poll = FakePoll((([71], [], []), ([], [], [])), stop)
        emitted = []

        HidrawEventSource(
            profile,
            HIDRAW_NODE,
            open_fn=api.open,
            read_fn=api.read,
            close_fn=api.close,
            select_fn=poll,
        ).run(stop, emitted.append)

        self.assertEqual(emitted, [])
        self.assertEqual(api.read_calls, [(71, HID_MAX_BUFFER_SIZE)])

    def test_reports_end_of_file_with_profile_and_node_and_closes(self):
        from input_action_controller.devices.hidraw import HidrawEventSource

        api = FakeHidrawApi(reports=(b"",))
        poll = FakePoll((([71], [], []),))

        with self.assertRaises(DeviceReadError) as raised:
            HidrawEventSource(
                hidraw_profile(),
                HIDRAW_NODE,
                open_fn=api.open,
                read_fn=api.read,
                close_fn=api.close,
                select_fn=poll,
            ).run(Event(), lambda matched: self.fail(f"unexpected match: {matched}"))

        self.assertIn("Desk headset", str(raised.exception))
        self.assertIn(HIDRAW_NODE, str(raised.exception))
        self.assertEqual(api.close_calls, [71])

    def test_reports_open_permission_error_with_profile_and_node(self):
        from input_action_controller.devices.hidraw import HidrawEventSource

        api = FakeHidrawApi(open_error=PermissionError("permission denied"))

        with self.assertRaises(DeviceReadError) as raised:
            HidrawEventSource(
                hidraw_profile(),
                HIDRAW_NODE,
                open_fn=api.open,
                read_fn=api.read,
                close_fn=api.close,
            ).run(Event(), lambda matched: None)

        self.assertIn("Desk headset", str(raised.exception))
        self.assertIn(HIDRAW_NODE, str(raised.exception))
        self.assertEqual(api.close_calls, [])


class EvdevEventSourceTests(unittest.TestCase):
    def test_iterates_matching_events_without_grabbing_and_closes(self):
        from input_action_controller.devices.evdev import EvdevEventSource

        device = FakeInputDevice(
            events=(
                FakeInputEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 1),
                FakeInputEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 0),
                FakeInputEvent(ecodes.EV_REL, ecodes.REL_X, 1),
            )
        )
        stop = Event()
        poll = FakePoll((([device.fd], [], []), ([], [], [])), stop)
        emitted = []

        EvdevEventSource(
            evdev_profile(),
            EVDEV_NODE,
            input_device_factory=lambda node: device,
            select_fn=poll,
            poll_interval_seconds=0.01,
        ).run(stop, emitted.append)

        self.assertEqual(device.read_calls, 1)
        self.assertEqual(device.grab_calls, 0)
        self.assertEqual(device.close_calls, 1)
        self.assertEqual(
            emitted,
            [MatchedInput(ActionRequest.TOGGLE, "Desk button", 30.0)],
        )
        self.assertLessEqual(poll.calls[0][3], 0.25)

    def test_reports_disconnect_with_profile_and_node_and_closes(self):
        from input_action_controller.devices.evdev import EvdevEventSource

        device = FakeInputDevice(read_error=OSError("device disconnected"))
        poll = FakePoll((([device.fd], [], []),))

        with self.assertRaises(DeviceReadError) as raised:
            EvdevEventSource(
                evdev_profile(),
                EVDEV_NODE,
                input_device_factory=lambda node: device,
                select_fn=poll,
            ).run(Event(), lambda matched: None)

        self.assertIn("Desk button", str(raised.exception))
        self.assertIn(EVDEV_NODE, str(raised.exception))
        self.assertEqual(device.close_calls, 1)
        self.assertEqual(device.grab_calls, 0)


if __name__ == "__main__":
    unittest.main()

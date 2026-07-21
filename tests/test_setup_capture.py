from dataclasses import dataclass
import inspect
import unittest

from evdev import ecodes
import tomlkit

from input_action_controller.config import ConfigError
from input_action_controller.setup.capture import (
    CaptureTimeout,
    EvdevTriggerDraft,
    HidrawTiming,
    HidrawTriggerDraft,
    InconsistentCapture,
    NoisyCapture,
    RawEvdevEvent,
    apply_device_draft,
    _capture_hidraw_trial,
    _collect_trial_reports,
    capture_evdev_presses,
    capture_hidraw,
    evdev_press_name,
)
from input_action_controller.setup.devices import SelectorDraft


@dataclass
class FakeEvdevStream:
    events: list[RawEvdevEvent | None]

    def __post_init__(self):
        self.timeouts: list[float] = []

    def read(self, timeout_seconds: float) -> RawEvdevEvent | None:
        self.timeouts.append(timeout_seconds)
        return self.events.pop(0)


class TimedEvdevStream:
    def __init__(self, clock: "FakeClock", delays: list[float]):
        self.clock = clock
        self.delays = delays
        self.timeouts: list[float] = []

    def read(self, timeout_seconds: float) -> RawEvdevEvent | None:
        self.timeouts.append(timeout_seconds)
        delay = self.delays.pop(0)
        if delay >= timeout_seconds:
            self.clock.advance(timeout_seconds)
            return None
        self.clock.advance(delay)
        return RawEvdevEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 1)


@dataclass
class FakeClock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class TimedReport:
    at_seconds: float
    report: bytes


@dataclass
class FakeHidrawStream:
    clock: FakeClock
    reports: list[TimedReport]

    def __post_init__(self) -> None:
        self.timeouts: list[float] = []

    def read(self, timeout_seconds: float) -> bytes | None:
        self.timeouts.append(timeout_seconds)
        if (
            self.reports
            and self.reports[0].at_seconds <= self.clock.now + timeout_seconds
        ):
            scheduled = self.reports.pop(0)
            self.clock.now = scheduled.at_seconds
            return scheduled.report
        self.clock.advance(timeout_seconds)
        return None


def hidraw_reports(
    on_trials: tuple[tuple[bytes, ...], ...],
    off_trials: tuple[tuple[bytes, ...], ...],
    *,
    baseline: tuple[bytes, ...] = (b"common",),
) -> list[TimedReport]:
    reports = [
        TimedReport(index * 0.1, report)
        for index, report in enumerate(baseline, start=1)
    ]
    current = 2.0
    for trial in (
        on_trials[0],
        off_trials[0],
        on_trials[1],
        off_trials[1],
        on_trials[2],
        off_trials[2],
    ):
        for index, report in enumerate(trial, start=1):
            reports.append(TimedReport(current + index * 0.01, report))
        current += len(trial) * 0.01 + 0.5
    return reports


class EvdevCaptureTests(unittest.TestCase):
    def test_filters_to_symbolic_key_and_button_presses(self):
        self.assertIsNone(evdev_press_name(ecodes.EV_REL, ecodes.REL_X, 1))
        self.assertIsNone(evdev_press_name(ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
        self.assertIsNone(evdev_press_name(ecodes.EV_KEY, ecodes.BTN_SIDE, 0))
        self.assertIsNone(evdev_press_name(ecodes.EV_KEY, ecodes.BTN_SIDE, 2))
        self.assertEqual(
            evdev_press_name(ecodes.EV_KEY, ecodes.BTN_SIDE, 1),
            "BTN_SIDE",
        )

    def test_uses_a_deterministic_symbolic_name_for_an_aliased_key_code(self):
        code = ecodes.ecodes["KEY_HANGEUL"]
        stream = FakeEvdevStream(
            [
                RawEvdevEvent(ecodes.EV_KEY, code, 1),
                None,
            ]
        )

        names = capture_evdev_presses(stream, timeout_seconds=2.5)

        self.assertEqual(names, ("KEY_HANGEUL",))

    def test_captures_deduplicated_press_names_until_stream_timeout(self):
        stream = FakeEvdevStream(
            [
                RawEvdevEvent(ecodes.EV_REL, ecodes.REL_X, 1),
                RawEvdevEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 0),
                RawEvdevEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 1),
                RawEvdevEvent(ecodes.EV_KEY, ecodes.BTN_SIDE, 1),
                RawEvdevEvent(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1),
                None,
            ]
        )

        names = capture_evdev_presses(stream, timeout_seconds=2.5, clock=FakeClock())

        self.assertEqual(names, ("BTN_SIDE", "BTN_EXTRA"))
        self.assertEqual(stream.timeouts, [2.5] * 6)

    def test_uses_one_deadline_across_repeated_events(self):
        clock = FakeClock()
        stream = TimedEvdevStream(clock, [2.0, 2.0, 2.0])

        names = capture_evdev_presses(
            stream,
            timeout_seconds=5.0,
            clock=clock,
        )

        self.assertEqual(names, ("BTN_SIDE",))
        self.assertEqual(stream.timeouts, [5.0, 3.0, 1.0])
        self.assertEqual(clock.now, 5.0)


class EvdevTriggerDraftTests(unittest.TestCase):
    def test_toggle_timeout_defaults_to_sixty_and_accepts_positive_or_zero_override(
        self,
    ):
        default = EvdevTriggerDraft(mode="toggle", toggle_events=("BTN_SIDE",))
        positive = EvdevTriggerDraft(
            mode="toggle",
            toggle_events=("BTN_SIDE",),
            toggle_off_timeout_seconds=12.5,
        )
        disabled = EvdevTriggerDraft(
            mode="toggle",
            toggle_events=("BTN_SIDE",),
            toggle_off_timeout_seconds=0,
        )

        self.assertEqual(default.toggle_off_timeout_seconds, 60.0)
        self.assertEqual(positive.toggle_off_timeout_seconds, 12.5)
        self.assertEqual(disabled.toggle_off_timeout_seconds, 0)

    def test_on_and_off_events_must_be_disjoint_by_numeric_event_code(self):
        draft = EvdevTriggerDraft(
            mode="on-off",
            on_events=("BTN_SIDE",),
            off_events=("BTN_EXTRA",),
        )

        self.assertEqual(draft.on_events, ("BTN_SIDE",))
        self.assertEqual(draft.off_events, ("BTN_EXTRA",))

        with self.assertRaisesRegex(ConfigError, "must not overlap"):
            EvdevTriggerDraft(
                mode="on-off",
                on_events=("KEY_HANGEUL",),
                off_events=("KEY_HANGUEL",),
            )


class HidrawCaptureTests(unittest.TestCase):
    def capture(
        self,
        on_trials: tuple[tuple[bytes, ...], ...],
        off_trials: tuple[tuple[bytes, ...], ...],
        *,
        baseline: tuple[bytes, ...] = (b"common",),
    ) -> tuple[HidrawTriggerDraft, FakeClock, list[tuple[str, int]], FakeHidrawStream]:
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock, hidraw_reports(on_trials, off_trials, baseline=baseline)
        )
        arms: list[tuple[str, int]] = []

        draft = capture_hidraw(
            stream,
            arm_trial=lambda direction, number: arms.append((direction, number)),
            clock=clock,
        )

        return draft, clock, arms, stream

    def test_records_a_two_second_baseline_then_captures_three_alternating_trials(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            hidraw_reports(
                ((b"common", b"on", b"on"),) * 3,
                ((b"common", b"off"),) * 3,
            ),
        )
        arms: list[tuple[str, int]] = []
        arm_times: list[float] = []

        draft = capture_hidraw(
            stream,
            arm_trial=lambda direction, number: (
                arms.append((direction, number)),
                arm_times.append(clock.monotonic()),
            ),
            clock=clock,
        )

        self.assertEqual(
            draft, HidrawTriggerDraft(on_reports=(b"on",), off_reports=(b"off",))
        )
        self.assertEqual(
            arms,
            [("on", 1), ("off", 1), ("on", 2), ("off", 2), ("on", 3), ("off", 3)],
        )
        self.assertEqual(arm_times[0], 2.0)
        self.assertEqual(stream.timeouts[0], 2.0)
        self.assertAlmostEqual(clock.monotonic(), 5.15)

    def test_uses_fake_clock_for_silence_without_wall_clock_sleep(self):
        draft, clock, arms, stream = self.capture(
            ((b"on",),) * 3,
            ((b"off",),) * 3,
        )

        self.assertEqual(draft.on_reports, (b"on",))
        self.assertEqual(arms[1], ("off", 1))
        self.assertAlmostEqual(clock.monotonic(), 5.06)
        self.assertTrue(all(timeout >= 0 for timeout in stream.timeouts))
        self.assertNotIn("sleep", inspect.getsource(capture_hidraw))

    def test_times_out_when_an_armed_trial_has_no_nonbaseline_report(self):
        clock = FakeClock()
        stream = FakeHidrawStream(clock, [TimedReport(0.1, b"common")])
        arms: list[tuple[str, int]] = []

        with self.assertRaises(CaptureTimeout):
            capture_hidraw(
                stream,
                arm_trial=lambda direction, number: arms.append((direction, number)),
                clock=clock,
            )

        self.assertEqual(arms, [("on", 1)])
        self.assertEqual(clock.monotonic(), 7.0)

    def test_ends_each_trial_after_five_hundred_milliseconds_of_silence(self):
        _, _, arms, _ = self.capture(
            ((b"on",),) * 3,
            ((b"off",),) * 3,
        )

        self.assertEqual(
            arms,
            [("on", 1), ("off", 1), ("on", 2), ("off", 2), ("on", 3), ("off", 3)],
        )

    def test_baseline_chatter_after_a_candidate_does_not_extend_candidate_silence(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            [
                TimedReport(0.1, b"common"),
                TimedReport(0.2, b"common"),
                TimedReport(0.3, b"common"),
            ],
        )

        reports = _collect_trial_reports(
            stream,
            clock,
            {b"common"},
            HidrawTiming(),
            b"on",
        )

        self.assertEqual(reports, frozenset({b"on"}))
        self.assertEqual(clock.monotonic(), 0.5)

    def test_rejects_first_nonbaseline_report_at_the_first_report_deadline(self):
        clock = FakeClock()
        stream = FakeHidrawStream(clock, [TimedReport(1.0, b"on")])

        with self.assertRaises(CaptureTimeout):
            _capture_hidraw_trial(
                stream,
                clock,
                set(),
                HidrawTiming(first_report_timeout_seconds=1.0),
            )

        self.assertEqual(clock.monotonic(), 1.0)

    def test_nonbaseline_report_at_the_silence_deadline_does_not_extend_the_trial(self):
        clock = FakeClock()
        stream = FakeHidrawStream(clock, [TimedReport(0.5, b"late")])

        reports = _collect_trial_reports(
            stream,
            clock,
            set(),
            HidrawTiming(),
            b"on",
        )

        self.assertEqual(reports, frozenset({b"on"}))
        self.assertEqual(clock.monotonic(), 0.5)

    def test_hard_deadline_ends_an_otherwise_quiet_trial(self):
        clock = FakeClock()
        stream = FakeHidrawStream(clock, [])

        reports = _collect_trial_reports(
            stream,
            clock,
            set(),
            HidrawTiming(silence_seconds=0.5, trial_hard_limit_seconds=0.5),
            b"on",
        )

        self.assertEqual(reports, frozenset({b"on"}))
        self.assertEqual(clock.monotonic(), 0.5)

    def test_completed_silence_wins_when_default_silence_and_hard_deadlines_match(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            [
                TimedReport(0.49, b"on"),
                TimedReport(0.98, b"on"),
                TimedReport(1.47, b"on"),
                TimedReport(1.5, b"on"),
            ],
        )

        reports = _collect_trial_reports(
            stream,
            clock,
            set(),
            HidrawTiming(),
            b"on",
        )

        self.assertEqual(reports, frozenset({b"on"}))
        self.assertEqual(clock.monotonic(), 2.0)

    def test_nonbaseline_activity_at_the_hard_deadline_is_noisy(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            [
                TimedReport(0.49, b"on"),
                TimedReport(0.98, b"on"),
                TimedReport(1.0, b"on"),
            ],
        )

        with self.assertRaises(NoisyCapture):
            _collect_trial_reports(
                stream,
                clock,
                set(),
                HidrawTiming(trial_hard_limit_seconds=1.0),
                b"on",
            )

        self.assertEqual(clock.monotonic(), 1.0)

    def test_rejects_a_trial_that_remains_active_beyond_the_two_second_limit(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            [
                TimedReport(0.1, b"common"),
                *(TimedReport(2.01 + index * 0.49, b"on") for index in range(5)),
            ],
        )

        with self.assertRaises(NoisyCapture):
            capture_hidraw(
                stream, arm_trial=lambda _direction, _number: None, clock=clock
            )

        self.assertEqual(clock.monotonic(), 4.01)

    def test_rejects_inconsistent_trial_report_sets(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            hidraw_reports(
                ((b"on",), (b"other-on",), (b"on",)),
                ((b"off",),) * 3,
            ),
        )

        with self.assertRaises(InconsistentCapture):
            capture_hidraw(
                stream, arm_trial=lambda _direction, _number: None, clock=clock
            )

    def test_rejects_empty_results_after_baseline_filtering(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            hidraw_reports(((b"common",),) * 3, ((b"common",),) * 3),
        )

        with self.assertRaises(CaptureTimeout):
            capture_hidraw(
                stream, arm_trial=lambda _direction, _number: None, clock=clock
            )

    def test_removes_shared_reports_and_rejects_an_empty_direction_result(self):
        clock = FakeClock()
        stream = FakeHidrawStream(
            clock,
            hidraw_reports(((b"shared",),) * 3, ((b"shared",),) * 3),
        )

        with self.assertRaises(InconsistentCapture):
            capture_hidraw(
                stream, arm_trial=lambda _direction, _number: None, clock=clock
            )

    def test_sorts_direction_reports_for_deterministic_toml(self):
        draft, _, _, _ = self.capture(
            ((b"z-on", b"a-on"), (b"a-on", b"z-on"), (b"z-on", b"a-on")),
            ((b"z-off", b"a-off"), (b"a-off", b"z-off"), (b"z-off", b"a-off")),
        )

        self.assertEqual(draft.on_reports, (b"a-on", b"z-on"))
        self.assertEqual(draft.off_reports, (b"a-off", b"z-off"))


class ApplyDeviceDraftTests(unittest.TestCase):
    def test_replaces_existing_profile_in_place(self):
        document = tomlkit.parse(
            '[actions.voice]\non_command = ["on"]\noff_command = ["off"]\n'
            '[[devices]]\nname = "Mouse"\naction = "voice"\n'
            'transport = "evdev"\nmode = "toggle"\nvendor_id = "1234"\n'
            'product_id = "5678"\ntoggle_events = ["BTN_EXTRA"]\n'
        )
        apply_device_draft(
            document,
            name="Mouse",
            action="voice",
            selectors=SelectorDraft(
                "evdev",
                "1234",
                "5678",
                None,
                classifier=("ID_INPUT_MOUSE", "1"),
            ),
            trigger=EvdevTriggerDraft(mode="toggle", toggle_events=("BTN_SIDE",)),
            replace_name="Mouse",
        )

        self.assertEqual(len(document["devices"]), 1)
        self.assertEqual(document["devices"][0]["toggle_events"], ["BTN_SIDE"])
        self.assertEqual(document["devices"][0]["input_classifier"], "ID_INPUT_MOUSE")

    def test_adds_hidraw_reports_and_runtime_selectors_without_udev_classifier(self):
        document = tomlkit.parse(
            "# Keep this comment.\n"
            "[actions.voice]\n"
            'on_command = ["on"]\n'
            'off_command = ["off"]\n'
        )
        selectors = SelectorDraft(
            transport="hidraw",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            serial="desk-headset",
            id_path="pci-0000:00:14.0-usb-0:2:1.3",
            classifier=("ID_INPUT_MOUSE", "1"),
        )

        apply_device_draft(
            document,
            name="Desk headset",
            action="voice",
            selectors=selectors,
            trigger=HidrawTriggerDraft(
                on_reports=(b"\x08\x02",),
                off_reports=(b"\x08\x00",),
            ),
        )

        device = document["devices"][0]
        self.assertEqual(device["transport"], "hidraw")
        self.assertEqual(device["mode"], "on-off")
        self.assertEqual(list(device["on_reports"]), ["08 02"])
        self.assertEqual(list(device["off_reports"]), ["08 00"])
        self.assertNotIn("classifier", device)
        self.assertEqual(document["devices"][0]["serial"], "desk-headset")
        self.assertIn("# Keep this comment.", tomlkit.dumps(document))

    def test_adds_evdev_trigger_fields_that_match_the_selected_mode(self):
        document = tomlkit.parse(
            '[actions.voice]\non_command = ["on"]\noff_command = ["off"]\n'
        )
        selectors = SelectorDraft(
            "evdev",
            "1234",
            "5678",
            None,
            classifier=("ID_INPUT_MOUSE", "1"),
        )

        apply_device_draft(
            document,
            name="Desk button",
            action="voice",
            selectors=selectors,
            trigger=EvdevTriggerDraft(
                mode="toggle",
                toggle_events=("BTN_SIDE",),
                toggle_off_timeout_seconds=0,
            ),
        )

        device = document["devices"][0]
        self.assertEqual(device["transport"], "evdev")
        self.assertEqual(device["input_classifier"], "ID_INPUT_MOUSE")
        self.assertEqual(device["toggle_events"], ["BTN_SIDE"])
        self.assertEqual(device["toggle_off_timeout_seconds"], 0)
        self.assertNotIn("interface_number", device)


if __name__ == "__main__":
    unittest.main()

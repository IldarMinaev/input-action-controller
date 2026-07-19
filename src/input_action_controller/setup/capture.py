from dataclasses import dataclass
from typing import Callable, Protocol

from evdev import ecodes
import tomlkit

from ..config import parse_config
from .devices import SelectorDraft


@dataclass(frozen=True)
class RawEvdevEvent:
    type: int
    code: int
    value: int


class EvdevStream(Protocol):
    def read(self, timeout_seconds: float) -> RawEvdevEvent | None: ...


class ReportStream(Protocol):
    def read(self, timeout_seconds: float) -> bytes | None: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...


class HidrawCaptureError(ValueError):
    """Base class for hidraw capture failures that the setup flow can retry."""


class CaptureTimeout(HidrawCaptureError):
    """Raised when a capture phase does not receive a required report in time."""


class NoisyCapture(HidrawCaptureError):
    """Raised when a trial does not become silent before its hard limit."""


class InconsistentCapture(HidrawCaptureError):
    """Raised when trial report sets cannot identify distinct directions."""


@dataclass(frozen=True)
class HidrawTiming:
    baseline_seconds: float = 2.0
    first_report_timeout_seconds: float = 5.0
    silence_seconds: float = 0.5
    trial_hard_limit_seconds: float = 2.0


@dataclass(frozen=True)
class HidrawTriggerDraft:
    on_reports: tuple[bytes, ...]
    off_reports: tuple[bytes, ...]


@dataclass(frozen=True)
class EvdevTriggerDraft:
    mode: str
    on_events: tuple[str, ...] = ()
    off_events: tuple[str, ...] = ()
    toggle_events: tuple[str, ...] = ()
    toggle_off_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        parse_config(_validation_config(self))


def evdev_press_name(event_type: int, code: int, value: int) -> str | None:
    """Return a symbolic event name only for a key or button press."""
    if event_type != ecodes.EV_KEY or value != 1:
        return None
    name = ecodes.KEY.get(code) or ecodes.bytype.get(ecodes.EV_KEY, {}).get(code)
    names = (name,) if isinstance(name, str) else name
    if not isinstance(names, (list, tuple)):
        return None
    return next(
        (
            candidate
            for candidate in sorted(names)
            if isinstance(candidate, str) and candidate.startswith(("KEY_", "BTN_"))
        ),
        None,
    )


def capture_evdev_presses(
    stream: EvdevStream,
    *,
    timeout_seconds: float,
) -> tuple[str, ...]:
    """Read deduplicated symbolic presses until the stream times out."""
    if timeout_seconds <= 0:
        raise ValueError("capture timeout must be positive")

    names: list[str] = []
    seen: set[str] = set()
    while (event := stream.read(timeout_seconds)) is not None:
        name = evdev_press_name(event.type, event.code, event.value)
        if name is not None and name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)


def capture_hidraw(
    stream: ReportStream,
    *,
    arm_trial: Callable[[str, int], None],
    clock: Clock,
    timing: HidrawTiming = HidrawTiming(),
) -> HidrawTriggerDraft:
    """Capture stable direction-specific hidraw reports with bounded trials."""
    _validate_hidraw_timing(timing)
    baseline = _capture_baseline(stream, clock, timing.baseline_seconds)
    trials = {"on": [], "off": []}

    for number in range(1, 4):
        for direction in ("on", "off"):
            arm_trial(direction, number)
            trials[direction].append(
                _capture_hidraw_trial(stream, clock, baseline, timing)
            )

    on_reports = _stable_trial_reports(trials["on"])
    off_reports = _stable_trial_reports(trials["off"])
    shared_reports = on_reports & off_reports
    on_reports -= shared_reports
    off_reports -= shared_reports
    if not on_reports or not off_reports or on_reports & off_reports:
        raise InconsistentCapture("hidraw directions do not have distinct reports")

    return HidrawTriggerDraft(
        on_reports=tuple(sorted(on_reports)),
        off_reports=tuple(sorted(off_reports)),
    )


def apply_device_draft(
    document: tomlkit.TOMLDocument,
    *,
    name: str,
    action: str,
    selectors: SelectorDraft,
    trigger: EvdevTriggerDraft | HidrawTriggerDraft,
) -> None:
    """Add a captured device profile to a TomlKit document."""
    devices = document.get("devices")
    if devices is None:
        devices = tomlkit.aot()
        document["devices"] = devices
    if not isinstance(devices, list):
        raise ValueError("devices must be a TOML array")
    if any(
        device.get("name") == name for device in devices if isinstance(device, dict)
    ):
        raise ValueError(f"duplicate device name: {name}")

    device = tomlkit.table()
    device["name"] = name
    device["action"] = action
    device["transport"] = selectors.transport
    device["vendor_id"] = selectors.vendor_id
    device["product_id"] = selectors.product_id
    for key, value in (
        ("interface_number", selectors.interface_number),
        ("serial", selectors.serial),
        ("id_path", selectors.id_path),
    ):
        if value is not None:
            device[key] = value

    if isinstance(trigger, HidrawTriggerDraft):
        if selectors.transport != "hidraw":
            raise ValueError("hidraw trigger requires a hidraw selector")
        device["mode"] = "on-off"
        device["on_reports"] = _toml_array(
            report.hex(" ") for report in trigger.on_reports
        )
        device["off_reports"] = _toml_array(
            report.hex(" ") for report in trigger.off_reports
        )
    else:
        if selectors.transport != "evdev":
            raise ValueError("evdev trigger requires an evdev selector")
        device["mode"] = trigger.mode
        if trigger.mode == "on-off":
            device["on_events"] = _toml_array(trigger.on_events)
            device["off_events"] = _toml_array(trigger.off_events)
        else:
            device["toggle_events"] = _toml_array(trigger.toggle_events)
            device["toggle_off_timeout_seconds"] = trigger.toggle_off_timeout_seconds

    devices.append(device)


def _capture_baseline(
    stream: ReportStream,
    clock: Clock,
    baseline_seconds: float,
) -> set[bytes]:
    deadline = clock.monotonic() + baseline_seconds
    reports: set[bytes] = set()
    while clock.monotonic() < deadline:
        report = stream.read(deadline - clock.monotonic())
        if report is not None:
            reports.add(report)
    return reports


def _capture_hidraw_trial(
    stream: ReportStream,
    clock: Clock,
    baseline: set[bytes],
    timing: HidrawTiming,
) -> frozenset[bytes]:
    first_report_deadline = clock.monotonic() + timing.first_report_timeout_seconds
    while clock.monotonic() < first_report_deadline:
        report = stream.read(first_report_deadline - clock.monotonic())
        if (
            report is not None
            and report not in baseline
            and clock.monotonic() < first_report_deadline
        ):
            return _collect_trial_reports(stream, clock, baseline, timing, report)
    raise CaptureTimeout("timed out waiting for a non-baseline hidraw report")


def _collect_trial_reports(
    stream: ReportStream,
    clock: Clock,
    baseline: set[bytes],
    timing: HidrawTiming,
    first_report: bytes,
) -> frozenset[bytes]:
    reports = {first_report}
    hard_deadline = clock.monotonic() + timing.trial_hard_limit_seconds
    silence_deadline = clock.monotonic() + timing.silence_seconds
    saw_followup_candidate = False
    while True:
        deadline = min(hard_deadline, silence_deadline)
        report = stream.read(deadline - clock.monotonic())
        now = clock.monotonic()
        if now >= hard_deadline:
            if now >= silence_deadline and (report is None or report in baseline):
                return frozenset(reports)
            if (
                report is not None and report not in baseline
            ) or saw_followup_candidate:
                raise NoisyCapture("hidraw trial remained active beyond its hard limit")
            return frozenset(reports)
        if now >= silence_deadline or report is None:
            return frozenset(reports)
        if report not in baseline:
            reports.add(report)
            silence_deadline = now + timing.silence_seconds
            saw_followup_candidate = True


def _stable_trial_reports(trials: list[frozenset[bytes]]) -> set[bytes]:
    first_trial = trials[0]
    if any(trial != first_trial for trial in trials[1:]):
        raise InconsistentCapture("hidraw trial report sets differ")
    return set(first_trial)


def _toml_array(values) -> tomlkit.items.Array:
    array = tomlkit.array()
    array.extend(values)
    return array


def _validate_hidraw_timing(timing: HidrawTiming) -> None:
    if (
        timing.baseline_seconds <= 0
        or timing.first_report_timeout_seconds <= 0
        or timing.silence_seconds <= 0
        or timing.trial_hard_limit_seconds <= 0
    ):
        raise ValueError("hidraw capture timing values must be positive")


def _validation_config(draft: EvdevTriggerDraft) -> dict:
    device = {
        "name": "captured-evdev-device",
        "action": "captured-action",
        "transport": "evdev",
        "mode": draft.mode,
        "vendor_id": "0000",
        "product_id": "0000",
    }
    if draft.mode == "on-off":
        device["on_events"] = list(draft.on_events)
        device["off_events"] = list(draft.off_events)
        if draft.toggle_events:
            device["toggle_events"] = list(draft.toggle_events)
        if draft.toggle_off_timeout_seconds != 60.0:
            device["toggle_off_timeout_seconds"] = draft.toggle_off_timeout_seconds
    elif draft.mode == "toggle":
        device["toggle_events"] = list(draft.toggle_events)
        device["toggle_off_timeout_seconds"] = draft.toggle_off_timeout_seconds
        if draft.on_events:
            device["on_events"] = list(draft.on_events)
        if draft.off_events:
            device["off_events"] = list(draft.off_events)

    return {
        "actions": {
            "captured-action": {
                "on_command": ["true"],
                "off_command": ["true"],
            }
        },
        "devices": [device],
    }

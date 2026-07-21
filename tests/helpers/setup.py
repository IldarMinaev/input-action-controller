from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from evdev import ecodes

from input_action_controller.devices.discovery import DeviceCandidate
from input_action_controller.setup.permissions import RenderedRule


def candidate(
    node: str,
    *,
    transport: str = "evdev",
    readable: bool = True,
    keyboard_class: bool = False,
    event_names: tuple[str, ...] | None = None,
    input_classifiers: tuple[tuple[str, str], ...] | None = None,
) -> DeviceCandidate:
    subsystem = "input" if transport == "evdev" else "hidraw"
    properties = {
        "DEVNAME": node,
        "ID_VENDOR_ID": "1234",
        "ID_MODEL_ID": "5678",
        "ID_USB_INTERFACE_NUM": "01",
        "ID_MODEL": "Desk control",
        "CURRENT_TAGS": ":seat:uaccess:",
    }
    classifiers = ()
    if transport == "evdev":
        classifiers = (
            input_classifiers
            if input_classifiers is not None
            else (("ID_INPUT_MOUSE", "1"),)
        )
        properties.update(classifiers)
    return DeviceCandidate(
        node=node,
        subsystem=subsystem,
        properties=properties,
        event_codes=(
            frozenset(
                ecodes.ecodes[name]
                for name in (event_names if event_names is not None else ("BTN_SIDE",))
            )
            if readable and transport == "evdev"
            else frozenset()
            if readable
            else None
        ),
        keyboard_class=keyboard_class,
        display_name="Desk control",
        classifiers=classifiers,
    )


class FakeDiscovery:
    def __init__(self, candidates=(), *, events=None, snapshots=()):
        self.candidates = tuple(candidates)
        self.events = events
        self.snapshots = deque(snapshots)
        self.calls = 0

    def enumerate(self):
        self.calls += 1
        if self.events is not None:
            self.events.append("discovery.enumerate")
        return self.snapshots.popleft() if self.snapshots else self.candidates

    def set(self, *candidates):
        self.candidates = tuple(candidates)


@dataclass
class FakeClock:
    now: float = 0.0

    @property
    def value(self) -> float:
        return self.now

    @value.setter
    def value(self, new_value: float) -> None:
        self.now = new_value

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePermissionTransaction:
    def __init__(
        self,
        rendered: RenderedRule,
        *,
        events: list[str] | None = None,
        install_error: Exception | None = None,
        reload_error: Exception | None = None,
        rollback_error: Exception | None = None,
        finalize_error: Exception | None = None,
        previous_access: str | None = None,
    ):
        self.rendered = rendered
        self.obsolete_destinations: tuple[Path, ...] = ()
        self.rollback_verification = None
        self.rollback_reconnect_required = False
        self.events = events
        self.install_error = install_error
        self.reload_error = reload_error
        self.rollback_error = rollback_error
        self.finalize_error = finalize_error
        self.previous_access = previous_access
        self.prepared = False
        self.install_started = False
        self.destination_write_succeeded = False
        self.installed = False
        self.rolled_back = False
        self.finalized = False
        self.preview_command_lines = (
            "/usr/bin/sudo -- /usr/bin/install -m 0644 /tmp/staged.rules "
            f"{rendered.destination}",
            "/usr/bin/sudo -- /usr/bin/udevadm control --reload-rules",
        )
        self.recovery_command_lines = (
            f"/usr/bin/sudo -- /usr/bin/rm -f -- {rendered.destination}",
            "/usr/bin/sudo -- /usr/bin/udevadm control --reload-rules",
        )

    @property
    def remaining_scope(self):
        return (
            self.rendered.scope
            if self.install_started and not self.rolled_back
            else None
        )

    @property
    def destination_changed(self):
        return self.install_started and not self.rolled_back

    def prepare(self):
        self.prepared = True
        if self.events is not None:
            self.events.append("permission.prepare")

    def install(self):
        self.install_started = True
        if self.events is not None:
            self.events.append("permission.install")
        if self.install_error is not None:
            raise self.install_error
        self.destination_write_succeeded = True
        if self.reload_error is not None:
            raise self.reload_error
        self.installed = True

    def rollback(self):
        if self.events is not None:
            self.events.append("permission.rollback")
        if self.rollback_error is not None:
            raise self.rollback_error
        self.rolled_back = True
        self.installed = False

    def finalize(self):
        if self.events is not None:
            self.events.append("permission.finalize")
        if self.finalize_error is not None:
            raise self.finalize_error
        self.finalized = True

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evdev import ecodes

from input_action_controller.config import load_config
from input_action_controller.devices.discovery import DeviceCandidate
from input_action_controller.setup.actions import (
    ActionCommandStyle,
    ActionDraft,
)
from input_action_controller.setup.capture import (
    EvdevTriggerDraft,
    RawEvdevEvent,
)
from input_action_controller.setup.config_editor import (
    BackupInfo,
    ConfigCommitError,
    ConfigEditor,
)
from input_action_controller.setup.devices import SelectorDraft
from input_action_controller.setup.permissions import (
    DeviceInstanceObservation,
    INPUT_GROUP_ACCESS,
    ReconnectVerificationError,
    UACCESS,
    render_rule,
    verify_reconnected_access,
)
from input_action_controller.setup.service import ServiceChoice, ServiceSnapshot
from input_action_controller.setup.session import (
    PostCommitChoice,
    SetupPermissionFactory,
    SetupCancelled,
    SetupDependencies,
    SetupExitRequested,
    SetupSession,
    _has_input_group_mode_access,
    _has_current_user_acl,
    run_setup,
)
from tests.helpers.setup import (
    FakeClock,
    FakeDiscovery,
    FakePermissionTransaction,
    candidate,
)


EXISTING_CONFIG = """# Keep the top-level comment.
[actions.voice]
# Keep the existing action comment.
on_command = ["/usr/bin/voice", "--on"]
off_command = ["/usr/bin/voice", "--off"]

[[devices]]
# Keep the existing device comment.
name = "Existing headset"
action = "voice"
transport = "hidraw"
mode = "on-off"
vendor_id = "047f"
product_id = "c056"
on_reports = ["08 02"]
off_reports = ["08 00"]
"""


def reconnected_discovery(
    selected: DeviceCandidate,
    reconnected: DeviceCandidate,
    *,
    events: list[str] | None = None,
) -> FakeDiscovery:
    return FakeDiscovery(
        (selected,),
        events=events,
        snapshots=((selected,), (reconnected,)),
    )


class FakeUdevDevice:
    def __init__(self, node, instance_id, *, action=None, sequence_number=None):
        self.node = node
        self.sys_path = instance_id
        self.action = action
        self.sequence_number = sequence_number
        self.device_node = node

    def get(self, name, default=None):
        if name == "DEVNAME":
            return self.node
        if name == "SEQNUM" and self.sequence_number is not None:
            return str(self.sequence_number)
        return default


class FakeUdevContext:
    def __init__(self, devices):
        self.devices = tuple(devices)

    def list_devices(self, *, subsystem):
        return self.devices


class FakeUdevMonitor:
    def __init__(self, events, *, on_poll=None):
        self.events = deque(events if isinstance(events, tuple) else (events,))
        self.on_poll = on_poll
        self.subsystems = []
        self.timeouts = []
        self.started = False

    def filter_by(self, subsystem, /):
        self.subsystems.append(subsystem)

    def start(self):
        self.started = True

    def poll(self, *, timeout):
        self.timeouts.append(timeout)
        event = self.events.popleft() if self.events else None
        if self.on_poll is not None:
            self.on_poll()
        return event


class FakeEvdevStream:
    def __init__(self, events=(), *, error: Exception | None = None):
        self.events = deque(events)
        self.error = error
        self.closed = False

    def read(self, _timeout_seconds: float):
        if self.error is not None:
            raise self.error
        return self.events.popleft() if self.events else None

    def close(self):
        self.closed = True


def side_button_stream() -> FakeEvdevStream:
    return FakeEvdevStream(
        (RawEvdevEvent(ecodes.EV_KEY, ecodes.ecodes["BTN_SIDE"], 1),)
    )


@dataclass(frozen=True)
class TimedReport:
    at: float
    report: bytes


class FakeReportStream:
    def __init__(self, clock: FakeClock, reports):
        self.clock = clock
        self.reports = deque(reports)
        self.closed = False

    def read(self, timeout_seconds: float):
        deadline = self.clock.value + timeout_seconds
        if self.reports and self.reports[0].at < deadline:
            item = self.reports.popleft()
            self.clock.value = item.at
            return item.report
        self.clock.value = deadline
        return None

    def close(self):
        self.closed = True


def stable_hidraw_reports() -> tuple[TimedReport, ...]:
    return (
        TimedReport(2.1, b"on"),
        TimedReport(2.7, b"off"),
        TimedReport(3.3, b"on"),
        TimedReport(3.9, b"off"),
        TimedReport(4.5, b"on"),
        TimedReport(5.1, b"off"),
    )


class FakeCaptureFactory:
    def __init__(
        self,
        streams,
        *,
        clock: FakeClock | None = None,
        events: list[str] | None = None,
    ):
        self.streams = deque(streams)
        self.clock = clock or FakeClock()
        self.events = events
        self.opened_candidates: list[DeviceCandidate] = []

    def open(self, selected):
        self.opened_candidates.append(selected)
        if self.events is not None:
            self.events.append("capture.open")
        stream = self.streams.popleft()
        if self.events is not None:
            original_close = stream.close

            def close():
                self.events.append("capture.close")
                original_close()

            stream.close = close
        return stream


class FakePermissionFactory:
    def __init__(
        self,
        reconnected: DeviceCandidate,
        *,
        events: list[str] | None = None,
        install_error: Exception | None = None,
        reload_error: Exception | None = None,
        verify_error: Exception | None = None,
        rollback_error: Exception | None = None,
        finalize_error: Exception | None = None,
        obsolete_destinations: tuple[Path, ...] = (),
        previous_access: str | None = None,
    ):
        self.reconnected = reconnected
        self.events = events
        self.install_error = install_error
        self.reload_error = reload_error
        self.verify_error = verify_error
        self.rollback_error = rollback_error
        self.finalize_error = finalize_error
        self.obsolete_destinations = obsolete_destinations
        self.previous_access = previous_access
        self.transaction: FakePermissionTransaction | None = None
        self.create_calls = []
        self.snapshot_calls = []
        self.monitor_calls = []
        self.verify_calls = []
        self.access = UACCESS
        self.observer_calls = 0
        self.access_calls = 0
        self.acl_calls = 0
        self.resolution_calls = 0

    def create(
        self,
        profile_name,
        selectors,
        candidates,
        *,
        access=UACCESS,
        replace_profile_name=None,
    ):
        if self.events is not None:
            self.events.append("permission.create")
        self.create_calls.append(
            (
                profile_name,
                selectors,
                tuple(candidates),
                access,
                replace_profile_name,
            )
        )
        self.access = access
        rendered = render_rule(profile_name, selectors, candidates, access=access)
        self.transaction = FakePermissionTransaction(
            rendered,
            events=self.events,
            install_error=self.install_error,
            reload_error=self.reload_error,
            rollback_error=self.rollback_error,
            finalize_error=self.finalize_error,
            previous_access=self.previous_access,
        )
        self.transaction.obsolete_destinations = self.obsolete_destinations
        return self.transaction

    def begin_reconnect_monitor(self, selectors):
        if self.events is not None:
            self.events.append("permission.monitor")
        monitor = object()
        self.monitor_calls.append((selectors, monitor))
        return monitor

    def verify_reconnected(self, selectors, reconnect_monitor, *, access=UACCESS):
        if self.events is not None:
            self.events.append("permission.verify")
        self.verify_calls.append((selectors, reconnect_monitor, access))
        if self.verify_error is not None:
            raise self.verify_error

        def observe():
            self.observer_calls += 1
            return DeviceInstanceObservation(self.reconnected, "new-instance")

        def access_checker(_candidate):
            self.access_calls += 1
            return True

        def acl(_candidate):
            self.acl_calls += 1
            return True

        def production_resolution(_candidate):
            self.resolution_calls += 1
            return True

        return verify_reconnected_access(
            selectors,
            previous_instance_ids=(),
            observe_after_udev_event=observe,
            access_checker=access_checker,
            acl_checker=acl,
            production_resolution_checker=production_resolution,
            access=access,
            group_mode_checker=lambda _candidate: True,
        )


class FakeService:
    def __init__(
        self,
        snapshot=ServiceSnapshot(enabled=True, active=True),
        *,
        events: list[str] | None = None,
        apply_failures: int = 0,
        apply_failure_state: ServiceSnapshot | None = None,
        restore_error: Exception | None = None,
        snapshot_errors=(),
        snapshot_error_after: int | None = None,
    ):
        self.state = snapshot
        self.original = snapshot
        self.events = events
        self.apply_failures = apply_failures
        self.apply_failure_state = apply_failure_state
        self.restore_error = restore_error
        self.snapshot_errors = deque(snapshot_errors)
        self.snapshot_error_after = snapshot_error_after
        self.snapshot_calls = 0
        self.stop_calls = 0
        self.apply_calls: list[ServiceChoice] = []
        self.restore_calls: list[ServiceSnapshot] = []

    @property
    def manual_daemon_command(self):
        return "input-action-controller --config /custom.toml daemon"

    def snapshot(self):
        if self.events is not None:
            self.events.append("service.snapshot")
        self.snapshot_calls += 1
        if self.snapshot_errors and (
            self.snapshot_error_after is None
            or self.snapshot_calls > self.snapshot_error_after
        ):
            raise self.snapshot_errors.popleft()
        return self.state

    def stop_for_setup(self, snapshot):
        if self.events is not None:
            self.events.append("service.stop")
        self.stop_calls += 1
        if snapshot.active:
            self.state = ServiceSnapshot(snapshot.enabled, False)

    def apply(self, choice):
        if self.events is not None:
            self.events.append("service.apply")
        self.apply_calls.append(choice)
        if self.apply_failures:
            self.apply_failures -= 1
            if self.apply_failure_state is not None:
                self.state = self.apply_failure_state
            raise RuntimeError("injected service apply failure")
        if choice is ServiceChoice.RESTART:
            self.state = ServiceSnapshot(self.state.enabled, True)
        elif choice is ServiceChoice.ENABLE_AND_START:
            self.state = ServiceSnapshot(True, True)
        elif choice in {
            ServiceChoice.PRESERVE_STOPPED,
            ServiceChoice.PRESERVE_INACTIVE,
        }:
            self.state = ServiceSnapshot(self.state.enabled, False)

    def restore(self, snapshot):
        if self.events is not None:
            self.events.append("service.restore")
        self.restore_calls.append(snapshot)
        if self.restore_error is not None:
            raise self.restore_error
        self.state = snapshot


class RecordingConfigTransaction:
    def __init__(
        self,
        transaction,
        *,
        events: list[str] | None = None,
        commit_error: Exception | None = None,
        restore_error: Exception | None = None,
    ):
        self.transaction = transaction
        self.events = events
        self.commit_error = commit_error
        self.restore_error = restore_error
        self.committed = False
        self.restored = False
        self.finalized = False

    def commit(self, document):
        if self.events is not None:
            self.events.append("config.commit")
        if self.commit_error is not None:
            raise self.commit_error
        self.transaction.commit(document)
        self.committed = True

    def restore(self):
        if self.events is not None:
            self.events.append("config.restore")
        if self.restore_error is not None:
            raise self.restore_error
        self.transaction.restore()
        self.restored = True

    def finalize(self):
        if self.events is not None:
            self.events.append("config.finalize")
        self.transaction.finalize()
        self.finalized = True


class RecordingConfigEditor:
    def __init__(
        self,
        editor: ConfigEditor,
        *,
        events: list[str] | None = None,
        commit_error: Exception | None = None,
        restore_error: Exception | None = None,
        backup_restore_error: Exception | None = None,
        backup_transaction=None,
    ):
        self.editor = editor
        self.events = events
        self.commit_error = commit_error
        self.restore_error = restore_error
        self.backup_restore_error = backup_restore_error
        self.backup_transaction = backup_transaction
        self.transaction: RecordingConfigTransaction | None = None
        self.backups: tuple[BackupInfo, ...] = ()
        self.restored_backups: list[BackupInfo] = []

    def load(self, explicit):
        if self.events is not None:
            self.events.append("config.load")
        return self.editor.load(explicit)

    def begin(self, editable):
        if self.events is not None:
            self.events.append("config.begin")
        self.transaction = RecordingConfigTransaction(
            self.editor.begin(editable),
            events=self.events,
            commit_error=self.commit_error,
            restore_error=self.restore_error,
        )
        return self.transaction

    def list_backups(self, _destination):
        return self.backups

    def restore_backup(self, _editable, backup):
        self.restored_backups.append(backup)
        if self.backup_restore_error is not None:
            raise self.backup_restore_error
        return self.backup_transaction


def new_action(name: str = "voice") -> ActionDraft:
    return ActionDraft(
        name=name,
        on_command=("/usr/bin/voice", "--on"),
        off_command=("/usr/bin/voice", "--off"),
        command_style=ActionCommandStyle.SEPARATE,
    )


class FakePrompts:
    def __init__(
        self,
        *,
        action: ActionDraft | None = None,
        selected: DeviceCandidate,
        profile_name: str = "Desk button",
        evdev_trigger: EvdevTriggerDraft | None = None,
        service_choice: ServiceChoice = ServiceChoice.RESTART,
        post_commit_choices=(),
        post_commit_error: Exception | None = None,
        fail_at: str | None = None,
        cancellation_error: SetupCancelled | None = None,
        confirm_permission: bool = True,
        confirm_managed_permission: bool = False,
        permission_access: str = UACCESS,
        backup: BackupInfo | None = None,
        events: list[str] | None = None,
    ):
        self.action = action or new_action()
        self.selected = selected
        self.profile_name = profile_name
        self.evdev_trigger = evdev_trigger or EvdevTriggerDraft(
            mode="toggle",
            toggle_events=("BTN_SIDE",),
        )
        self.service_choice = service_choice
        self.post_commit_choices = deque(post_commit_choices)
        self.post_commit_error = post_commit_error
        self.fail_at = fail_at
        self.cancellation_error = cancellation_error
        self.confirm_permission_result = confirm_permission
        self.confirm_managed_permission_result = confirm_managed_permission
        self.permission_access = permission_access
        self.backup = backup
        self.events = events
        self.captured_events = ()
        self.armed_trials = []
        self.errors: list[str] = []
        self.recovery_failures: list[tuple[str, str]] = []
        self.recovery_failure_transactions = []
        self.post_commit_recoveries = []
        self.manual_permission = []
        self.reconnect_instructions = []
        self.successes = []
        self.custom_commands = []
        self.restored_backups = []

    def _visit(self, phase):
        if self.events is not None:
            self.events.append(f"prompts.{phase}")
        if self.fail_at == phase:
            raise self.cancellation_error or SetupCancelled(f"cancelled at {phase}")

    def choose_action(self, _editable):
        self._visit("choose_action")
        return self.action

    def choose_device(self, _candidates):
        self._visit("choose_device")
        return self.selected

    def choose_profile_name(self, _editable, _candidate):
        self._visit("choose_profile_name")
        return self.profile_name

    def confirm_port_binding(self, _candidate):
        self._visit("confirm_port_binding")
        return False

    def confirm_keyboard_capture(self, _profile_name, _candidate):
        self._visit("confirm_keyboard_capture")
        return True

    def choose_evdev_trigger(self, events, *, default_toggle_timeout_seconds=60.0):
        self._visit("choose_evdev_trigger")
        self.captured_events = tuple(events)
        assert self.evdev_trigger is not None
        return self.evdev_trigger

    def arm_hidraw_trial(self, direction, number):
        self._visit("arm_hidraw_trial")
        self.armed_trials.append((direction, number))

    def arm_evdev_capture(self, _candidate, _timeout_seconds):
        self._visit("arm_evdev_capture")

    def retry_capture(self, _error):
        self._visit("retry_capture")
        return False

    def confirm_permission(self, _profile_name, _transaction):
        self._visit("confirm_permission")
        return self.confirm_permission_result

    def confirm_managed_permission(self, _profile_name, _candidate):
        self._visit("confirm_managed_permission")
        return self.confirm_managed_permission_result

    def choose_permission_access(self, _profile_name):
        self._visit("choose_permission_access")
        return self.permission_access

    def show_manual_permission(self, transaction):
        self._visit("show_manual_permission")
        self.manual_permission.append(transaction.preview_command_lines)

    def show_reconnect_instruction(self, timeout_seconds):
        self._visit("show_reconnect_instruction")
        self.reconnect_instructions.append(timeout_seconds)

    def confirm_config(self, _diff):
        self._visit("confirm_config")
        return True

    def choose_backup(self, _backups):
        self._visit("choose_backup")
        return self.backup

    def report_backup_restored(self, backup):
        self.restored_backups.append(backup)

    def choose_service(self, _snapshot, _compatible):
        self._visit("choose_service")
        return self.service_choice

    def show_custom_service_command(self, command):
        self.custom_commands.append(command)

    def choose_post_commit_recovery(
        self,
        error,
        *,
        can_retry,
        destination,
        service_state,
    ):
        self._visit("post_commit_recovery")
        if self.post_commit_error is not None:
            raise self.post_commit_error
        self.post_commit_recoveries.append(
            (str(error), can_retry, destination, service_state),
        )
        choice = self.post_commit_choices.popleft()
        if choice is PostCommitChoice.RETRY:
            self.assert_retry_available = can_retry
        return choice

    def report_error(self, error):
        self.errors.append(str(error))

    def report_recovery_failure(self, component, error, transaction=None):
        self.recovery_failures.append((component, str(error)))
        self.recovery_failure_transactions.append(transaction)
        if transaction is not None:
            self.manual_permission.append(transaction.recovery_command_lines)

    def report_success(self, destination, service_state, *, physical_cycle):
        self.successes.append((destination, service_state, physical_cycle))

    def report_cancelled(self, message):
        self.errors.append(message)


def existing_action() -> ActionDraft:
    return new_action("voice")


class SetupSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = self.enterContext(TemporaryDirectory())
        self.root = Path(self.temporary_directory)
        self.xdg = self.root / "xdg"
        self.destination = self.xdg / "input-action-controller" / "config.toml"
        editor = ConfigEditor(
            environ={"XDG_CONFIG_HOME": str(self.xdg)},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc" / "config.toml",
        )
        self.editor = RecordingConfigEditor(editor)

    def run_session(
        self,
        *,
        selected: DeviceCandidate,
        prompts: FakePrompts,
        capture_factory: FakeCaptureFactory,
        permission_factory: FakePermissionFactory | None = None,
        service: FakeService | None = None,
        editor: RecordingConfigEditor | None = None,
        candidates: tuple[DeviceCandidate, ...] | None = None,
    ):
        service = service or FakeService()
        permission_factory = permission_factory or FakePermissionFactory(selected)
        initial_candidates = candidates or (selected,)
        reconnected_candidates = tuple(
            permission_factory.reconnected if item.node == selected.node else item
            for item in initial_candidates
        )
        session = SetupSession(
            None,
            SetupDependencies(
                config_editor=editor or self.editor,
                discovery=FakeDiscovery(
                    initial_candidates,
                    snapshots=(initial_candidates, reconnected_candidates),
                ),
                capture_factory=capture_factory,
                permission_factory=permission_factory,
                service=service,
                prompts=prompts,
            ),
        )
        return session.run(), service, permission_factory

    def test_creates_a_new_evdev_configuration_and_validates_the_saved_file(self):
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
        )

        result, _service, permission_factory = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
        )

        config = load_config(self.destination)
        self.assertEqual(result, 0)
        self.assertEqual(config.actions[0].name, "voice")
        self.assertEqual(config.devices[0].name, "Desk button")
        self.assertEqual(config.devices[0].toggle_events, ("BTN_SIDE",))
        self.assertEqual(prompts.captured_events, ("BTN_SIDE",))
        self.assertTrue(stream.closed)
        self.assertEqual(permission_factory.create_calls, [])
        self.assertEqual(prompts.reconnect_instructions, [])

    def test_updates_existing_evdev_profile_and_persists_classifier(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(
            "[actions.voice]\n"
            'on_command = ["/usr/bin/true"]\n'
            'off_command = ["/usr/bin/true"]\n\n'
            "[[devices]]\n"
            'name = "Mouse button"\n'
            'action = "voice"\n'
            'transport = "evdev"\n'
            'mode = "toggle"\n'
            'vendor_id = "1234"\n'
            'product_id = "5678"\n'
            'interface_number = "01"\n'
            'toggle_events = ["BTN_EXTRA"]\n'
            "toggle_off_timeout_seconds = 0\n",
            encoding="utf-8",
        )
        selected = candidate("/dev/input/event4")
        sibling = candidate(
            "/dev/input/event5",
            event_names=("KEY_A",),
            keyboard_class=True,
            input_classifiers=(("ID_INPUT_KEYBOARD", "1"),),
        )

        class UpdatePrompts(FakePrompts):
            def choose_profile_operation(self, names):
                self._visit("choose_profile_operation")
                self.offered_profiles = names
                return "Mouse button"

            def choose_evdev_trigger(
                self, events, *, default_toggle_timeout_seconds=60.0
            ):
                self._visit("choose_evdev_trigger")
                self.captured_events = tuple(events)
                self.received_toggle_timeout = default_toggle_timeout_seconds
                return EvdevTriggerDraft(
                    mode="toggle",
                    toggle_events=("BTN_SIDE",),
                    toggle_off_timeout_seconds=default_toggle_timeout_seconds,
                )

        prompts = UpdatePrompts(
            selected=selected,
            profile_name="unused",
        )
        permissions = FakePermissionFactory(selected)
        result, _service, permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((side_button_stream(),)),
            permission_factory=permissions,
            candidates=(selected, sibling),
        )

        self.assertEqual(result, 0)
        config = load_config(self.destination)
        self.assertEqual(len(config.devices), 1)
        self.assertEqual(config.devices[0].name, "Mouse button")
        self.assertEqual(config.devices[0].input_classifier, "ID_INPUT_MOUSE")
        self.assertEqual(config.devices[0].toggle_off_timeout_seconds, 0)
        self.assertEqual(prompts.received_toggle_timeout, 0)
        self.assertEqual(permissions.create_calls[0][4], "Mouse button")

    def test_rollback_of_same_path_profile_update_verifies_after_reconnect(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(
            "[actions.voice]\n"
            'on_command = ["/usr/bin/true"]\n'
            'off_command = ["/usr/bin/true"]\n\n'
            "[[devices]]\n"
            'name = "Mouse button"\n'
            'action = "voice"\n'
            'transport = "evdev"\n'
            'mode = "toggle"\n'
            'vendor_id = "1234"\n'
            'product_id = "5678"\n'
            'interface_number = "01"\n'
            'toggle_events = ["BTN_EXTRA"]\n',
            encoding="utf-8",
        )
        selected = candidate("/dev/input/event4")
        sibling = candidate(
            "/dev/input/event5",
            event_names=("KEY_A",),
            keyboard_class=True,
            input_classifiers=(("ID_INPUT_KEYBOARD", "1"),),
        )
        events: list[str] = []

        class UpdatePrompts(FakePrompts):
            def choose_profile_operation(self, _names):
                self._visit("choose_profile_operation")
                return "Mouse button"

        prompts = UpdatePrompts(
            selected=selected,
            profile_name="unused",
            fail_at="confirm_config",
            events=events,
        )
        permissions = FakePermissionFactory(
            selected,
            events=events,
            previous_access=INPUT_GROUP_ACCESS,
        )

        result, _service, _permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((side_button_stream(),)),
            permission_factory=permissions,
            candidates=(selected, sibling),
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(permissions.monitor_calls), 2)
        self.assertEqual(len(permissions.verify_calls), 2)
        rollback_selectors, _monitor, rollback_access = permissions.verify_calls[1]
        self.assertIsNone(rollback_selectors.classifier)
        self.assertEqual(rollback_access, INPUT_GROUP_ACCESS)
        rollback_index = events.index("permission.rollback")
        self.assertEqual(events[rollback_index - 1], "permission.monitor")
        self.assertEqual(
            events[rollback_index + 1 : rollback_index + 3],
            ["prompts.show_reconnect_instruction", "permission.verify"],
        )

    def test_final_runtime_profile_selects_captured_node(self):
        selected = candidate("/dev/input/event4")
        siblings = (
            candidate(
                "/dev/input/event5",
                event_names=("KEY_A",),
                keyboard_class=True,
                input_classifiers=(("ID_INPUT_KEYBOARD", "1"),),
            ),
            candidate(
                "/dev/input/event6",
                event_names=("KEY_B",),
                keyboard_class=True,
                input_classifiers=(("ID_INPUT_KEYBOARD", "1"),),
            ),
        )
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
        )

        result, _service, _permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
            candidates=(selected, *siblings),
        )

        self.assertEqual(result, 0)
        self.assertEqual(load_config(self.destination).devices[-1].name, "Desk button")

    def test_unavailable_or_wrong_node_final_profile_rolls_back_before_confirmation(
        self,
    ):
        cases = (
            (
                "unavailable",
                candidate("/dev/input/event8", event_names=("KEY_A",)),
                "unavailable",
            ),
            (
                "wrong node",
                candidate("/dev/input/event9"),
                "available",
            ),
        )

        for label, final_candidate, status in cases:
            with self.subTest(case=label):
                selected = candidate("/dev/input/event4", readable=False)
                reconnected = candidate("/dev/input/event4", readable=True)
                events: list[str] = []
                prompts = FakePrompts(
                    selected=selected,
                    events=events,
                )
                permissions = FakePermissionFactory(reconnected)
                service = FakeService()
                discovery = FakeDiscovery(
                    (selected,),
                    snapshots=((selected,), (final_candidate,)),
                )
                stream = side_button_stream()

                result = SetupSession(
                    None,
                    SetupDependencies(
                        config_editor=self.editor,
                        discovery=discovery,
                        capture_factory=FakeCaptureFactory((stream,)),
                        permission_factory=permissions,
                        service=service,
                        prompts=prompts,
                    ),
                ).run()

                self.assertEqual(result, 1)
                assert permissions.transaction is not None
                self.assertTrue(permissions.transaction.rolled_back)
                self.assertEqual(service.restore_calls, [service.original])
                self.assertFalse(self.destination.exists())
                self.assertIn(status, prompts.errors[0])
                self.assertNotIn("prompts.confirm_config", events)

    def test_unavailable_existing_profile_does_not_block_a_new_available_profile(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            action=existing_action(),
            selected=selected,
        )

        result, _service, _permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
        )

        self.assertEqual(result, 0)
        self.assertEqual(load_config(self.destination).devices[-1].name, "Desk button")

    def test_final_profile_node_conflict_fails_before_confirmation(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(
            "[actions.voice]\n"
            'on_command = ["/usr/bin/voice", "--on"]\n'
            'off_command = ["/usr/bin/voice", "--off"]\n\n'
            "[[devices]]\n"
            'name = "Existing button"\n'
            'action = "voice"\n'
            'transport = "evdev"\n'
            'mode = "toggle"\n'
            'vendor_id = "1234"\n'
            'product_id = "5678"\n'
            'interface_number = "01"\n'
            'toggle_events = ["BTN_SIDE"]\n',
            encoding="utf-8",
        )
        original = self.destination.read_bytes()
        selected = candidate("/dev/input/event4")
        events: list[str] = []
        prompts = FakePrompts(
            action=existing_action(),
            selected=selected,
            confirm_managed_permission=True,
            events=events,
        )
        permissions = FakePermissionFactory(selected)
        service = FakeService()

        result, _service, permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((side_button_stream(),)),
            permission_factory=permissions,
            service=service,
        )

        self.assertEqual(result, 1)
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [service.original])
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertIn("device-node-conflict", prompts.errors[0])
        self.assertNotIn("prompts.confirm_config", events)

    def test_readable_device_can_opt_in_to_the_managed_permission_transaction(self):
        events: list[str] = []
        selected = candidate("/dev/input/event4")
        reconnected = candidate("/dev/input/event4")
        stream = side_button_stream()
        editor = RecordingConfigEditor(self.editor.editor, events=events)
        service = FakeService(events=events)
        prompts = FakePrompts(
            selected=selected,
            confirm_managed_permission=True,
            events=events,
        )
        permissions = FakePermissionFactory(reconnected, events=events)
        capture = FakeCaptureFactory((stream,), events=events)

        result, _service, permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=capture,
            permission_factory=permissions,
            service=service,
            editor=editor,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [
                event
                for event in events
                if event
                in {
                    "permission.create",
                    "permission.prepare",
                    "prompts.confirm_permission",
                    "permission.install",
                    "prompts.show_reconnect_instruction",
                    "permission.verify",
                    "capture.open",
                }
            ],
            [
                "permission.create",
                "permission.prepare",
                "prompts.confirm_permission",
                "permission.install",
                "prompts.show_reconnect_instruction",
                "permission.verify",
                "capture.open",
            ],
        )
        self.assertEqual(prompts.reconnect_instructions, [60.0])
        self.assertLess(
            events.index("config.commit"), events.index("permission.finalize")
        )
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.finalized)

    def test_creates_a_new_hidraw_configuration(self):
        events: list[str] = []
        selected = candidate("/dev/hidraw4", transport="hidraw")
        clock = FakeClock()
        stream = FakeReportStream(clock, stable_hidraw_reports())
        prompts = FakePrompts(
            selected=selected,
            profile_name="Desk headset",
            events=events,
        )

        result, _service, _permission_factory = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,), clock=clock, events=events),
        )

        config = load_config(self.destination)
        self.assertEqual(result, 0)
        self.assertEqual(config.devices[0].on_reports, (b"on",))
        self.assertEqual(config.devices[0].off_reports, (b"off",))
        self.assertEqual(
            prompts.armed_trials,
            [
                ("on", 1),
                ("off", 1),
                ("on", 2),
                ("off", 2),
                ("on", 3),
                ("off", 3),
            ],
        )
        self.assertTrue(stream.closed)
        self.assertNotIn("prompts.arm_evdev_capture", events)

    def test_readable_evdev_readiness_precedes_open(self):
        events: list[str] = []
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            events=events,
        )
        capture = FakeCaptureFactory((stream,), events=events)

        result, _service, permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=capture,
        )

        self.assertEqual(result, 0)
        self.assertEqual(permissions.create_calls, [])
        self.assertLess(
            events.index("prompts.arm_evdev_capture"),
            events.index("capture.open"),
        )

    def test_evdev_readiness_cancellation_rolls_back_permission_and_restores_service(
        self,
    ):
        for error in (
            SetupExitRequested("Cancelled by user."),
            SetupCancelled("Input ended."),
        ):
            with self.subTest(error=type(error).__name__):
                self.destination.parent.mkdir(parents=True, exist_ok=True)
                self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
                original = self.destination.read_bytes()
                events: list[str] = []
                selected = candidate("/dev/input/event4", readable=False)
                reconnected = candidate("/dev/input/event4", readable=True)
                stream = FakeEvdevStream()
                prompts = FakePrompts(
                    action=existing_action(),
                    selected=selected,
                    fail_at="arm_evdev_capture",
                    cancellation_error=error,
                    events=events,
                )
                permissions = FakePermissionFactory(reconnected, events=events)
                service = FakeService(events=events)
                capture = FakeCaptureFactory((stream,), events=events)

                result, _service, permissions = self.run_session(
                    selected=selected,
                    prompts=prompts,
                    capture_factory=capture,
                    permission_factory=permissions,
                    service=service,
                )

                self.assertEqual(result, 0)
                assert permissions.transaction is not None
                self.assertTrue(permissions.transaction.rolled_back)
                self.assertEqual(service.restore_calls, [service.original])
                self.assertEqual(self.destination.read_bytes(), original)
                self.assertEqual(capture.opened_candidates, [])
                self.assertNotIn("capture.open", events)

    def test_adds_a_device_to_an_existing_action_without_rewriting_comments(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            action=existing_action(),
            selected=selected,
        )

        result, _service, _permission_factory = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
        )

        content = self.destination.read_text(encoding="utf-8")
        config = load_config(self.destination)
        self.assertEqual(result, 0)
        self.assertEqual([action.name for action in config.actions], ["voice"])
        self.assertEqual(
            [profile.name for profile in config.devices],
            ["Existing headset", "Desk button"],
        )
        self.assertIn("# Keep the top-level comment.", content)
        self.assertIn("# Keep the existing action comment.", content)
        self.assertIn("# Keep the existing device comment.", content)

    def test_adds_a_new_action_and_device_to_an_existing_commented_config(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            action=new_action("dictation"),
            selected=selected,
        )

        result, _service, _permission_factory = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
        )

        content = self.destination.read_text(encoding="utf-8")
        config = load_config(self.destination)
        self.assertEqual(result, 0)
        self.assertEqual(
            [action.name for action in config.actions],
            ["voice", "dictation"],
        )
        self.assertEqual(config.devices[-1].action, "dictation")
        self.assertIn("# Keep the top-level comment.", content)
        self.assertIn("# Keep the existing action comment.", content)
        self.assertIn("# Keep the existing device comment.", content)

    def test_unreadable_device_uses_event_instance_acl_and_production_resolution_contract(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
        )
        permission_factory = FakePermissionFactory(reconnected)
        capture_factory = FakeCaptureFactory((stream,))

        result, _service, permission_factory = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=capture_factory,
            permission_factory=permission_factory,
        )

        self.assertEqual(result, 0)
        self.assertIs(
            permission_factory.verify_calls[0][1],
            permission_factory.monitor_calls[0][1],
        )
        self.assertEqual(permission_factory.observer_calls, 1)
        self.assertEqual(permission_factory.access_calls, 1)
        self.assertEqual(permission_factory.acl_calls, 1)
        self.assertEqual(permission_factory.resolution_calls, 1)
        self.assertEqual(capture_factory.opened_candidates, [reconnected])
        assert permission_factory.transaction is not None
        self.assertTrue(permission_factory.transaction.finalized)

    def test_explicit_advanced_permission_access_is_passed_to_rule_factory(self):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            permission_access=INPUT_GROUP_ACCESS,
        )
        permissions = FakePermissionFactory(reconnected)

        result, _service, permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
            permission_factory=permissions,
        )

        self.assertEqual(result, 0)
        self.assertEqual(permissions.create_calls[0][3], INPUT_GROUP_ACCESS)
        assert permissions.transaction is not None
        self.assertIn('GROUP="input"', permissions.transaction.rendered.content)

    def test_selected_backup_is_restored_before_the_setup_session_continues(self):
        backup = BackupInfo(
            self.root / "config.toml.bak.20260716T120000Z",
            datetime(2026, 7, 16, 12, tzinfo=UTC),
        )
        self.editor.backups = (backup,)
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            backup=backup,
        )

        result, _service, _permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
        )

        self.assertEqual(result, 0)
        self.assertEqual(self.editor.restored_backups, [backup])
        self.assertEqual(prompts.restored_backups, [backup])

    def test_potentially_committed_backup_restore_uses_post_commit_recovery(self):
        backup = BackupInfo(
            self.root / "config.toml.bak.20260716T120000Z",
            datetime(2026, 7, 16, 12, tzinfo=UTC),
        )
        backup_transaction = RecordingConfigTransaction(
            self.editor.editor.begin(self.editor.load(None)),
        )
        error = ConfigCommitError(
            "injected backup restore failure", potentially_committed=True
        )
        error.transaction = backup_transaction
        editor = RecordingConfigEditor(
            self.editor.editor,
            backup_restore_error=error,
            backup_transaction=backup_transaction,
        )
        editor.backups = (backup,)
        selected = candidate("/dev/input/event4")
        prompts = FakePrompts(
            selected=selected,
            backup=backup,
            post_commit_choices=(PostCommitChoice.RESTORE,),
        )
        service = FakeService()

        result, _service, _permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory(()),
            service=service,
            editor=editor,
        )

        self.assertEqual(result, 1)
        self.assertEqual(service.stop_calls, 1)
        self.assertEqual(len(prompts.post_commit_recoveries), 1)
        self.assertEqual(prompts.post_commit_recoveries[0][2], self.destination)
        self.assertTrue(backup_transaction.restored)

    def test_potentially_committed_backup_restore_eof_leaves_state_in_place(self):
        backup = BackupInfo(
            self.root / "config.toml.bak.20260716T120000Z",
            datetime(2026, 7, 16, 12, tzinfo=UTC),
        )
        backup_transaction = RecordingConfigTransaction(
            self.editor.editor.begin(self.editor.load(None)),
        )
        error = ConfigCommitError(
            "injected backup restore failure", potentially_committed=True
        )
        error.transaction = backup_transaction
        editor = RecordingConfigEditor(
            self.editor.editor,
            backup_restore_error=error,
            backup_transaction=backup_transaction,
        )
        editor.backups = (backup,)
        selected = candidate("/dev/input/event4")
        prompts = FakePrompts(
            selected=selected,
            backup=backup,
            post_commit_error=SetupCancelled("Input ended."),
        )
        service = FakeService()

        result, _service, _permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory(()),
            service=service,
            editor=editor,
        )

        self.assertEqual(result, 1)
        self.assertFalse(backup_transaction.restored)
        self.assertEqual(service.restore_calls, [])
        self.assertIn("Input ended.", prompts.errors)

    def test_permission_path_obeys_the_transaction_order(self):
        events: list[str] = []
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        editor = RecordingConfigEditor(self.editor.editor, events=events)
        service = FakeService(events=events)
        prompts = FakePrompts(
            selected=selected,
            events=events,
        )
        permissions = FakePermissionFactory(reconnected, events=events)
        capture = FakeCaptureFactory((stream,), events=events)
        discovery = FakeDiscovery(
            (selected,),
            events=events,
            snapshots=((selected,), (reconnected,)),
        )

        def validate_saved(path):
            events.append("config.production_validate")
            return load_config(path)

        with patch(
            "input_action_controller.setup.session.load_config",
            side_effect=validate_saved,
        ):
            result = SetupSession(
                None,
                SetupDependencies(
                    config_editor=editor,
                    discovery=discovery,
                    capture_factory=capture,
                    permission_factory=permissions,
                    service=service,
                    prompts=prompts,
                ),
            ).run()

        self.assertEqual(result, 0)
        self.assertLess(
            events.index("prompts.show_reconnect_instruction"),
            events.index("prompts.arm_evdev_capture"),
        )
        self.assertLess(
            events.index("permission.verify"),
            events.index("prompts.arm_evdev_capture"),
        )
        self.assertLess(
            events.index("prompts.arm_evdev_capture"),
            events.index("capture.open"),
        )
        self.assertEqual(
            events,
            [
                "config.load",
                "service.snapshot",
                "service.stop",
                "discovery.enumerate",
                "prompts.choose_device",
                "prompts.choose_action",
                "prompts.choose_profile_name",
                "prompts.choose_permission_access",
                "permission.create",
                "permission.prepare",
                "prompts.confirm_permission",
                "permission.monitor",
                "permission.install",
                "prompts.show_reconnect_instruction",
                "permission.verify",
                "prompts.arm_evdev_capture",
                "capture.open",
                "prompts.choose_evdev_trigger",
                "capture.close",
                "discovery.enumerate",
                "prompts.confirm_config",
                "prompts.choose_service",
                "config.begin",
                "config.commit",
                "config.production_validate",
                "service.apply",
                "service.snapshot",
                "permission.finalize",
                "config.finalize",
            ],
        )

    def test_precommit_failures_rollback_permission_restore_service_and_leave_config_unchanged(
        self,
    ):
        cases = (
            ("reconnect", "failure", 1),
            ("capture", "failure", 1),
            ("validation", "failure", 1),
            ("confirm_config", "exit", 0),
            ("choose_service", "cancel", 0),
            ("commit", "failure", 1),
        )

        for phase, kind, expected in cases:
            with self.subTest(phase=phase):
                self.destination.parent.mkdir(parents=True, exist_ok=True)
                self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
                original = self.destination.read_bytes()
                selected = candidate("/dev/input/event4", readable=False)
                reconnected = candidate("/dev/input/event4", readable=True)
                stream = FakeEvdevStream(
                    (
                        RawEvdevEvent(
                            ecodes.EV_KEY,
                            ecodes.ecodes["BTN_SIDE"],
                            1,
                        ),
                    ),
                    error=(
                        RuntimeError("injected capture failure")
                        if phase == "capture"
                        else None
                    ),
                )
                prompts = FakePrompts(
                    action=existing_action(),
                    selected=selected,
                    profile_name=f"Desk button {phase}",
                    fail_at=phase if kind in {"cancel", "exit"} else None,
                    cancellation_error=(
                        SetupExitRequested("Cancelled by user.")
                        if kind == "exit"
                        else None
                    ),
                )
                permissions = FakePermissionFactory(
                    reconnected,
                    verify_error=(
                        RuntimeError("injected reconnect failure")
                        if phase == "reconnect"
                        else None
                    ),
                )
                service = FakeService()
                editor = RecordingConfigEditor(
                    self.editor.editor,
                    commit_error=(
                        RuntimeError("injected commit failure")
                        if phase == "commit"
                        else None
                    ),
                )

                context = (
                    patch(
                        "input_action_controller.setup.session.parse_config",
                        side_effect=RuntimeError("injected validation failure"),
                    )
                    if phase == "validation"
                    else patch(
                        "input_action_controller.setup.session.parse_config",
                        wraps=__import__(
                            "input_action_controller.config",
                            fromlist=["parse_config"],
                        ).parse_config,
                    )
                )
                with context:
                    result = SetupSession(
                        None,
                        SetupDependencies(
                            config_editor=editor,
                            discovery=reconnected_discovery(selected, reconnected),
                            capture_factory=FakeCaptureFactory((stream,)),
                            permission_factory=permissions,
                            service=service,
                            prompts=prompts,
                        ),
                    ).run()

                self.assertEqual(result, expected)
                self.assertEqual(self.destination.read_bytes(), original)
                assert permissions.transaction is not None
                self.assertTrue(permissions.transaction.rolled_back)
                self.assertEqual(service.restore_calls, [service.original])
                if phase != "reconnect":
                    self.assertTrue(stream.closed)

    def test_clean_cancellation_becomes_failure_when_rollback_fails_and_recovery_is_visible(
        self,
    ):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            action=existing_action(),
            selected=selected,
            fail_at="confirm_config",
        )
        permissions = FakePermissionFactory(
            reconnected,
            rollback_error=RuntimeError("injected permission rollback failure"),
        )
        service = FakeService()

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertEqual(service.restore_calls, [])
        self.assertEqual(
            prompts.recovery_failures,
            [("permission", "injected permission rollback failure")],
        )
        self.assertIn(
            permissions.transaction.recovery_command_lines,
            prompts.manual_permission,
        )

    def test_postcommit_service_failure_can_retry_without_rolling_back_valid_state(
        self,
    ):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            action=existing_action(),
            selected=selected,
            post_commit_choices=(PostCommitChoice.RETRY,),
        )
        permissions = FakePermissionFactory(reconnected)
        service = FakeService(apply_failures=1)

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 0)
        self.assertEqual(len(service.apply_calls), 2)
        self.assertEqual(service.restore_calls, [])
        assert permissions.transaction is not None
        self.assertFalse(permissions.transaction.rolled_back)
        self.assertTrue(permissions.transaction.finalized)
        self.assertEqual(load_config(self.destination).devices[-1].name, "Desk button")

    def test_postcommit_service_failure_can_restore_config_permission_and_service(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text(EXISTING_CONFIG, encoding="utf-8")
        original = self.destination.read_bytes()
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            action=existing_action(),
            selected=selected,
            post_commit_choices=(PostCommitChoice.RESTORE,),
        )
        permissions = FakePermissionFactory(reconnected)
        service = FakeService(apply_failures=1)

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertEqual(self.destination.read_bytes(), original)
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [service.original])
        assert self.editor.transaction is not None
        self.assertTrue(self.editor.transaction.restored)

    def test_failed_config_restore_reports_its_recovery_transaction(self):
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            post_commit_choices=(PostCommitChoice.RESTORE,),
        )
        editor = RecordingConfigEditor(
            self.editor.editor,
            restore_error=RuntimeError("injected config restore failure"),
        )

        result, _service, _permissions = self.run_session(
            selected=selected,
            prompts=prompts,
            capture_factory=FakeCaptureFactory((stream,)),
            service=FakeService(apply_failures=1),
            editor=editor,
        )

        self.assertEqual(result, 1)
        self.assertIn(
            ("config", "injected config restore failure"), prompts.recovery_failures
        )
        assert editor.transaction is not None
        self.assertIn(editor.transaction, prompts.recovery_failure_transactions)

    def test_approved_permission_install_failure_rolls_back_uncertain_destination(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        prompts = FakePrompts(
            selected=selected,
        )
        permissions = FakePermissionFactory(
            reconnected,
            install_error=RuntimeError("injected permission install failure"),
        )
        service = FakeService()

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory(()),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertFalse(self.destination.exists())
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.install_started)
        self.assertFalse(permissions.transaction.destination_write_succeeded)
        self.assertTrue(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [service.original])
        self.assertEqual(prompts.manual_permission, [])

    def test_reload_failure_after_a_confirmed_permission_write_rolls_back(self):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        prompts = FakePrompts(
            selected=selected,
        )
        permissions = FakePermissionFactory(
            reconnected,
            reload_error=RuntimeError("injected permission reload failure"),
        )
        service = FakeService()

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory(()),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.destination_write_succeeded)
        self.assertTrue(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [service.original])
        self.assertEqual(prompts.manual_permission, [])

    def test_potentially_committed_config_error_does_not_restore_after_prompt_error(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            post_commit_error=RuntimeError("injected recovery prompt failure"),
        )
        permissions = FakePermissionFactory(reconnected)
        service = FakeService()
        editor = RecordingConfigEditor(
            self.editor.editor,
            commit_error=ConfigCommitError(
                "injected potentially committed configuration failure",
                potentially_committed=True,
            ),
        )

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        assert editor.transaction is not None
        self.assertFalse(editor.transaction.restored)
        assert permissions.transaction is not None
        self.assertFalse(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [])
        self.assertIn("injected recovery prompt failure", prompts.errors)

    def test_finalization_failure_leaves_committed_config_permission_and_service_in_place(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
        )
        permissions = FakePermissionFactory(
            reconnected,
            finalize_error=RuntimeError("injected permission finalize failure"),
        )
        service = FakeService()

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertEqual(load_config(self.destination).devices[-1].name, "Desk button")
        assert permissions.transaction is not None
        self.assertFalse(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [])
        self.assertIn("injected permission finalize failure", prompts.errors)

    def test_postcommit_recovery_cancellation_restores_prior_state(self):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            post_commit_error=SetupExitRequested("Cancelled by user."),
        )
        permissions = FakePermissionFactory(reconnected)
        service = FakeService(apply_failures=1)

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertFalse(self.destination.exists())
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [service.original])
        assert self.editor.transaction is not None
        self.assertTrue(self.editor.transaction.restored)
        self.assertIn("Cancelled by user.", prompts.errors)

    def test_postcommit_permission_rollback_failure_keeps_service_stopped(self):
        selected = candidate("/dev/input/event4")
        prompts = FakePrompts(
            selected=selected,
        )
        permissions = FakePermissionFactory(selected)
        service = FakeService()
        session = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=FakeDiscovery((selected,)),
                capture_factory=FakeCaptureFactory(()),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        )
        transaction = FakePermissionTransaction(
            render_rule(
                "Desk button",
                SelectorDraft(
                    transport="evdev",
                    vendor_id="1234",
                    product_id="5678",
                    interface_number="01",
                    classifier=("ID_INPUT_MOUSE", "1"),
                ),
                (selected,),
            ),
            rollback_error=RuntimeError("injected rollback failure"),
        )
        config_transaction = Mock()

        recovered = session._restore_committed_state(
            service.original,
            config_transaction,
            transaction,
            True,
        )

        self.assertFalse(recovered)
        config_transaction.restore.assert_called_once_with()
        self.assertEqual(service.restore_calls, [])

    def test_postcommit_recovery_eof_leaves_potentially_committed_state_in_place(self):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            post_commit_error=SetupCancelled("Input ended."),
        )
        permissions = FakePermissionFactory(reconnected)
        service = FakeService(apply_failures=1)

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertEqual(load_config(self.destination).devices[-1].name, "Desk button")
        assert permissions.transaction is not None
        self.assertFalse(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [])
        assert self.editor.transaction is not None
        self.assertFalse(self.editor.transaction.restored)
        self.assertIn("Input ended.", prompts.errors)

    def test_postcommit_failure_reports_committed_destination_and_actual_service_state(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            post_commit_choices=(PostCommitChoice.RESTORE,),
        )
        permissions = FakePermissionFactory(reconnected)
        changed_state = ServiceSnapshot(enabled=False, active=True)
        service = FakeService(
            apply_failures=1,
            apply_failure_state=changed_state,
        )

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertEqual(
            prompts.post_commit_recoveries,
            [
                (
                    "injected service apply failure",
                    True,
                    self.destination,
                    changed_state,
                ),
            ],
        )

    def test_postcommit_service_observation_failure_is_visible_when_exit_request_restores_state(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            post_commit_error=SetupExitRequested("Cancelled by user."),
        )
        permissions = FakePermissionFactory(reconnected)
        service = FakeService(
            apply_failures=1,
            snapshot_errors=(RuntimeError("injected service snapshot failure"),),
            snapshot_error_after=1,
        )

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=reconnected_discovery(selected, reconnected),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 1)
        self.assertFalse(self.destination.exists())
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.rolled_back)
        self.assertEqual(service.restore_calls, [service.original])
        assert self.editor.transaction is not None
        self.assertTrue(self.editor.transaction.restored)
        self.assertEqual(
            prompts.recovery_failures,
            [("service observation", "injected service snapshot failure")],
        )

    def test_declining_privileged_install_preserves_manual_commands_and_restores_service(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=False)
        reconnected = candidate("/dev/input/event4", readable=True)
        prompts = FakePrompts(
            selected=selected,
            confirm_permission=False,
        )
        permissions = FakePermissionFactory(reconnected)
        service = FakeService()

        result = SetupSession(
            None,
            SetupDependencies(
                config_editor=self.editor,
                discovery=FakeDiscovery((selected,)),
                capture_factory=FakeCaptureFactory(()),
                permission_factory=permissions,
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 0)
        self.assertFalse(self.destination.exists())
        assert permissions.transaction is not None
        self.assertTrue(permissions.transaction.prepared)
        self.assertFalse(permissions.transaction.install_started)
        self.assertEqual(
            prompts.manual_permission,
            [permissions.transaction.preview_command_lines],
        )
        self.assertEqual(service.restore_calls, [service.original])

    def test_custom_config_restores_the_original_packaged_service_state_after_commit(
        self,
    ):
        selected = candidate("/dev/input/event4")
        stream = side_button_stream()
        prompts = FakePrompts(
            selected=selected,
            service_choice=ServiceChoice.PRESERVE_STOPPED,
        )
        service = FakeService()
        destination = self.root / "custom.toml"

        result = SetupSession(
            destination,
            SetupDependencies(
                config_editor=self.editor,
                discovery=FakeDiscovery((selected,)),
                capture_factory=FakeCaptureFactory((stream,)),
                permission_factory=FakePermissionFactory(selected),
                service=service,
                prompts=prompts,
            ),
        ).run()

        self.assertEqual(result, 0)
        self.assertTrue(destination.exists())
        self.assertEqual(service.apply_calls, [])
        self.assertEqual(service.restore_calls, [service.original])
        self.assertEqual(prompts.custom_commands, [service.manual_daemon_command])


class SetupCompositionTests(unittest.TestCase):
    @staticmethod
    def selectors():
        return __import__(
            "input_action_controller.setup.devices",
            fromlist=["SelectorDraft"],
        ).SelectorDraft(
            transport="evdev",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            classifier=("ID_INPUT_MOUSE", "1"),
        )

    def test_permission_factory_requires_a_matching_remove_before_add(self):
        selected = candidate("/dev/input/event4")
        cases = {
            "change only": (
                FakeUdevDevice(
                    selected.node, "/sys/old", action="change", sequence_number=12
                ),
            ),
            "add only": (
                FakeUdevDevice(
                    selected.node, "/sys/new", action="add", sequence_number=12
                ),
            ),
            "unrelated remove then add": (
                FakeUdevDevice(
                    "/dev/input/event9",
                    "/sys/other",
                    action="remove",
                    sequence_number=12,
                ),
                FakeUdevDevice(
                    selected.node, "/sys/new", action="add", sequence_number=13
                ),
            ),
        }
        for name, events in cases.items():
            with self.subTest(name=name):
                monitor = FakeUdevMonitor(events)
                factory = SetupPermissionFactory(
                    FakeDiscovery((selected,)),
                    context=FakeUdevContext(()),
                    monitor_factory=lambda _context, monitor=monitor: monitor,
                    access_fn=lambda _node, _mode: True,
                    acl_checker=lambda _candidate: True,
                )
                selectors = self.selectors()
                with self.assertRaisesRegex(ReconnectVerificationError, "Timed out"):
                    factory.verify_reconnected(
                        selectors,
                        factory.begin_reconnect_monitor(selectors),
                    )

    def test_permission_factory_uses_classifier_for_provisional_resolution(self):
        selected = candidate("/dev/input/event4")
        sibling = candidate(
            "/dev/input/event5",
            event_names=("KEY_A",),
            keyboard_class=True,
            input_classifiers=(("ID_INPUT_KEYBOARD", "1"),),
        )
        monitor = FakeUdevMonitor(
            (
                FakeUdevDevice(
                    selected.node,
                    "/sys/devices/old",
                    action="remove",
                    sequence_number=12,
                ),
                FakeUdevDevice(
                    selected.node, "/sys/devices/new", action="add", sequence_number=13
                ),
            )
        )
        factory = SetupPermissionFactory(
            FakeDiscovery((selected, sibling)),
            context=FakeUdevContext(()),
            monitor_factory=lambda _context: monitor,
            access_fn=lambda _node, _mode: True,
            acl_checker=lambda _candidate: True,
        )
        selectors = self.selectors()

        resolved = factory.verify_reconnected(
            selectors,
            factory.begin_reconnect_monitor(selectors),
        )

        self.assertEqual(resolved.node, "/dev/input/event4")

    def test_permission_factory_verifies_a_new_udev_instance_acl_and_runtime_resolution(
        self,
    ):
        old = candidate("/dev/input/event4", readable=False)
        new = candidate("/dev/input/event8", readable=True)
        monitor = FakeUdevMonitor(
            (
                FakeUdevDevice(
                    old.node, "/sys/devices/old", action="remove", sequence_number=12
                ),
                FakeUdevDevice(
                    new.node, "/sys/devices/new", action="add", sequence_number=13
                ),
            )
        )
        discovery = FakeDiscovery((new,), snapshots=((old,), (new,), (new,)))
        access_modes = []
        factory = SetupPermissionFactory(
            discovery,
            context=FakeUdevContext(()),
            monitor_factory=lambda _context: monitor,
            access_fn=lambda _node, mode: access_modes.append(mode) or True,
            acl_checker=lambda _candidate: True,
        )
        selectors = self.selectors()

        reconnect_monitor = factory.begin_reconnect_monitor(selectors)
        resolved = factory.verify_reconnected(selectors, reconnect_monitor)

        self.assertEqual(resolved, new)
        self.assertNotEqual(old.node, new.node)
        self.assertEqual(monitor.subsystems, ["input"])
        self.assertEqual(access_modes, [os.R_OK | os.W_OK])

    def test_permission_factory_rejects_effective_write_removed_by_acl_mask(self):
        selected = candidate("/dev/input/event4")
        monitor = FakeUdevMonitor(
            (
                FakeUdevDevice(
                    selected.node,
                    "/sys/devices/old",
                    action="remove",
                    sequence_number=12,
                ),
                FakeUdevDevice(
                    selected.node,
                    "/sys/devices/new",
                    action="add",
                    sequence_number=13,
                ),
            )
        )
        modes = []
        factory = SetupPermissionFactory(
            FakeDiscovery((selected,)),
            context=FakeUdevContext(()),
            monitor_factory=lambda _context: monitor,
            access_fn=lambda _node, mode: modes.append(mode) or False,
            acl_checker=lambda _candidate: True,
        )
        selectors = self.selectors()

        with self.assertRaisesRegex(ReconnectVerificationError, "effective read/write"):
            factory.verify_reconnected(
                selectors,
                factory.begin_reconnect_monitor(selectors),
            )

        self.assertEqual(modes, [os.R_OK | os.W_OK])

    def test_permission_factory_uses_a_post_monitor_event_sequence_and_skips_unrelated_events(
        self,
    ):
        selected = candidate("/dev/input/event4", readable=True)
        unrelated = FakeUdevDevice(
            None, "/sys/devices/parent", action="add", sequence_number=10
        )
        nonmatching = FakeUdevDevice(
            "/dev/input/event8", "/sys/devices/other", action="add", sequence_number=11
        )
        removed = FakeUdevDevice(
            selected.node,
            "/sys/devices/old",
            action="remove",
            sequence_number=12,
        )
        matching = FakeUdevDevice(
            selected.node,
            "/sys/devices/new",
            action="add",
            sequence_number=13,
        )
        clock = {"value": 0.0}
        monitor = FakeUdevMonitor(
            (unrelated, nonmatching, removed, matching),
            on_poll=lambda: clock.__setitem__("value", clock["value"] + 10.0),
        )
        factory = SetupPermissionFactory(
            FakeDiscovery((selected,)),
            context=FakeUdevContext(()),
            monitor_factory=lambda _context: monitor,
            access_fn=lambda _node, _mode: True,
            acl_checker=lambda _candidate: True,
            monotonic=lambda: clock["value"],
        )
        selectors = self.selectors()

        monitor_token = factory.begin_reconnect_monitor(selectors)
        clock["value"] = 100.0
        resolved = factory.verify_reconnected(selectors, monitor_token)

        self.assertEqual(resolved, selected)
        self.assertEqual(monitor.subsystems, ["input"])
        self.assertTrue(monitor.started)
        self.assertEqual(monitor.timeouts, [60.0, 50.0, 40.0, 30.0])

    def test_permission_factory_uses_group_mode_verification_without_a_user_acl(self):
        selected = candidate("/dev/input/event4", readable=True)
        monitor = FakeUdevMonitor(
            (
                FakeUdevDevice(
                    selected.node,
                    "/sys/devices/old",
                    action="remove",
                    sequence_number=12,
                ),
                FakeUdevDevice(
                    selected.node, "/sys/devices/new", action="add", sequence_number=13
                ),
            )
        )
        group_checks = []
        factory = SetupPermissionFactory(
            FakeDiscovery((selected,)),
            context=FakeUdevContext(()),
            monitor_factory=lambda _context: monitor,
            access_fn=lambda _node, _mode: True,
            acl_checker=lambda _candidate: self.fail(
                "uaccess ACL must not be required"
            ),
            group_checker=lambda item: group_checks.append(item.node) or True,
        )
        selectors = self.selectors()

        resolved = factory.verify_reconnected(
            selectors,
            factory.begin_reconnect_monitor(selectors),
            access=INPUT_GROUP_ACCESS,
        )

        self.assertEqual(resolved, selected)
        self.assertEqual(group_checks, [selected.node])

    def test_acl_check_uses_numeric_uid_output_and_requires_read_write_permission(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("Result", (), {"returncode": 0, "stdout": "user:1000:rw-\n"})()

        result = _has_current_user_acl(
            candidate("/dev/input/event4"),
            runner=runner,
            uid=lambda: 1000,
        )

        self.assertTrue(result)
        self.assertIn("--numeric", calls[0][0])

    def test_acl_check_rejects_read_only_and_other_uid_entries(self):
        def check(output):
            return _has_current_user_acl(
                candidate("/dev/input/event4"),
                runner=lambda _argv, **_kwargs: type(
                    "Result", (), {"returncode": 0, "stdout": output}
                )(),
                uid=lambda: 1000,
            )

        self.assertFalse(check("user:1000:r--\n"))
        self.assertFalse(check("user:1001:rw-\n"))

    def test_input_group_checker_requires_input_gid_and_group_read_mode(self):
        node_stat = SimpleNamespace(st_gid=42, st_mode=0o660)

        result = _has_input_group_mode_access(
            candidate("/dev/input/event4"),
            stat_fn=lambda _node: node_stat,
            group_lookup=lambda _name: SimpleNamespace(gr_gid=42),
        )
        wrong_group = _has_input_group_mode_access(
            candidate("/dev/input/event4"),
            stat_fn=lambda _node: node_stat,
            group_lookup=lambda _name: SimpleNamespace(gr_gid=43),
        )
        no_group_read = _has_input_group_mode_access(
            candidate("/dev/input/event4"),
            stat_fn=lambda _node: SimpleNamespace(st_gid=42, st_mode=0o600),
            group_lookup=lambda _name: SimpleNamespace(gr_gid=42),
        )

        self.assertTrue(result)
        self.assertFalse(wrong_group)
        self.assertFalse(no_group_read)

    def test_run_setup_constructs_real_adapters_without_starting_privileged_operations(
        self,
    ):
        location = Mock()
        session = Mock()
        session.run.return_value = 0
        context = Mock()
        editor = Mock()
        discovery = Mock()
        capture_factory = Mock()
        permission_factory = Mock()
        service = Mock()
        prompts = Mock()
        with (
            patch(
                "input_action_controller.setup.session.pyudev.Context",
                return_value=context,
            ),
            patch(
                "input_action_controller.setup.session.ConfigEditor",
                return_value=editor,
            ),
            patch(
                "input_action_controller.setup.session.resolve_editable_config",
                return_value=location,
            ),
            patch(
                "input_action_controller.setup.session.DeviceDiscovery",
                return_value=discovery,
            ),
            patch(
                "input_action_controller.setup.session.SetupCaptureFactory",
                return_value=capture_factory,
            ),
            patch(
                "input_action_controller.setup.session.SetupPermissionFactory",
                return_value=permission_factory,
            ) as permission_constructor,
            patch(
                "input_action_controller.setup.service.ServiceManager",
                return_value=service,
            ),
            patch(
                "input_action_controller.setup.prompts.ConsoleSetupPrompts",
                return_value=prompts,
            ),
            patch(
                "input_action_controller.setup.session.SetupSession",
                return_value=session,
            ) as session_constructor,
        ):
            result = run_setup(Path("relative.toml"))

        self.assertEqual(result, 0)
        permission_constructor.assert_called_once_with(discovery, context=context)
        dependencies = session_constructor.call_args.args[1]
        self.assertIs(dependencies.config_editor, editor)
        self.assertIs(dependencies.permission_factory, permission_factory)
        self.assertIs(dependencies.service, service)
        self.assertIs(dependencies.prompts, prompts)


if __name__ == "__main__":
    unittest.main()

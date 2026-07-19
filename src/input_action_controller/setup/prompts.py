from __future__ import annotations

from collections.abc import Callable, Sequence
import math
from pathlib import Path
import sys
from typing import TextIO

from ..devices.discovery import DeviceCandidate
from .actions import ActionCommandStyle, ActionDraft, parse_command_line
from .capture import EvdevTriggerDraft
from .config_editor import BackupInfo, EditableConfig
from .permissions import (
    GROUP_FALLBACK_WARNING,
    INPUT_GROUP_ACCESS,
    PermissionTransaction,
    UACCESS,
)
from .service import ServiceChoice, ServiceSnapshot
from .session import PostCommitChoice, SetupCancelled, SetupExitRequested


class ConsoleSetupPrompts:
    """Collect explicit setup choices from a line-oriented terminal."""

    def __init__(
        self,
        *,
        input_stream: TextIO = sys.stdin,
        output: TextIO = sys.stdout,
        which: Callable[[str], str | None],
    ):
        self._input = input_stream
        self._output = output
        self._which = which

    def choose_action(self, editable: EditableConfig) -> ActionDraft:
        actions = editable.document.get("actions")
        existing = tuple(actions) if isinstance(actions, dict) else ()
        if existing:
            self._write("Existing actions: " + ", ".join(existing))
        name = self._required("Action name: ")
        if name in existing:
            action = actions[name]
            on_command = tuple(action.get("on_command", ()))
            off_command = tuple(action.get("off_command", ()))
            if not on_command or not off_command:
                raise ValueError(
                    f"Existing action {name!r} does not define both commands."
                )
            self._write(f"Using existing action {name!r}.")
            return ActionDraft(
                name=name, on_command=on_command, off_command=off_command
            )

        self._write("Action command style:")
        self._write("  1. Separate on and off commands")
        self._write("  2. One toggle command for both directions")
        style = self._choice("Choose [1]: ", ("1", "2"), default="1")
        if style == "2":
            command = self._command("Toggle command: ")
            self._write(f"argv: {list(command)!r}")
            return self._configure_action_failure_behavior(
                ActionDraft(
                    name=name,
                    on_command=command,
                    off_command=command,
                    command_style=ActionCommandStyle.TOGGLE,
                )
            )

        on_command = self._command("On command: ")
        off_command = self._command("Off command: ")
        self._write(f"on argv: {list(on_command)!r}")
        self._write(f"off argv: {list(off_command)!r}")
        return self._configure_action_failure_behavior(
            ActionDraft(name=name, on_command=on_command, off_command=off_command)
        )

    def choose_device(self, candidates: Sequence[DeviceCandidate]) -> DeviceCandidate:
        if not candidates:
            raise ValueError("No supported input device candidates were found.")
        self._write("Discovered devices:")
        for index, candidate in enumerate(candidates, start=1):
            properties = candidate.properties
            details = [
                candidate.display_name or "unknown device",
                candidate.subsystem,
                f"{properties.get('ID_VENDOR_ID', '?')}:{properties.get('ID_MODEL_ID', '?')}",
                f"node={candidate.node}",
            ]
            if candidate.properties.get("ID_USB_INTERFACE_NUM"):
                details.append(
                    f"interface={candidate.properties['ID_USB_INTERFACE_NUM']}"
                )
            if candidate.properties.get("ID_SERIAL_SHORT"):
                details.append(f"serial={candidate.properties['ID_SERIAL_SHORT']}")
            if candidate.properties.get("ID_PATH"):
                details.append(f"id_path={candidate.properties['ID_PATH']}")
            details.append(
                "readable"
                if candidate.event_codes is not None
                else "permission required"
            )
            self._write(f"  {index}. " + ", ".join(details))
        return candidates[self._number("Choose device: ", len(candidates)) - 1]

    def choose_profile_name(
        self, editable: EditableConfig, candidate: DeviceCandidate
    ) -> str:
        existing = {
            str(device.get("name"))
            for device in editable.document.get("devices", ())
            if isinstance(device, dict) and device.get("name")
        }
        default = candidate.display_name or Path(candidate.node).name
        while True:
            name = self._read(f"Profile name [{default}]: ").strip() or default
            if name not in existing:
                return name
            self._write(f"Profile name {name!r} already exists.")

    def confirm_port_binding(self, candidate: DeviceCandidate) -> bool:
        self._write(
            f"No stable serial distinguishes {candidate.node}. ID_PATH binds the profile to a USB port."
        )
        return self._confirm("Use this port-bound fallback? [y/N/x] ")

    def confirm_keyboard_capture(
        self, profile_name: str, candidate: DeviceCandidate
    ) -> bool:
        self._write(
            f"{candidate.node} is keyboard-class input. Capturing it can expose ordinary key presses."
        )
        return self._read(f"Type {profile_name!r} to continue: ") == profile_name

    def confirm_managed_permission(
        self,
        profile_name: str,
        candidate: DeviceCandidate,
    ) -> bool:
        self._write(f"{candidate.node} is readable through existing permissions.")
        return self._confirm(
            "Create a managed permission rule for this readable device? [y/N/x] "
        )

    def confirm_permission(
        self, profile_name: str, transaction: PermissionTransaction
    ) -> bool:
        rendered = transaction.rendered
        scope = rendered.scope
        self._write("Proposed udev rule:")
        self._write(rendered.content.rstrip())
        self._write(
            "Current scope: " + (", ".join(scope.current_nodes) or "no connected nodes")
        )
        self._write("Future scope: " + scope.future_predicate)
        if scope.broadened_fields:
            self._write("Rule cannot express: " + ", ".join(scope.broadened_fields))
        self._write("Privileged commands:")
        for command in transaction.preview_command_lines:
            self._write(command)
        self._write(
            "Reconnect the device after installation so setup can verify a new udev event and ACL."
        )
        if scope.keyboard_class:
            if (
                self._read(f"Type {profile_name!r} to approve keyboard-class scope: ")
                != profile_name
            ):
                return False
        return self._confirm("Install this rule with sudo? [y/N/x] ")

    def show_manual_permission(self, transaction: PermissionTransaction) -> None:
        self._write(
            "Permission rule was not installed. Run these reviewed commands manually:"
        )
        for command in transaction.preview_command_lines:
            self._write(command)

    def show_reconnect_instruction(self, timeout_seconds: float) -> None:
        self._write(
            "Rule installed. Reconnect the device now; "
            f"waiting up to {timeout_seconds:g} seconds..."
        )

    def choose_permission_access(self, profile_name: str) -> str:
        self._write(f'Default access for {profile_name!r}: TAG+="uaccess".')
        if not self._confirm("Use advanced input group access instead? [y/N/x] "):
            return UACCESS
        self._write(GROUP_FALLBACK_WARNING)
        if not self._confirm('Use GROUP="input" and MODE="0660"? [y/N/x] '):
            return UACCESS
        return INPUT_GROUP_ACCESS

    def choose_evdev_trigger(self, events: Sequence[str]) -> EvdevTriggerDraft:
        if not events:
            raise ValueError("No key or button press was captured.")
        self._write("Captured presses:")
        for index, event in enumerate(events, start=1):
            self._write(f"  {index}. {event}")
        self._write("Trigger mode:")
        self._write("  1. Toggle")
        self._write("  2. Separate on and off")
        mode = self._choice("Choose [1]: ", ("1", "2"), default="1")
        if mode == "1":
            event = events[self._number("Choose toggle event: ", len(events)) - 1]
            draft = EvdevTriggerDraft(mode="toggle", toggle_events=(event,))
            self._write(
                f"Default automatic off timeout: {draft.toggle_off_timeout_seconds:g} seconds."
            )
            if not self._confirm("Configure automatic off timeout? [y/N/x] "):
                return draft
            timeout = self._nonnegative_number(
                "Automatic off timeout seconds [60]: ", default=60.0
            )
            return EvdevTriggerDraft(
                mode="toggle",
                toggle_events=(event,),
                toggle_off_timeout_seconds=timeout,
            )
        on_event = events[self._number("Choose on event: ", len(events)) - 1]
        off_event = events[self._number("Choose off event: ", len(events)) - 1]
        return EvdevTriggerDraft(
            mode="on-off", on_events=(on_event,), off_events=(off_event,)
        )

    def arm_hidraw_trial(self, direction: str, number: int) -> None:
        self._read(
            f"Trial {number}/3: press Enter, then operate the control once for {direction}: "
        )

    def arm_evdev_capture(
        self,
        candidate: DeviceCandidate,
        timeout_seconds: float,
    ) -> None:
        self._write(f"Device ready for capture: {candidate.node}.")
        prompt = (
            f"Press Enter to start a {timeout_seconds:g}-second capture, "
            "or x to cancel: "
        )
        while True:
            value = self._read(prompt).strip().casefold()
            if not value:
                self._write("Capture started. Operate the intended control once.")
                return
            if value == "x":
                raise SetupExitRequested("Cancelled by user.")
            self._write("Press Enter to start capture, or x to cancel.")

    def retry_capture(self, error: Exception) -> bool:
        self._write(f"Capture failed: {error}")
        return self._confirm("Retry capture? [y/N/x] ")

    def confirm_config(self, diff: str) -> bool:
        self._write("Proposed configuration diff:")
        self._write(diff.rstrip() or "(no changes)")
        return self._confirm("Save this configuration? [y/N/x] ")

    def choose_service(
        self, snapshot: ServiceSnapshot, compatible: bool
    ) -> ServiceChoice:
        if not compatible:
            self._write("The packaged service cannot use this configuration path.")
            return (
                ServiceChoice.PRESERVE_STOPPED
                if snapshot.active
                else ServiceChoice.PRESERVE_INACTIVE
            )
        default = (
            ServiceChoice.RESTART
            if snapshot.active
            else ServiceChoice.PRESERVE_INACTIVE
        )
        self._write("Service action:")
        options = (
            ("1", ServiceChoice.RESTART, "restart"),
            ("2", ServiceChoice.PRESERVE_STOPPED, "leave stopped"),
            ("3", ServiceChoice.ENABLE_AND_START, "enable and start"),
            ("4", ServiceChoice.PRESERVE_INACTIVE, "preserve inactive state"),
        )
        for number, _choice, label in options:
            self._write(f"  {number}. {label}")
        default_number = next(
            number for number, choice, _label in options if choice is default
        )
        selected = self._choice(
            f"Choose [{default_number}]: ",
            tuple(item[0] for item in options),
            default=default_number,
        )
        choice = next(
            choice for number, choice, _label in options if number == selected
        )
        self._write("Result: " + _service_result(choice, snapshot))
        return choice

    def show_custom_service_command(self, command: str) -> None:
        self._write("Run the selected configuration in the foreground with:")
        self._write(command)

    def choose_backup(self, backups: Sequence[BackupInfo]) -> BackupInfo | None:
        if not backups:
            raise ValueError("No configuration backups are available.")
        ordered = tuple(
            sorted(backups, key=lambda backup: backup.timestamp, reverse=True)
        )
        self._write("Available configuration backups:")
        self._write("  0. Keep the current configuration")
        for index, backup in enumerate(ordered, start=1):
            self._write(
                f"  {index}. {backup.path.name} ({backup.timestamp.isoformat()})"
            )
        selected = self._number(
            "Choose backup to restore: ", len(ordered), allow_zero=True
        )
        return None if selected == 0 else ordered[selected - 1]

    def choose_post_commit_recovery(
        self,
        error: Exception,
        *,
        can_retry: bool,
        destination: Path,
        service_state: ServiceSnapshot | None,
    ) -> PostCommitChoice:
        self._write(
            f"Saved configuration {destination}, but setup could not complete: {error}"
        )
        if service_state is not None:
            self._write(
                "Observed service state: "
                + ("enabled" if service_state.enabled else "disabled")
                + ", "
                + ("active" if service_state.active else "inactive")
            )
        if can_retry and self._confirm("Retry the selected service action? [y/N/x] "):
            return PostCommitChoice.RETRY
        self._read("Press Enter to restore the prior configuration and service state: ")
        return PostCommitChoice.RESTORE

    def report_error(self, error: Exception) -> None:
        self._write(f"Setup failed: {error}")

    def report_recovery_failure(
        self, component: str, error: Exception, transaction=None
    ) -> None:
        self._write(f"Could not restore {component}: {error}")
        if transaction is not None:
            commands = getattr(transaction, "recovery_command_lines", ())
            if commands:
                self._write("Recovery commands:")
                for command in commands:
                    self._write(command)
            if getattr(transaction, "remaining_scope", None) is not None:
                self._write(
                    "Remaining access scope: "
                    + transaction.remaining_scope.future_predicate
                )
            artifacts = getattr(transaction, "recovery_artifacts", ())
            if getattr(transaction, "destination_may_be_active", False):
                self._write("The committed configuration may still be active.")
            if artifacts:
                self._write("Configuration recovery artifacts:")
                for artifact in artifacts:
                    self._write(str(artifact))

    def report_backup_restored(self, backup: BackupInfo) -> None:
        self._write(f"Restored configuration backup: {backup.path}")

    def report_success(
        self,
        destination: Path | None,
        service_state: ServiceSnapshot,
        *,
        physical_cycle: bool,
    ) -> None:
        self._write(f"Saved configuration: {destination}")
        self._write(
            "Service state: "
            + ("enabled" if service_state.enabled else "disabled")
            + ", "
            + ("active" if service_state.active else "inactive")
        )
        if physical_cycle:
            self._write(
                "Keep the device reconnected once before using the saved profile."
            )

    def report_cancelled(self, message: str) -> None:
        self._write(f"Setup cancelled: {message}")

    def _command(self, prompt: str) -> tuple[str, ...]:
        while True:
            try:
                return parse_command_line(self._required(prompt), which=self._which)
            except ValueError as error:
                self._write(str(error))

    def _confirm(self, prompt: str, *, default: bool = False) -> bool:
        while True:
            value = self._read(prompt).strip().casefold()
            if not value:
                return default
            if value in {"y", "yes"}:
                return True
            if value in {"n", "no"}:
                return False
            if value == "x":
                raise SetupExitRequested("Cancelled by user.")
            self._write("Enter y/yes, n/no, or x to cancel.")

    def _confirm_default(self, prompt: str, *, default: bool) -> bool:
        suffix = "[Y/n/x] " if default else "[y/N/x] "
        return self._confirm(prompt + suffix, default=default)

    def _choice(self, prompt: str, choices: Sequence[str], *, default: str) -> str:
        while True:
            value = self._read(prompt).strip() or default
            if value in choices:
                return value
            self._write("Choose one of: " + ", ".join(choices))

    def _number(self, prompt: str, upper: int, *, allow_zero: bool = False) -> int:
        while True:
            value = self._read(prompt).strip()
            try:
                number = int(value)
            except ValueError:
                number = 0
            if (allow_zero and number == 0) or 1 <= number <= upper:
                return number
            lower = 0 if allow_zero else 1
            self._write(f"Enter a number from {lower} to {upper}.")

    def _nonnegative_number(self, prompt: str, *, default: float) -> float:
        while True:
            value = self._read(prompt).strip()
            if not value:
                return default
            try:
                number = float(value)
            except ValueError:
                self._write("Enter a nonnegative number.")
                continue
            if math.isfinite(number) and number >= 0:
                return number
            self._write("Enter a nonnegative number.")

    def _configure_action_failure_behavior(self, draft: ActionDraft) -> ActionDraft:
        self._write(
            "Default failure behavior: "
            f"skip off after failed on={draft.skip_off_after_failed_on}, "
            f"skip on after failed off={draft.skip_on_after_failed_off}."
        )
        if not self._confirm("Configure advanced failure behavior? [y/N/x] "):
            return draft
        skip_off = self._confirm_default(
            "Skip off after a failed on command? ",
            default=bool(draft.skip_off_after_failed_on),
        )
        skip_on = self._confirm_default(
            "Skip on after a failed off command? ",
            default=bool(draft.skip_on_after_failed_off),
        )
        return ActionDraft(
            name=draft.name,
            on_command=draft.on_command,
            off_command=draft.off_command,
            skip_off_after_failed_on=skip_off,
            skip_on_after_failed_off=skip_on,
            off_on_shutdown=draft.off_on_shutdown,
            command_style=draft.command_style,
        )

    def _required(self, prompt: str) -> str:
        while True:
            value = self._read(prompt).strip()
            if value:
                return value
            self._write("A value is required.")

    def _read(self, prompt: str) -> str:
        self._output.write(prompt)
        self._output.flush()
        value = self._input.readline()
        if value == "":
            raise SetupCancelled("Input ended.")
        return value.rstrip("\n")

    def _write(self, message: str) -> None:
        self._output.write(message + "\n")
        self._output.flush()


def _service_result(choice: ServiceChoice, snapshot: ServiceSnapshot) -> str:
    if choice is ServiceChoice.RESTART:
        return "enabled and active" if snapshot.enabled else "disabled and active"
    if choice is ServiceChoice.ENABLE_AND_START:
        return "enabled and active"
    if choice is ServiceChoice.PRESERVE_STOPPED:
        return "enabled and inactive" if snapshot.enabled else "disabled and inactive"
    return "enabled and inactive" if snapshot.enabled else "disabled and inactive"

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import unified_diff
from enum import StrEnum
from functools import partial
import grp
import os
from pathlib import Path
import select
import shutil
import subprocess
import time
import stat
from typing import Any

import tomlkit
import pyudev
from evdev import InputDevice

from ..config import load_config, parse_config
from ..devices.discovery import DeviceCandidate, DeviceDiscovery, resolve_profiles
from ..models import AppConfig
from .actions import ActionDraft, apply_action_draft
from .capture import (
    HidrawCaptureError,
    capture_evdev_presses,
    capture_hidraw,
    apply_device_draft,
)
from .config_editor import ConfigCommitError, ConfigEditor, resolve_editable_config
from .devices import AmbiguousSelectorError, SelectorDraft, propose_selectors
from .permissions import (
    UACCESS,
    PermissionTransaction,
    ReconnectVerificationError,
    render_rule,
    verify_reconnected_candidate,
)
from .service import ServiceChoice, ServiceSnapshot


EVDEV_CAPTURE_TIMEOUT_SECONDS = 5.0
RECONNECT_TIMEOUT_SECONDS = 60.0
HIDRAW_READ_SIZE = 16 * 1024


class SetupCancelled(RuntimeError):
    """Raised when the user ends setup before it commits configuration."""


class SetupExitRequested(SetupCancelled):
    """Raised when the user explicitly requests setup exit with x."""


class PostCommitChoice(StrEnum):
    RETRY = "retry"
    RESTORE = "restore"


@dataclass
class SetupDependencies:
    config_editor: ConfigEditor
    discovery: Any
    capture_factory: Any
    permission_factory: Any
    service: Any
    prompts: Any


@dataclass(frozen=True)
class ReconnectMonitor:
    monitor: Any
    previously_matching_nodes: frozenset[str]


class SetupCaptureFactory:
    """Open a selected device as a bounded setup capture stream."""

    clock = time

    def __init__(
        self,
        *,
        input_device_factory=InputDevice,
        open_fn=os.open,
        read_fn=os.read,
        close_fn=os.close,
        select_fn=select.select,
    ):
        self._input_device_factory = input_device_factory
        self._open = open_fn
        self._read = read_fn
        self._close = close_fn
        self._select = select_fn

    def open(self, candidate: DeviceCandidate):
        if candidate.subsystem == "input":
            return _EvdevSetupStream(
                candidate.node,
                input_device_factory=self._input_device_factory,
                select_fn=self._select,
            )
        if candidate.subsystem == "hidraw":
            return _HidrawSetupStream(
                candidate.node,
                open_fn=self._open,
                read_fn=self._read,
                close_fn=self._close,
                select_fn=self._select,
            )
        raise ValueError(f"Unsupported capture subsystem: {candidate.subsystem}")


class _EvdevSetupStream:
    def __init__(self, node: str, *, input_device_factory, select_fn):
        self._device = input_device_factory(node)
        self._select = select_fn

    def read(self, timeout_seconds: float):
        readable, _, _ = self._select([self._device.fd], [], [], timeout_seconds)
        if not readable:
            return None
        return self._device.read_one()

    def close(self) -> None:
        self._device.close()


class _HidrawSetupStream:
    def __init__(self, node: str, *, open_fn, read_fn, close_fn, select_fn):
        self._descriptor = open_fn(node, os.O_RDONLY | os.O_NONBLOCK)
        self._read = read_fn
        self._close = close_fn
        self._select = select_fn

    def read(self, timeout_seconds: float) -> bytes | None:
        readable, _, _ = self._select([self._descriptor], [], [], timeout_seconds)
        if not readable:
            return None
        report = self._read(self._descriptor, HIDRAW_READ_SIZE)
        return report or None

    def close(self) -> None:
        self._close(self._descriptor)


class SetupPermissionFactory:
    """Install one reviewed rule and verify access on a fresh udev instance."""

    def __init__(
        self,
        discovery: DeviceDiscovery,
        *,
        context=None,
        monitor_factory=pyudev.Monitor.from_netlink,
        access_fn=os.access,
        acl_checker=None,
        group_checker=None,
        reconnect_timeout_seconds: float = RECONNECT_TIMEOUT_SECONDS,
        monotonic=time.monotonic,
    ):
        if reconnect_timeout_seconds <= 0:
            raise ValueError("reconnect_timeout_seconds must be positive")
        self._discovery = discovery
        self._context = context if context is not None else pyudev.Context()
        self._monitor_factory = monitor_factory
        self._access = access_fn
        self._acl_checker = acl_checker or _has_current_user_acl
        self._group_checker = group_checker or _has_input_group_mode_access
        self._reconnect_timeout_seconds = reconnect_timeout_seconds
        self._monotonic = monotonic

    def create(
        self,
        profile_name: str,
        selectors: SelectorDraft,
        candidates: tuple[DeviceCandidate, ...],
        *,
        access: str = UACCESS,
    ) -> PermissionTransaction:
        return PermissionTransaction(
            render_rule(profile_name, selectors, candidates, access=access)
        )

    def begin_reconnect_monitor(self, selectors: SelectorDraft) -> ReconnectMonitor:
        monitor = self._monitor_factory(self._context)
        expected_subsystem = "input" if selectors.transport == "evdev" else "hidraw"
        monitor.filter_by(expected_subsystem)
        previously_matching_nodes = frozenset(
            candidate.node
            for candidate in self._discovery.enumerate()
            if _candidate_matches_selectors(candidate, selectors)
        )
        monitor.start()
        return ReconnectMonitor(
            monitor=monitor,
            previously_matching_nodes=previously_matching_nodes,
        )

    def verify_reconnected(
        self,
        selectors: SelectorDraft,
        reconnect_monitor: ReconnectMonitor,
        *,
        access: str = UACCESS,
    ) -> DeviceCandidate:
        deadline = self._monotonic() + self._reconnect_timeout_seconds
        observed_matching_remove = False
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ReconnectVerificationError(
                    "Timed out waiting for a reconnected device."
                )
            device = reconnect_monitor.monitor.poll(timeout=remaining)
            if device is None:
                raise ReconnectVerificationError(
                    "Timed out waiting for a reconnected device."
                )
            action = getattr(device, "action", None)
            node = device.get("DEVNAME") or getattr(device, "device_node", None)
            if node is None:
                continue
            node = str(node)
            if action == "remove":
                if node in reconnect_monitor.previously_matching_nodes:
                    observed_matching_remove = True
                continue
            if action != "add" or not observed_matching_remove:
                continue
            candidates = self._discovery.enumerate()
            candidate = next(
                (
                    item
                    for item in candidates
                    if item.node == node
                    and _candidate_matches_selectors(item, selectors)
                ),
                None,
            )
            if candidate is None:
                continue
            return verify_reconnected_candidate(
                selectors,
                candidate,
                access_checker=lambda item: self._access(item.node, os.R_OK),
                acl_checker=self._acl_checker,
                production_resolution_checker=lambda item: self._resolves_provisionally(
                    item, selectors
                ),
                access=access,
                group_mode_checker=self._group_checker,
            )

    def _resolves_provisionally(
        self,
        candidate: DeviceCandidate,
        selectors: SelectorDraft,
    ) -> bool:
        matches = tuple(
            item
            for item in self._discovery.enumerate()
            if _candidate_matches_selectors(item, selectors)
        )
        return len(matches) == 1 and matches[0].node == candidate.node


def _has_current_user_acl(
    candidate: DeviceCandidate,
    *,
    runner=subprocess.run,
    uid=os.getuid,
) -> bool:
    """Check that uaccess granted this user read permission on the new node."""
    result = runner(
        (
            "/usr/bin/getfacl",
            "--numeric",
            "--absolute-names",
            "--omit-header",
            candidate.node,
        ),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )
    if getattr(result, "returncode", 0) != 0:
        return False
    expected_uid = str(uid())
    for line in (getattr(result, "stdout", "") or "").splitlines():
        kind, separator, remainder = line.partition(":")
        if kind != "user" or not separator:
            continue
        subject, separator, permissions = remainder.partition(":")
        if subject == expected_uid and separator and "r" in permissions:
            return True
    return False


def _has_input_group_mode_access(
    candidate: DeviceCandidate,
    *,
    stat_fn=os.stat,
    group_lookup=grp.getgrnam,
) -> bool:
    """Verify the rule-selected input group and group-read mode on the node."""
    try:
        node_stat = stat_fn(candidate.node)
        input_group_id = group_lookup("input").gr_gid
    except (KeyError, OSError):
        return False
    return node_stat.st_gid == input_group_id and bool(
        stat.S_IMODE(node_stat.st_mode) & stat.S_IRGRP
    )


def _candidate_matches_selectors(
    candidate: DeviceCandidate, selectors: SelectorDraft
) -> bool:
    expected_subsystem = "input" if selectors.transport == "evdev" else "hidraw"
    properties = candidate.properties
    return (
        candidate.subsystem == expected_subsystem
        and properties.get("ID_VENDOR_ID", "").lower() == selectors.vendor_id.lower()
        and properties.get("ID_MODEL_ID", "").lower() == selectors.product_id.lower()
        and (
            selectors.interface_number is None
            or properties.get("ID_USB_INTERFACE_NUM", "").lower().zfill(2)
            == selectors.interface_number.lower().zfill(2)
        )
        and (
            selectors.serial is None
            or properties.get("ID_SERIAL_SHORT") == selectors.serial
        )
        and (
            selectors.id_path is None or properties.get("ID_PATH") == selectors.id_path
        )
        and (
            selectors.classifier is None
            or properties.get(selectors.classifier[0]) == selectors.classifier[1]
        )
    )


def run_setup(config_path: Path | None) -> int:
    """Run setup with the concrete local-user adapters."""
    from .prompts import ConsoleSetupPrompts
    from .service import ServiceManager

    context = pyudev.Context()
    config_editor = ConfigEditor()
    location = resolve_editable_config(
        config_path,
        environ=os.environ,
        cwd=Path.cwd(),
        home=Path.home(),
    )
    discovery = DeviceDiscovery(context=context)
    dependencies = SetupDependencies(
        config_editor=config_editor,
        discovery=discovery,
        capture_factory=SetupCaptureFactory(),
        permission_factory=SetupPermissionFactory(discovery, context=context),
        service=ServiceManager(location),
        prompts=ConsoleSetupPrompts(which=shutil.which),
    )
    return SetupSession(config_path, dependencies).run()


class SetupSession:
    """Coordinate setup changes around the atomic configuration commit boundary."""

    def __init__(self, config_path: Path | None, dependencies: SetupDependencies):
        self._config_path = config_path
        self._dependencies = dependencies

    def run(self) -> int:
        editable = None
        service_snapshot: ServiceSnapshot | None = None
        permission_transaction = None
        config_transaction = None
        commit_started = False
        permission_installed = False
        config_committed = False

        try:
            editable = self._dependencies.config_editor.load(self._config_path)
            service_snapshot = self._dependencies.service.snapshot()
            self._dependencies.service.stop_for_setup(service_snapshot)
            backups = self._dependencies.config_editor.list_backups(
                editable.location.destination,
            )
            if backups:
                backup = self._dependencies.prompts.choose_backup(backups)
                if backup is not None:
                    try:
                        self._dependencies.config_editor.restore_backup(
                            editable, backup
                        )
                    except ConfigCommitError as error:
                        if (
                            error.potentially_committed
                            and error.transaction is not None
                        ):
                            return self._recover_after_commit(
                                error,
                                service_snapshot,
                                None,
                                error.transaction,
                                None,
                                editable.location.destination,
                                permission_installed=False,
                                can_retry=False,
                            )
                        raise
                    self._dependencies.prompts.report_backup_restored(backup)
                    editable = self._dependencies.config_editor.load(self._config_path)

            action = self._dependencies.prompts.choose_action(editable)
            candidates = tuple(self._dependencies.discovery.enumerate())
            selected = self._dependencies.prompts.choose_device(candidates)
            profile_name = self._dependencies.prompts.choose_profile_name(
                editable,
                selected,
            )
            selectors = self._propose_selectors(selected, candidates)
            self._confirm_keyboard_capture(profile_name, selected)

            capture_candidate = selected
            manage_permission = selected.event_codes is None
            if not manage_permission:
                manage_permission = (
                    self._dependencies.prompts.confirm_managed_permission(
                        profile_name,
                        selected,
                    )
                )
            if manage_permission:
                access = self._dependencies.prompts.choose_permission_access(
                    profile_name
                )
                permission_transaction = self._dependencies.permission_factory.create(
                    profile_name,
                    selectors,
                    candidates,
                    access=access,
                )
                permission_transaction.prepare()
                if not self._dependencies.prompts.confirm_permission(
                    profile_name,
                    permission_transaction,
                ):
                    self._dependencies.prompts.show_manual_permission(
                        permission_transaction,
                    )
                    raise SetupCancelled("Permission installation was declined.")
                reconnect_monitor = (
                    self._dependencies.permission_factory.begin_reconnect_monitor(
                        selectors,
                    )
                )
                try:
                    permission_transaction.install()
                except Exception as error:
                    if permission_transaction.destination_write_succeeded:
                        permission_installed = True
                    else:
                        self._dependencies.prompts.report_recovery_failure(
                            "permission",
                            error,
                            permission_transaction,
                        )
                    raise
                permission_installed = True
                self._dependencies.prompts.show_reconnect_instruction(
                    RECONNECT_TIMEOUT_SECONDS,
                )
                capture_candidate = (
                    self._dependencies.permission_factory.verify_reconnected(
                        selectors,
                        reconnect_monitor,
                        access=access,
                    )
                )

            trigger = self._capture_trigger(capture_candidate)
            document = self._proposed_document(
                editable.document, action, profile_name, selectors, trigger
            )
            config = self._validate_document(document)
            self._validate_runtime_profile(config, profile_name, capture_candidate.node)
            diff = self._document_diff(editable.document, document)
            if not self._dependencies.prompts.confirm_config(diff):
                raise SetupCancelled("Configuration was not confirmed.")

            if editable.location.packaged_service_compatible:
                service_choice = self._dependencies.prompts.choose_service(
                    service_snapshot,
                    True,
                )
                service_operation = partial(
                    self._dependencies.service.apply,
                    service_choice,
                )
            else:
                self._dependencies.prompts.show_custom_service_command(
                    self._dependencies.service.manual_daemon_command,
                )
                service_choice = None
                service_operation = partial(
                    self._dependencies.service.restore,
                    service_snapshot,
                )

            config_transaction = self._dependencies.config_editor.begin(editable)
            commit_started = True
            try:
                config_transaction.commit(document)
                config_committed = True
            except ConfigCommitError as error:
                if error.potentially_committed:
                    config_committed = True
                    return self._recover_after_commit(
                        error,
                        service_snapshot,
                        service_choice,
                        config_transaction,
                        permission_transaction,
                        editable.location.destination,
                        permission_installed=permission_installed,
                        can_retry=False,
                    )
                raise

            try:
                load_config(editable.location.destination)
            except Exception as error:
                return self._recover_after_commit(
                    error,
                    service_snapshot,
                    service_choice,
                    config_transaction,
                    permission_transaction,
                    editable.location.destination,
                    permission_installed=permission_installed,
                    can_retry=False,
                )

            try:
                service_operation()
            except Exception as error:
                return self._recover_after_commit(
                    error,
                    service_snapshot,
                    service_choice,
                    config_transaction,
                    permission_transaction,
                    editable.location.destination,
                    permission_installed=permission_installed,
                    can_retry=True,
                    retry_service_operation=service_operation,
                )

            return self._finalize_success(
                editable.location.destination,
                config_transaction,
                permission_transaction,
            )
        except SetupCancelled as error:
            if config_committed:
                self._dependencies.prompts.report_cancelled(str(error))
                return 1
            recovered = self._recover_before_commit(
                service_snapshot,
                permission_transaction,
                config_transaction,
                commit_started,
                permission_installed,
            )
            if recovered:
                self._dependencies.prompts.report_cancelled(str(error))
                return 0
            return 1
        except Exception as error:
            self._dependencies.prompts.report_error(error)
            if config_committed:
                return 1
            self._recover_before_commit(
                service_snapshot,
                permission_transaction,
                config_transaction,
                commit_started,
                permission_installed,
            )
            return 1

    def _propose_selectors(
        self,
        selected: DeviceCandidate,
        candidates: tuple[DeviceCandidate, ...],
    ) -> SelectorDraft:
        try:
            return propose_selectors(selected, candidates)
        except AmbiguousSelectorError:
            if not self._dependencies.prompts.confirm_port_binding(selected):
                raise
            return propose_selectors(
                selected,
                candidates,
                allow_port_binding=True,
            )

    def _confirm_keyboard_capture(
        self,
        profile_name: str,
        selected: DeviceCandidate,
    ) -> None:
        if (
            selected.keyboard_class
            and not self._dependencies.prompts.confirm_keyboard_capture(
                profile_name,
                selected,
            )
        ):
            raise SetupCancelled("Keyboard capture was not confirmed.")

    def _capture_trigger(self, candidate: DeviceCandidate):
        while True:
            if candidate.subsystem == "input":
                self._dependencies.prompts.arm_evdev_capture(
                    candidate,
                    EVDEV_CAPTURE_TIMEOUT_SECONDS,
                )
            stream = self._dependencies.capture_factory.open(candidate)
            try:
                if candidate.subsystem == "input":
                    events = capture_evdev_presses(
                        stream,
                        timeout_seconds=EVDEV_CAPTURE_TIMEOUT_SECONDS,
                    )
                    return self._dependencies.prompts.choose_evdev_trigger(events)
                if candidate.subsystem == "hidraw":
                    return capture_hidraw(
                        stream,
                        arm_trial=self._dependencies.prompts.arm_hidraw_trial,
                        clock=getattr(
                            self._dependencies.capture_factory, "clock", time
                        ),
                    )
                raise ValueError(
                    f"Unsupported capture subsystem: {candidate.subsystem}"
                )
            except HidrawCaptureError as error:
                if not self._dependencies.prompts.retry_capture(error):
                    raise
            finally:
                stream.close()

    @staticmethod
    def _proposed_document(
        original: tomlkit.TOMLDocument,
        action: ActionDraft,
        profile_name: str,
        selectors: SelectorDraft,
        trigger: Any,
    ) -> tomlkit.TOMLDocument:
        document = tomlkit.parse(tomlkit.dumps(original))
        actions = document.get("actions")
        if not isinstance(actions, dict) or action.name not in actions:
            apply_action_draft(document, action)
        apply_device_draft(
            document,
            name=profile_name,
            action=action.name,
            selectors=selectors,
            trigger=trigger,
        )
        return document

    @staticmethod
    def _validate_document(document: tomlkit.TOMLDocument) -> AppConfig:
        return parse_config(tomlkit.parse(tomlkit.dumps(document)))

    def _validate_runtime_profile(
        self,
        config: AppConfig,
        profile_name: str,
        captured_node: str,
    ) -> None:
        resolutions = resolve_profiles(
            config.devices,
            self._dependencies.discovery.enumerate(),
        )
        resolution = next(
            item for item in resolutions if item.profile.name == profile_name
        )
        if not resolution.is_available or resolution.node != captured_node:
            raise ValueError(
                f"Captured profile {profile_name!r} failed runtime resolution: "
                f"{resolution.status}."
            )

    @staticmethod
    def _document_diff(
        original: tomlkit.TOMLDocument,
        document: tomlkit.TOMLDocument,
    ) -> str:
        return "".join(
            unified_diff(
                tomlkit.dumps(original).splitlines(keepends=True),
                tomlkit.dumps(document).splitlines(keepends=True),
                fromfile="current configuration",
                tofile="proposed configuration",
            )
        )

    def _recover_after_commit(
        self,
        error: Exception,
        service_snapshot: ServiceSnapshot,
        service_choice: ServiceChoice | None,
        config_transaction: Any,
        permission_transaction: Any,
        destination: Path,
        *,
        permission_installed: bool,
        can_retry: bool,
        retry_service_operation: Callable[[], None] | None = None,
    ) -> int:
        service_state = self._observe_service_state()
        while True:
            try:
                choice = self._dependencies.prompts.choose_post_commit_recovery(
                    error,
                    can_retry=can_retry,
                    destination=destination,
                    service_state=service_state,
                )
            except SetupExitRequested as cancelled:
                self._restore_committed_state(
                    service_snapshot,
                    config_transaction,
                    permission_transaction,
                    permission_installed,
                )
                self._dependencies.prompts.report_cancelled(str(cancelled))
                return 1
            except SetupCancelled as cancelled:
                self._dependencies.prompts.report_cancelled(str(cancelled))
                return 1

            if (
                choice is PostCommitChoice.RETRY
                and can_retry
                and retry_service_operation is not None
            ):
                try:
                    retry_service_operation()
                except Exception as retry_error:
                    error = retry_error
                    service_state = self._observe_service_state()
                    continue
                return self._finalize_success(
                    destination,
                    config_transaction,
                    permission_transaction,
                )

            if choice is PostCommitChoice.RESTORE:
                self._restore_committed_state(
                    service_snapshot,
                    config_transaction,
                    permission_transaction,
                    permission_installed,
                )
                return 1

            self._dependencies.prompts.report_error(
                ValueError("Unsupported post-commit recovery choice."),
            )

    def _recover_before_commit(
        self,
        service_snapshot: ServiceSnapshot | None,
        permission_transaction: Any,
        config_transaction: Any,
        commit_started: bool,
        permission_installed: bool,
    ) -> bool:
        recovered = True
        if commit_started and config_transaction is not None:
            recovered = (
                self._restore_component(
                    "config",
                    config_transaction.restore,
                    transaction=config_transaction,
                )
                and recovered
            )
        if permission_installed:
            recovered = (
                self._restore_component(
                    "permission",
                    permission_transaction.rollback,
                    transaction=permission_transaction,
                )
                and recovered
            )
        if service_snapshot is not None:
            recovered = (
                self._restore_component(
                    "service",
                    lambda: self._dependencies.service.restore(service_snapshot),
                )
                and recovered
            )
        return recovered

    def _restore_committed_state(
        self,
        service_snapshot: ServiceSnapshot,
        config_transaction: Any,
        permission_transaction: Any,
        permission_installed: bool,
    ) -> bool:
        recovered = self._restore_component(
            "config",
            config_transaction.restore,
            transaction=config_transaction,
        )
        if permission_installed:
            recovered = (
                self._restore_component(
                    "permission",
                    permission_transaction.rollback,
                    transaction=permission_transaction,
                )
                and recovered
            )
        return (
            self._restore_component(
                "service",
                lambda: self._dependencies.service.restore(service_snapshot),
            )
            and recovered
        )

    def _finalize_success(
        self,
        destination: Path | None,
        config_transaction: Any,
        permission_transaction: Any,
    ) -> int:
        service_state = self._dependencies.service.snapshot()
        if permission_transaction is not None:
            permission_transaction.finalize()
        config_transaction.finalize()
        self._dependencies.prompts.report_success(
            destination,
            service_state,
            physical_cycle=permission_transaction is not None,
        )
        return 0

    def _observe_service_state(self) -> ServiceSnapshot | None:
        try:
            return self._dependencies.service.snapshot()
        except Exception as error:
            self._dependencies.prompts.report_recovery_failure(
                "service observation",
                error,
            )
            return None

    def _restore_component(
        self, component: str, operation, *, transaction=None
    ) -> bool:
        try:
            operation()
        except Exception as error:
            self._dependencies.prompts.report_recovery_failure(
                component,
                error,
                transaction,
            )
            return False
        return True

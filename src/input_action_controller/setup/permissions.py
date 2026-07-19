from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
from typing import Protocol
import unicodedata

from ..devices.classifiers import INPUT_CLASSIFIER_NAMES, is_key_classifier
from ..devices.discovery import DeviceCandidate
from .devices import SelectorDraft


MANAGED_RULES_DIRECTORY = Path("/etc/udev/rules.d")
UACCESS = "uaccess"
INPUT_GROUP_ACCESS = "input-group"
GROUP_FALLBACK_WARNING = (
    "Membership in the input group can expose other matching input nodes."
)

_MANAGED_FILENAME = re.compile(
    r"70-input-action-controller-"
    r"[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{12}\.rules"
)
_USB_ID = re.compile(r"[0-9a-f]{4}")
_INTERFACE_NUMBER = re.compile(r"[0-9a-f]{2}")
_SUDO = "/usr/bin/sudo"
_INSTALL = "/usr/bin/install"
_RM = "/usr/bin/rm"
_UDEVADM = "/usr/bin/udevadm"
_RELOAD_RULES = (
    _SUDO,
    "--",
    _UDEVADM,
    "control",
    "--reload-rules",
)


class InvalidManagedDestination(ValueError):
    """Raised when a transaction targets a path setup does not own."""


class UnreadableManagedRule(RuntimeError):
    """Raised when setup cannot snapshot an existing managed rule."""


class ReconnectVerificationError(RuntimeError):
    """Raised when a reconnect observation fails identity or access checks."""


@dataclass(frozen=True)
class ScopeReport:
    current_nodes: tuple[str, ...]
    future_predicate: str
    broadened_fields: tuple[str, ...]
    keyboard_class: bool


@dataclass(frozen=True)
class RenderedRule:
    destination: Path
    content: str
    scope: ScopeReport

    @property
    def access_warning(self) -> str | None:
        if 'GROUP="input"' in self.content:
            return GROUP_FALLBACK_WARNING
        return None


@dataclass(frozen=True)
class DeviceInstanceObservation:
    candidate: DeviceCandidate
    instance_id: str


class PostUdevEventObserver(Protocol):
    """Return one device observation created after a new udev event."""

    def __call__(self) -> DeviceInstanceObservation: ...


class AccessChecker(Protocol):
    def __call__(self, candidate: DeviceCandidate) -> bool: ...


class AclChecker(Protocol):
    def __call__(self, candidate: DeviceCandidate) -> bool: ...


class GroupModeChecker(Protocol):
    def __call__(self, candidate: DeviceCandidate) -> bool: ...


class ProductionResolutionChecker(Protocol):
    def __call__(self, candidate: DeviceCandidate) -> bool: ...


class ArgvRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        check: bool,
        shell: bool,
    ) -> object: ...


ExistingRuleReader = Callable[[Path], tuple[bytes, int] | None]


def render_rule(
    profile_name: str,
    selectors: SelectorDraft,
    candidates: Sequence[DeviceCandidate],
    *,
    access: str = UACCESS,
) -> RenderedRule:
    """Render one managed rule and describe every node it can affect."""
    normalized = _normalize_selectors(selectors)
    if access not in {UACCESS, INPUT_GROUP_ACCESS}:
        raise ValueError(
            f"unsupported udev access mode {access!r}; choose {UACCESS!r} "
            f"or explicitly choose {INPUT_GROUP_ACCESS!r}"
        )

    match_clauses = _match_clauses(normalized)
    access_clauses = (
        ('TAG+="uaccess"',) if access == UACCESS else ('GROUP="input"', 'MODE="0660"')
    )
    content = ", ".join((*match_clauses, *access_clauses)) + "\n"
    current_matches = tuple(
        candidate
        for candidate in candidates
        if _candidate_matches(candidate, normalized)
    )
    current_nodes = tuple(sorted({candidate.node for candidate in current_matches}))
    future_predicate = (
        "Any later device matching "
        + ", ".join(match_clauses)
        + " receives the same access."
    )
    if access == INPUT_GROUP_ACCESS:
        future_predicate += f" {GROUP_FALLBACK_WARNING}"
    keyboard_class = is_key_classifier(normalized.classifier) or any(
        candidate.keyboard_class for candidate in current_matches
    )
    scope = ScopeReport(
        current_nodes=current_nodes,
        future_predicate=future_predicate,
        broadened_fields=(("event_codes",) if normalized.transport == "evdev" else ()),
        keyboard_class=keyboard_class,
    )
    destination = MANAGED_RULES_DIRECTORY / (
        "70-input-action-controller-"
        f"{_profile_slug(profile_name)}-{_selector_hash(normalized)}.rules"
    )
    return RenderedRule(destination=destination, content=content, scope=scope)


def verify_reconnected_access(
    selectors: SelectorDraft,
    *,
    previous_instance_ids: Sequence[str],
    observe_after_udev_event: PostUdevEventObserver,
    access_checker: AccessChecker,
    acl_checker: AclChecker,
    production_resolution_checker: ProductionResolutionChecker,
    access: str = UACCESS,
    group_mode_checker: GroupModeChecker | None = None,
) -> DeviceCandidate:
    """Require a new event instance, mode-specific access, and production resolution."""
    normalized = _normalize_selectors(selectors)
    observation = observe_after_udev_event()
    if not observation.instance_id.strip():
        raise ReconnectVerificationError(
            "Reconnect observation is missing a stable instance identity."
        )
    if observation.instance_id in frozenset(previous_instance_ids):
        raise ReconnectVerificationError(
            "Reconnect observation refers to an old device instance."
        )

    return verify_reconnected_candidate(
        normalized,
        observation.candidate,
        access_checker=access_checker,
        acl_checker=acl_checker,
        production_resolution_checker=production_resolution_checker,
        access=access,
        group_mode_checker=group_mode_checker,
    )


def verify_reconnected_candidate(
    selectors: SelectorDraft,
    candidate: DeviceCandidate,
    *,
    access_checker: AccessChecker,
    acl_checker: AclChecker,
    production_resolution_checker: ProductionResolutionChecker,
    access: str = UACCESS,
    group_mode_checker: GroupModeChecker | None = None,
) -> DeviceCandidate:
    """Require selector, access, and provisional-resolution checks for one new node."""
    normalized = _normalize_selectors(selectors)

    if not _candidate_matches(candidate, normalized):
        raise ReconnectVerificationError(
            f"Observed node {candidate.node} does not match the configured selectors."
        )
    if not access_checker(candidate):
        raise ReconnectVerificationError(
            f"New node {candidate.node} does not grant read access."
        )
    if access == UACCESS:
        if not acl_checker(candidate):
            raise ReconnectVerificationError(
                f"New node {candidate.node} does not have the expected ACL."
            )
    elif access == INPUT_GROUP_ACCESS:
        if group_mode_checker is None or not group_mode_checker(candidate):
            raise ReconnectVerificationError(
                f"New node {candidate.node} does not have the expected input-group mode."
            )
    else:
        raise ValueError(f"unsupported udev access mode {access!r}")
    if not production_resolution_checker(candidate):
        raise ReconnectVerificationError(
            f"New node {candidate.node} failed production resolution."
        )
    return candidate


class PermissionTransaction:
    """Own provisional installation and exact recovery for one managed rule."""

    def __init__(
        self,
        rendered: RenderedRule,
        runner: ArgvRunner = subprocess.run,
        *,
        staging_directory: Path | None = None,
        existing_rule_reader: ExistingRuleReader | None = None,
    ):
        _validate_managed_destination(rendered.destination)
        self.rendered = rendered
        self._runner = runner
        self._staging_directory = staging_directory
        self._existing_rule_reader = existing_rule_reader or _read_existing_rule
        self._existing_rule: tuple[bytes, int] | None = None
        self._prepared = False
        self._destination_changed = False
        self._destination_write_succeeded = False
        self._rollback_applied = False
        self._installed = False
        self._finalized = False
        self._staging_path: Path | None = None
        self._rollback_staging_path: Path | None = None

    @property
    def staging_path(self) -> Path | None:
        return self._staging_path

    @property
    def rollback_staging_path(self) -> Path | None:
        return self._rollback_staging_path

    @property
    def preview_commands(self) -> tuple[tuple[str, ...], ...]:
        if self._finalized or self._staging_path is None:
            return ()
        return (self._install_command(), _RELOAD_RULES)

    @property
    def preview_command_lines(self) -> tuple[str, ...]:
        return tuple(shlex.join(command) for command in self.preview_commands)

    @property
    def recovery_commands(self) -> tuple[tuple[str, ...], ...]:
        if self._finalized or self._staging_path is None:
            return ()
        if self._destination_changed:
            if self._rollback_applied:
                return (_RELOAD_RULES,)
            return (self._rollback_command(), _RELOAD_RULES)
        return self.preview_commands

    @property
    def recovery_command_lines(self) -> tuple[str, ...]:
        return tuple(shlex.join(command) for command in self.recovery_commands)

    @property
    def remaining_scope(self) -> ScopeReport | None:
        return self.rendered.scope if self._destination_changed else None

    @property
    def destination_write_succeeded(self) -> bool:
        """Whether the privileged destination write command returned successfully."""
        return self._destination_write_succeeded

    def install(self) -> None:
        if self._finalized:
            raise RuntimeError("Cannot install a finalized permission transaction.")
        if self._destination_changed:
            raise RuntimeError("The managed rule is already provisional.")
        self.prepare()
        install_command = self._install_command()
        self._destination_changed = True
        self._run(install_command)
        self._destination_write_succeeded = True
        self._run(_RELOAD_RULES)
        self._installed = True

    def rollback(self) -> None:
        if self._finalized:
            raise RuntimeError("Cannot roll back a finalized permission transaction.")
        if not self._destination_changed:
            return
        if not self._rollback_applied:
            self._run(self._rollback_command())
            self._rollback_applied = True
        self._run(_RELOAD_RULES)
        self._destination_changed = False
        self._installed = False
        self._remove_staging()

    def finalize(self) -> None:
        if self._finalized:
            return
        if not self._installed:
            raise RuntimeError(
                "Cannot finalize before the managed rule is installed and reloaded."
            )
        self._remove_staging()
        self._destination_changed = False
        self._finalized = True

    def prepare(self) -> None:
        if self._prepared:
            return
        try:
            existing = self._existing_rule_reader(self.rendered.destination)
        except OSError as error:
            raise UnreadableManagedRule(
                f"Cannot read existing managed rule {self.rendered.destination}: "
                f"{error}. Fix its read access before replacing it."
            ) from error
        if existing is not None:
            content, mode = existing
            if not isinstance(content, bytes) or not isinstance(mode, int):
                raise TypeError(
                    "existing_rule_reader must return (bytes, mode) or None"
                )
            self._existing_rule = (content, stat.S_IMODE(mode))

        try:
            self._staging_path = _write_staging_file(
                self.rendered.content.encode("utf-8"),
                directory=self._staging_directory,
                prefix=f"{self.rendered.destination.stem}.",
            )
            if self._existing_rule is not None:
                self._rollback_staging_path = _write_staging_file(
                    self._existing_rule[0],
                    directory=self._staging_directory,
                    prefix=f"{self.rendered.destination.stem}.rollback.",
                )
        except BaseException:
            self._remove_staging()
            raise
        self._prepared = True

    def _install_command(self) -> tuple[str, ...]:
        if self._staging_path is None:
            raise RuntimeError("Permission staging has not been prepared.")
        return (
            _SUDO,
            "--",
            _INSTALL,
            "-m",
            "0644",
            str(self._staging_path),
            str(self.rendered.destination),
        )

    def _rollback_command(self) -> tuple[str, ...]:
        if self._existing_rule is None:
            return (
                _SUDO,
                "--",
                _RM,
                "-f",
                "--",
                str(self.rendered.destination),
            )
        if self._rollback_staging_path is None:
            raise RuntimeError("Rollback staging has not been prepared.")
        return (
            _SUDO,
            "--",
            _INSTALL,
            "-m",
            f"{self._existing_rule[1]:04o}",
            str(self._rollback_staging_path),
            str(self.rendered.destination),
        )

    def _run(self, argv: tuple[str, ...]) -> None:
        result = self._runner(argv, check=True, shell=False)
        returncode = getattr(result, "returncode", 0)
        if returncode:
            raise subprocess.CalledProcessError(returncode, argv)

    def _remove_staging(self) -> None:
        paths = (self._staging_path, self._rollback_staging_path)
        for path in paths:
            if path is not None:
                path.unlink(missing_ok=True)
        self._staging_path = None
        self._rollback_staging_path = None


def _normalize_selectors(selectors: SelectorDraft) -> SelectorDraft:
    if selectors.transport not in {"evdev", "hidraw"}:
        raise ValueError(f"unsupported transport: {selectors.transport}")
    vendor_id = selectors.vendor_id.lower()
    product_id = selectors.product_id.lower()
    if _USB_ID.fullmatch(vendor_id) is None:
        raise ValueError(f"invalid USB vendor ID: {selectors.vendor_id!r}")
    if _USB_ID.fullmatch(product_id) is None:
        raise ValueError(f"invalid USB product ID: {selectors.product_id!r}")

    interface_number = selectors.interface_number
    if interface_number is not None:
        interface_number = interface_number.lower().zfill(2)
        if _INTERFACE_NUMBER.fullmatch(interface_number) is None:
            raise ValueError(
                f"invalid USB interface number: {selectors.interface_number!r}"
            )

    classifier = selectors.classifier
    if classifier is not None:
        if selectors.transport != "evdev":
            raise ValueError("a stable classifier is valid only for evdev rules")
        if classifier[0] not in INPUT_CLASSIFIER_NAMES or classifier[1] != "1":
            raise ValueError(f"unsupported evdev classifier: {classifier!r}")

    for name, value in (
        ("serial", selectors.serial),
        ("ID path", selectors.id_path),
    ):
        if value is not None:
            _validate_rule_value(name, value)

    return replace(
        selectors,
        vendor_id=vendor_id,
        product_id=product_id,
        interface_number=interface_number,
    )


def _match_clauses(selectors: SelectorDraft) -> tuple[str, ...]:
    subsystem, kernel = (
        ("input", "event*") if selectors.transport == "evdev" else ("hidraw", "hidraw*")
    )
    clauses = [
        'ACTION=="add"',
        f'SUBSYSTEM=="{subsystem}"',
        f'KERNEL=="{kernel}"',
        f'ATTRS{{idVendor}}=="{selectors.vendor_id}"',
        f'ATTRS{{idProduct}}=="{selectors.product_id}"',
    ]
    if selectors.interface_number is not None:
        clauses.append(
            "ENV{ID_USB_INTERFACE_NUM}=="
            f'"{_escape_rule_value(selectors.interface_number)}"'
        )
    if selectors.serial is not None:
        clauses.append(
            f'ENV{{ID_SERIAL_SHORT}}=="{_escape_rule_value(selectors.serial)}"'
        )
    if selectors.id_path is not None:
        clauses.append(f'ENV{{ID_PATH}}=="{_escape_rule_value(selectors.id_path)}"')
    if selectors.classifier is not None:
        name, value = selectors.classifier
        clauses.append(f'ENV{{{name}}}=="{value}"')
    return tuple(clauses)


def _candidate_matches(
    candidate: DeviceCandidate,
    selectors: SelectorDraft,
) -> bool:
    expected_subsystem = "input" if selectors.transport == "evdev" else "hidraw"
    expected_prefix = (
        "/dev/input/event" if selectors.transport == "evdev" else "/dev/hidraw"
    )
    if candidate.subsystem != expected_subsystem:
        return False
    if not candidate.node.startswith(expected_prefix):
        return False
    properties = candidate.properties
    if properties.get("ID_VENDOR_ID", "").lower() != selectors.vendor_id:
        return False
    if properties.get("ID_MODEL_ID", "").lower() != selectors.product_id:
        return False
    if selectors.interface_number is not None:
        interface_number = properties.get("ID_USB_INTERFACE_NUM")
        if (
            interface_number is None
            or interface_number.lower().zfill(2) != selectors.interface_number
        ):
            return False
    if (
        selectors.serial is not None
        and properties.get("ID_SERIAL_SHORT") != selectors.serial
    ):
        return False
    if selectors.id_path is not None and properties.get("ID_PATH") != selectors.id_path:
        return False
    if selectors.classifier is not None:
        name, value = selectors.classifier
        if properties.get(name) != value:
            return False
    return True


def _profile_slug(profile_name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", profile_name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    slug = slug[:55].rstrip("-") or "profile"
    profile_hash = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()
    return f"{slug}-{profile_hash}"


def _selector_hash(selectors: SelectorDraft) -> str:
    canonical = json.dumps(
        asdict(selectors),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()[:12]


def _validate_rule_value(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains a control character")


def _escape_rule_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validate_managed_destination(destination: Path) -> None:
    expected = MANAGED_RULES_DIRECTORY / destination.name
    if (
        not destination.is_absolute()
        or destination.parent != MANAGED_RULES_DIRECTORY
        or destination != expected
        or _MANAGED_FILENAME.fullmatch(destination.name) is None
    ):
        raise InvalidManagedDestination(
            f"Permission destination is outside the managed rule scheme: {destination}"
        )


def _read_existing_rule(path: Path) -> tuple[bytes, int] | None:
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(path_info.st_mode):
        raise OSError(f"managed destination is not a regular file: {path}")

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            raise OSError(f"managed destination is not a regular file: {path}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                return b"".join(chunks), stat.S_IMODE(file_info.st_mode)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _write_staging_file(
    content: bytes,
    *,
    directory: Path | None,
    prefix: str,
) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=prefix,
        suffix=".rules",
        dir=None if directory is None else directory,
    )
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError(f"Cannot write permission staging file {path}.")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return path

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import secrets
import shlex
from typing import Callable, Mapping

import tomlkit

from ..config import ConfigError, parse_config


CONFIG_DIRECTORY = "input-action-controller"
CONFIG_FILENAME = "config.toml"
SYSTEM_CONFIG = Path("/etc") / CONFIG_DIRECTORY / CONFIG_FILENAME
SYSTEM_ROOT = Path("/etc")


class SetupConfigError(RuntimeError):
    pass


class ConfigCommitError(SetupConfigError):
    def __init__(
        self,
        message: str,
        *,
        potentially_committed: bool,
        transaction: "ConfigTransaction | None" = None,
    ):
        self.potentially_committed = potentially_committed
        self.transaction = transaction
        state = (
            "may have replaced the destination"
            if potentially_committed
            else "did not replace the destination"
        )
        super().__init__(f"{message}; setup {state}")


@dataclass(frozen=True)
class ConfigLocation:
    source: Path | None
    destination: Path
    seed_from_system: bool
    packaged_service_compatible: bool


@dataclass
class EditableConfig:
    location: ConfigLocation
    document: tomlkit.TOMLDocument
    existed: bool


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    timestamp: datetime


class FileOperations:
    """Small injectable set of operations that form the save transaction."""

    def open_file(self, name: str, flags: int, mode: int, *, directory_fd: int) -> int:
        return os.open(name, flags, mode, dir_fd=directory_fd)

    def write(self, descriptor: int, content: bytes) -> int:
        return os.write(descriptor, content)

    def chmod(self, descriptor: int, mode: int) -> None:
        os.fchmod(descriptor, mode)

    def fsync(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)

    def replace(self, source: str, destination: str, *, directory_fd: int) -> None:
        os.replace(
            source, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )

    def rename(self, source: str, destination: str, *, directory_fd: int) -> None:
        os.replace(
            source, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )

    def link(self, source: str, destination: str, *, directory_fd: int) -> None:
        os.link(
            source,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )

    def unlink(self, name: str, *, directory_fd: int) -> None:
        os.unlink(name, dir_fd=directory_fd)

    def stat(self, name: str, *, directory_fd: int) -> os.stat_result:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

    def mkdir(self, name: str, mode: int, *, directory_fd: int) -> None:
        os.mkdir(name, mode, dir_fd=directory_fd)

    def open_directory(self, name: str, *, directory_fd: int | None = None) -> int:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        return os.open(name, flags, dir_fd=directory_fd)

    def read_file(self, name: str, *, directory_fd: int) -> bytes:
        descriptor = self.open_file(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            0,
            directory_fd=directory_fd,
        )
        try:
            chunks = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            self.close(descriptor)

    def directory_path(self, descriptor: int) -> Path:
        return Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve(strict=True)


def resolve_editable_config(
    explicit: Path | None,
    *,
    environ: Mapping[str, str],
    cwd: Path,
    home: Path,
) -> ConfigLocation:
    return _resolve_editable_config(
        explicit,
        environ=environ,
        cwd=cwd,
        home=home,
        system_config=SYSTEM_CONFIG,
        system_root=SYSTEM_ROOT,
    )


def _resolve_editable_config(
    explicit: Path | None,
    *,
    environ: Mapping[str, str],
    cwd: Path,
    home: Path,
    system_config: Path,
    system_root: Path,
) -> ConfigLocation:
    xdg_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    xdg_destination = _normalize_path(
        xdg_home / CONFIG_DIRECTORY / CONFIG_FILENAME, cwd
    )
    override = explicit if explicit is not None else _environment_path(environ)

    if override is not None:
        destination = _normalize_path(override, cwd)
        _reject_system_destination(destination, system_config, system_root)
        _reject_symbolic_link(destination)
        _reject_non_file_destination(destination)
        _reject_unwritable_destination(destination)
        return ConfigLocation(
            source=destination if destination.exists() else None,
            destination=destination,
            seed_from_system=False,
            packaged_service_compatible=destination == xdg_destination,
        )

    _reject_system_destination(xdg_destination, system_config, system_root)
    _reject_symbolic_link(xdg_destination)
    _reject_non_file_destination(xdg_destination)
    _reject_unwritable_destination(xdg_destination)
    if xdg_destination.exists():
        return ConfigLocation(
            source=xdg_destination,
            destination=xdg_destination,
            seed_from_system=False,
            packaged_service_compatible=True,
        )

    if system_config.is_file():
        return ConfigLocation(
            source=system_config,
            destination=xdg_destination,
            seed_from_system=True,
            packaged_service_compatible=True,
        )

    return ConfigLocation(
        source=None,
        destination=xdg_destination,
        seed_from_system=False,
        packaged_service_compatible=True,
    )


def _environment_path(environ: Mapping[str, str]) -> Path | None:
    value = environ.get("INPUT_ACTION_CONTROLLER_CONFIG")
    return Path(value) if value else None


def _normalize_path(path: Path, cwd: Path) -> Path:
    if path.is_absolute():
        return Path(os.path.normpath(path))
    return Path(os.path.normpath(cwd / path))


def _reject_system_destination(
    destination: Path, system_config: Path, system_root: Path
) -> None:
    try:
        resolved = destination.resolve(strict=False)
    except OSError as error:
        raise SetupConfigError(
            f"Cannot resolve configuration destination {destination}: {error}"
        ) from error
    if (
        destination == system_config
        or _is_within(destination, system_root)
        or _is_within(resolved, system_root)
    ):
        raise SetupConfigError(
            f"Setup does not edit /etc configuration {destination}. Copy it to a user-writable path and rerun setup."
        )


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _reject_symbolic_link(destination: Path) -> None:
    if destination.is_symlink():
        target = destination.resolve(strict=False)
        raise SetupConfigError(
            f"Cannot edit symbolic link {destination}; it resolves to {target}. "
            "Rerun setup with --config naming that target explicitly."
        )


def _reject_non_file_destination(destination: Path) -> None:
    if destination.exists() and not destination.is_file():
        raise SetupConfigError(
            f"Configuration destination is not a regular file: {destination}"
        )


def _reject_unwritable_destination(destination: Path) -> None:
    parent = destination.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise SetupConfigError(
            f"Configuration destination is not writable: {destination}"
        )


class ConfigEditor:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        home: Path | None = None,
        system_config: Path = SYSTEM_CONFIG,
        system_root: Path = SYSTEM_ROOT,
        operations: FileOperations | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._environ = dict(os.environ if environ is None else environ)
        self._cwd = Path.cwd() if cwd is None else cwd
        self._home = Path.home() if home is None else home
        self._system_config = system_config
        self._system_root = system_root
        self._operations = operations or FileOperations()
        self._now = now

    def load(self, explicit: Path | None) -> EditableConfig:
        location = _resolve_editable_config(
            explicit,
            environ=self._environ,
            cwd=self._cwd,
            home=self._home,
            system_config=self._system_config,
            system_root=self._system_root,
        )
        if location.source is None:
            return EditableConfig(
                location=location, document=tomlkit.document(), existed=False
            )

        try:
            content = location.source.read_text(encoding="utf-8")
            document = tomlkit.parse(content)
            parse_config(document)
        except (ConfigError, OSError, ValueError) as error:
            raise SetupConfigError(
                f"Cannot edit invalid configuration {location.source}: {error}"
            ) from error
        return EditableConfig(
            location=location,
            document=document,
            existed=location.source == location.destination,
        )

    def begin(self, editable: EditableConfig) -> "ConfigTransaction":
        return ConfigTransaction(
            editable,
            operations=self._operations,
            now=self._now,
            system_root=self._system_root,
        )

    @staticmethod
    def list_backups(destination: Path) -> tuple[BackupInfo, ...]:
        return list_backups(destination)

    def restore_backup(
        self, editable: EditableConfig, backup: BackupInfo
    ) -> "ConfigTransaction":
        available = self.list_backups(editable.location.destination)
        if backup not in available:
            raise SetupConfigError(
                f"Backup is not available for restoration: {backup.path}"
            )
        try:
            document = tomlkit.parse(backup.path.read_text(encoding="utf-8"))
            parse_config(document)
        except (ConfigError, OSError, ValueError) as error:
            raise SetupConfigError(
                f"Cannot restore invalid backup {backup.path}: {error}"
            ) from error

        transaction = self.begin(editable)
        transaction.commit(document)
        return transaction


class ConfigTransaction:
    def __init__(
        self,
        editable: EditableConfig,
        *,
        operations: FileOperations,
        now: Callable[[], datetime],
        system_root: Path,
    ):
        self._editable = editable
        self._operations = operations
        self._now = now
        self._system_root = system_root.resolve(strict=False)
        self._temporary_name: str | None = None
        self._backup_name: str | None = None
        self._quarantine_name: str | None = None
        self._published_identity: tuple[int, int] | None = None
        self._replaced = False

    def commit(self, document: tomlkit.TOMLDocument) -> None:
        directory_fd: int | None = None
        try:
            content = tomlkit.dumps(document)
            parse_config(tomlkit.parse(content))
            directory_fd = self._open_destination_directory()
            self._temporary_name = self._write_temporary(
                content.encode("utf-8"), directory_fd
            )
            if self._editable.existed:
                self._backup_name = self._write_backup(directory_fd)
                self._fsync_directory(directory_fd)
                self._operations.replace(
                    self._temporary_name,
                    self._destination_name,
                    directory_fd=directory_fd,
                )
                self._replaced = True
                self._temporary_name = None
            else:
                try:
                    self._operations.link(
                        self._temporary_name,
                        self._destination_name,
                        directory_fd=directory_fd,
                    )
                except FileExistsError as error:
                    raise SetupConfigError(
                        f"Configuration destination appeared after setup loaded it: "
                        f"{self._editable.location.destination}"
                    ) from error
                self._replaced = True
                published = self._operations.stat(
                    self._temporary_name, directory_fd=directory_fd
                )
                self._published_identity = (published.st_dev, published.st_ino)
                self._operations.unlink(self._temporary_name, directory_fd=directory_fd)
                self._temporary_name = None
            self._fsync_directory(directory_fd)
        except ConfigCommitError as error:
            if error.transaction is None:
                error.transaction = self
            raise
        except Exception as error:
            self._cleanup_temporary(directory_fd)
            raise ConfigCommitError(
                f"Cannot save configuration {self._editable.location.destination}: {error}",
                potentially_committed=self._replaced,
                transaction=self,
            ) from error
        finally:
            if directory_fd is not None:
                self._operations.close(directory_fd)

    def restore(self) -> None:
        if not self._replaced:
            return
        directory_fd: int | None = None
        try:
            directory_fd = self._open_destination_directory()
            if self._editable.existed:
                if self._backup_name is None:
                    raise SetupConfigError("No backup is available for restoration")
                self._operations.replace(
                    self._backup_name,
                    self._destination_name,
                    directory_fd=directory_fd,
                )
                self._backup_name = None
            else:
                self._remove_published_destination(directory_fd)
            self._fsync_directory(directory_fd)
            self._replaced = False
            self._published_identity = None
        except SetupConfigError as error:
            message = self._with_recovery_artifact(str(error))
            if message == str(error):
                raise
            raise SetupConfigError(message) from error
        except Exception as error:
            raise SetupConfigError(
                self._with_recovery_artifact(
                    f"Cannot restore configuration {self._editable.location.destination}: {error}"
                )
            ) from error
        finally:
            if directory_fd is not None:
                self._operations.close(directory_fd)

    def finalize(self) -> None:
        if self._temporary_name is None:
            return
        directory_fd = self._open_destination_directory()
        try:
            self._cleanup_temporary(directory_fd)
        finally:
            self._operations.close(directory_fd)

    @property
    def destination_may_be_active(self) -> bool:
        return self._replaced

    @property
    def recovery_artifacts(self) -> tuple[Path, ...]:
        names = (self._backup_name, self._quarantine_name, self._temporary_name)
        return tuple(self._destination_path(name) for name in names if name is not None)

    @property
    def recovery_commands(self) -> tuple[tuple[str, ...], ...]:
        source_name: str | None
        if self._quarantine_name is not None:
            source_name = self._quarantine_name
        else:
            source_name = self._backup_name
        if source_name is not None:
            return (
                (
                    "/usr/bin/mv",
                    "--",
                    str(self._destination_path(source_name)),
                    str(self._editable.location.destination),
                ),
                (
                    "/usr/bin/sync",
                    "--",
                    str(self._editable.location.destination.parent),
                ),
            )
        if self._replaced and not self._editable.existed:
            return (
                ("/usr/bin/rm", "-f", "--", str(self._editable.location.destination)),
                (
                    "/usr/bin/sync",
                    "--",
                    str(self._editable.location.destination.parent),
                ),
            )
        return ()

    @property
    def recovery_command_lines(self) -> tuple[str, ...]:
        return tuple(shlex.join(command) for command in self.recovery_commands)

    @property
    def _destination_name(self) -> str:
        return self._editable.location.destination.name

    def _write_temporary(self, content: bytes, directory_fd: int) -> str:
        for _ in range(100):
            name = f".{self._editable.location.destination.name}.tmp.{secrets.token_hex(8)}"
            try:
                descriptor = self._open_new_file(name, directory_fd)
            except FileExistsError:
                continue
            self._temporary_name = name
            self._write_open_file(descriptor, content)
            return name
        raise SetupConfigError("Cannot allocate a unique temporary configuration path")

    def _write_backup(self, directory_fd: int) -> str:
        content = self._operations.read_file(
            self._destination_name, directory_fd=directory_fd
        )
        timestamp = self._now().astimezone(UTC)
        stem = (
            f"{self._editable.location.destination.name}.bak.{timestamp:%Y%m%dT%H%M%SZ}"
        )
        for suffix in range(100):
            name = stem if suffix == 0 else f"{stem}.{suffix}"
            try:
                self._write_new_file(name, content, directory_fd)
            except FileExistsError:
                continue
            return name
        raise SetupConfigError("Cannot allocate a unique configuration backup path")

    def _write_new_file(self, name: str, content: bytes, directory_fd: int) -> None:
        descriptor = self._open_new_file(name, directory_fd)
        self._write_open_file(descriptor, content)

    def _open_new_file(self, name: str, directory_fd: int) -> int:
        return self._operations.open_file(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            directory_fd=directory_fd,
        )

    def _write_open_file(self, descriptor: int, content: bytes) -> None:
        try:
            self._operations.chmod(descriptor, 0o600)
            _write_all(self._operations, descriptor, content)
            self._operations.fsync(descriptor)
        finally:
            self._operations.close(descriptor)

    def _open_destination_directory(self) -> int:
        destination_directory = self._editable.location.destination.parent
        if not destination_directory.is_absolute():
            raise SetupConfigError(
                f"Configuration directory is not absolute: {destination_directory}"
            )
        descriptor = self._operations.open_directory(os.sep)
        try:
            for component in destination_directory.parts[1:]:
                parent_descriptor = descriptor
                try:
                    child_descriptor = self._operations.open_directory(
                        component,
                        directory_fd=parent_descriptor,
                    )
                except FileNotFoundError:
                    self._operations.mkdir(
                        component, 0o700, directory_fd=parent_descriptor
                    )
                    child_descriptor = self._operations.open_directory(
                        component,
                        directory_fd=parent_descriptor,
                    )
                    self._fsync_directory(child_descriptor)
                    self._fsync_directory(parent_descriptor)
                except OSError as error:
                    raise SetupConfigError(
                        f"Cannot open configuration directory component {component}: {error}"
                    ) from error
                try:
                    self._operations.close(parent_descriptor)
                except Exception:
                    self._operations.close(child_descriptor)
                    raise
                descriptor = child_descriptor
            actual_directory = self._operations.directory_path(descriptor)
            if _is_within(actual_directory, self._system_root):
                raise SetupConfigError(
                    f"Setup does not edit /etc configuration {self._editable.location.destination}. "
                    "Copy it to a user-writable path and rerun setup."
                )
            return descriptor
        except Exception:
            self._operations.close(descriptor)
            raise

    def _fsync_directory(self, directory_fd: int) -> None:
        self._operations.fsync(directory_fd)

    def _remove_published_destination(self, directory_fd: int) -> None:
        if self._quarantine_name is None:
            try:
                quarantine_name = self._reserve_quarantine_name(directory_fd)
            except Exception:
                self._cleanup_reserved_quarantine(directory_fd)
                raise
            try:
                self._operations.rename(
                    self._destination_name,
                    quarantine_name,
                    directory_fd=directory_fd,
                )
            except FileNotFoundError:
                self._cleanup_reserved_quarantine(directory_fd)
                return
            except Exception:
                self._cleanup_reserved_quarantine(directory_fd)
                raise

        quarantine_name = self._quarantine_name
        if quarantine_name is None:
            raise SetupConfigError(
                "No quarantine name is available for configuration rollback"
            )

        self._fsync_directory(directory_fd)
        if self._published_identity is None:
            raise SetupConfigError(
                f"Cannot perform safe rollback without a published configuration identity; "
                f"recovery artifact remains at {self._destination_path(quarantine_name)}"
            )
        quarantined = self._operations.stat(quarantine_name, directory_fd=directory_fd)
        quarantined_identity = (quarantined.st_dev, quarantined.st_ino)
        if quarantined_identity == self._published_identity:
            self._operations.unlink(quarantine_name, directory_fd=directory_fd)
            self._quarantine_name = None
            return

        try:
            self._operations.link(
                quarantine_name,
                self._destination_name,
                directory_fd=directory_fd,
            )
        except FileExistsError as error:
            raise SetupConfigError(
                f"Cannot perform safe rollback because destination {self._editable.location.destination} "
                "was recreated; "
                f"recovery artifact remains at {self._destination_path(quarantine_name)}"
            ) from error
        self._operations.unlink(quarantine_name, directory_fd=directory_fd)
        self._quarantine_name = None
        self._fsync_directory(directory_fd)
        raise SetupConfigError(
            "Cannot perform safe rollback because the configuration destination was replaced; "
            "the concurrent configuration was restored"
        )

    def _reserve_quarantine_name(self, directory_fd: int) -> str:
        for _ in range(100):
            name = f".{self._destination_name}.rollback.{secrets.token_hex(8)}"
            try:
                descriptor = self._open_new_file(name, directory_fd)
            except FileExistsError:
                continue
            self._quarantine_name = name
            try:
                self._operations.chmod(descriptor, 0o600)
            finally:
                self._operations.close(descriptor)
            return name
        raise SetupConfigError("Cannot allocate a unique configuration rollback path")

    def _cleanup_reserved_quarantine(self, directory_fd: int) -> None:
        if self._quarantine_name is None:
            return
        try:
            self._operations.unlink(self._quarantine_name, directory_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            self._quarantine_name = None

    def _with_recovery_artifact(self, message: str) -> str:
        if self._quarantine_name is None or "recovery artifact remains at" in message:
            return message
        return f"{message}; recovery artifact remains at {self._destination_path(self._quarantine_name)}"

    def _destination_path(self, name: str) -> Path:
        return self._editable.location.destination.with_name(name)

    def _cleanup_temporary(self, directory_fd: int | None) -> None:
        if self._temporary_name is None or directory_fd is None:
            return
        try:
            self._operations.unlink(self._temporary_name, directory_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        finally:
            self._temporary_name = None


def _write_all(operations: FileOperations, descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = operations.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("write returned no progress")
        offset += written


def list_backups(destination: Path) -> tuple[BackupInfo, ...]:
    pattern = re.compile(
        rf"^{re.escape(destination.name)}\.bak\.(\d{{8}}T\d{{6}}Z)(?:\.(\d+))?$"
    )
    backups: list[tuple[BackupInfo, int]] = []
    try:
        candidates = destination.parent.iterdir()
    except OSError:
        return ()
    for path in candidates:
        match = pattern.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        try:
            timestamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        backups.append(
            (BackupInfo(path=path, timestamp=timestamp), int(match.group(2) or 0))
        )
    backups.sort(key=lambda item: (item[0].timestamp, item[1]), reverse=True)
    return tuple(item[0] for item in backups)

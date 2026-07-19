import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import tomlkit

from input_action_controller.setup.config_editor import (
    ConfigCommitError,
    ConfigEditor,
    FileOperations,
    SetupConfigError,
    list_backups,
    resolve_editable_config,
)


VALID_CONFIG = """# Retain this comment.
[actions.voice]
on_command = ["/usr/bin/voice", "on"]
off_command = ["/usr/bin/voice", "off"]

[[devices]]
name = "Headset"
action = "voice"
transport = "hidraw"
mode = "on-off"
vendor_id = "047f"
product_id = "c056"
on_reports = ["08 02"]
off_reports = ["08 00"]
"""

CONCURRENT_CONFIG = VALID_CONFIG.replace("Headset", "Concurrent headset")
RECREATED_CONFIG = VALID_CONFIG.replace("Headset", "Recreated headset")


class RecordingFileOperations(FileOperations):
    def __init__(self, *, fail_at: str | None = None):
        self.events: list[str] = []
        self.fail_at = fail_at
        self._kinds: dict[int, str] = {}

    def open_file(self, name: str, flags: int, mode: int, *, directory_fd: int) -> int:
        descriptor = super().open_file(name, flags, mode, directory_fd=directory_fd)
        self._kinds[descriptor] = "temp" if ".tmp." in name else "backup"
        return descriptor

    def write(self, descriptor: int, content: bytes) -> int:
        self._record(f"write-{self._kinds[descriptor]}")
        return super().write(descriptor, content)

    def fsync(self, descriptor: int) -> None:
        self._record(f"fsync-{self._kinds.get(descriptor, 'directory')}")
        super().fsync(descriptor)

    def close(self, descriptor: int) -> None:
        kind = self._kinds.pop(descriptor, None)
        if kind == "temp":
            self._record("close-temp")
        super().close(descriptor)

    def replace(self, source: str, destination: str, *, directory_fd: int) -> None:
        self._record("replace-destination")
        super().replace(source, destination, directory_fd=directory_fd)

    def link(self, source: str, destination: str, *, directory_fd: int) -> None:
        self._record("link-destination")
        super().link(source, destination, directory_fd=directory_fd)

    def open_directory(self, name: str, *, directory_fd: int | None = None) -> int:
        descriptor = super().open_directory(name, directory_fd=directory_fd)
        self._kinds[descriptor] = "directory"
        return descriptor

    def _record(self, event: str) -> None:
        self.events.append(event)
        if self.fail_at == event:
            raise OSError(f"injected failure at {event}")


class ConcurrentDestinationOperations(RecordingFileOperations):
    def __init__(self, destination: Path):
        super().__init__()
        self.destination = destination

    def close(self, descriptor: int) -> None:
        is_temporary = self._kinds.get(descriptor) == "temp"
        super().close(descriptor)
        if is_temporary:
            self.destination.write_text(CONCURRENT_CONFIG, encoding="utf-8")


class DirectoryRecordingOperations(RecordingFileOperations):
    def __init__(self):
        super().__init__()
        self._directories: dict[int, Path] = {}

    def mkdir(self, name: str, mode: int, *, directory_fd: int) -> None:
        self.events.append(f"mkdir-{name}")
        super().mkdir(name, mode, directory_fd=directory_fd)

    def open_directory(self, name: str, *, directory_fd: int | None = None) -> int:
        descriptor = FileOperations.open_directory(
            self, name, directory_fd=directory_fd
        )
        self._directories[descriptor] = FileOperations.directory_path(self, descriptor)
        return descriptor

    def fsync(self, descriptor: int) -> None:
        path = self._directories.get(descriptor)
        if path is None:
            super().fsync(descriptor)
            return
        self.events.append(f"fsync-{path.name}")
        FileOperations.fsync(self, descriptor)

    def close(self, descriptor: int) -> None:
        self._directories.pop(descriptor, None)
        super().close(descriptor)


class CompetingPublicationOperations(FileOperations):
    def __init__(self, destination: Path):
        self.destination = destination

    def replace(self, source, destination, *args, **kwargs) -> None:
        self.destination.write_text(CONCURRENT_CONFIG, encoding="utf-8")
        super().replace(source, destination, *args, **kwargs)

    def link(self, source, destination, *, directory_fd: int) -> None:
        self.destination.write_text(CONCURRENT_CONFIG, encoding="utf-8")
        super().link(source, destination, directory_fd=directory_fd)


class ReplacingPublishedDestinationOperations(FileOperations):
    def __init__(self, destination: Path):
        self.destination = destination

    def link(self, source: str, destination: str, *, directory_fd: int) -> None:
        super().link(source, destination, directory_fd=directory_fd)
        replacement = self.destination.with_name("competing-after-publication.toml")
        replacement.write_text(CONCURRENT_CONFIG, encoding="utf-8")
        os.replace(replacement, self.destination)


class QuarantineRecordingOperations(FileOperations):
    def __init__(self):
        self.events: list[str] = []

    def rename(self, source: str, destination: str, *, directory_fd: int) -> None:
        self.events.append("quarantine-destination")
        super().rename(source, destination, directory_fd=directory_fd)

    def link(self, source: str, destination: str, *, directory_fd: int) -> None:
        if ".rollback." in source:
            self.events.append("recover-quarantine")
        super().link(source, destination, directory_fd=directory_fd)

    def unlink(self, name: str, *, directory_fd: int) -> None:
        if ".rollback." in name:
            self.events.append("unlink-quarantine")
        super().unlink(name, directory_fd=directory_fd)

    def fsync(self, descriptor: int) -> None:
        self.events.append("fsync-directory")
        super().fsync(descriptor)


class RecreationDuringRecoveryOperations(QuarantineRecordingOperations):
    def __init__(self, destination: Path):
        super().__init__()
        self.destination = destination

    def rename(self, source: str, destination: str, *, directory_fd: int) -> None:
        super().rename(source, destination, directory_fd=directory_fd)
        self.destination.write_text(RECREATED_CONFIG, encoding="utf-8")


class FailingRollbackOperations(FileOperations):
    def __init__(self):
        self.fail_rename = False
        self.fail_fsync_after_rename = False
        self.fail_stat_after_rename = False
        self._renamed = False

    def rename(self, source: str, destination: str, *, directory_fd: int) -> None:
        if self.fail_rename:
            raise OSError("injected rename failure")
        super().rename(source, destination, directory_fd=directory_fd)
        self._renamed = True

    def fsync(self, descriptor: int) -> None:
        if self._renamed and self.fail_fsync_after_rename:
            self.fail_fsync_after_rename = False
            raise OSError("injected quarantine fsync failure")
        super().fsync(descriptor)

    def stat(self, name: str, *, directory_fd: int):
        if self._renamed and self.fail_stat_after_rename:
            self.fail_stat_after_rename = False
            raise FileNotFoundError("injected quarantine stat failure")
        return super().stat(name, directory_fd=directory_fd)


class ConfigLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = self.enterContext(TemporaryDirectory())
        self.root = Path(self.temporary_directory)
        self.home = self.root / "home"
        self.xdg = self.root / "xdg"
        self.system = self.root / "etc" / "input-action-controller" / "config.toml"
        self.system.parent.mkdir(parents=True)

    def resolve(
        self, explicit: Path | None = None, environ: dict[str, str] | None = None
    ):
        from unittest.mock import patch

        with patch(
            "input_action_controller.setup.config_editor.SYSTEM_CONFIG", self.system
        ):
            return resolve_editable_config(
                explicit,
                environ=environ or {},
                cwd=self.root,
                home=self.home,
            )

    def test_explicit_paths_are_selected_and_normalized(self):
        existing = self.root / "existing.toml"
        existing.write_text(VALID_CONFIG, encoding="utf-8")

        location = self.resolve(Path("existing.toml"))

        self.assertEqual(location.source, existing)
        self.assertEqual(location.destination, existing)
        self.assertTrue(location.packaged_service_compatible is False)

        missing = self.resolve(self.root / "new.toml")
        self.assertIsNone(missing.source)
        self.assertEqual(missing.destination, self.root / "new.toml")

    def test_explicit_normal_xdg_destination_remains_service_compatible(self):
        destination = self.home / ".config" / "input-action-controller" / "config.toml"
        destination.parent.mkdir(parents=True)
        destination.write_text(VALID_CONFIG, encoding="utf-8")

        location = self.resolve(destination)

        self.assertEqual(location.destination, destination)
        self.assertTrue(location.packaged_service_compatible)

    def test_environment_override_and_existing_xdg_config_take_precedence(self):
        override = self.root / "override.toml"
        override.write_text(VALID_CONFIG, encoding="utf-8")
        location = self.resolve(
            environ={"INPUT_ACTION_CONTROLLER_CONFIG": str(override)}
        )
        self.assertEqual(location.source, override)
        self.assertEqual(location.destination, override)
        self.assertFalse(location.packaged_service_compatible)

        xdg_config = self.xdg / "input-action-controller" / "config.toml"
        xdg_config.parent.mkdir(parents=True)
        xdg_config.write_text(VALID_CONFIG, encoding="utf-8")
        location = self.resolve(environ={"XDG_CONFIG_HOME": str(self.xdg)})
        self.assertEqual(location.source, xdg_config)
        self.assertEqual(location.destination, xdg_config)
        self.assertTrue(location.packaged_service_compatible)

    def test_system_config_is_only_an_in_memory_seed(self):
        self.system.write_text(VALID_CONFIG, encoding="utf-8")
        location = self.resolve(environ={"XDG_CONFIG_HOME": str(self.xdg)})

        self.assertEqual(location.source, self.system)
        self.assertTrue(location.seed_from_system)
        self.assertEqual(
            location.destination,
            self.xdg / "input-action-controller" / "config.toml",
        )
        self.assertFalse(location.destination.exists())
        self.assertTrue(location.packaged_service_compatible)

    def test_missing_config_uses_a_new_xdg_destination(self):
        location = self.resolve()

        self.assertIsNone(location.source)
        self.assertFalse(location.seed_from_system)
        self.assertEqual(
            location.destination,
            self.home / ".config" / "input-action-controller" / "config.toml",
        )
        self.assertTrue(location.packaged_service_compatible)

    def test_refuses_system_and_symbolic_link_destinations(self):
        self.system.write_text(VALID_CONFIG, encoding="utf-8")
        with self.assertRaisesRegex(SetupConfigError, "(?i)copy"):
            self.resolve(self.system)

        target = self.root / "target.toml"
        target.write_text(VALID_CONFIG, encoding="utf-8")
        link = self.root / "link.toml"
        link.symlink_to(target)
        with self.assertRaisesRegex(SetupConfigError, str(target)):
            self.resolve(link)

        broken = self.root / "broken.toml"
        broken.symlink_to(self.root / "missing.toml")
        with self.assertRaisesRegex(SetupConfigError, "symbolic link"):
            self.resolve(broken)

    def test_refuses_missing_destination_below_an_etc_symlink(self):
        linked_parent = self.root / "linked-etc"
        linked_parent.symlink_to("/etc", target_is_directory=True)

        with self.assertRaisesRegex(SetupConfigError, "does not edit /etc"):
            self.resolve(linked_parent / "input-action-controller" / "new.toml")

    def test_refuses_xdg_destination_below_an_etc_symlink(self):
        self.xdg.symlink_to("/etc", target_is_directory=True)

        with self.assertRaisesRegex(SetupConfigError, "does not edit /etc"):
            self.resolve(environ={"XDG_CONFIG_HOME": str(self.xdg)})

    def test_refuses_an_unwritable_destination(self):
        from unittest.mock import patch

        with patch(
            "input_action_controller.setup.config_editor.os.access", return_value=False
        ):
            with self.assertRaisesRegex(SetupConfigError, "writable"):
                self.resolve(self.root / "unwritable.toml")


class ConfigEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = self.enterContext(TemporaryDirectory())
        self.root = Path(self.temporary_directory)
        self.home = self.root / "home"
        self.xdg = self.root / "xdg"
        self.system = self.root / "etc" / "input-action-controller" / "config.toml"
        self.system.parent.mkdir(parents=True)

    def editor(self) -> ConfigEditor:
        return ConfigEditor(
            environ={"XDG_CONFIG_HOME": str(self.xdg)},
            cwd=self.root,
            home=self.home,
            system_config=self.system,
        )

    def test_load_preserves_valid_existing_toml_and_validates_it(self):
        destination = self.xdg / "input-action-controller" / "config.toml"
        destination.parent.mkdir(parents=True)
        destination.write_text(VALID_CONFIG, encoding="utf-8")

        editable = self.editor().load(None)
        editable.document["actions"]["voice"]["on_command"] = [
            "/usr/bin/voice",
            "--changed",
        ]
        serialized = tomlkit.dumps(editable.document)

        self.assertTrue(editable.existed)
        self.assertEqual(editable.location.destination, destination)
        self.assertIn("# Retain this comment.", serialized)
        self.assertIn('on_command = ["/usr/bin/voice", "--changed"]', serialized)
        self.assertLess(
            serialized.index("# Retain this comment."),
            serialized.index("[actions.voice]"),
        )
        self.assertLess(
            serialized.index("[actions.voice]"), serialized.index("[[devices]]")
        )

    def test_loads_system_seed_without_creating_destination(self):
        self.system.write_text(VALID_CONFIG, encoding="utf-8")

        editable = self.editor().load(None)

        self.assertFalse(editable.existed)
        self.assertTrue(editable.location.seed_from_system)
        self.assertIn("Retain this comment", tomlkit.dumps(editable.document))
        self.assertFalse(editable.location.destination.exists())

    def test_rejects_invalid_toml_before_editing(self):
        destination = self.xdg / "input-action-controller" / "config.toml"
        destination.parent.mkdir(parents=True)
        destination.write_text("[actions\n", encoding="utf-8")

        with self.assertRaises(SetupConfigError):
            self.editor().load(None)


class ConfigTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = self.enterContext(TemporaryDirectory())
        self.root = Path(self.temporary_directory)
        self.destination = self.root / "config.toml"
        self.destination.write_text(VALID_CONFIG, encoding="utf-8")
        self.timestamp = datetime(2026, 7, 16, 2, 15, 30, tzinfo=UTC)

    def editable(self):
        return ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
        ).load(self.destination)

    def test_commit_writes_a_durable_backup_before_replacement(self):
        operations = RecordingFileOperations()
        transaction = ConfigEditor(
            operations=operations, now=lambda: self.timestamp
        ).begin(self.editable())

        transaction.commit(self.editable().document)

        self.assertEqual(
            operations.events,
            [
                "write-temp",
                "fsync-temp",
                "close-temp",
                "write-backup",
                "fsync-backup",
                "fsync-directory",
                "replace-destination",
                "fsync-directory",
            ],
        )
        backup = self.root / "config.toml.bak.20260716T021530Z"
        self.assertEqual(backup.read_text(encoding="utf-8"), VALID_CONFIG)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_backup_mode_is_0600_despite_the_process_umask(self):
        previous_umask = os.umask(0o777)
        try:
            transaction = ConfigEditor(now=lambda: self.timestamp).begin(
                self.editable()
            )
            transaction.commit(self.editable().document)
        finally:
            os.umask(previous_umask)

        backup = self.root / "config.toml.bak.20260716T021530Z"
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_backup_collisions_increment_and_list_newest_first(self):
        first = self.root / "config.toml.bak.20260716T021530Z"
        first.write_text("first", encoding="utf-8")
        second = self.root / "config.toml.bak.20260716T021531Z.1"
        second.write_text("second", encoding="utf-8")
        (self.root / "config.toml.bak.invalid").write_text("ignored", encoding="utf-8")
        (self.root / "other.toml.bak.20260716T021532Z").write_text(
            "ignored", encoding="utf-8"
        )

        transaction = ConfigEditor(now=lambda: self.timestamp).begin(self.editable())
        transaction.commit(self.editable().document)

        self.assertTrue((self.root / "config.toml.bak.20260716T021530Z.1").exists())
        self.assertEqual(
            [item.path.name for item in list_backups(self.destination)],
            [
                "config.toml.bak.20260716T021531Z.1",
                "config.toml.bak.20260716T021530Z.1",
                "config.toml.bak.20260716T021530Z",
            ],
        )

    def test_restore_backup_validates_and_commits_an_explicit_selected_backup(self):
        restored_content = VALID_CONFIG.replace("Headset", "Restored headset")
        backup = self.root / "config.toml.bak.20260715T120000Z"
        backup.write_text(restored_content, encoding="utf-8")
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            now=lambda: self.timestamp,
        )
        editable = editor.load(self.destination)

        editor.restore_backup(editable, list_backups(self.destination)[0])

        self.assertEqual(self.destination.read_text(encoding="utf-8"), restored_content)
        self.assertTrue(backup.exists())

    def test_restore_backup_retains_a_potentially_committed_transaction(self):
        backup = self.root / "config.toml.bak.20260715T120000Z"
        backup.write_text(
            VALID_CONFIG.replace("Headset", "Restored headset"), encoding="utf-8"
        )
        operations = RecordingFileOperations()
        original_fsync = operations.fsync
        directory_fsyncs = 0

        def fail_post_publication_sync(descriptor: int) -> None:
            nonlocal directory_fsyncs
            if operations._kinds.get(descriptor) == "directory":
                directory_fsyncs += 1
                if directory_fsyncs == 2:
                    raise OSError("injected post-publication failure")
            original_fsync(descriptor)

        operations.fsync = fail_post_publication_sync
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )

        with self.assertRaises(ConfigCommitError) as raised:
            editor.restore_backup(
                editor.load(self.destination), list_backups(self.destination)[0]
            )

        self.assertTrue(raised.exception.potentially_committed)
        transaction = raised.exception.transaction
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.destination_may_be_active)
        self.assertTrue(transaction.recovery_artifacts)

    def test_committed_transaction_exposes_destination_state_and_recovery_artifacts(
        self,
    ):
        transaction = ConfigEditor(now=lambda: self.timestamp).begin(self.editable())

        transaction.commit(self.editable().document)

        self.assertTrue(transaction.destination_may_be_active)
        self.assertEqual(
            transaction.recovery_artifacts,
            (self.root / "config.toml.bak.20260716T021530Z",),
        )
        self.assertEqual(
            transaction.recovery_commands,
            (
                (
                    "/usr/bin/mv",
                    "--",
                    str(self.root / "config.toml.bak.20260716T021530Z"),
                    str(self.destination),
                ),
                ("/usr/bin/sync", "--", str(self.root)),
            ),
        )
        self.assertIn("/usr/bin/mv --", transaction.recovery_command_lines[0])

    def test_newly_published_configuration_recovery_removes_then_syncs(self):
        destination = self.root / "new.toml"
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))

        self.assertEqual(
            transaction.recovery_commands,
            (
                ("/usr/bin/rm", "-f", "--", str(destination)),
                ("/usr/bin/sync", "--", str(self.root)),
            ),
        )

    def test_failure_before_replacement_keeps_destination_unchanged(self):
        operations = RecordingFileOperations(fail_at="fsync-backup")
        transaction = ConfigEditor(
            operations=operations, now=lambda: self.timestamp
        ).begin(self.editable())

        with self.assertRaises(ConfigCommitError) as raised:
            transaction.commit(self.editable().document)

        self.assertFalse(raised.exception.potentially_committed)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), VALID_CONFIG)
        self.assertIn(
            "config.toml.bak.20260716T021530Z",
            {path.name for path in self.root.iterdir()},
        )

    def test_write_failure_after_temporary_creation_removes_the_temporary_file(self):
        operations = RecordingFileOperations(fail_at="write-temp")
        transaction = ConfigEditor(
            operations=operations, now=lambda: self.timestamp
        ).begin(self.editable())

        with self.assertRaises(ConfigCommitError) as raised:
            transaction.commit(self.editable().document)

        self.assertFalse(raised.exception.potentially_committed)
        self.assertFalse(any(".tmp." in path.name for path in self.root.iterdir()))

    def test_failure_after_replacement_is_reported_as_potentially_committed(self):
        operations = RecordingFileOperations(fail_at="fsync-directory")
        transaction = ConfigEditor(
            operations=operations, now=lambda: self.timestamp
        ).begin(self.editable())
        operations.events.extend(
            ["write-temp", "fsync-temp", "close-temp", "write-backup", "fsync-backup"]
        )

        with self.assertRaises(ConfigCommitError) as raised:
            transaction.commit(self.editable().document)

        self.assertFalse(raised.exception.potentially_committed)

        operations = RecordingFileOperations()
        transaction = ConfigEditor(
            operations=operations, now=lambda: self.timestamp
        ).begin(self.editable())
        original_fsync = operations.fsync
        calls = 0

        def fail_second_directory_fsync(descriptor: int) -> None:
            nonlocal calls
            if operations._kinds.get(descriptor) == "directory":
                calls += 1
                if calls == 2:
                    raise OSError("injected post-replacement failure")
            original_fsync(descriptor)

        operations.fsync = fail_second_directory_fsync
        with self.assertRaises(ConfigCommitError) as raised:
            transaction.commit(self.editable().document)

        self.assertTrue(raised.exception.potentially_committed)

    def test_restore_reinstates_existing_config_and_removes_new_config(self):
        document = self.editable().document
        transaction = ConfigEditor(now=lambda: self.timestamp).begin(self.editable())
        transaction.commit(document)
        self.destination.write_text("changed", encoding="utf-8")
        transaction.restore()
        self.assertEqual(self.destination.read_text(encoding="utf-8"), VALID_CONFIG)

        new_destination = self.root / "new.toml"
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
        )
        new_editable = editor.load(new_destination)
        new_transaction = editor.begin(new_editable)
        new_transaction.commit(tomlkit.parse(VALID_CONFIG))
        self.assertTrue(new_destination.exists())
        new_transaction.restore()
        self.assertFalse(new_destination.exists())

    def test_restore_preserves_a_replacement_of_a_newly_published_config(self):
        destination = self.root / "new.toml"
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))
        replacement = self.root / "replacement.toml"
        replacement.write_text(CONCURRENT_CONFIG, encoding="utf-8")
        os.replace(replacement, destination)

        with self.assertRaisesRegex(SetupConfigError, "safe rollback"):
            transaction.restore()

        self.assertEqual(destination.read_text(encoding="utf-8"), CONCURRENT_CONFIG)

    def test_restore_preserves_a_destination_replaced_after_publication(self):
        destination = self.root / "new.toml"
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=ReplacingPublishedDestinationOperations(destination),
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))

        with self.assertRaisesRegex(SetupConfigError, "safe rollback"):
            transaction.restore()

        self.assertEqual(destination.read_text(encoding="utf-8"), CONCURRENT_CONFIG)

    def test_restore_quarantines_and_restores_a_concurrent_replacement(self):
        destination = self.root / "new.toml"
        operations = QuarantineRecordingOperations()
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))
        replacement = self.root / "replacement.toml"
        replacement.write_text(CONCURRENT_CONFIG, encoding="utf-8")
        os.replace(replacement, destination)
        operations.events.clear()

        with self.assertRaisesRegex(SetupConfigError, "safe rollback"):
            transaction.restore()

        self.assertEqual(destination.read_text(encoding="utf-8"), CONCURRENT_CONFIG)
        self.assertEqual(
            operations.events,
            [
                "quarantine-destination",
                "fsync-directory",
                "recover-quarantine",
                "unlink-quarantine",
                "fsync-directory",
            ],
        )
        self.assertFalse(list(self.root.glob(".new.toml.rollback.*")))

    def test_restore_preserves_quarantine_when_destination_is_recreated_during_recovery(
        self,
    ):
        destination = self.root / "new.toml"
        operations = RecreationDuringRecoveryOperations(destination)
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))
        replacement = self.root / "replacement.toml"
        replacement.write_text(CONCURRENT_CONFIG, encoding="utf-8")
        os.replace(replacement, destination)
        operations.events.clear()

        with self.assertRaisesRegex(SetupConfigError, "was recreated") as raised:
            transaction.restore()

        artifacts = list(self.root.glob(".new.toml.rollback.*"))
        self.assertEqual(destination.read_text(encoding="utf-8"), RECREATED_CONFIG)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].read_text(encoding="utf-8"), CONCURRENT_CONFIG)
        self.assertIn(str(destination), str(raised.exception))
        self.assertIn(str(artifacts[0]), str(raised.exception))
        self.assertEqual(
            operations.events,
            ["quarantine-destination", "fsync-directory", "recover-quarantine"],
        )
        self.assertEqual(
            transaction.recovery_commands,
            (
                ("/usr/bin/mv", "--", str(artifacts[0]), str(destination)),
                ("/usr/bin/sync", "--", str(self.root)),
            ),
        )

    def test_restore_reports_and_retries_a_quarantine_after_directory_fsync_failure(
        self,
    ):
        destination = self.root / "new.toml"
        operations = FailingRollbackOperations()
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))
        operations.fail_fsync_after_rename = True

        with self.assertRaises(SetupConfigError) as raised:
            transaction.restore()

        artifacts = list(self.root.glob(".new.toml.rollback.*"))
        self.assertFalse(destination.exists())
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].read_text(encoding="utf-8"), VALID_CONFIG)
        self.assertIn(str(artifacts[0]), str(raised.exception))

        transaction.restore()

        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".new.toml.rollback.*")))

    def test_restore_cleans_a_reserved_quarantine_after_rename_failure(self):
        destination = self.root / "new.toml"
        operations = FailingRollbackOperations()
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))
        operations.fail_rename = True

        with self.assertRaisesRegex(SetupConfigError, "injected rename failure"):
            transaction.restore()

        self.assertEqual(destination.read_text(encoding="utf-8"), VALID_CONFIG)
        self.assertFalse(list(self.root.glob(".new.toml.rollback.*")))

    def test_restore_reports_a_post_rename_quarantine_stat_failure(self):
        destination = self.root / "new.toml"
        operations = FailingRollbackOperations()
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))
        operations.fail_stat_after_rename = True

        with self.assertRaisesRegex(
            SetupConfigError, "injected quarantine stat failure"
        ) as raised:
            transaction.restore()

        artifacts = list(self.root.glob(".new.toml.rollback.*"))
        self.assertFalse(destination.exists())
        self.assertEqual(len(artifacts), 1)
        self.assertIn(str(artifacts[0]), str(raised.exception))

    def test_restore_ignores_a_newly_published_destination_deleted_before_quarantine(
        self,
    ):
        destination = self.root / "new.toml"
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            now=lambda: self.timestamp,
        )
        transaction = editor.begin(editor.load(destination))
        transaction.commit(tomlkit.parse(VALID_CONFIG))
        destination.unlink()

        transaction.restore()

        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".new.toml.rollback.*")))

    def test_aborts_when_an_absent_destination_appears_before_replacement(self):
        destination = self.root / "new.toml"
        operations = ConcurrentDestinationOperations(destination)
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        editable = editor.load(destination)
        transaction = editor.begin(editable)

        with self.assertRaises(ConfigCommitError) as raised:
            transaction.commit(tomlkit.parse(VALID_CONFIG))

        self.assertFalse(raised.exception.potentially_committed)
        self.assertEqual(destination.read_text(encoding="utf-8"), CONCURRENT_CONFIG)
        transaction.restore()
        self.assertEqual(destination.read_text(encoding="utf-8"), CONCURRENT_CONFIG)

    def test_commit_synchronizes_each_created_configuration_directory(self):
        destination = self.root / "first" / "second" / "config.toml"
        operations = DirectoryRecordingOperations()
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        editable = editor.load(destination)

        editor.begin(editable).commit(tomlkit.parse(VALID_CONFIG))

        self.assertEqual(
            operations.events,
            [
                "mkdir-first",
                "fsync-first",
                f"fsync-{self.root.name}",
                "mkdir-second",
                "fsync-second",
                "fsync-first",
                "write-temp",
                "fsync-temp",
                "close-temp",
                "link-destination",
                "fsync-second",
            ],
        )

    def test_commit_refuses_an_ancestor_replaced_by_the_system_root(self):
        system_root = self.root / "system"
        system_root.mkdir()
        ancestor = self.root / "editable"
        ancestor.mkdir()
        destination = ancestor / "config.toml"
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=system_root / "input-action-controller" / "config.toml",
            system_root=system_root,
            now=lambda: self.timestamp,
        )
        editable = editor.load(destination)
        ancestor.rename(self.root / "original-editable")
        ancestor.symlink_to(system_root, target_is_directory=True)

        with self.assertRaises(ConfigCommitError) as raised:
            editor.begin(editable).commit(tomlkit.parse(VALID_CONFIG))

        self.assertFalse(raised.exception.potentially_committed)
        self.assertFalse((system_root / "config.toml").exists())

    def test_absent_destination_uses_atomic_no_replace_publication(self):
        destination = self.root / "new.toml"
        operations = CompetingPublicationOperations(destination)
        editor = ConfigEditor(
            environ={},
            cwd=self.root,
            home=self.root,
            system_config=self.root / "etc.toml",
            operations=operations,
            now=lambda: self.timestamp,
        )
        editable = editor.load(destination)

        with self.assertRaises(ConfigCommitError) as raised:
            editor.begin(editable).commit(tomlkit.parse(VALID_CONFIG))

        self.assertFalse(raised.exception.potentially_committed)
        self.assertEqual(destination.read_text(encoding="utf-8"), CONCURRENT_CONFIG)


if __name__ == "__main__":
    unittest.main()

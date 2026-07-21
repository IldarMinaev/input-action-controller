import ctypes
import errno
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import os
from pathlib import Path
from contextlib import redirect_stderr
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RELEASE = ROOT / "scripts" / "public-release"
PUBLIC_RELEASE_LOADER = SourceFileLoader("public_release", str(PUBLIC_RELEASE))
PUBLIC_RELEASE_SPEC = importlib.util.spec_from_loader(
    "public_release", PUBLIC_RELEASE_LOADER
)
if PUBLIC_RELEASE_SPEC is None or PUBLIC_RELEASE_SPEC.loader is None:
    raise RuntimeError(f"cannot load {PUBLIC_RELEASE}")
public_release = importlib.util.module_from_spec(PUBLIC_RELEASE_SPEC)
PUBLIC_RELEASE_SPEC.loader.exec_module(public_release)


REQUIRED_FIXTURE_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".gitignore",
    "LICENSE",
    "PKGBUILD",
    "README.md",
    "config.example.toml",
    "docs/configuration.md",
    "docs/device-discovery.md",
    "docs/maintaining.md",
    "docs/examples/dsnote-gnome-wayland.md",
    "docs/examples/handy-gnome-wayland.md",
    "packaging/input-action-controller.service",
    "pyproject.toml",
    "requirements-ci.txt",
    "renovate.json",
    "scripts/makepkg",
    "scripts/public-release",
    "scripts/update-aur-package",
    "scripts/verify-artifacts",
    "src/input_action_controller/__init__.py",
    "tests/test_example.py",
)


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fixture_number = 0
        identity = {
            "GIT_AUTHOR_NAME": "Public Release Tests",
            "GIT_AUTHOR_EMAIL": "public-release-tests@example.invalid",
            "GIT_COMMITTER_NAME": "Public Release Tests",
            "GIT_COMMITTER_EMAIL": "public-release-tests@example.invalid",
        }
        self.git_environment_patch = mock.patch.dict(
            os.environ,
            identity,
            clear=False,
        )
        self.git_environment_patch.start()
        self.git_environment = os.environ.copy()

    def tearDown(self):
        self.git_environment_patch.stop()
        self.temporary_directory.cleanup()

    def git(
        self,
        repository: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *arguments),
            cwd=repository,
            env=self.git_environment,
            check=check,
            capture_output=True,
            text=True,
        )

    def create_public_fixture(self, name: str | None = None) -> Path:
        if name is None:
            self.fixture_number += 1
            name = f"public-{self.fixture_number}"
        root = self.root / name
        for relative_path in REQUIRED_FIXTURE_FILES:
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture for {relative_path}\n", encoding="utf-8")
        (root / "scripts" / "public-release").chmod(0o755)
        return root

    def create_source_repository(self, name: str = "source") -> Path:
        source = self.create_public_fixture(name)
        self.git(source, "init", "--initial-branch=main")

        internal = source / "docs" / "superpowers" / "plan.md"
        internal.parent.mkdir(parents=True)
        internal.write_text("internal\n", encoding="utf-8")

        self.git(source, "add", "--all")
        self.git(source, "commit", "--message=source fixture")
        return source

    def assert_branch_absent(self, source: Path) -> None:
        result = self.git(
            source,
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/release-candidate",
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)

    def assert_no_destructive_cleanup(
        self,
        git_operations: list[tuple[str, ...]],
    ) -> None:
        self.assertFalse(
            any(
                arguments[:4] == ("git", "worktree", "remove", "--force")
                for arguments in git_operations
            ),
            git_operations,
        )
        self.assertFalse(
            any(
                arguments[:3] == ("git", "update-ref", "-d")
                for arguments in git_operations
            ),
            git_operations,
        )

    def run_public_release(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (str(PUBLIC_RELEASE), *arguments),
            cwd=ROOT,
            env=self.git_environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_accepts_exact_public_fixture(self):
        public_release.verify_public_tree(self.create_public_fixture())

    def test_rejects_internal_superpowers_artifacts(self):
        root = self.create_public_fixture()
        forbidden = root / "docs" / "superpowers" / "plan.md"
        forbidden.parent.mkdir(parents=True)
        forbidden.write_text("internal", encoding="utf-8")

        with self.assertRaisesRegex(
            public_release.PublicTreeError,
            "docs/superpowers",
        ):
            public_release.verify_public_tree(root)

    def test_rejects_forbidden_generated_and_private_state(self):
        forbidden_paths = (
            ".superpowers/sdd/plan.md",
            "release.pkg.tar.zst",
            "release.tar.gz",
            "build/output.txt",
            "candidate.log",
            ".env",
            "credentials.json",
            "config.toml",
            "70-input-action-controller.rules",
            ".config/systemd/user/input-action-controller.service",
        )
        for relative_path in forbidden_paths:
            with self.subTest(relative_path=relative_path):
                root = self.create_public_fixture()
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private", encoding="utf-8")

                with self.assertRaisesRegex(
                    public_release.PublicTreeError,
                    relative_path.split("/", maxsplit=1)[0]
                    if "/" in relative_path
                    else relative_path,
                ):
                    public_release.verify_public_tree(root)

    def test_rejects_non_python_files_under_variable_prefixes(self):
        for relative_path in (
            "src/input_action_controller/private.json",
            "tests/fixture.toml",
        ):
            with self.subTest(relative_path=relative_path):
                root = self.create_public_fixture()
                path = root / relative_path
                path.write_text("private", encoding="utf-8")

                with self.assertRaisesRegex(
                    public_release.PublicTreeError,
                    relative_path,
                ):
                    public_release.verify_public_tree(root)

    def test_rejects_missing_required_file(self):
        root = self.create_public_fixture()
        (root / "LICENSE").unlink()

        with self.assertRaisesRegex(public_release.PublicTreeError, "LICENSE"):
            public_release.verify_public_tree(root)

    def test_rejects_symbolic_links(self):
        root = self.create_public_fixture()
        link = root / "tests" / "test_link.py"
        link.symlink_to("test_example.py")

        with self.assertRaisesRegex(
            public_release.PublicTreeError,
            "tests/test_link.py",
        ):
            public_release.verify_public_tree(root)

    def test_rejects_nested_hidden_components(self):
        for relative_path, hidden_component in (
            ("tests/.superpowers/secret.py", "tests/.superpowers"),
            (
                "src/input_action_controller/.internal/secret.py",
                "src/input_action_controller/.internal",
            ),
            ("tests/helpers/.git/secret.py", "tests/helpers/.git"),
        ):
            with self.subTest(relative_path=relative_path):
                root = self.create_public_fixture()
                hidden = root / relative_path
                hidden.parent.mkdir(parents=True)
                hidden.write_text("private\n", encoding="utf-8")

                with self.assertRaisesRegex(
                    public_release.PublicTreeError,
                    hidden_component,
                ):
                    public_release.verify_public_tree(root)

    def test_rejects_filesystem_fifo(self):
        root = self.create_public_fixture()
        fifo = root / "tests" / "private.py"
        os.mkfifo(fifo)

        with self.assertRaisesRegex(
            public_release.PublicTreeError,
            "tests/private.py",
        ):
            public_release.verify_public_tree(root)

    def test_candidate_contains_one_root_commit_with_exact_message_and_modes(self):
        source = self.create_source_repository()
        source_head = self.git(source, "rev-parse", "HEAD").stdout.strip()
        untracked_log = source / "local-build.log"
        untracked_archive = source / "local-release.tar.gz"
        untracked_log.write_text("local log\n", encoding="utf-8")
        untracked_archive.write_text("local archive\n", encoding="utf-8")
        destination = self.root / "candidate"

        commit = public_release.create_candidate(source, destination)

        self.assertEqual(
            self.git(destination, "rev-list", "--count", "HEAD").stdout.strip(),
            "1",
        )
        self.assertEqual(
            self.git(destination, "rev-list", "--max-parents=0", "HEAD").stdout.strip(),
            commit,
        )
        self.assertEqual(
            self.git(destination, "log", "-1", "--format=%s").stdout.strip(),
            "feat: initial release",
        )
        self.assertEqual(
            self.git(destination, "rev-parse", "HEAD").stdout.strip(), commit
        )
        self.assertEqual(
            self.git(destination, "branch", "--show-current").stdout.strip(),
            "release-candidate",
        )
        self.assertTrue(
            self.git(destination, "ls-tree", "HEAD", "scripts/public-release")
            .stdout.strip()
            .startswith("100755 blob ")
        )
        self.assertFalse((destination / "docs" / "superpowers").exists())
        self.assertFalse((destination / "local-build.log").exists())
        self.assertFalse((destination / "local-release.tar.gz").exists())
        self.assertEqual(
            self.git(source, "rev-parse", "HEAD").stdout.strip(), source_head
        )
        self.assertEqual(
            self.git(source, "branch", "--show-current").stdout.strip(), "main"
        )
        self.assertTrue(untracked_log.is_file())
        self.assertTrue(untracked_archive.is_file())
        self.assertEqual(self.git(source, "remote").stdout, "")
        self.assertEqual(self.git(source, "tag").stdout, "")

    def test_rejects_existing_destination_without_modifying_it(self):
        source = self.create_source_repository()
        destination = self.root / "candidate"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(
            public_release.PublicTreeError,
            "destination already exists",
        ):
            public_release.create_candidate(source, destination)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
        self.assert_branch_absent(source)

    def test_rejects_dangling_destination_symlink_without_following_it(self):
        source = self.create_source_repository()
        destination = self.root / "candidate"
        target = self.root / "missing-target"
        destination.symlink_to(target)

        with self.assertRaisesRegex(
            public_release.PublicTreeError,
            "destination already exists",
        ):
            public_release.create_candidate(source, destination)

        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.readlink(), target)
        self.assertFalse(target.exists())
        self.assert_branch_absent(source)

    def test_rejects_existing_release_candidate_branch_without_modifying_it(self):
        source = self.create_source_repository()
        branch_commit = self.git(source, "rev-parse", "HEAD").stdout.strip()
        self.git(source, "branch", "release-candidate")
        destination = self.root / "candidate"

        with self.assertRaisesRegex(
            public_release.PublicTreeError,
            "local branch already exists: release-candidate",
        ):
            public_release.create_candidate(source, destination)

        self.assertFalse(destination.exists())
        self.assertEqual(
            self.git(source, "rev-parse", "release-candidate").stdout.strip(),
            branch_commit,
        )

    def test_rejects_dirty_tracked_source(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                source = self.create_source_repository(f"source-{staged}")
                (source / "README.md").write_text("dirty\n", encoding="utf-8")
                if staged:
                    self.git(source, "add", "README.md")
                destination = self.root / f"candidate-{staged}"

                message = (
                    "source has staged tracked changes"
                    if staged
                    else "source has unstaged tracked changes"
                )
                with self.assertRaisesRegex(
                    public_release.PublicTreeError,
                    message,
                ):
                    public_release.create_candidate(source, destination)

                self.assertFalse(destination.exists())
                self.assert_branch_absent(source)

    def test_rejects_dirty_tracked_source_for_custom_ref(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                source = self.create_source_repository(f"custom-ref-{staged}")
                self.git(source, "branch", "public-source")
                (source / "README.md").write_text("dirty\n", encoding="utf-8")
                if staged:
                    self.git(source, "add", "README.md")
                destination = self.root / f"custom-candidate-{staged}"

                message = (
                    "source has staged tracked changes"
                    if staged
                    else "source has unstaged tracked changes"
                )
                with self.assertRaisesRegex(
                    public_release.PublicTreeError,
                    message,
                ):
                    public_release.create_candidate(
                        source,
                        destination,
                        ref="public-source",
                    )

                self.assertFalse(destination.exists())
                self.assert_branch_absent(source)

    def test_resolves_custom_ref_once_and_exports_the_immutable_commit(self):
        source = self.create_source_repository()
        first_commit = self.git(source, "rev-parse", "HEAD").stdout.strip()
        self.git(source, "branch", "public-source", first_commit)
        (source / "README.md").write_text("newer ref content\n", encoding="utf-8")
        self.git(source, "add", "README.md")
        self.git(source, "commit", "--message=newer source")
        newer_commit = self.git(source, "rev-parse", "HEAD").stdout.strip()
        destination = self.root / "candidate"
        original_run = public_release._run
        ref_moved = False
        resolution_count = 0

        def move_ref_after_resolution(*arguments, cwd, operation):
            nonlocal ref_moved, resolution_count
            result = original_run(*arguments, cwd=cwd, operation=operation)
            resolves_ref = arguments == (
                "git",
                "rev-parse",
                "--verify",
                "--end-of-options",
                "public-source^{commit}",
            )
            if resolves_ref:
                resolution_count += 1
            inspects_ref_tree = (
                len(arguments) > 3
                and arguments[:3] == ("git", "ls-tree", "-r")
                and "public-source" in arguments
            )
            if not ref_moved and (resolves_ref or inspects_ref_tree):
                self.git(
                    source,
                    "update-ref",
                    "refs/heads/public-source",
                    newer_commit,
                    first_commit,
                )
                ref_moved = True
            return result

        with mock.patch.object(public_release, "_run", move_ref_after_resolution):
            public_release.create_candidate(source, destination, ref="public-source")

        self.assertTrue(ref_moved)
        self.assertEqual(resolution_count, 1)
        self.assertEqual(
            (destination / "README.md").read_text(encoding="utf-8"),
            "fixture for README.md\n",
        )

    def test_rejects_unexpected_broad_root_path_before_worktree_add(self):
        for relative_path in (
            ".github/workflows/private.yml",
            "src/other/private.py",
            "tests/private.json",
        ):
            with self.subTest(relative_path=relative_path):
                source = self.create_source_repository(
                    "broad-root-" + relative_path.replace("/", "-")
                )
                unexpected = source / relative_path
                unexpected.parent.mkdir(parents=True, exist_ok=True)
                unexpected.write_text("private\n", encoding="utf-8")
                self.git(source, "add", relative_path)
                self.git(source, "commit", "--message=add unexpected path")
                destination = self.root / ("candidate-" + str(self.fixture_number))

                with mock.patch.object(
                    public_release,
                    "_run",
                    wraps=public_release._run,
                ) as run:
                    with self.assertRaisesRegex(
                        public_release.PublicTreeError,
                        relative_path,
                    ):
                        public_release.create_candidate(source, destination)

                worktree_adds = [
                    call
                    for call in run.call_args_list
                    if call.args[:3] == ("git", "worktree", "add")
                ]
                self.assertEqual(worktree_adds, [])

    def test_rejects_tracked_nested_hidden_path_before_worktree_add(self):
        source = self.create_source_repository()
        hidden = source / "tests" / ".superpowers" / "secret.py"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("private\n", encoding="utf-8")
        self.git(source, "add", "tests/.superpowers/secret.py")
        self.git(source, "commit", "--message=add hidden test")
        destination = self.root / "candidate"

        with mock.patch.object(
            public_release,
            "_run",
            wraps=public_release._run,
        ) as run:
            with self.assertRaisesRegex(
                public_release.PublicTreeError,
                "tests/.superpowers/secret.py",
            ):
                public_release.create_candidate(source, destination)

        worktree_adds = [
            call
            for call in run.call_args_list
            if call.args[:3] == ("git", "worktree", "add")
        ]
        self.assertEqual(worktree_adds, [])

    def test_private_source_tree_is_never_materialized(self):
        source = self.create_source_repository()
        attributes = source / ".gitattributes"
        private = source / "private-tree.txt"
        attributes.write_text("private-tree.txt filter=private\n", encoding="utf-8")
        private.write_text("private\n", encoding="utf-8")
        self.git(source, "add", ".gitattributes", "private-tree.txt")
        self.git(source, "commit", "--message=add private source tree")
        marker = self.root / "private-filter-ran"
        smudge = self.root / "smudge-filter"
        smudge.write_text(
            '#!/bin/sh\ntouch "$PUBLIC_RELEASE_FILTER_MARKER"\ncat\n',
            encoding="utf-8",
        )
        smudge.chmod(0o755)
        self.git(source, "config", "filter.private.smudge", str(smudge))
        self.git(source, "config", "filter.private.clean", "cat")
        self.git_environment["PUBLIC_RELEASE_FILTER_MARKER"] = str(marker)
        destination = self.root / "candidate"

        with mock.patch.dict(os.environ, self.git_environment, clear=False):
            with mock.patch.object(
                public_release,
                "_run",
                wraps=public_release._run,
            ) as run:
                public_release.create_candidate(source, destination)

        self.assertFalse(marker.exists())
        self.assertFalse((destination / "private-tree.txt").exists())
        worktree_add = next(
            call
            for call in run.call_args_list
            if call.args[:3] == ("git", "worktree", "add")
        )
        self.assertIn("--no-checkout", worktree_add.args)

    def test_disables_hooks_signing_fsmonitor_and_external_attributes(self):
        source = self.create_source_repository()
        marker = self.root / "hook-ran"
        hook = source / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            f"#!/bin/sh\ntouch {marker}\nexit 1\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        self.git(source, "config", "commit.gpgsign", "true")
        self.git(source, "config", "gpg.program", "false")
        destination = self.root / "candidate"

        with mock.patch.object(
            public_release.subprocess,
            "run",
            wraps=subprocess.run,
        ) as run:
            public_release.create_candidate(source, destination)

        self.assertFalse(marker.exists())
        git_calls = [
            call.args[0] for call in run.call_args_list if call.args[0][0] == "git"
        ]
        self.assertTrue(git_calls)
        for arguments in git_calls:
            with self.subTest(arguments=arguments):
                self.assertIn("core.hooksPath=/dev/null", arguments)
                self.assertIn("commit.gpgSign=false", arguments)
                self.assertIn("core.fsmonitor=false", arguments)
                self.assertIn("core.attributesFile=/dev/null", arguments)

    def test_isolates_candidate_from_hostile_git_environment(self):
        source = self.create_source_repository()
        decoy = self.root / "decoy"
        decoy.mkdir()
        self.git(decoy, "init", "--initial-branch=main")
        destination = self.root / "candidate"

        external_index = self.root / "external.index"
        external_index_environment = self.git_environment | {
            "GIT_INDEX_FILE": str(external_index)
        }
        subprocess.run(
            ("git", "read-tree", "HEAD"),
            cwd=source,
            env=external_index_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        external_index_before = external_index.read_bytes()

        marker = self.root / "hostile-helper.marker"
        marker.write_text("keep\n", encoding="utf-8")
        helper = self.root / "hostile-git-helper"
        helper.write_text(
            '#!/bin/sh\nprintf "ran\\n" >> "$HOSTILE_GIT_MARKER"\n',
            encoding="utf-8",
        )
        helper.chmod(0o755)
        attributes = self.root / "hostile.attributes"
        attributes.write_text("README.md diff=hostile\n", encoding="utf-8")
        hostile_environment = {
            "GIT_AUTHOR_NAME": self.git_environment["GIT_AUTHOR_NAME"],
            "GIT_AUTHOR_EMAIL": self.git_environment["GIT_AUTHOR_EMAIL"],
            "GIT_COMMITTER_NAME": self.git_environment["GIT_COMMITTER_NAME"],
            "GIT_COMMITTER_EMAIL": self.git_environment["GIT_COMMITTER_EMAIL"],
            "GIT_DIR": str(decoy / ".git"),
            "GIT_WORK_TREE": str(source),
            "GIT_INDEX_FILE": str(external_index),
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": str(helper),
            "GIT_CONFIG_KEY_1": "core.attributesFile",
            "GIT_CONFIG_VALUE_1": str(attributes),
            "GIT_CONFIG_KEY_2": "diff.hostile.textconv",
            "GIT_CONFIG_VALUE_2": str(helper),
            "GIT_CONFIG_KEY_3": "core.hooksPath",
            "GIT_CONFIG_VALUE_3": str(self.root),
            "HOSTILE_GIT_MARKER": str(marker),
        }

        with mock.patch.dict(os.environ, hostile_environment, clear=False):
            public_release.create_candidate(source, destination)

        self.assertEqual(
            self.git(destination, "status", "--porcelain").stdout,
            "",
        )
        self.assertEqual(external_index.read_bytes(), external_index_before)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(
            self.git(
                destination, "log", "-1", "--format=%an <%ae>|%cn <%ce>"
            ).stdout.strip(),
            "Public Release Tests <public-release-tests@example.invalid>"
            "|Public Release Tests <public-release-tests@example.invalid>",
        )

    def test_failed_worktree_add_does_not_claim_or_remove_destination(self):
        source = self.create_source_repository()
        destination = self.root / "candidate"
        original_run = public_release._run
        staging: Path | None = None

        def fail_after_worktree_add(*arguments, cwd, operation):
            nonlocal staging
            result = original_run(*arguments, cwd=cwd, operation=operation)
            if arguments[:3] == ("git", "worktree", "add"):
                staging = Path(arguments[-2])
                raise public_release.PublicTreeError("simulated worktree add failure")
            return result

        with mock.patch.object(public_release, "_run", fail_after_worktree_add):
            with self.assertRaisesRegex(
                public_release.PublicTreeError,
                "simulated worktree add failure.*recovery required",
            ) as raised:
                public_release.create_candidate(source, destination)

        self.assertFalse(destination.exists())
        self.assertIsNotNone(staging)
        if staging is not None:
            self.assertTrue(staging.exists())
            self.assertIn(str(staging), str(raised.exception))
        self.assertNotIn("refs/heads/release-candidate", str(raised.exception))
        worktrees = self.git(source, "worktree", "list", "--porcelain").stdout
        if staging is not None:
            self.assertIn(str(staging), worktrees)

    def test_rejects_tracked_source_symlink_and_cleans_up(self):
        source = self.create_source_repository()
        link = source / "tests" / "test_link.py"
        link.symlink_to("test_example.py")
        self.git(source, "add", "tests/test_link.py")
        self.git(source, "commit", "--message=add source symlink")
        destination = self.root / "candidate"

        with self.assertRaisesRegex(
            public_release.PublicTreeError,
            "tests/test_link.py",
        ):
            public_release.create_candidate(source, destination)

        self.assertFalse(destination.exists())
        self.assert_branch_absent(source)

    def test_post_worktree_failure_preserves_staging_without_destructive_cleanup(self):
        source = self.create_source_repository()
        destination = self.root / "candidate"
        original_run_result = public_release._run_result
        staging: Path | None = None
        git_operations: list[tuple[str, ...]] = []

        def capture_git_operations(*arguments, cwd, operation):
            nonlocal staging
            git_operations.append(arguments)
            if arguments[:3] == ("git", "worktree", "add"):
                staging = Path(arguments[-2])
            return original_run_result(*arguments, cwd=cwd, operation=operation)

        with (
            mock.patch.object(
                public_release,
                "_run_result",
                side_effect=capture_git_operations,
            ),
            mock.patch.object(
                public_release,
                "verify_public_tree",
                side_effect=public_release.PublicTreeError(
                    "simulated verification failure"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                public_release.PublicTreeError,
                "simulated verification failure.*staging preserved.*recovery required",
            ) as raised:
                public_release.create_candidate(source, destination)

        self.assertFalse(destination.exists())
        self.assertIsNotNone(staging)
        worktrees = self.git(source, "worktree", "list", "--porcelain").stdout
        if staging is not None:
            self.assertTrue(staging.is_dir())
            self.assertIn(str(staging), worktrees)
            self.assertIn(str(staging), str(raised.exception))
        self.assertNotIn("refs/heads/release-candidate", str(raised.exception))
        self.assert_branch_absent(source)
        self.assert_no_destructive_cleanup(git_operations)

    def test_atomic_publish_preserves_competing_destination_entries(self):
        for competitor in ("file", "empty-directory", "directory", "symlink"):
            with self.subTest(competitor=competitor):
                source = self.create_source_repository(f"race-source-{competitor}")
                destination = self.root / f"candidate-{competitor}"
                target = self.root / f"target-{competitor}"
                staging: Path | None = None
                original_rename = getattr(
                    public_release, "_rename_directory_noreplace", None
                )
                original_run_result = public_release._run_result
                git_operations: list[tuple[str, ...]] = []
                self.assertIsNotNone(original_rename)

                def capture_git_operations(*arguments, cwd, operation):
                    git_operations.append(arguments)
                    return original_run_result(
                        *arguments,
                        cwd=cwd,
                        operation=operation,
                    )

                def install_competitor_and_rename(staging_path, destination_path):
                    nonlocal staging
                    staging = staging_path
                    if competitor == "file":
                        destination.write_bytes(b"replacement\n")
                    elif competitor == "empty-directory":
                        destination.mkdir()
                    elif competitor == "directory":
                        destination.mkdir()
                        (destination / "keep.txt").write_bytes(b"keep\n")
                    else:
                        target.mkdir()
                        (target / "keep.txt").write_bytes(b"keep\n")
                        destination.symlink_to(target, target_is_directory=True)
                    if original_rename is not None:
                        original_rename(staging_path, destination_path)

                with (
                    mock.patch.object(
                        public_release,
                        "_run_result",
                        side_effect=capture_git_operations,
                    ),
                    mock.patch.object(
                        public_release,
                        "_rename_directory_noreplace",
                        side_effect=install_competitor_and_rename,
                    ),
                ):
                    with self.assertRaisesRegex(
                        public_release.PublicTreeError,
                        "destination already exists.*staging preserved.*recovery required",
                    ) as raised:
                        public_release.create_candidate(source, destination)

                self.assertIsNotNone(staging)
                self.assertTrue(os.path.lexists(destination))
                if competitor == "file":
                    self.assertEqual(destination.read_bytes(), b"replacement\n")
                    nested_root = destination
                elif competitor == "empty-directory":
                    self.assertEqual(list(destination.iterdir()), [])
                    nested_root = destination
                elif competitor == "directory":
                    self.assertEqual(
                        sorted(path.name for path in destination.iterdir()),
                        ["keep.txt"],
                    )
                    self.assertEqual((destination / "keep.txt").read_bytes(), b"keep\n")
                    nested_root = destination
                else:
                    self.assertTrue(destination.is_symlink())
                    self.assertEqual(destination.readlink(), target)
                    self.assertEqual(
                        sorted(path.name for path in target.iterdir()), ["keep.txt"]
                    )
                    self.assertEqual((target / "keep.txt").read_bytes(), b"keep\n")
                    nested_root = target
                if staging is not None:
                    self.assertFalse((nested_root / staging.name).exists())
                    self.assertTrue(staging.is_dir())
                    self.assertIn(str(staging), str(raised.exception))
                    worktrees = self.git(
                        source, "worktree", "list", "--porcelain"
                    ).stdout
                    self.assertIn(str(staging), worktrees)
                self.assertFalse((nested_root / "README.md").exists())
                self.assertIn(
                    "git update-ref -d refs/heads/release-candidate",
                    str(raised.exception),
                )
                self.assert_no_destructive_cleanup(git_operations)
                candidate_commit = self.git(
                    source, "rev-parse", "--verify", "release-candidate"
                ).stdout.strip()
                self.assertIn(
                    "git update-ref -d refs/heads/release-candidate "
                    + candidate_commit,
                    str(raised.exception),
                )

    def test_success_publishes_and_registers_exact_destination(self):
        source = self.create_source_repository()
        destination = self.root / "candidate"
        original_run_result = public_release._run_result
        staging: Path | None = None
        git_operations: list[tuple[tuple[str, ...], Path]] = []

        def capture_git_operations(*arguments, cwd, operation):
            nonlocal staging
            git_operations.append((arguments, cwd))
            if arguments[:3] == ("git", "worktree", "add"):
                staging = Path(arguments[-2])
            return original_run_result(*arguments, cwd=cwd, operation=operation)

        with mock.patch.object(
            public_release,
            "_run_result",
            side_effect=capture_git_operations,
        ):
            commit = public_release.create_candidate(source, destination)

        self.assertIsNotNone(staging)
        if staging is not None:
            self.assertEqual(staging.parent, destination.parent)
            self.assertTrue(staging.name.startswith(f".{destination.name}."))
            self.assertFalse(staging.exists())
        self.assertEqual(
            self.git(destination, "rev-parse", "HEAD").stdout.strip(), commit
        )
        self.assertEqual(self.git(destination, "status", "--porcelain").stdout, "")
        worktrees = self.git(source, "worktree", "list", "--porcelain").stdout
        registered_paths = [
            Path(line.removeprefix("worktree "))
            for line in worktrees.splitlines()
            if line.startswith("worktree ")
        ]
        self.assertEqual(registered_paths, [source, destination])
        if staging is not None:
            self.assertNotIn(str(staging), worktrees)
            self.assertIn(
                (("git", "status", "--porcelain=v1", "--untracked-files=all"), staging),
                git_operations,
            )
        self.assertIn(
            (("git", "worktree", "repair", str(destination)), source),
            git_operations,
        )
        self.assertFalse(
            any(
                arguments[:3] == ("git", "worktree", "move")
                for arguments, _cwd in git_operations
            )
        )

    def test_repair_failure_preserves_published_candidate_and_branch(self):
        source = self.create_source_repository()
        destination = self.root / "candidate's path"
        original_run_result = public_release._run_result
        staging: Path | None = None

        def fail_worktree_repair(*arguments, cwd, operation):
            nonlocal staging
            if arguments[:3] == ("git", "worktree", "add"):
                staging = Path(arguments[-2])
            if arguments[:3] == ("git", "worktree", "repair"):
                return subprocess.CompletedProcess(
                    arguments,
                    1,
                    stdout="",
                    stderr="simulated repair failure\n",
                )
            return original_run_result(*arguments, cwd=cwd, operation=operation)

        with mock.patch.object(
            public_release,
            "_run_result",
            side_effect=fail_worktree_repair,
        ):
            with self.assertRaisesRegex(
                public_release.PublicTreeError,
                "recovery required",
            ) as raised:
                public_release.create_candidate(source, destination)

        self.assertIsNotNone(staging)
        self.assertTrue(destination.is_dir())
        if staging is not None:
            self.assertFalse(staging.exists())
            worktrees = self.git(source, "worktree", "list", "--porcelain").stdout
            self.assertIn(str(staging), worktrees)
        candidate_commit = self.git(destination, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            self.git(source, "rev-parse", "release-candidate").stdout.strip(),
            candidate_commit,
        )
        self.assertIn(str(destination), str(raised.exception))
        self.assertIn(
            "git worktree repair " + shlex.quote(str(destination)),
            str(raised.exception),
        )

    def test_unsupported_renameat2_preserves_staging_for_recovery(self):
        class MissingRenameAt2:
            pass

        class EnosysRenameAt2:
            argtypes = None
            restype = None

            def __call__(self, *_arguments):
                ctypes.set_errno(errno.ENOSYS)
                return -1

        class EnosysLibc:
            renameat2 = EnosysRenameAt2()

        for support, libc in (
            ("missing-symbol", MissingRenameAt2()),
            ("enosys", EnosysLibc()),
        ):
            with self.subTest(support=support):
                release_ctypes = getattr(public_release, "ctypes", None)
                self.assertIsNotNone(release_ctypes)
                source = self.create_source_repository(f"source-{support}")
                destination = self.root / f"candidate-{support}"
                original_run_result = public_release._run_result
                staging: Path | None = None

                def capture_staging(*arguments, cwd, operation):
                    nonlocal staging
                    if arguments[:3] == ("git", "worktree", "add"):
                        staging = Path(arguments[-2])
                    return original_run_result(*arguments, cwd=cwd, operation=operation)

                with (
                    mock.patch.object(
                        public_release,
                        "_run_result",
                        side_effect=capture_staging,
                    ),
                    mock.patch.object(
                        release_ctypes,
                        "CDLL",
                        return_value=libc,
                    ),
                ):
                    with self.assertRaisesRegex(
                        public_release.PublicTreeError,
                        "renameat2.*recovery required",
                    ) as raised:
                        public_release.create_candidate(source, destination)

                self.assertFalse(os.path.lexists(destination))
                self.assertIsNotNone(staging)
                if staging is not None:
                    self.assertTrue(staging.is_dir())
                    self.assertIn(str(staging), str(raised.exception))
                    worktrees = self.git(
                        source, "worktree", "list", "--porcelain"
                    ).stdout
                    self.assertIn(str(staging), worktrees)
                    candidate_commit = self.git(
                        staging, "rev-parse", "HEAD"
                    ).stdout.strip()
                    self.assertEqual(
                        self.git(
                            source, "rev-parse", "release-candidate"
                        ).stdout.strip(),
                        candidate_commit,
                    )

    def test_failure_before_publication_preserves_random_staging(self):
        source = self.create_source_repository()
        destination = self.root / "candidate"
        sibling = self.root / "keep"
        sibling.mkdir()
        marker = sibling / "marker"
        marker.write_text("keep\n", encoding="utf-8")
        original_run = public_release._run
        staging: Path | None = None

        def capture_worktree_add(*arguments, cwd, operation):
            nonlocal staging
            if arguments[:3] == ("git", "worktree", "add"):
                staging = Path(arguments[-2])
            return original_run(*arguments, cwd=cwd, operation=operation)

        with (
            mock.patch.object(public_release, "_run", side_effect=capture_worktree_add),
            mock.patch.object(
                public_release,
                "verify_public_tree",
                side_effect=public_release.PublicTreeError(
                    "simulated verification failure"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                public_release.PublicTreeError,
                "simulated verification failure.*staging preserved.*recovery required",
            ) as raised:
                public_release.create_candidate(source, destination)

        self.assertIsNotNone(staging)
        if staging is not None:
            self.assertTrue(staging.name.startswith(f".{destination.name}."))
            self.assertTrue(staging.is_dir())
            self.assertIn(str(staging), str(raised.exception))
        self.assertFalse(destination.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
        worktrees = self.git(source, "worktree", "list", "--porcelain").stdout
        if staging is not None:
            self.assertIn(str(staging), worktrees)
        self.assert_branch_absent(source)

    def test_cleanup_preserves_raced_replacement_branch(self):
        source = self.create_source_repository()
        replacement = self.git(source, "rev-parse", "HEAD").stdout.strip()
        destination = self.root / "candidate"
        original_run = public_release._run
        staging: Path | None = None
        replacement_installed = False

        def fail_before_worktree_attachment(*arguments, cwd, operation):
            nonlocal replacement_installed, staging
            if arguments[:3] == ("git", "worktree", "add"):
                staging = Path(arguments[-2])
            if arguments[:3] == ("git", "symbolic-ref", "HEAD"):
                self.git(
                    source,
                    "update-ref",
                    "refs/heads/release-candidate",
                    replacement,
                )
                replacement_installed = True
                raise public_release.PublicTreeError("simulated finalization failure")
            return original_run(*arguments, cwd=cwd, operation=operation)

        with mock.patch.object(public_release, "_run", fail_before_worktree_attachment):
            with self.assertRaisesRegex(
                public_release.PublicTreeError,
                "simulated finalization failure",
            ):
                public_release.create_candidate(source, destination)

        self.assertTrue(replacement_installed)
        self.assertFalse(destination.exists())
        if staging is not None:
            self.assertTrue(staging.is_dir())
        self.assertEqual(
            self.git(source, "rev-parse", "release-candidate").stdout.strip(),
            replacement,
        )

    def test_main_normalizes_expected_operational_errors(self):
        for error in (OSError("permission denied"), UnicodeError("invalid path")):
            with self.subTest(error=type(error).__name__):
                stderr = io.StringIO()
                with mock.patch.object(
                    public_release,
                    "verify_public_tree",
                    side_effect=error,
                ):
                    with redirect_stderr(stderr):
                        result = public_release.main(["verify-tree", str(self.root)])

                self.assertEqual(result, 1)
                self.assertIn(str(error), stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_create_candidate_cli_prints_candidate_commit(self):
        source = self.create_source_repository()
        destination = self.root / "candidate"

        result = self.run_public_release(
            "create-candidate",
            str(destination),
            "--source",
            str(source),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(destination.is_dir(), "candidate destination was not created")
        self.assertEqual(
            result.stdout.strip(),
            self.git(destination, "rev-parse", "HEAD").stdout.strip(),
        )

    def test_create_candidate_cli_reports_staged_and_unstaged_changes(self):
        for staged, message in (
            (False, "source has unstaged tracked changes"),
            (True, "source has staged tracked changes"),
        ):
            with self.subTest(staged=staged):
                source = self.create_source_repository(f"cli-dirty-{staged}")
                (source / "README.md").write_text("dirty\n", encoding="utf-8")
                if staged:
                    self.git(source, "add", "README.md")

                result = self.run_public_release(
                    "create-candidate",
                    str(self.root / f"cli-candidate-{staged}"),
                    "--source",
                    str(source),
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn(f"error: {message}", result.stderr)
                self.assertNotIn("command failed", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_create_candidate_cli_identifies_ref_resolution_failure(self):
        source = self.create_source_repository()

        result = self.run_public_release(
            "create-candidate",
            str(self.root / "candidate"),
            "--source",
            str(source),
            "--ref",
            "missing-ref",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "error: cannot resolve release ref 'missing-ref':",
            result.stderr,
        )
        self.assertNotIn("command failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_verify_tree_cli_reports_forbidden_path(self):
        root = self.create_public_fixture()
        forbidden = root / "private.log"
        forbidden.write_text("private\n", encoding="utf-8")

        result = self.run_public_release("verify-tree", str(root))

        self.assertEqual(result.returncode, 1)
        self.assertIn("private.log", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_rejects_commands_outside_the_public_release_contract(self):
        result = self.run_public_release("push")

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)


if __name__ == "__main__":
    unittest.main()

import io
import importlib.util
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from urllib.parse import unquote
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PKGBUILD = ROOT / "PKGBUILD"
MAKEPKG = ROOT / "scripts" / "makepkg"
ARTIFACT_VERIFIER = ROOT / "scripts" / "verify-artifacts"
VERIFY_ARTIFACTS_LOADER = SourceFileLoader("verify_artifacts", str(ARTIFACT_VERIFIER))
VERIFY_ARTIFACTS_SPEC = importlib.util.spec_from_loader(
    "verify_artifacts", VERIFY_ARTIFACTS_LOADER
)
if VERIFY_ARTIFACTS_SPEC is None or VERIFY_ARTIFACTS_SPEC.loader is None:
    raise RuntimeError(f"cannot load {ARTIFACT_VERIFIER}")
verify_artifacts = importlib.util.module_from_spec(VERIFY_ARTIFACTS_SPEC)
VERIFY_ARTIFACTS_SPEC.loader.exec_module(verify_artifacts)
SERVICE = ROOT / "packaging" / "input-action-controller.service"
RUNTIME_SCAN_PATHS = (
    ROOT / "LICENSE",
    ROOT / "src",
    ROOT / "tests",
    ROOT / "PKGBUILD",
    ROOT / "packaging",
    ROOT / "README.md",
    ROOT / "config.example.toml",
    ROOT / "docs" / "device-discovery.md",
    ROOT / "docs" / "examples",
)
OBSOLETE_RUNTIME_IDENTIFIERS = (
    "voice" + "_input_controller",
    "voice" + "-input-controller",
    "gnome-shell" + "-extension",
    "python-" + "dbus-next",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def is_project_worktree() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and Path(result.stdout.strip()) == ROOT


class PackageMetadataTests(unittest.TestCase):
    def test_pins_ci_tools_and_configures_ruff_for_python_311(self):
        requirements = read(ROOT / "requirements-ci.txt").splitlines()
        self.assertEqual(requirements, ["build==1.5.0", "ruff==0.15.22"])

        pyproject = read(ROOT / "pyproject.toml")
        self.assertIn('[tool.ruff]\ntarget-version = "py311"', pyproject)
        self.assertIn(
            'extend-include = ["scripts/verify-artifacts", "scripts/public-release"]',
            pyproject,
        )

    def test_runtime_paths_contain_no_obsolete_identifiers(self):
        files = tuple(
            path
            for scan_path in RUNTIME_SCAN_PATHS
            for path in (scan_path.rglob("*") if scan_path.is_dir() else (scan_path,))
            if path.is_file() and "__pycache__" not in path.parts
        )

        for identifier in OBSOLETE_RUNTIME_IDENTIFIERS:
            with self.subTest(identifier=identifier):
                matches = [
                    path.relative_to(ROOT) for path in files if identifier in read(path)
                ]
                self.assertEqual(
                    matches, [], f"obsolete identifier {identifier!r}: {matches}"
                )

    def test_declares_generic_package_identity_and_runtime_dependencies(self):
        content = read(PKGBUILD)
        self.assertRegex(
            content,
            r"(?m)^# Maintainer: Ildar Minaev <ildar\.minaev@gmail\.com>$",
        )
        self.assertRegex(content, r"(?m)^pkgname=input-action-controller$")
        self.assertRegex(content, r"(?m)^pkgver=0\.1\.0$")
        self.assertIn("pkgrel=1", content)
        self.assertRegex(
            content,
            r'(?m)^url="https://github\.com/IldarMinaev/input-action-controller"$',
        )

        dependencies_match = re.search(r"(?m)^depends=\(([^)]*)\)$", content)
        self.assertIsNotNone(dependencies_match, "missing depends declaration")
        if dependencies_match is None:
            return
        dependencies = dependencies_match.group(1)

        for dependency in (
            "python",
            "python-pyudev",
            "python-evdev",
            "python-tomlkit",
            "systemd",
            "sudo",
            "acl",
            "coreutils",
        ):
            with self.subTest(dependency=dependency):
                self.assertIn(f"'{dependency}'", dependencies)

        self.assertIn(
            'dependencies = ["pyudev", "evdev", "tomlkit"]',
            read(ROOT / "pyproject.toml"),
        )

        for dependency in (
            "dsnote",
            "handy",
            "gnome-shell",
            "ydotool",
            "wl-clipboard",
            OBSOLETE_RUNTIME_IDENTIFIERS[3],
        ):
            with self.subTest(dependency=dependency):
                self.assertNotIn(dependency, dependencies)

    def test_declares_check_dependencies_for_package_tests(self):
        content = read(PKGBUILD)
        check_dependencies_match = re.search(r"(?m)^checkdepends=\(([^)]*)\)$", content)
        self.assertIsNotNone(
            check_dependencies_match, "missing checkdepends declaration"
        )
        if check_dependencies_match is None:
            return
        check_dependencies = check_dependencies_match.group(1)

        for dependency in (
            "python-pyudev",
            "python-evdev",
            "python-tomlkit",
            "git",
        ):
            with self.subTest(dependency=dependency):
                self.assertIn(f"'{dependency}'", check_dependencies)

    def test_installs_only_generic_runtime_assets_and_task_eight_documentation(self):
        content = read(PKGBUILD)
        required_paths = (
            "packaging/input-action-controller.service",
            "usr/lib/systemd/user/input-action-controller.service",
            "usr/share/doc/input-action-controller/config.example.toml",
            "usr/share/doc/input-action-controller/README.md",
            "usr/share/doc/input-action-controller/docs/configuration.md",
            "usr/share/doc/input-action-controller/docs/device-discovery.md",
            "usr/share/doc/input-action-controller/docs/examples/handy-gnome-wayland.md",
            "usr/share/doc/input-action-controller/docs/examples/dsnote-gnome-wayland.md",
            "usr/share/licenses/$pkgname/LICENSE",
        )
        for path in required_paths:
            with self.subTest(path=path):
                self.assertIn(path, content)

        forbidden_paths = (
            OBSOLETE_RUNTIME_IDENTIFIERS[1],
            "60-plantronics-voice-input.rules",
            OBSOLETE_RUNTIME_IDENTIFIERS[2],
            "glib-compile-schemas",
            "install=",
            "/home/",
        )
        for path in forbidden_paths:
            with self.subTest(path=path):
                self.assertNotIn(path, content)

    def test_does_not_install_or_manage_an_active_configuration(self):
        content = read(PKGBUILD)
        self.assertNotIn("backup=(", content)
        self.assertNotIn("etc/input-action-controller/config.toml", content)

    def test_packaged_template_is_commented_and_device_neutral(self):
        template = read(ROOT / "config.example.toml")
        nonblank = [line for line in template.splitlines() if line.strip()]
        self.assertTrue(nonblank)
        self.assertTrue(all(line.lstrip().startswith("#") for line in nonblank))
        for forbidden in (
            "handy",
            "dsnote",
            "plantronics",
            "xiaomi",
            "047f",
            "c056",
            "2717",
            "5070",
            "08 02",
            "08 00",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template.lower())


class UserServiceTests(unittest.TestCase):
    def test_installs_the_configured_generic_user_service(self):
        self.assertTrue(SERVICE.is_file(), f"missing service: {SERVICE}")
        if not SERVICE.is_file():
            return
        self.assertEqual(
            read(SERVICE),
            "[Unit]\n"
            "Description=Map Linux input events to configurable actions\n"
            "After=graphical-session.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "ExecStart=/usr/bin/input-action-controller daemon\n"
            "Restart=on-failure\n"
            "RestartSec=2\n"
            "\n"
            "[Install]\n"
            "WantedBy=graphical-session.target\n",
        )


class MakepkgScriptTests(unittest.TestCase):
    def test_builds_the_renamed_source_archive_and_writes_the_package_at_root(self):
        content = read(MAKEPKG)
        self.assertIn("pkgname=input-action-controller", content)
        self.assertIn("pkgrel=1", content)
        self.assertIn('archive="$source_dir/$pkgname-$pkgver.tar.gz"', content)
        self.assertIn('PKGDEST="$root"', content)
        self.assertNotIn("dist-pkg", content)

    def test_source_archive_contains_only_required_project_files(self):
        if not is_project_worktree():
            self.skipTest("source archive membership requires the project worktree")

        with tempfile.TemporaryDirectory() as temporary_directory:
            build_root = Path(temporary_directory) / "makepkg"
            environment = os.environ | {
                "MAKEPKG_BUILD_ROOT": str(build_root),
                "SOURCE_DATE_EPOCH": "0",
            }
            result = subprocess.run(
                [str(MAKEPKG), "--source-only"],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            archive = build_root / "sources" / "input-action-controller-0.1.0.tar.gz"
            self.assertTrue(archive.is_file(), f"missing source archive: {archive}")
            first_archive = archive.read_bytes()
            with tarfile.open(archive, mode="r:gz") as source_archive:
                members = set(source_archive.getnames())
                markdown_documents = {
                    member.name: source_archive.extractfile(member)
                    .read()
                    .decode("utf-8")
                    for member in source_archive.getmembers()
                    if member.isfile() and member.name.endswith(".md")
                }

            second_result = subprocess.run(
                [str(MAKEPKG), "--source-only"],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertEqual(first_archive, archive.read_bytes())

        source_root = "input-action-controller-0.1.0/"
        required_members = (
            ".github/dependabot.yml",
            ".github/workflows/ci.yml",
            ".gitignore",
            "PKGBUILD",
            "pyproject.toml",
            "requirements-ci.txt",
            "config.example.toml",
            "scripts/makepkg",
            "scripts/public-release",
            "scripts/verify-artifacts",
            "packaging/input-action-controller.service",
            "src/input_action_controller/cli.py",
            "docs/configuration.md",
            "docs/device-discovery.md",
            "docs/examples/handy-gnome-wayland.md",
            "tests/test_packaging.py",
            "tests/helpers/command_tree.py",
        )
        for member in required_members:
            with self.subTest(member=member):
                self.assertIn(source_root + member, members)

        forbidden_fragments = (
            ".superpowers",
            "docs/superpowers/",
            "dist-pkg",
            ".makepkg",
            OBSOLETE_RUNTIME_IDENTIFIERS[1],
            OBSOLETE_RUNTIME_IDENTIFIERS[2],
            "udev/",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertFalse(
                    any(fragment in member for member in members),
                    f"unexpected source archive member containing {fragment}",
                )

        self.assertFalse(
            any(".pkg.tar." in member for member in members),
            "unexpected binary package artifact in source archive",
        )

        link_pattern = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
        broken_links = []
        for document, content in markdown_documents.items():
            for raw_target in link_pattern.findall(content):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                if not target or target.startswith("#"):
                    continue
                if re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                    continue
                target_path = unquote(target.split("#", 1)[0])
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(document), target_path)
                )
                if resolved not in members:
                    broken_links.append(f"{document} -> {target}")
        self.assertEqual(
            broken_links,
            [],
            "broken local Markdown links in source archive:\n"
            + "\n".join(broken_links),
        )

    def test_build_removes_stale_makepkg_build_tree_before_invocation(self):
        if not is_project_worktree():
            self.skipTest("makepkg wrapper behavior requires the project worktree")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            build_root = temporary_root / "makepkg"
            stale_marker = build_root / "build" / "stale-marker"
            stale_marker.parent.mkdir(parents=True)
            stale_marker.write_text("stale wheel input", encoding="utf-8")

            environment = os.environ | {
                "MAKEPKG_BUILD_ROOT": str(build_root),
                "SOURCE_DATE_EPOCH": "0",
            }
            result = subprocess.run(
                [str(MAKEPKG), "--source-only"],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_runs_the_artifact_verifier_after_makepkg(self):
        content = read(MAKEPKG)
        self.assertIn("scripts/verify-artifacts", content)
        self.assertIn('makepkg "$@"', content)
        self.assertNotIn('exec makepkg "$@"', content)

    def test_installed_documentation_member_paths_preserve_readme_links(self):
        documentation_root = "usr/share/doc/input-action-controller"
        link_pattern = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
        linked_members = set()
        for raw_target in link_pattern.findall(read(ROOT / "README.md")):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith("#"):
                continue
            if re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue
            linked_members.add(
                posixpath.normpath(
                    f"{documentation_root}/{unquote(target.split('#', 1)[0])}"
                )
            )

        self.assertTrue(linked_members)
        for member in linked_members:
            with self.subTest(member=member):
                self.assertIn(member, verify_artifacts.PACKAGE_MEMBERS)


class ArtifactVerifierTests(unittest.TestCase):
    runtime_modules = tuple(
        path.relative_to(ROOT / "src" / "input_action_controller").as_posix()
        for path in sorted((ROOT / "src" / "input_action_controller").rglob("*.py"))
    )
    runtime_dependencies = (
        "python",
        "python-pyudev",
        "python-evdev",
        "python-tomlkit",
        "systemd",
        "sudo",
        "acl",
        "coreutils",
    )

    def test_verifies_the_release_package_version(self):
        self.assertEqual(verify_artifacts.PACKAGE_VERSION, "0.1.0-1")

    def test_accepts_wheel_only_verification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            wheel = (
                Path(temporary_directory)
                / "input_action_controller-0.1.0-py3-none-any.whl"
            )
            self.create_wheel(wheel)

            result = subprocess.run(
                [sys.executable, str(ARTIFACT_VERIFIER), "--wheel", str(wheel)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("artifact verification passed", result.stdout)

    def create_wheel(self, path: Path, excluded_modules: tuple[str, ...] = ()) -> None:
        with zipfile.ZipFile(path, mode="w") as wheel:
            for module in self.runtime_modules:
                if module not in excluded_modules:
                    wheel.writestr(f"input_action_controller/{module}", "")
            wheel.writestr(
                "input_action_controller-0.1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\n"
                "Name: input-action-controller\n"
                "Version: 0.1.0\n"
                "Requires-Dist: pyudev\n"
                "Requires-Dist: evdev\n"
                "Requires-Dist: tomlkit\n",
            )

    def create_package(
        self,
        path: Path,
        *,
        python_version: str = "3.14",
        extra_python_versions: tuple[str, ...] = (),
        include_package_tree: bool = True,
        dependencies: tuple[str, ...] | None = None,
        extra_members: tuple[str, ...] = (),
    ) -> None:
        members = (
            "usr/bin/input-action-controller",
            "usr/lib/systemd/user/input-action-controller.service",
            "usr/share/doc/input-action-controller/config.example.toml",
            "usr/share/doc/input-action-controller/README.md",
            "usr/share/doc/input-action-controller/docs/configuration.md",
            "usr/share/doc/input-action-controller/docs/device-discovery.md",
            "usr/share/doc/input-action-controller/docs/examples/handy-gnome-wayland.md",
            "usr/share/doc/input-action-controller/docs/examples/dsnote-gnome-wayland.md",
            "usr/share/licenses/input-action-controller/LICENSE",
            *extra_members,
        )
        package_versions = (
            (python_version, *extra_python_versions) if include_package_tree else ()
        )
        package_members = (
            f"usr/lib/python{version}/site-packages/input_action_controller/{module}"
            for version in package_versions
            for module in self.runtime_modules
        )
        with tarfile.open(path, mode="w:gz") as package:
            package_dependencies = dependencies or self.runtime_dependencies
            metadata = (
                "pkgname = input-action-controller\n"
                "pkgver = 0.1.0-1\n"
                + "".join(
                    f"depend = {dependency}\n" for dependency in package_dependencies
                )
            ).encode()
            metadata_info = tarfile.TarInfo(".PKGINFO")
            metadata_info.size = len(metadata)
            package.addfile(metadata_info, io.BytesIO(metadata))
            for member in (*members, *package_members):
                member_info = tarfile.TarInfo(member)
                package.addfile(member_info, io.BytesIO())

    def run_verifier(
        self, wheel: Path, package: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ARTIFACT_VERIFIER),
                "--wheel",
                str(wheel),
                "--package",
                str(package),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_accepts_a_different_python_minor_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            wheel = temporary_root / "input_action_controller-0.1.0-py3-none-any.whl"
            package = temporary_root / "input-action-controller-0.1.0-1-any.pkg.tar.zst"
            self.create_wheel(wheel)
            self.create_package(package, python_version="3.13")

            result = self.run_verifier(wheel, package)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_a_wheel_missing_a_runtime_module(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            wheel = temporary_root / "input_action_controller-0.1.0-py3-none-any.whl"
            package = temporary_root / "input-action-controller-0.1.0-1-any.pkg.tar.zst"
            self.create_wheel(wheel, ("daemon.py",))
            self.create_package(package)

            result = self.run_verifier(wheel, package)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wheel is missing required members", result.stderr)

    def test_rejects_missing_repeated_package_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            wheel = temporary_root / "input_action_controller-0.1.0-py3-none-any.whl"
            package = temporary_root / "input-action-controller-0.1.0-1-any.pkg.tar.zst"
            self.create_wheel(wheel)
            self.create_package(
                package,
                dependencies=tuple(
                    dependency
                    for dependency in self.runtime_dependencies
                    if dependency != "python-tomlkit"
                ),
            )

            result = self.run_verifier(wheel, package)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("package is missing required dependencies", result.stderr)

    def test_rejects_ambiguous_python_site_packages_trees(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            wheel = temporary_root / "input_action_controller-0.1.0-py3-none-any.whl"
            package = temporary_root / "input-action-controller-0.1.0-1-any.pkg.tar.zst"
            self.create_wheel(wheel)
            self.create_package(
                package, python_version="3.13", extra_python_versions=("3.14",)
            )

            result = self.run_verifier(wheel, package)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one Python site-packages tree", result.stderr)

    def test_rejects_a_missing_python_site_packages_tree(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            wheel = temporary_root / "input_action_controller-0.1.0-py3-none-any.whl"
            package = temporary_root / "input-action-controller-0.1.0-1-any.pkg.tar.zst"
            self.create_wheel(wheel)
            self.create_package(package, include_package_tree=False)

            result = self.run_verifier(wheel, package)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one Python site-packages tree", result.stderr)

    def test_rejects_an_active_package_configuration(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            wheel = temporary_root / "input_action_controller-0.1.0-py3-none-any.whl"
            package = temporary_root / "input-action-controller-0.1.0-1-any.pkg.tar.zst"
            self.create_wheel(wheel)
            self.create_package(
                package,
                extra_members=("etc/input-action-controller/config.toml",),
            )

            result = self.run_verifier(wheel, package)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forbidden package member", result.stderr)


if __name__ == "__main__":
    unittest.main()

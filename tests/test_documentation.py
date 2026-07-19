from pathlib import Path
import re
import unittest
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CONFIG_GUIDE = ROOT / "docs" / "configuration.md"
DEVICE_GUIDE = ROOT / "docs" / "device-discovery.md"
HANDY_GUIDE = ROOT / "docs" / "examples" / "handy-gnome-wayland.md"
DSNOTE_GUIDE = ROOT / "docs" / "examples" / "dsnote-gnome-wayland.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ReadmeContractTests(unittest.TestCase):
    def test_explains_the_hardware_button_use_case_and_runtime_boundary(self):
        content = read(README)
        required = (
            "hardware button",
            "raw `hidraw` reports",
            "`hidraw` and `evdev` input sources",
            "`on-off` and `toggle` input trigger modes",
            "stable udev-backed device resolution and hotplug recovery",
            "## Why use it?",
            "## What it does not do",
            "does not perform speech recognition",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_the_quick_start_and_reference_guides(self):
        content = read(README)
        required = (
            "./scripts/makepkg -si",
            "input-action-controller setup",
            "input-action-controller config-check",
            "systemctl --user enable --now input-action-controller.service",
            "systemctl --user status input-action-controller.service",
            "input-action-controller status",
            "[configuration reference](docs/configuration.md)",
            "[device-discovery guide](docs/device-discovery.md)",
            "[Handy GNOME Wayland guide](docs/examples/handy-gnome-wayland.md)",
            "[Speech Note GNOME Wayland guide](docs/examples/dsnote-gnome-wayland.md)",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_common_operations(self):
        content = read(README)
        required = (
            "## Actions",
            "## Inputs and selection",
            "## Discover and monitor devices",
            "input-action-controller devices",
            "input-action-controller monitor --device",
            "## Run the service",
            "journalctl --user -u input-action-controller.service",
            "## Upgrade",
            "systemctl --user restart input-action-controller.service",
            "## Remove",
            "sudo pacman -R input-action-controller",
            "## Development",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_uses_input_modes_rather_than_action_types(self):
        content = read(README)
        self.assertNotIn("an `on-off` action", content)
        self.assertNotIn("a `toggle` action", content)

    def test_documents_xdg_configuration_removal(self):
        content = read(README)
        required = (
            "Package removal does not remove your XDG configuration.",
            "config_home=${XDG_CONFIG_HOME:-$HOME/.config}/input-action-controller",
            'rm -r "$config_home"',
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)


class ConfigurationGuideContractTests(unittest.TestCase):
    def test_documents_configuration_precedence_setup_restrictions_and_manual_toml(
        self,
    ):
        self.assertTrue(
            CONFIG_GUIDE.is_file(), f"missing configuration guide: {CONFIG_GUIDE}"
        )
        content = " ".join(read(CONFIG_GUIDE).split())
        required = (
            "stops an active service while editing",
            "restores the previous service state if setup is cancelled before the configuration is committed",
            "input-action-controller --config PATH setup",
            "INPUT_ACTION_CONTROLLER_CONFIG",
            "$XDG_CONFIG_HOME/input-action-controller/config.toml",
            "/etc/input-action-controller/config.toml",
            "does not edit `/etc` configuration",
            "symbolic link destination",
            "${XDG_CONFIG_HOME:-$HOME/.config}/input-action-controller",
            "/usr/share/doc/input-action-controller/config.example.toml",
            'strategy = "priority"',
            'strategy = "all"',
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)
        self.assertNotIn("input-action-controller setup --config PATH", content)

    def test_uses_a_device_neutral_complete_toml_example(self):
        content = read(CONFIG_GUIDE)
        required = (
            'name = "Example hidraw on-off device"',
            'name = "Example evdev toggle button"',
            'vendor_id = "1234"',
            'product_id = "5678"',
            'on_reports = ["01 02"]',
            'off_reports = ["01 00"]',
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)
        for value in ("Plantronics", "Xiaomi", "047f", "c056", "08 02", "08 00"):
            with self.subTest(value=value):
                self.assertNotIn(value, content)

    def test_documents_actions_selection_and_triggers(self):
        self.assertTrue(
            CONFIG_GUIDE.is_file(), f"missing configuration guide: {CONFIG_GUIDE}"
        )
        content = read(CONFIG_GUIDE)
        required = (
            "argv arrays",
            "skip_off_after_failed_on",
            "skip_on_after_failed_off",
            "multiple device profiles",
            "serial",
            "id_path",
            "ambiguous-device",
            "device-node-conflict",
            'mode = "on-off"',
            "toggle_events",
            "toggle_off_timeout_seconds = 0",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_cleanup_shutdown_status_and_custom_paths(self):
        self.assertTrue(
            CONFIG_GUIDE.is_file(), f"missing configuration guide: {CONFIG_GUIDE}"
        )
        content = read(CONFIG_GUIDE)
        required = (
            "process group",
            "shutdown_timeout_seconds",
            "off_on_shutdown = true",
            "configuration: valid",
            "runtime lock:",
            "service activity and runtime lock independently",
            "does not use a custom configuration path",
            "input-action-controller --config /absolute/path/config.toml daemon",
            "daemon/monitor lock contention",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_monitor_lock_contention_with_foreground_daemon(self):
        content = " ".join(read(CONFIG_GUIDE).split())
        required = (
            "`monitor` refuses to run while any daemon owns the runtime lock",
            "Stopping the packaged service may be insufficient",
            "foreground daemon still holds it",
            "monitor exits with lock-contention status 3",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_readable_device_managed_permission_migration(self):
        self.assertTrue(
            CONFIG_GUIDE.is_file(), f"missing configuration guide: {CONFIG_GUIDE}"
        )
        content = " ".join(read(CONFIG_GUIDE).split())
        required = (
            "readable devices normally skip permission changes",
            "Create a managed permission rule for this readable device? [y/N/x]",
            "removes broad rules and `input` group membership only after reconnect and action tests pass",
            "[device-discovery guide](device-discovery.md)",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)


class DeviceDiscoveryContractTests(unittest.TestCase):
    def test_documents_classifier_first_composite_evdev_setup(self):
        content = " ".join(read(DEVICE_GUIDE).split())
        required = (
            "Composite evdev devices",
            "standard udev `ID_INPUT_*` classifier",
            "serial number or `ID_PATH`",
            "selected classifier appears in the managed udev rule",
            "not stored in the runtime profile",
            "same capability-based resolver used by the daemon",
            "stops before saving",
            "does not identify the selected node uniquely",
            "`ID_PATH` is offered only as an explicit fallback",
            "unique after classifier narrowing",
            "bound to that physical port",
            "keyboard-class confirmation",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

        for classifier in (
            "ID_INPUT_TRACKBALL",
            "ID_INPUT_POINTINGSTICK",
            "ID_INPUT_TOUCHPAD",
            "ID_INPUT_TOUCHSCREEN",
            "ID_INPUT_TABLET_PAD",
            "ID_INPUT_TABLET",
            "ID_INPUT_JOYSTICK",
            "ID_INPUT_KEYBOARD",
            "ID_INPUT_MOUSE",
            "ID_INPUT_KEY",
        ):
            with self.subTest(classifier=classifier):
                self.assertIn(classifier, content)

    def test_documents_manual_discovery_as_an_advanced_fallback(self):
        content = " ".join(read(DEVICE_GUIDE).split())
        required = (
            "advanced fallback",
            "input-action-controller setup",
            "seed for the user configuration",
            "deferred until setup saves",
            "symbolic link",
            "Current scope",
            "Future scope",
            'TAG+="uaccess"',
            'GROUP="input"',
            "can expose other matching input nodes",
            "config.toml.bak.YYYYMMDDTHHMMSSZ",
            "recovery artifact",
            "Recovery commands",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_discovery_monitoring_and_capture_tools(self):
        content = read(DEVICE_GUIDE)
        required = (
            "lsusb",
            "udevadm info",
            "libinput debug-events",
            "evtest",
            "usbhid-dump",
            "input-action-controller devices",
            "input-action-controller monitor --device",
            "can interfere with normal input",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_narrow_reviewable_rules_and_access_verification(self):
        content = read(DEVICE_GUIDE)
        required = (
            'SUBSYSTEM=="hidraw"',
            'KERNEL=="event*"',
            "ID_SERIAL_SHORT",
            "id_path",
            "Inspect the generated rule",
            "sudo install",
            "udevadm test",
            "udevadm control --reload-rules",
            "udevadm trigger",
            "Set `node` to the rediscovered path before checking access:",
            "node=/dev/hidrawN",
            "getfacl",
            "cannot be represented",
            "keyboard-class access permits observing ordinary keys",
            "ships no broad udev rule",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)

    def test_documents_the_verified_xiaomi_thumb_button_profile(self):
        content = read(DEVICE_GUIDE)
        required = (
            "Xiaomi Wireless Mouse 3 Colorful Mouse",
            "2717:5070",
            "/dev/input/event4",
            'strategy = "all"',
            'name = "Xiaomi Wireless Mouse 3 rear thumb button"',
            'interface_number = "00"',
            'toggle_events = ["BTN_SIDE"]',
            "toggle_off_timeout_seconds = 60",
            "BTN_EXTRA",
            "unassigned",
            "timeout `5`",
            "timeout `0`",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)


class ApplicationGuideContractTests(unittest.TestCase):
    def test_dependency_matrices_assign_statuses_to_each_dependency(self):
        expected = {
            HANDY_GUIDE: {
                "`input-action-controller`": "required",
                "Handy executable and model": "required",
                "`wtype`": "optional for selected insertion mode",
                "`dotool` and its `input` group access": "optional for selected insertion mode",
                "`wl-clipboard`": "optional for selected insertion mode",
                "`ydotool` and its daemon/socket": "optional for selected insertion mode",
                "Speech Note delivery tools": "not required by input-action-controller",
            },
            DSNOTE_GUIDE: {
                "`input-action-controller`": "required",
                "Speech Note and its model": "required",
                "`ydotool` daemon and socket": "optional for selected insertion mode",
                "Flatpak access to the ydotool socket": "optional for selected insertion mode",
                "`wl-clipboard`": "optional for selected insertion mode",
                "Speech Note delivery tools": "not required by input-action-controller",
            },
        }
        for path, statuses in expected.items():
            content = read(path)
            with self.subTest(path=path):
                for dependency, status in statuses.items():
                    with self.subTest(dependency=dependency):
                        self.assertIn(f"| {dependency} | {status} |", content)

    def test_each_guide_has_a_dependency_matrix_with_controller_boundaries(self):
        for path in (HANDY_GUIDE, DSNOTE_GUIDE):
            content = read(path)
            with self.subTest(path=path):
                for value in (
                    "Dependency matrix",
                    "required",
                    "optional for selected insertion mode",
                    "not required by input-action-controller",
                    "command -v",
                    "End-to-end check",
                ):
                    self.assertIn(value, content)

    def test_both_guides_cover_complete_arch_gnome_wayland_setup(self):
        for path in (HANDY_GUIDE, DSNOTE_GUIDE):
            content = read(path)
            with self.subTest(path=path):
                for value in (
                    "pacman -Ss",
                    "yay -Ss",
                    "GNOME",
                    "Wayland",
                    "wl-clipboard",
                    "ydotool",
                    "wrapper",
                    "[actions.voice_input]",
                    "[[devices]]",
                    "on_command = [",
                    "off_command = [",
                    "systemctl --user enable --now input-action-controller.service",
                    "input-action-controller status",
                    "journalctl --user -u input-action-controller.service",
                ):
                    self.assertIn(value, content)

    def test_handy_guide_uses_verified_command_and_documents_state_boundary(self):
        content = read(HANDY_GUIDE)
        required = (
            "https://github.com/cjpais/Handy",
            "command -v handy",
            "handy --toggle-transcription",
            "Overlay Position",
            "None",
            "wtype",
            "dotool",
            "Handy does not document it",
            "skip_off_after_failed_on = true",
            "skip_on_after_failed_off = true",
            "UI or hotkey",
            "desynchronized",
            "only upstream-documented controller command",
            "selected insertion path",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, content)
        for value in (
            "Ctrl+V paste method",
            "Typing Tool",
            "external-script insertion mode",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, content)

    def test_speech_note_guide_keeps_stop_action_conditional_and_truthful(self):
        content = read(DSNOTE_GUIDE)
        normalized = " ".join(content.split())
        required = (
            "https://github.com/mkiol/dsnote",
            "command -v dsnote",
            "flatpak run net.mkiol.SpeechNote",
            "--action start-listening-clipboard",
            "stop-listening",
            "--help",
            "Upstream's documented CLI examples omit it",
            "only after the installed build lists `stop-listening`",
            "no documented result-preserving off action",
            "`--action cancel` discards the result",
            "skip_off_after_failed_on = false",
            "skip_on_after_failed_off = false",
            "does not fix Speech Note clipboard delivery",
            "Speech Note's responsibility",
            "native package and Flatpak are separate paths",
            "installed build",
            "ydotool daemon",
            "socket",
            "Only for `start-listening-active-window`",
            "Only for a wrapper that invokes `wl-copy` or `wl-paste`",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, normalized)
        self.assertNotIn(
            "verified `start-listening-clipboard` and `stop-listening`",
            normalized,
        )

    def test_speech_note_guide_does_not_promote_an_unverified_native_build(self):
        content = read(DSNOTE_GUIDE)
        for value in (
            "native package and Flatpak are separate paths",
            "Do not treat `stop-listening` as portable",
            "--action cancel",
            "focused target",
        ):
            with self.subTest(value=value):
                self.assertIn(value, content)
        self.assertNotIn("The verified native build used this direct pair", content)


class DocumentationStructureTests(unittest.TestCase):
    def test_every_local_markdown_link_resolves(self):
        link_pattern = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
        failures = []
        documents = set(ROOT.glob("*.md")) | set((ROOT / "docs").rglob("*.md"))
        for document in sorted(documents):
            for raw_target in link_pattern.findall(read(document)):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                if not target or target.startswith("#"):
                    continue
                if re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                    continue
                target_path = unquote(target.split("#", 1)[0])
                if not (document.parent / target_path).resolve().exists():
                    failures.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(
            failures, [], "broken local Markdown links:\n" + "\n".join(failures)
        )


if __name__ == "__main__":
    unittest.main()

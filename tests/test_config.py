import copy
from dataclasses import FrozenInstanceError
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from input_action_controller.config import (
    ConfigError,
    load_config,
    parse_config,
    resolve_config_path,
)
from input_action_controller.models import (
    ActionRequest,
    ActionState,
    DeviceStrategy,
    EvdevProfile,
    FailedDirection,
    HidrawProfile,
)


VALID_CONFIG = """
[runner]
timeout_seconds = 5.0
shutdown_timeout_seconds = 10.0

[device_selection]
strategy = "all"

[actions.voice_input]
on_command = ["/usr/bin/handy", "--toggle-transcription"]
off_command = ["/usr/bin/handy", "--toggle-transcription"]
skip_off_after_failed_on = true
skip_on_after_failed_off = true
off_on_shutdown = true

[[devices]]
name = "Plantronics Blackwire C3220"
action = "voice_input"
transport = "hidraw"
mode = "on-off"
vendor_id = "047F"
product_id = "c056"
interface_number = "3"
on_reports = ["08 02"]
off_reports = ["08 00"]

[[devices]]
name = "Mouse side button"
action = "voice_input"
transport = "evdev"
mode = "toggle"
vendor_id = "1234"
product_id = "5678"
toggle_events = ["BTN_SIDE"]
"""


class FakeEcodes:
    ecodes = {
        "BTN_SIDE": 275,
        "KEY_ALIAS": 183,
        "KEY_F13": 183,
        "REL_X": 0,
    }
    keys = {
        183: "KEY_F13",
        275: "BTN_SIDE",
    }


class ConfigTests(unittest.TestCase):
    def load(self, text: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            with patch(
                "input_action_controller.config._load_ecodes", return_value=FakeEcodes
            ):
                return load_config(path)

    def parse(self, raw):
        with patch(
            "input_action_controller.config._load_ecodes", return_value=FakeEcodes
        ):
            return parse_config(raw)

    def valid_raw(self):
        return tomllib.loads(VALID_CONFIG)

    def test_loads_actions_transports_and_defaults(self):
        config = self.load(VALID_CONFIG)

        self.assertEqual(config.runner.timeout_seconds, 5.0)
        self.assertEqual(config.runner.shutdown_timeout_seconds, 10.0)
        self.assertEqual(config.device_selection.strategy, DeviceStrategy.ALL)
        self.assertEqual(config.actions[0].name, "voice_input")
        self.assertEqual(
            config.actions[0].on_command,
            ("/usr/bin/handy", "--toggle-transcription"),
        )
        self.assertTrue(config.actions[0].skip_off_after_failed_on)
        self.assertTrue(config.actions[0].skip_on_after_failed_off)
        self.assertTrue(config.actions[0].off_on_shutdown)

        hidraw = config.devices[0]
        self.assertIsInstance(hidraw, HidrawProfile)
        self.assertEqual(hidraw.vendor_id, "047f")
        self.assertEqual(hidraw.product_id, "c056")
        self.assertEqual(hidraw.interface_number, "03")
        self.assertEqual(hidraw.on_reports, (bytes.fromhex("08 02"),))
        self.assertEqual(hidraw.off_reports, (bytes.fromhex("08 00"),))

        evdev = config.devices[1]
        self.assertIsInstance(evdev, EvdevProfile)
        self.assertEqual(evdev.toggle_events, ("BTN_SIDE",))
        self.assertEqual(evdev.on_events, ())
        self.assertEqual(evdev.off_events, ())
        self.assertEqual(evdev.toggle_off_timeout_seconds, 60.0)
        self.assertIsNone(evdev.input_classifier)

    def test_accepts_and_validates_evdev_input_classifier(self):
        raw = self.valid_raw()
        raw["devices"][1]["input_classifier"] = "ID_INPUT_MOUSE"
        self.assertEqual(
            self.parse(raw).devices[1].input_classifier,
            "ID_INPUT_MOUSE",
        )

        raw["devices"][1]["input_classifier"] = "ID_INPUT_VENDOR_SPECIAL"
        with self.assertRaisesRegex(ConfigError, "input_classifier"):
            self.parse(raw)

        raw = self.valid_raw()
        raw["devices"][0]["input_classifier"] = "ID_INPUT_MOUSE"
        with self.assertRaisesRegex(ConfigError, "unknown"):
            self.parse(raw)

    def test_applies_all_omitted_defaults(self):
        raw = self.valid_raw()
        del raw["runner"]
        del raw["device_selection"]
        action = raw["actions"]["voice_input"]
        del action["skip_off_after_failed_on"]
        del action["skip_on_after_failed_off"]
        del action["off_on_shutdown"]

        config = self.parse(raw)

        self.assertEqual(config.runner.timeout_seconds, 5.0)
        self.assertEqual(config.runner.shutdown_timeout_seconds, 10.0)
        self.assertEqual(config.device_selection.strategy, DeviceStrategy.PRIORITY)
        self.assertFalse(config.actions[0].skip_off_after_failed_on)
        self.assertFalse(config.actions[0].skip_on_after_failed_off)
        self.assertTrue(config.actions[0].off_on_shutdown)

    def test_models_are_frozen_and_enums_have_stable_values(self):
        config = self.load(VALID_CONFIG)

        with self.assertRaises(FrozenInstanceError):
            config.runner.timeout_seconds = 2.0
        self.assertEqual(
            [item.value for item in ActionRequest], ["on", "off", "toggle", "auto-off"]
        )
        self.assertEqual(
            [item.value for item in ActionState], ["off", "on", "uncertain"]
        )
        self.assertEqual([item.value for item in FailedDirection], ["on", "off"])

    def test_rejects_unknown_keys_at_every_level(self):
        cases = {
            "root": lambda raw: raw.update(extra=True),
            "runner": lambda raw: raw["runner"].update(extra=True),
            "device selection": lambda raw: raw["device_selection"].update(extra=True),
            "action": lambda raw: raw["actions"]["voice_input"].update(extra=True),
            "device": lambda raw: raw["devices"][0].update(extra=True),
        }

        for label, mutate in cases.items():
            with self.subTest(level=label):
                raw = self.valid_raw()
                mutate(raw)
                with self.assertRaisesRegex(ConfigError, "unknown"):
                    self.parse(raw)

    def test_rejects_duplicate_names_and_unknown_actions(self):
        cases = {
            "duplicate device name": lambda raw: raw["devices"].append(
                copy.deepcopy(raw["devices"][0])
            ),
            "unknown action": lambda raw: raw["devices"][0].update(action="missing"),
        }

        for label, mutate in cases.items():
            with self.subTest(case=label):
                raw = self.valid_raw()
                mutate(raw)
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_rejects_invalid_commands(self):
        cases = {
            "empty argv": [],
            "empty argv entry": ["/usr/bin/handy", ""],
            "argv is a string": "/usr/bin/handy",
            "argv entry is not a string": ["/usr/bin/handy", 1],
        }

        for label, value in cases.items():
            with self.subTest(case=label):
                raw = self.valid_raw()
                raw["actions"]["voice_input"]["on_command"] = value
                with self.assertRaisesRegex(ConfigError, "on_command"):
                    self.parse(raw)

    def test_rejects_nul_bytes_in_each_action_command(self):
        for command_name in ("on_command", "off_command"):
            with self.subTest(command_name=command_name):
                raw = self.valid_raw()
                raw["actions"]["voice_input"][command_name] = [
                    "/usr/bin/handy",
                    "contains\x00nul",
                ]

                with self.assertRaisesRegex(
                    ConfigError,
                    rf"^actions\.voice_input\.{command_name} must not contain NUL bytes$",
                ):
                    self.parse(raw)

    def test_accepts_unicode_and_whitespace_command_arguments(self):
        raw = self.valid_raw()
        raw["actions"]["voice_input"]["on_command"] = [
            "/usr/bin/handy",
            "  start voice  ",
            "\u00fcber",
        ]
        raw["actions"]["voice_input"]["off_command"] = [
            "/usr/bin/handy",
            "  stop voice  ",
            "\u0433\u043e\u0442\u043e\u0432\u043e",
        ]

        config = self.parse(raw)

        self.assertEqual(
            config.actions[0].on_command,
            ("/usr/bin/handy", "  start voice  ", "\u00fcber"),
        )
        self.assertEqual(
            config.actions[0].off_command,
            (
                "/usr/bin/handy",
                "  stop voice  ",
                "\u0433\u043e\u0442\u043e\u0432\u043e",
            ),
        )

    def test_rejects_invalid_numbers(self):
        cases = (
            ("boolean runner timeout", ("runner", "timeout_seconds"), True),
            ("zero runner timeout", ("runner", "timeout_seconds"), 0),
            ("negative shutdown timeout", ("runner", "shutdown_timeout_seconds"), -1),
            ("nonfinite runner timeout", ("runner", "timeout_seconds"), float("nan")),
            (
                "nonfinite shutdown timeout",
                ("runner", "shutdown_timeout_seconds"),
                float("inf"),
            ),
            (
                "boolean toggle timeout",
                ("devices", 1, "toggle_off_timeout_seconds"),
                True,
            ),
            (
                "negative toggle timeout",
                ("devices", 1, "toggle_off_timeout_seconds"),
                -0.1,
            ),
            (
                "nonfinite toggle timeout",
                ("devices", 1, "toggle_off_timeout_seconds"),
                float("inf"),
            ),
        )

        for label, path, value in cases:
            with self.subTest(case=label):
                raw = self.valid_raw()
                target = raw
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_rejects_malformed_selectors_and_reports(self):
        cases = {
            "short vendor id": ("vendor_id", "47f"),
            "nonhex product id": ("product_id", "c05z"),
            "long interface": ("interface_number", "003"),
            "nonhex interface": ("interface_number", "zz"),
            "empty serial": ("serial", ""),
            "empty id path": ("id_path", ""),
            "empty report": ("on_reports", [""]),
            "partial report": ("on_reports", ["8"]),
            "nonhex report": ("on_reports", ["08 0Z"]),
            "empty report list": ("on_reports", []),
        }

        for label, (key, value) in cases.items():
            with self.subTest(case=label):
                raw = self.valid_raw()
                raw["devices"][0][key] = value
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_rejects_overlapping_triggers(self):
        cases = {
            "hidraw": lambda raw: raw["devices"][0].update(off_reports=["08 02"]),
            "evdev": lambda raw: raw["devices"][1].update(
                mode="on-off", on_events=["KEY_F13"], off_events=["KEY_F13"]
            ),
            "evdev aliases": lambda raw: raw["devices"][1].update(
                mode="on-off", on_events=["KEY_F13"], off_events=["KEY_ALIAS"]
            ),
        }

        for label, mutate in cases.items():
            with self.subTest(transport=label):
                raw = self.valid_raw()
                if label.startswith("evdev"):
                    del raw["devices"][1]["toggle_events"]
                mutate(raw)
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_rejects_invalid_transport_modes_and_fields(self):
        cases = {
            "hidraw toggle": lambda raw: raw["devices"][0].update(mode="toggle"),
            "hidraw evdev field": lambda raw: raw["devices"][0].update(
                toggle_events=["BTN_SIDE"]
            ),
            "evdev hidraw field": lambda raw: raw["devices"][1].update(
                on_reports=["08 02"]
            ),
            "unsupported transport": lambda raw: raw["devices"][0].update(
                transport="other"
            ),
            "evdev on-off missing off": lambda raw: raw["devices"][1].update(
                mode="on-off", on_events=["KEY_F13"]
            ),
        }

        for label, mutate in cases.items():
            with self.subTest(case=label):
                raw = self.valid_raw()
                if label == "evdev on-off missing off":
                    del raw["devices"][1]["toggle_events"]
                mutate(raw)
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_rejects_invalid_evdev_event_names_and_codes(self):
        cases = {
            "unknown name": ["KEY_DOES_NOT_EXIST"],
            "non-key event": ["REL_X"],
            "numeric code": [275],
            "empty list": [],
        }

        for label, value in cases.items():
            with self.subTest(case=label):
                raw = self.valid_raw()
                raw["devices"][1]["toggle_events"] = value
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_rejects_wrong_container_and_scalar_types(self):
        cases = {
            "runner": lambda raw: raw.update(runner=[]),
            "device selection": lambda raw: raw.update(device_selection="priority"),
            "actions": lambda raw: raw.update(actions=[]),
            "devices": lambda raw: raw.update(devices={}),
            "action definition": lambda raw: raw["actions"].update(voice_input=[]),
            "device definition": lambda raw: raw["devices"].__setitem__(0, []),
            "boolean option": lambda raw: raw["actions"]["voice_input"].update(
                off_on_shutdown=1
            ),
        }

        for label, mutate in cases.items():
            with self.subTest(case=label):
                raw = self.valid_raw()
                mutate(raw)
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_requires_actions_and_devices(self):
        for key in ("actions", "devices"):
            with self.subTest(key=key):
                raw = self.valid_raw()
                del raw[key]
                with self.assertRaises(ConfigError):
                    self.parse(raw)

    def test_config_path_precedence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            override = root / "override.toml"
            xdg = root / "xdg"
            xdg_config = xdg / "input-action-controller" / "config.toml"
            xdg_config.parent.mkdir(parents=True)
            xdg_config.write_text("", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "INPUT_ACTION_CONTROLLER_CONFIG": str(override),
                    "XDG_CONFIG_HOME": str(xdg),
                },
                clear=True,
            ):
                self.assertEqual(resolve_config_path(), override)

            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=True):
                self.assertEqual(resolve_config_path(), xdg_config)

            xdg_config.unlink()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=True):
                self.assertEqual(
                    resolve_config_path(),
                    Path("/etc/input-action-controller/config.toml"),
                )

    def test_config_path_uses_default_xdg_home_when_environment_is_unset(self):
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            config_path = home / ".config" / "input-action-controller" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("", encoding="utf-8")

            with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
                self.assertEqual(resolve_config_path(), config_path)

    def test_empty_xdg_config_home_uses_home_default_instead_of_cwd(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home_config = home / ".config" / "input-action-controller" / "config.toml"
            home_config.parent.mkdir(parents=True)
            home_config.write_text("", encoding="utf-8")

            working_directory = root / "work"
            cwd_config = working_directory / "input-action-controller" / "config.toml"
            cwd_config.parent.mkdir(parents=True)
            cwd_config.write_text("", encoding="utf-8")

            previous_directory = Path.cwd()
            try:
                os.chdir(working_directory)
                with patch.dict(
                    os.environ,
                    {"HOME": str(home), "XDG_CONFIG_HOME": ""},
                    clear=True,
                ):
                    self.assertEqual(resolve_config_path(), home_config)
            finally:
                os.chdir(previous_directory)

    def test_explicit_path_takes_precedence_over_environment(self):
        with TemporaryDirectory() as directory:
            explicit = Path(directory) / "explicit.toml"
            with patch.dict(
                os.environ,
                {"INPUT_ACTION_CONTROLLER_CONFIG": "/ignored/config.toml"},
                clear=True,
            ):
                with patch(
                    "input_action_controller.config.parse_config",
                    return_value="parsed",
                ) as parser:
                    explicit.write_text(VALID_CONFIG, encoding="utf-8")
                    self.assertEqual(load_config(explicit), "parsed")
                    parser.assert_called_once()

    @unittest.skipUnless(
        importlib.util.find_spec("evdev"), "python-evdev is not installed"
    )
    def test_installed_evdev_table_accepts_symbolic_key_name(self):
        raw = self.valid_raw()
        config = parse_config(raw)
        self.assertEqual(config.devices[1].toggle_events, ("BTN_SIDE",))


if __name__ == "__main__":
    unittest.main()

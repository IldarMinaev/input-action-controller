from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from evdev import ecodes

from input_action_controller.cli import build_parser, main
from input_action_controller.config import ConfigError
from input_action_controller.daemon import (
    DEFAULT_SOURCE_FACTORY as DAEMON_SOURCE_FACTORY,
)
import input_action_controller.diagnostics as diagnostics
from input_action_controller.diagnostics import (
    DEFAULT_SOURCE_FACTORY,
    RawEvdevEvent,
    run_devices,
    run_monitor,
    run_status,
)
from input_action_controller.locking import LockContendedError
from input_action_controller.models import (
    ActionConfig,
    AppConfig,
    DeviceSelectionConfig,
    DeviceStrategy,
    EvdevProfile,
    HidrawProfile,
    RunnerConfig,
)
from input_action_controller.devices.discovery import DeviceCandidate
from tests.helpers.setup import FakeDiscovery


def diagnostic_config(
    strategy: DeviceStrategy = DeviceStrategy.PRIORITY,
) -> AppConfig:
    return AppConfig(
        runner=RunnerConfig(),
        device_selection=DeviceSelectionConfig(strategy),
        actions=(
            ActionConfig(
                "voice",
                ("/bin/available", "--on"),
                ("/bin/missing", "--off"),
            ),
        ),
        devices=(
            HidrawProfile(
                name="Primary",
                action="voice",
                vendor_id="1111",
                product_id="0001",
                on_reports=(b"\x08\x02",),
                off_reports=(b"\x08\x00",),
            ),
            EvdevProfile(
                name="Keyboard",
                action="voice",
                vendor_id="2222",
                product_id="0002",
                interface_number="01",
                serial="serial",
                id_path="pci-path",
                mode="toggle",
                toggle_events=("KEY_F13",),
            ),
        ),
    )


def candidate(
    profile,
    node: str,
    *,
    event_codes=frozenset(),
    keyboard_class=False,
) -> DeviceCandidate:
    subsystem = "input" if isinstance(profile, EvdevProfile) else "hidraw"
    properties = {
        "DEVNAME": node,
        "ID_VENDOR_ID": profile.vendor_id,
        "ID_MODEL_ID": profile.product_id,
    }
    if profile.interface_number is not None:
        properties["ID_USB_INTERFACE_NUM"] = profile.interface_number
    if profile.serial is not None:
        properties["ID_SERIAL_SHORT"] = profile.serial
    if profile.id_path is not None:
        properties["ID_PATH"] = profile.id_path
    return DeviceCandidate(
        node=node,
        subsystem=subsystem,
        properties=properties,
        event_codes=event_codes,
        keyboard_class=keyboard_class,
    )


class FakeLock:
    def __init__(self, *, owner_pid=None):
        self.owner_pid = owner_pid
        self.acquired = False
        self.released = False

    def acquire(self):
        if self.owner_pid is not None:
            raise LockContendedError(
                Path("/tmp/input-action-controller.lock"),
                self.owner_pid,
            )
        self.acquired = True
        return self

    def release(self):
        self.released = True


class FakeInput(StringIO):
    def __init__(self, value="", *, tty=False):
        super().__init__(value)
        self.tty = tty

    def isatty(self):
        return self.tty


class FakeSource:
    def __init__(self, items=(), error=None):
        self.items = tuple(items)
        self.error = error
        self.runs = 0

    def run(self, stop, emit):
        self.runs += 1
        for item in self.items:
            emit(item)
        if self.error is not None:
            raise self.error


class FakeSourceFactory:
    def __init__(self, source):
        self.source = source
        self.calls = []

    def __call__(self, profile, node):
        self.calls.append((profile, node))
        return self.source


class CliParserTests(unittest.TestCase):
    def test_parses_every_documented_command(self):
        cases = (
            (["setup"], "setup", None),
            (["daemon"], "daemon", None),
            (["status"], "status", None),
            (["config-check"], "config-check", None),
            (["devices"], "devices", None),
            (["monitor", "--device", "Headset"], "monitor", "Headset"),
        )

        for argv, command, device in cases:
            with self.subTest(command=command):
                arguments = build_parser().parse_args(argv)
                self.assertEqual(arguments.command, command)
                self.assertEqual(getattr(arguments, "device", None), device)

    def test_global_config_path_applies_to_every_command(self):
        commands = (
            ["setup"],
            ["daemon"],
            ["status"],
            ["config-check"],
            ["devices"],
            ["monitor", "--device", "Headset"],
        )

        for command in commands:
            with self.subTest(command=command[0]):
                arguments = build_parser().parse_args(
                    ["--config", "/tmp/controller.toml", *command]
                )
                self.assertEqual(arguments.config, Path("/tmp/controller.toml"))

    def test_rejects_generic_action_commands(self):
        for command in ("start", "stop", "toggle"):
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    build_parser().parse_args([command])
                self.assertEqual(raised.exception.code, 2)

    def test_main_returns_usage_status_for_invalid_arguments(self):
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["monitor"]), 2)

    def test_help_lists_setup(self):
        self.assertIn("setup", build_parser().format_help())


class SystemdServiceDiagnosticsTests(unittest.TestCase):
    def test_query_is_bounded_and_uses_direct_argv_without_a_shell(self):
        calls = []

        def run_command(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "active\n", "")

        activity = diagnostics.query_user_service_activity(
            run_command=run_command,
        )

        self.assertEqual(activity, diagnostics.ServiceActivity("active", True))
        self.assertEqual(
            calls,
            [
                (
                    (
                        "systemctl",
                        "--user",
                        "is-active",
                        "input-action-controller.service",
                    ),
                    {
                        "check": False,
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE,
                        "text": True,
                        "timeout": 1.0,
                        "shell": False,
                    },
                )
            ],
        )

    def test_query_reports_unknown_when_systemctl_or_user_bus_is_unavailable(self):
        cases = (
            (
                FileNotFoundError("systemctl"),
                diagnostics.ServiceActivity(
                    "unknown (systemctl unavailable)",
                    False,
                ),
            ),
            (
                subprocess.TimeoutExpired(("systemctl",), 1.0),
                diagnostics.ServiceActivity("unknown (query timed out)", False),
            ),
            (
                subprocess.CompletedProcess(
                    ("systemctl",),
                    1,
                    "",
                    "Failed to connect to bus: No medium found\n",
                ),
                diagnostics.ServiceActivity(
                    "unknown (Failed to connect to bus: No medium found)",
                    False,
                ),
            ),
        )

        for outcome, expected in cases:
            with self.subTest(outcome=outcome):

                def run_command(argv, **kwargs):
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome

                self.assertEqual(
                    diagnostics.query_user_service_activity(
                        run_command=run_command,
                    ),
                    expected,
                )


class StatusDiagnosticsTests(unittest.TestCase):
    def test_reports_lock_executables_all_profiles_and_priority_selection(self):
        config = diagnostic_config()
        primary, keyboard = config.devices
        discovery = FakeDiscovery(
            (
                candidate(primary, "/dev/hidraw1"),
                candidate(
                    keyboard,
                    "/dev/input/event3",
                    event_codes=None,
                    keyboard_class=True,
                ),
            )
        )
        output = StringIO()

        result = run_status(
            config,
            discovery_factory=lambda: discovery,
            lock_factory=lambda: FakeLock(owner_pid=77),
            which_fn=lambda executable: (
                executable if executable == "/bin/available" else None
            ),
            systemctl_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv,
                3,
                "failed\n",
                "",
            ),
            output=output,
        )

        report = output.getvalue()
        self.assertEqual(result, 0)
        self.assertEqual(discovery.calls, 1)
        self.assertIn("configuration: valid", report)
        self.assertIn("service activity: failed", report)
        self.assertIn("runtime lock: held (PID 77)", report)
        self.assertIn("executable voice on: available (/bin/available)", report)
        self.assertIn("executable voice off: unavailable (/bin/missing)", report)
        self.assertIn("profile Primary: available (/dev/hidraw1) [selected]", report)
        self.assertIn(
            "profile Keyboard: permission-denied (/dev/input/event3)",
            report,
        )
        self.assertIn("selection priority: Primary", report)
        self.assertNotIn("application", report.casefold())
        self.assertNotIn("action state", report.casefold())

    def test_all_strategy_marks_every_available_profile_active(self):
        config = diagnostic_config(DeviceStrategy.ALL)
        primary, keyboard = config.devices
        key_code = ecodes.ecodes["KEY_F13"]
        discovery = FakeDiscovery(
            (
                candidate(primary, "/dev/hidraw1"),
                candidate(
                    keyboard,
                    "/dev/input/event3",
                    event_codes=frozenset({key_code}),
                    keyboard_class=True,
                ),
            )
        )
        lock = FakeLock()
        output = StringIO()

        result = run_status(
            config,
            discovery_factory=lambda: discovery,
            lock_factory=lambda: lock,
            which_fn=lambda executable: executable,
            systemctl_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv,
                0,
                "active\n",
                "",
            ),
            output=output,
        )

        report = output.getvalue()
        self.assertEqual(result, 0)
        self.assertTrue(lock.released)
        self.assertIn("service activity: active", report)
        self.assertIn("runtime lock: free", report)
        self.assertIn("profile Primary: available (/dev/hidraw1) [selected]", report)
        self.assertIn(
            "profile Keyboard: available (/dev/input/event3) [selected]",
            report,
        )
        self.assertIn("selection all: Primary, Keyboard", report)


class DeviceDiagnosticsTests(unittest.TestCase):
    def test_lists_stable_selectors_and_keyboard_warning_without_raw_events(self):
        config = diagnostic_config()
        primary, keyboard = config.devices
        key_code = ecodes.ecodes["KEY_F13"]
        discovery = FakeDiscovery(
            (
                candidate(primary, "/dev/hidraw1"),
                candidate(
                    keyboard,
                    "/dev/input/event3",
                    event_codes=frozenset({key_code}),
                    keyboard_class=True,
                ),
            )
        )
        output = StringIO()

        result = run_devices(
            discovery_factory=lambda: discovery,
            output=output,
        )

        report = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn(
            "device /dev/hidraw1: transport=hidraw vendor_id=1111 product_id=0001",
            report,
        )
        self.assertIn(
            "device /dev/input/event3: transport=evdev vendor_id=2222 "
            "product_id=0002 interface_number=01 serial=serial id_path=pci-path",
            report,
        )
        self.assertIn(
            "warning /dev/input/event3: keyboard-class access can observe ordinary key events",
            report,
        )
        self.assertNotIn(str(key_code), report)
        self.assertNotIn("KEY_F13", report)
        self.assertNotIn("08 02", report)


class MonitorDiagnosticsTests(unittest.TestCase):
    def test_daemon_and_monitor_share_the_default_source_factory(self):
        self.assertIs(DEFAULT_SOURCE_FACTORY, DAEMON_SOURCE_FACTORY)

    def test_refuses_active_daemon_before_discovery_or_source_creation(self):
        config = diagnostic_config()
        discovery = FakeDiscovery()
        source_factory = FakeSourceFactory(FakeSource())
        output = StringIO()

        result = run_monitor(
            config,
            "Primary",
            lock_factory=lambda: FakeLock(owner_pid=88),
            discovery_factory=lambda: discovery,
            source_factory=source_factory,
            stdin=FakeInput(),
            output=output,
        )

        self.assertEqual(result, 3)
        self.assertEqual(discovery.calls, 0)
        self.assertEqual(source_factory.calls, [])
        self.assertIn("daemon is active (PID 88)", output.getvalue())

    def test_resolves_only_named_hidraw_profile_and_prints_hex_without_actions(self):
        config = diagnostic_config()
        primary = config.devices[0]
        discovery = FakeDiscovery((candidate(primary, "/dev/hidraw1"),))
        lock = FakeLock()
        source = FakeSource((b"\x08\x02", b"\xff"))
        source_factory = FakeSourceFactory(source)
        output = StringIO()

        result = run_monitor(
            config,
            "Primary",
            lock_factory=lambda: lock,
            discovery_factory=lambda: discovery,
            source_factory=source_factory,
            stdin=FakeInput(),
            output=output,
        )

        self.assertEqual(result, 0)
        self.assertEqual(discovery.calls, 1)
        self.assertEqual(source_factory.calls, [(primary, "/dev/hidraw1")])
        self.assertEqual(source.runs, 1)
        self.assertTrue(lock.released)
        self.assertIn("08 02", output.getvalue())
        self.assertIn("FF", output.getvalue())

    def test_keyboard_monitor_requires_tty_and_explicit_yes(self):
        config = diagnostic_config()
        keyboard = config.devices[1]
        key_code = ecodes.ecodes["KEY_F13"]
        keyboard_candidate = candidate(
            keyboard,
            "/dev/input/event3",
            event_codes=frozenset({key_code}),
            keyboard_class=True,
        )

        for stdin, expected_status in (
            (FakeInput(tty=False), 2),
            (FakeInput("no\n", tty=True), 2),
        ):
            with self.subTest(tty=stdin.tty, answer=stdin.getvalue()):
                lock = FakeLock()
                source_factory = FakeSourceFactory(FakeSource())
                result = run_monitor(
                    config,
                    "Keyboard",
                    lock_factory=lambda: lock,
                    discovery_factory=lambda: FakeDiscovery((keyboard_candidate,)),
                    source_factory=source_factory,
                    stdin=stdin,
                    output=StringIO(),
                )
                self.assertEqual(result, expected_status)
                self.assertEqual(source_factory.calls, [])
                self.assertTrue(lock.released)

        lock = FakeLock()
        source = FakeSource((RawEvdevEvent(ecodes.EV_KEY, key_code, 1),))
        source_factory = FakeSourceFactory(source)
        output = StringIO()
        result = run_monitor(
            config,
            "Keyboard",
            lock_factory=lambda: lock,
            discovery_factory=lambda: FakeDiscovery((keyboard_candidate,)),
            source_factory=source_factory,
            stdin=FakeInput("YeS\n", tty=True),
            output=output,
        )

        self.assertEqual(result, 0)
        self.assertEqual(source.runs, 1)
        self.assertTrue(lock.released)
        self.assertIn("EV_KEY KEY_F13 press", output.getvalue())

    def test_source_failure_is_runtime_failure_and_releases_lock(self):
        config = diagnostic_config()
        primary = config.devices[0]
        lock = FakeLock()
        output = StringIO()

        result = run_monitor(
            config,
            "Primary",
            lock_factory=lambda: lock,
            discovery_factory=lambda: FakeDiscovery(
                (candidate(primary, "/dev/hidraw1"),)
            ),
            source_factory=FakeSourceFactory(
                FakeSource(error=RuntimeError("reader failed"))
            ),
            stdin=FakeInput(),
            output=output,
        )

        self.assertEqual(result, 1)
        self.assertTrue(lock.released)
        self.assertIn("reader failed", output.getvalue())


class CliDispatchTests(unittest.TestCase):
    def test_setup_bypasses_config_loading_and_forwards_the_selected_path(self):
        with (
            patch("input_action_controller.cli.os.geteuid", return_value=1000),
            patch(
                "input_action_controller.cli.load_config",
                side_effect=AssertionError("setup must not load runtime config"),
            ) as load,
            patch(
                "input_action_controller.cli.run_setup",
                return_value=0,
            ) as setup,
        ):
            result = main(["--config", "/tmp/new-config.toml", "setup"])

        self.assertEqual(result, 0)
        load.assert_not_called()
        setup.assert_called_once_with(Path("/tmp/new-config.toml"))

    def test_setup_refuses_effective_uid_zero_before_starting_the_session(self):
        with (
            patch("input_action_controller.cli.os.geteuid", return_value=0),
            patch("input_action_controller.cli.run_setup") as setup,
            redirect_stderr(StringIO()) as error,
        ):
            result = main(["setup"])

        self.assertEqual(result, 2)
        setup.assert_not_called()
        self.assertIn("root", error.getvalue().casefold())

    def test_config_check_loads_selected_config_and_opens_no_device(self):
        config = diagnostic_config()
        output = StringIO()
        with (
            patch(
                "input_action_controller.cli.load_config", return_value=config
            ) as load,
            patch("input_action_controller.cli.run_status") as status,
            patch("input_action_controller.cli.run_devices") as devices,
            patch("input_action_controller.cli.run_monitor") as monitor,
            redirect_stdout(output),
        ):
            result = main(["--config", "/tmp/config.toml", "config-check"])

        self.assertEqual(result, 0)
        load.assert_called_once_with(Path("/tmp/config.toml"))
        status.assert_not_called()
        devices.assert_not_called()
        monitor.assert_not_called()
        self.assertEqual(output.getvalue(), "configuration: valid\n")

    def test_devices_dispatches_without_loading_configuration(self):
        with (
            patch(
                "input_action_controller.cli.load_config",
                side_effect=ConfigError("invalid configuration"),
            ) as load,
            patch(
                "input_action_controller.cli.run_devices",
                return_value=0,
            ) as devices,
        ):
            result = main(["--config", "/missing/config.toml", "devices"])

        self.assertEqual(result, 0)
        load.assert_not_called()
        devices.assert_called_once_with()

    def test_dispatches_commands_and_preserves_their_statuses(self):
        config = diagnostic_config()
        cases = (
            ("status", ["status"], "run_status", 1),
            ("devices", ["devices"], "run_devices", 0),
            ("monitor", ["monitor", "--device", "Primary"], "run_monitor", 3),
        )

        for label, argv, target, expected in cases:
            with self.subTest(command=label):
                with (
                    patch(
                        "input_action_controller.cli.load_config",
                        return_value=config,
                    ),
                    patch(
                        f"input_action_controller.cli.{target}",
                        return_value=expected,
                    ) as dispatch,
                ):
                    result = main(argv)
                    self.assertEqual(result, expected)
                    if label == "devices":
                        dispatch.assert_called_once_with()
                    elif label == "monitor":
                        dispatch.assert_called_once_with(config, "Primary")
                    else:
                        dispatch.assert_called_once_with(config)

        with (
            patch("input_action_controller.cli.load_config", return_value=config),
            patch(
                "input_action_controller.cli.run_daemon",
                new=AsyncMock(return_value=3),
            ) as daemon,
        ):
            self.assertEqual(main(["daemon"]), 3)
            daemon.assert_awaited_once_with(config)

    def test_configuration_errors_return_usage_status_for_every_command(self):
        commands = (
            ["daemon"],
            ["status"],
            ["config-check"],
            ["monitor", "--device", "Primary"],
        )

        for argv in commands:
            with self.subTest(command=argv[0]):
                with (
                    patch(
                        "input_action_controller.cli.load_config",
                        side_effect=ConfigError("invalid configuration"),
                    ),
                    redirect_stderr(StringIO()) as error,
                ):
                    self.assertEqual(main(argv), 2)
                    self.assertIn("invalid configuration", error.getvalue())


if __name__ == "__main__":
    unittest.main()

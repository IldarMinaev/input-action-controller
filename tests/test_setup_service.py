from dataclasses import dataclass
from pathlib import Path
import subprocess
import unittest

from input_action_controller.setup.config_editor import ConfigLocation
from input_action_controller.setup.service import (
    ServiceChoice,
    ServiceConfigurationError,
    ServiceManager,
    ServiceSnapshot,
)


SYSTEMCTL = "/usr/bin/systemctl"
SERVICE = "input-action-controller.service"


@dataclass(frozen=True)
class RunCall:
    argv: tuple[str, ...]
    check: bool
    shell: bool
    stdout: int | None = None
    stderr: int | None = None
    text: bool = False


class FakeSystemctlRunner:
    def __init__(self, *, enabled: bool, active: bool):
        self.enabled = enabled
        self.active = active
        self.calls: list[RunCall] = []
        self.return_codes: dict[str, int] = {}
        self.stdout: dict[str, str] = {}
        self.stderr: dict[str, str] = {}

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        check: bool,
        shell: bool,
        stdout: int | None = None,
        stderr: int | None = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess:
        self.calls.append(RunCall(argv, check, shell, stdout, stderr, text))
        operation = argv[2]
        returncode = self.return_codes.get(operation, self._return_code(operation))
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, argv)
        return subprocess.CompletedProcess(
            argv,
            returncode,
            self.stdout.get(operation, self._stdout(operation)),
            self.stderr.get(operation, ""),
        )

    def _return_code(self, operation: str) -> int:
        if operation == "is-enabled":
            return 0 if self.enabled else 1
        if operation == "is-active":
            return 0 if self.active else 3
        return 0

    def _stdout(self, operation: str) -> str:
        if operation == "is-enabled":
            return "enabled\n" if self.enabled else "disabled\n"
        if operation == "is-active":
            return "active\n" if self.active else "inactive\n"
        return ""


class ServiceManagerTests(unittest.TestCase):
    def manager(
        self,
        runner: FakeSystemctlRunner,
        *,
        packaged_service_compatible: bool = True,
        destination: Path = Path(
            "/home/example/.config/input-action-controller/config.toml"
        ),
    ) -> ServiceManager:
        location = ConfigLocation(
            source=destination,
            destination=destination,
            seed_from_system=False,
            packaged_service_compatible=packaged_service_compatible,
        )
        return ServiceManager(location, runner=runner)

    def test_snapshot_covers_the_enabled_active_matrix_with_known_nonzero_states(self):
        cases = (
            (True, True),
            (False, True),
            (True, False),
            (False, False),
        )

        for enabled, active in cases:
            with self.subTest(enabled=enabled, active=active):
                runner = FakeSystemctlRunner(enabled=enabled, active=active)
                manager = self.manager(runner)

                self.assertEqual(manager.snapshot(), ServiceSnapshot(enabled, active))
                self.assertEqual(
                    runner.calls,
                    [
                        RunCall(
                            (SYSTEMCTL, "--user", "is-enabled", SERVICE),
                            False,
                            False,
                            subprocess.PIPE,
                            subprocess.PIPE,
                            True,
                        ),
                        RunCall(
                            (SYSTEMCTL, "--user", "is-active", SERVICE),
                            False,
                            False,
                        ),
                    ],
                )

    def test_snapshot_rejects_unknown_nonzero_state_codes(self):
        runner = FakeSystemctlRunner(enabled=False, active=False)
        runner.return_codes["is-active"] = 1

        with self.assertRaises(subprocess.CalledProcessError):
            self.manager(runner).snapshot()

    def test_snapshot_rejects_non_boolean_is_enabled_states_despite_ambiguous_return_codes(
        self,
    ):
        cases = (
            ("static", 0),
            ("indirect", 0),
            ("generated", 0),
            ("transient", 0),
            ("enabled-runtime", 0),
            ("alias", 0),
            ("masked", 1),
        )

        for state, returncode in cases:
            with self.subTest(state=state, returncode=returncode):
                runner = FakeSystemctlRunner(enabled=True, active=True)
                runner.return_codes["is-enabled"] = returncode
                runner.stdout["is-enabled"] = f"{state}\n"

                with self.assertRaisesRegex(ValueError, state):
                    self.manager(runner).snapshot()

                self.assertEqual(len(runner.calls), 1)

    def test_snapshot_propagates_is_enabled_operational_errors_instead_of_treating_them_as_disabled(
        self,
    ):
        runner = FakeSystemctlRunner(enabled=False, active=True)
        runner.stdout["is-enabled"] = ""
        runner.stderr["is-enabled"] = "Failed to connect to bus: No medium found\n"

        with self.assertRaises(subprocess.CalledProcessError):
            self.manager(runner).snapshot()

    def test_stop_for_setup_stops_only_an_active_service(self):
        for active in (True, False):
            with self.subTest(active=active):
                runner = FakeSystemctlRunner(enabled=True, active=active)

                self.manager(runner).stop_for_setup(ServiceSnapshot(True, active))

                expected = (
                    [RunCall((SYSTEMCTL, "--user", "stop", SERVICE), True, False)]
                    if active
                    else []
                )
                self.assertEqual(runner.calls, expected)

    def test_restore_reinstates_both_dimensions_for_every_original_state(self):
        cases = (
            (True, True, ("enable", "start")),
            (False, True, ("disable", "start")),
            (True, False, ("enable", "stop")),
            (False, False, ("disable", "stop")),
        )

        for enabled, active, operations in cases:
            with self.subTest(enabled=enabled, active=active):
                runner = FakeSystemctlRunner(enabled=False, active=False)

                self.manager(runner).restore(ServiceSnapshot(enabled, active))

                self.assertEqual(
                    runner.calls,
                    [
                        RunCall((SYSTEMCTL, "--user", operation, SERVICE), True, False)
                        for operation in operations
                    ],
                )

    def test_active_service_defaults_to_restart_and_preserve_stopped_is_explicit(self):
        runner = FakeSystemctlRunner(enabled=True, active=True)
        manager = self.manager(runner)

        self.assertEqual(
            manager.default_choice(ServiceSnapshot(enabled=True, active=True)),
            ServiceChoice.RESTART,
        )
        manager.apply(ServiceChoice.PRESERVE_STOPPED)

        self.assertEqual(runner.calls, [])

    def test_inactive_service_defaults_to_preserving_its_inactive_state(self):
        manager = self.manager(FakeSystemctlRunner(enabled=True, active=False))

        self.assertEqual(
            manager.default_choice(ServiceSnapshot(enabled=True, active=False)),
            ServiceChoice.PRESERVE_INACTIVE,
        )

    def test_apply_uses_the_selected_service_action(self):
        cases = (
            (ServiceChoice.RESTART, ("restart",)),
            (ServiceChoice.ENABLE_AND_START, ("enable", "start")),
            (ServiceChoice.PRESERVE_STOPPED, ()),
            (ServiceChoice.PRESERVE_INACTIVE, ()),
        )

        for choice, operations in cases:
            with self.subTest(choice=choice):
                runner = FakeSystemctlRunner(enabled=True, active=False)

                self.manager(runner).apply(choice)

                self.assertEqual(
                    runner.calls,
                    [
                        RunCall((SYSTEMCTL, "--user", operation, SERVICE), True, False)
                        for operation in operations
                    ],
                )

    def test_custom_config_rejects_packaged_service_actions_and_returns_exact_daemon_command(
        self,
    ):
        destination = Path("/absolute/custom.toml")
        runner = FakeSystemctlRunner(enabled=False, active=False)
        manager = self.manager(
            runner,
            packaged_service_compatible=False,
            destination=destination,
        )

        self.assertEqual(
            manager.manual_daemon_command,
            "input-action-controller --config /absolute/custom.toml daemon",
        )
        for choice in (ServiceChoice.RESTART, ServiceChoice.ENABLE_AND_START):
            with self.subTest(choice=choice):
                with self.assertRaises(ServiceConfigurationError):
                    manager.apply(choice)
        manager.apply(ServiceChoice.PRESERVE_INACTIVE)

        self.assertEqual(runner.calls, [])

    def test_custom_config_quotes_a_foreground_daemon_path_with_spaces(self):
        destination = Path("/absolute/custom config.toml")
        manager = self.manager(
            FakeSystemctlRunner(enabled=False, active=False),
            packaged_service_compatible=False,
            destination=destination,
        )

        self.assertEqual(
            manager.manual_daemon_command,
            "input-action-controller --config '/absolute/custom config.toml' daemon",
        )

    def test_custom_config_can_restore_the_original_packaged_service_state(self):
        runner = FakeSystemctlRunner(enabled=False, active=False)
        manager = self.manager(runner, packaged_service_compatible=False)

        manager.restore(ServiceSnapshot(enabled=True, active=True))

        self.assertEqual(
            runner.calls,
            [
                RunCall((SYSTEMCTL, "--user", "enable", SERVICE), True, False),
                RunCall((SYSTEMCTL, "--user", "start", SERVICE), True, False),
            ],
        )


if __name__ == "__main__":
    unittest.main()

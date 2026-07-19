from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import shlex
import subprocess

from .config_editor import ConfigLocation


SYSTEMCTL = "/usr/bin/systemctl"
SERVICE_UNIT = "input-action-controller.service"
_INACTIVE_EXIT_CODE = 3


class ServiceConfigurationError(RuntimeError):
    """Raised when the packaged service cannot use the selected config."""


@dataclass(frozen=True)
class ServiceSnapshot:
    enabled: bool
    active: bool


class ServiceChoice(StrEnum):
    RESTART = "restart"
    PRESERVE_STOPPED = "preserve-stopped"
    ENABLE_AND_START = "enable-and-start"
    PRESERVE_INACTIVE = "preserve-inactive"


class ServiceManager:
    """Manage the packaged user service without changing its original state."""

    def __init__(
        self,
        location: ConfigLocation,
        *,
        runner: Callable = subprocess.run,
    ):
        self._location = location
        self._runner = runner

    @property
    def manual_daemon_command(self) -> str:
        return shlex.join(
            (
                "input-action-controller",
                "--config",
                str(self._location.destination),
                "daemon",
            )
        )

    @staticmethod
    def default_choice(snapshot: ServiceSnapshot) -> ServiceChoice:
        if snapshot.active:
            return ServiceChoice.RESTART
        return ServiceChoice.PRESERVE_INACTIVE

    def snapshot(self) -> ServiceSnapshot:
        return ServiceSnapshot(
            enabled=self._query_enabled(),
            active=self._query_state("is-active", _INACTIVE_EXIT_CODE),
        )

    def stop_for_setup(self, snapshot: ServiceSnapshot) -> None:
        if snapshot.active:
            self._run("stop")

    def apply(self, choice: ServiceChoice) -> None:
        if choice in {ServiceChoice.RESTART, ServiceChoice.ENABLE_AND_START}:
            self._require_packaged_service_config()

        if choice == ServiceChoice.RESTART:
            self._run("restart")
        elif choice == ServiceChoice.ENABLE_AND_START:
            self._run("enable")
            self._run("start")
        elif choice not in {
            ServiceChoice.PRESERVE_STOPPED,
            ServiceChoice.PRESERVE_INACTIVE,
        }:
            raise ValueError(f"unsupported service choice: {choice!r}")

    def restore(self, snapshot: ServiceSnapshot) -> None:
        self._run("enable" if snapshot.enabled else "disable")
        self._run("start" if snapshot.active else "stop")

    def _require_packaged_service_config(self) -> None:
        if not self._location.packaged_service_compatible:
            raise ServiceConfigurationError(
                "The packaged user service does not use the selected configuration. "
                f"Run {self.manual_daemon_command} instead."
            )

    def _query_state(self, operation: str, inactive_exit_code: int) -> bool:
        argv = self._argv(operation)
        result = self._runner(argv, check=False, shell=False)
        returncode = getattr(result, "returncode", 0)
        if returncode == 0:
            return True
        if returncode == inactive_exit_code:
            return False
        raise subprocess.CalledProcessError(returncode, argv)

    def _query_enabled(self) -> bool:
        argv = self._argv("is-enabled")
        result = self._runner(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        state = " ".join((getattr(result, "stdout", "") or "").split())
        if state == "enabled":
            return True
        if state == "disabled":
            return False

        returncode = getattr(result, "returncode", 0)
        if state:
            raise ValueError(
                f"systemctl is-enabled returned unsupported state {state!r}"
            )
        raise subprocess.CalledProcessError(returncode, argv)

    def _run(self, operation: str) -> None:
        argv = self._argv(operation)
        result = self._runner(argv, check=True, shell=False)
        returncode = getattr(result, "returncode", 0)
        if returncode:
            raise subprocess.CalledProcessError(returncode, argv)

    @staticmethod
    def _argv(operation: str) -> tuple[str, ...]:
        return (SYSTEMCTL, "--user", operation, SERVICE_UNIT)

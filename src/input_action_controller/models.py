from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


class DeviceStrategy(StrEnum):
    PRIORITY = "priority"
    ALL = "all"


class ActionRequest(StrEnum):
    ON = "on"
    OFF = "off"
    TOGGLE = "toggle"
    AUTO_OFF = "auto-off"


class ActionState(StrEnum):
    OFF = "off"
    ON = "on"
    UNCERTAIN = "uncertain"


class FailedDirection(StrEnum):
    ON = "on"
    OFF = "off"


@dataclass(frozen=True)
class RunnerConfig:
    timeout_seconds: float = 5.0
    shutdown_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ActionConfig:
    name: str
    on_command: tuple[str, ...]
    off_command: tuple[str, ...]
    skip_off_after_failed_on: bool = False
    skip_on_after_failed_off: bool = False
    off_on_shutdown: bool = True


@dataclass(frozen=True)
class DeviceSelectionConfig:
    strategy: DeviceStrategy = DeviceStrategy.PRIORITY


@dataclass(frozen=True, kw_only=True)
class DeviceSelector:
    name: str
    action: str
    vendor_id: str
    product_id: str
    interface_number: str | None = None
    serial: str | None = None
    id_path: str | None = None


@dataclass(frozen=True, kw_only=True)
class HidrawProfile(DeviceSelector):
    on_reports: tuple[bytes, ...]
    off_reports: tuple[bytes, ...]


@dataclass(frozen=True, kw_only=True)
class EvdevProfile(DeviceSelector):
    mode: str
    input_classifier: str | None = None
    on_events: tuple[str, ...] = ()
    off_events: tuple[str, ...] = ()
    toggle_events: tuple[str, ...] = ()
    toggle_off_timeout_seconds: float = 60.0


DeviceProfile: TypeAlias = HidrawProfile | EvdevProfile


@dataclass(frozen=True)
class AppConfig:
    runner: RunnerConfig
    device_selection: DeviceSelectionConfig
    actions: tuple[ActionConfig, ...]
    devices: tuple[DeviceProfile, ...]

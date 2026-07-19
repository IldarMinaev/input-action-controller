import math
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping

from .models import (
    ActionConfig,
    AppConfig,
    DeviceProfile,
    DeviceSelectionConfig,
    DeviceStrategy,
    EvdevProfile,
    HidrawProfile,
    RunnerConfig,
)


class ConfigError(ValueError):
    pass


_ROOT_KEYS = frozenset({"runner", "device_selection", "actions", "devices"})
_RUNNER_KEYS = frozenset({"timeout_seconds", "shutdown_timeout_seconds"})
_DEVICE_SELECTION_KEYS = frozenset({"strategy"})
_ACTION_KEYS = frozenset(
    {
        "on_command",
        "off_command",
        "skip_off_after_failed_on",
        "skip_on_after_failed_off",
        "off_on_shutdown",
    }
)
_COMMON_DEVICE_KEYS = frozenset(
    {
        "name",
        "action",
        "transport",
        "mode",
        "vendor_id",
        "product_id",
        "interface_number",
        "serial",
        "id_path",
    }
)
_HIDRAW_KEYS = _COMMON_DEVICE_KEYS | {"on_reports", "off_reports"}
_EVDEV_KEYS = _COMMON_DEVICE_KEYS | {
    "on_events",
    "off_events",
    "toggle_events",
    "toggle_off_timeout_seconds",
}
_ALL_DEVICE_KEYS = _HIDRAW_KEYS | _EVDEV_KEYS
_HEX_ID = re.compile(r"[0-9a-fA-F]{4}\Z")
_HEX_INTERFACE = re.compile(r"[0-9a-fA-F]{1,2}\Z")
_HEX_REPORT = re.compile(r"[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2})*\Z")


def resolve_config_path() -> Path:
    override = os.environ.get("INPUT_ACTION_CONTROLLER_CONFIG")
    if override:
        return Path(override)

    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    candidate = xdg_config_home / "input-action-controller" / "config.toml"
    if candidate.is_file():
        return candidate

    return Path("/etc/input-action-controller/config.toml")


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path if path is not None else resolve_config_path()
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    return parse_config(raw)


def parse_config(raw: Mapping[str, Any]) -> AppConfig:
    root = _require_mapping(raw, "configuration")
    _reject_unknown_keys(root, _ROOT_KEYS, "configuration")

    runner = _parse_runner(root.get("runner", {}))
    device_selection = _parse_device_selection(root.get("device_selection", {}))
    actions = _parse_actions(_required(root, "actions", "configuration"))
    action_names = {action.name for action in actions}
    devices = _parse_devices(_required(root, "devices", "configuration"), action_names)
    return AppConfig(
        runner=runner,
        device_selection=device_selection,
        actions=actions,
        devices=devices,
    )


def _parse_runner(value: Any) -> RunnerConfig:
    table = _require_mapping(value, "runner")
    _reject_unknown_keys(table, _RUNNER_KEYS, "runner")
    return RunnerConfig(
        timeout_seconds=_positive_number(
            table.get("timeout_seconds", 5.0), "runner.timeout_seconds"
        ),
        shutdown_timeout_seconds=_positive_number(
            table.get("shutdown_timeout_seconds", 10.0),
            "runner.shutdown_timeout_seconds",
        ),
    )


def _parse_device_selection(value: Any) -> DeviceSelectionConfig:
    table = _require_mapping(value, "device_selection")
    _reject_unknown_keys(table, _DEVICE_SELECTION_KEYS, "device_selection")
    raw_strategy = table.get("strategy", DeviceStrategy.PRIORITY.value)
    if not isinstance(raw_strategy, str):
        raise ConfigError("device_selection.strategy must be 'priority' or 'all'")
    try:
        strategy = DeviceStrategy(raw_strategy)
    except ValueError as error:
        raise ConfigError(
            "device_selection.strategy must be 'priority' or 'all'"
        ) from error
    return DeviceSelectionConfig(strategy=strategy)


def _parse_actions(value: Any) -> tuple[ActionConfig, ...]:
    table = _require_mapping(value, "actions")
    if not table:
        raise ConfigError("actions must define at least one action")

    actions = []
    for name, raw_action in table.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("action names must be nonempty strings")
        action = _require_mapping(raw_action, f"actions.{name}")
        _reject_unknown_keys(action, _ACTION_KEYS, f"actions.{name}")
        actions.append(
            ActionConfig(
                name=name,
                on_command=_argv(
                    _required(action, "on_command", f"actions.{name}"),
                    f"actions.{name}.on_command",
                ),
                off_command=_argv(
                    _required(action, "off_command", f"actions.{name}"),
                    f"actions.{name}.off_command",
                ),
                skip_off_after_failed_on=_boolean(
                    action.get("skip_off_after_failed_on", False),
                    f"actions.{name}.skip_off_after_failed_on",
                ),
                skip_on_after_failed_off=_boolean(
                    action.get("skip_on_after_failed_off", False),
                    f"actions.{name}.skip_on_after_failed_off",
                ),
                off_on_shutdown=_boolean(
                    action.get("off_on_shutdown", True),
                    f"actions.{name}.off_on_shutdown",
                ),
            )
        )
    return tuple(actions)


def _parse_devices(value: Any, action_names: set[str]) -> tuple[DeviceProfile, ...]:
    if not isinstance(value, list):
        raise ConfigError("devices must be an array of tables")
    if not value:
        raise ConfigError("devices must define at least one device")

    profiles: list[DeviceProfile] = []
    names: set[str] = set()
    for index, raw_device in enumerate(value):
        context = f"devices[{index}]"
        device = _require_mapping(raw_device, context)
        _reject_unknown_keys(device, _ALL_DEVICE_KEYS, context)
        transport = _nonempty_string(
            _required(device, "transport", context), f"{context}.transport"
        )

        if transport == "hidraw":
            _reject_unknown_keys(device, _HIDRAW_KEYS, context)
            profile = _parse_hidraw(device, context)
        elif transport == "evdev":
            _reject_unknown_keys(device, _EVDEV_KEYS, context)
            profile = _parse_evdev(device, context)
        else:
            raise ConfigError(f"{context}.transport must be 'hidraw' or 'evdev'")

        if profile.name in names:
            raise ConfigError(f"duplicate device name: {profile.name!r}")
        if profile.action not in action_names:
            raise ConfigError(
                f"{context}.action references unknown action {profile.action!r}"
            )
        names.add(profile.name)
        profiles.append(profile)
    return tuple(profiles)


def _parse_hidraw(device: Mapping[str, Any], context: str) -> HidrawProfile:
    mode = _nonempty_string(_required(device, "mode", context), f"{context}.mode")
    if mode != "on-off":
        raise ConfigError(f"{context}.mode must be 'on-off' for hidraw")

    selectors = _parse_selectors(device, context)
    on_reports = _reports(
        _required(device, "on_reports", context), f"{context}.on_reports"
    )
    off_reports = _reports(
        _required(device, "off_reports", context), f"{context}.off_reports"
    )
    if set(on_reports) & set(off_reports):
        raise ConfigError(f"{context} on_reports and off_reports must not overlap")
    return HidrawProfile(**selectors, on_reports=on_reports, off_reports=off_reports)


def _parse_evdev(device: Mapping[str, Any], context: str) -> EvdevProfile:
    mode = _nonempty_string(_required(device, "mode", context), f"{context}.mode")
    selectors = _parse_selectors(device, context)

    if mode == "on-off":
        _reject_present(
            device, {"toggle_events", "toggle_off_timeout_seconds"}, context
        )
        on_events = _events(
            _required(device, "on_events", context), f"{context}.on_events"
        )
        off_events = _events(
            _required(device, "off_events", context), f"{context}.off_events"
        )
        if _event_codes(on_events) & _event_codes(off_events):
            raise ConfigError(f"{context} on_events and off_events must not overlap")
        return EvdevProfile(
            **selectors,
            mode=mode,
            on_events=on_events,
            off_events=off_events,
        )

    if mode == "toggle":
        _reject_present(device, {"on_events", "off_events"}, context)
        toggle_events = _events(
            _required(device, "toggle_events", context),
            f"{context}.toggle_events",
        )
        timeout = _nonnegative_number(
            device.get("toggle_off_timeout_seconds", 60.0),
            f"{context}.toggle_off_timeout_seconds",
        )
        return EvdevProfile(
            **selectors,
            mode=mode,
            toggle_events=toggle_events,
            toggle_off_timeout_seconds=timeout,
        )

    raise ConfigError(f"{context}.mode must be 'on-off' or 'toggle' for evdev")


def _parse_selectors(device: Mapping[str, Any], context: str) -> dict[str, str | None]:
    return {
        "name": _nonempty_string(_required(device, "name", context), f"{context}.name"),
        "action": _nonempty_string(
            _required(device, "action", context), f"{context}.action"
        ),
        "vendor_id": _usb_id(
            _required(device, "vendor_id", context), f"{context}.vendor_id"
        ),
        "product_id": _usb_id(
            _required(device, "product_id", context), f"{context}.product_id"
        ),
        "interface_number": _optional_interface(
            device.get("interface_number"), f"{context}.interface_number"
        ),
        "serial": _optional_string(device.get("serial"), f"{context}.serial"),
        "id_path": _optional_string(device.get("id_path"), f"{context}.id_path"),
    }


def _load_ecodes():
    from evdev import ecodes

    return ecodes


def _events(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a nonempty array of event names")

    ecodes = _load_ecodes()
    events = []
    codes = set()
    for event_name in value:
        if not isinstance(event_name, str) or not event_name.startswith(
            ("KEY_", "BTN_")
        ):
            raise ConfigError(f"{context} must contain symbolic KEY_* or BTN_* names")
        code = ecodes.ecodes.get(event_name)
        if not isinstance(code, int) or code not in ecodes.keys:
            raise ConfigError(f"{context} contains non-EV_KEY name {event_name!r}")
        if code in codes:
            raise ConfigError(f"{context} must not contain duplicate event codes")
        codes.add(code)
        events.append(event_name)
    return tuple(events)


def _event_codes(event_names: tuple[str, ...]) -> set[int]:
    ecodes = _load_ecodes()
    return {ecodes.ecodes[event_name] for event_name in event_names}


def _reports(value: Any, context: str) -> tuple[bytes, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a nonempty array of HID reports")
    reports = tuple(_report(item, context) for item in value)
    if len(set(reports)) != len(reports):
        raise ConfigError(f"{context} must not contain duplicate HID reports")
    return reports


def _report(value: Any, context: str) -> bytes:
    if not isinstance(value, str) or _HEX_REPORT.fullmatch(value) is None:
        raise ConfigError(f"{context} contains an invalid HID report")
    return bytes.fromhex(value)


def _usb_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or _HEX_ID.fullmatch(value) is None:
        raise ConfigError(f"{context} must be exactly four hexadecimal digits")
    return value.lower()


def _optional_interface(value: Any, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _HEX_INTERFACE.fullmatch(value) is None:
        raise ConfigError(f"{context} must be one or two hexadecimal digits")
    return value.lower().zfill(2)


def _optional_string(value: Any, context: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, context)


def _argv(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a nonempty argv array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ConfigError(f"{context} must contain only nonempty strings")
    if any("\x00" in item for item in value):
        raise ConfigError(f"{context} must not contain NUL bytes")
    return tuple(value)


def _positive_number(value: Any, context: str) -> float:
    result = _finite_number(value, context)
    if result <= 0:
        raise ConfigError(f"{context} must be positive")
    return result


def _nonnegative_number(value: Any, context: str) -> float:
    result = _finite_number(value, context)
    if result < 0:
        raise ConfigError(f"{context} must be nonnegative")
    return result


def _finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{context} must be a finite number")
    return result


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be a boolean")
    return value


def _nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{context} must be a nonempty string")
    return value


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{context} must be a table")
    return value


def _required(table: Mapping[str, Any], key: str, context: str) -> Any:
    if key not in table:
        raise ConfigError(f"{context}.{key} is required")
    return table[key]


def _reject_unknown_keys(
    table: Mapping[str, Any], allowed: set[str] | frozenset[str], context: str
) -> None:
    unknown = set(table) - allowed
    if unknown:
        names = ", ".join(sorted(repr(key) for key in unknown))
        raise ConfigError(f"{context} has unknown keys: {names}")


def _reject_present(
    table: Mapping[str, Any], disallowed: set[str], context: str
) -> None:
    present = set(table) & disallowed
    if present:
        names = ", ".join(sorted(present))
        raise ConfigError(f"{context} fields do not apply to this mode: {names}")

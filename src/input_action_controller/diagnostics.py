from dataclasses import dataclass
import shutil
import subprocess
import sys
from threading import Event
from typing import Callable, TextIO

from evdev import ecodes

from .devices.discovery import DeviceCandidate, DeviceDiscovery, resolve_profiles
from .devices.evdev import EvdevEventSource
from .devices.hidraw import HidrawEventSource
from .devices.manager import SourceFactory, _default_source_factory
from .locking import LockContendedError, RuntimeLock, RuntimeLockError
from .models import AppConfig, DeviceStrategy


SUCCESS = 0
RUNTIME_FAILURE = 1
USAGE_FAILURE = 2
LOCK_CONTENTION = 3
DEFAULT_SOURCE_FACTORY = _default_source_factory
SERVICE_UNIT = "input-action-controller.service"
SERVICE_QUERY_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class ServiceActivity:
    value: str
    known: bool


@dataclass(frozen=True)
class RawEvdevEvent:
    type: int
    code: int
    value: int


class _RawHidrawMatcher:
    @staticmethod
    def match(report: bytes) -> bytes:
        return report


class _RawEvdevMatcher:
    @staticmethod
    def match(event_type: int, code: int, value: int) -> RawEvdevEvent:
        return RawEvdevEvent(event_type, code, value)


def query_user_service_activity(
    *,
    run_command: Callable = subprocess.run,
) -> ServiceActivity:
    argv = ("systemctl", "--user", "is-active", SERVICE_UNIT)
    try:
        completed = run_command(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SERVICE_QUERY_TIMEOUT_SECONDS,
            shell=False,
        )
    except FileNotFoundError:
        return ServiceActivity("unknown (systemctl unavailable)", False)
    except subprocess.TimeoutExpired:
        return ServiceActivity("unknown (query timed out)", False)
    except OSError as error:
        detail = error.strerror or str(error)
        return ServiceActivity(f"unknown (query failed: {detail})", False)

    state = " ".join((completed.stdout or "").split())
    if state:
        return ServiceActivity(state, True)

    detail = " ".join((completed.stderr or "").split())
    if detail:
        return ServiceActivity(f"unknown ({detail})", False)
    return ServiceActivity(
        f"unknown (systemctl exited {completed.returncode} without a state)",
        False,
    )


def run_status(
    config: AppConfig,
    *,
    discovery_factory: Callable[[], DeviceDiscovery] | None = None,
    lock_factory: Callable[[], RuntimeLock] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    systemctl_runner: Callable = subprocess.run,
    output: TextIO | None = None,
) -> int:
    discovery_factory = discovery_factory or DeviceDiscovery
    lock_factory = lock_factory or RuntimeLock
    output = output or sys.stdout
    print("configuration: valid", file=output)
    result = SUCCESS

    service_activity = query_user_service_activity(run_command=systemctl_runner)
    print(f"service activity: {service_activity.value}", file=output)
    if not service_activity.known:
        result = RUNTIME_FAILURE

    try:
        lock = lock_factory()
        try:
            lock.acquire()
        except LockContendedError as error:
            owner = str(error.owner_pid) if error.owner_pid is not None else "unknown"
            print(f"runtime lock: held (PID {owner})", file=output)
        else:
            lock.release()
            print("runtime lock: free", file=output)
    except RuntimeLockError as error:
        print(f"runtime lock: unknown ({error})", file=output)
        result = RUNTIME_FAILURE

    for action in config.actions:
        for direction, command in (
            ("on", action.on_command),
            ("off", action.off_command),
        ):
            executable = command[0]
            availability = (
                "available" if which_fn(executable) is not None else "unavailable"
            )
            print(
                f"executable {action.name} {direction}: {availability} ({executable})",
                file=output,
            )

    try:
        candidates = discovery_factory().enumerate()
        resolutions = resolve_profiles(config.devices, candidates)
    except Exception as error:
        print(f"devices: unavailable ({error})", file=output)
        return RUNTIME_FAILURE

    available = tuple(item for item in resolutions if item.is_available)
    if config.device_selection.strategy is DeviceStrategy.PRIORITY:
        selected = available[:1]
    else:
        selected = available
    selected_names = {item.profile.name for item in selected}

    for resolution in resolutions:
        node = f" ({resolution.node})" if resolution.node is not None else ""
        marker = " [selected]" if resolution.profile.name in selected_names else ""
        print(
            f"profile {resolution.profile.name}: {resolution.status}{node}{marker}",
            file=output,
        )

    names = ", ".join(item.profile.name for item in selected) or "none"
    print(f"selection {config.device_selection.strategy.value}: {names}", file=output)
    return result


def run_devices(
    *,
    discovery_factory: Callable[[], DeviceDiscovery] | None = None,
    output: TextIO | None = None,
) -> int:
    discovery_factory = discovery_factory or DeviceDiscovery
    output = output or sys.stdout
    try:
        candidates = discovery_factory().enumerate()
    except Exception as error:
        print(f"devices: unavailable ({error})", file=output)
        return RUNTIME_FAILURE

    if not candidates:
        print("devices: none", file=output)
        return SUCCESS

    for candidate in sorted(candidates, key=lambda item: (item.node, item.subsystem)):
        transport = "evdev" if candidate.subsystem == "input" else "hidraw"
        selectors = [f"transport={transport}"]
        for property_name, output_name in (
            ("ID_VENDOR_ID", "vendor_id"),
            ("ID_MODEL_ID", "product_id"),
            ("ID_USB_INTERFACE_NUM", "interface_number"),
            ("ID_SERIAL_SHORT", "serial"),
            ("ID_PATH", "id_path"),
        ):
            value = candidate.properties.get(property_name)
            if value is not None:
                selectors.append(f"{output_name}={value}")
        print(f"device {candidate.node}: {' '.join(selectors)}", file=output)
        if candidate.keyboard_class:
            print(
                f"warning {candidate.node}: keyboard-class access can observe ordinary key events",
                file=output,
            )
    return SUCCESS


def run_monitor(
    config: AppConfig,
    device_name: str,
    *,
    discovery_factory: Callable[[], DeviceDiscovery] | None = None,
    lock_factory: Callable[[], RuntimeLock] | None = None,
    source_factory: SourceFactory = DEFAULT_SOURCE_FACTORY,
    stdin: TextIO | None = None,
    output: TextIO | None = None,
) -> int:
    discovery_factory = discovery_factory or DeviceDiscovery
    lock_factory = lock_factory or RuntimeLock
    stdin = stdin or sys.stdin
    output = output or sys.stdout

    matches = tuple(
        profile for profile in config.devices if profile.name == device_name
    )
    if len(matches) != 1:
        print(f"unknown device profile: {device_name}", file=output)
        return USAGE_FAILURE
    profile = matches[0]

    try:
        lock = lock_factory()
        lock.acquire()
    except LockContendedError as error:
        owner = str(error.owner_pid) if error.owner_pid is not None else "unknown"
        print(f"daemon is active (PID {owner})", file=output)
        return LOCK_CONTENTION
    except RuntimeLockError as error:
        print(f"cannot acquire monitor lock: {error}", file=output)
        return RUNTIME_FAILURE

    try:
        candidates = discovery_factory().enumerate()
        resolution = resolve_profiles((profile,), candidates)[0]
        if not resolution.is_available:
            node = f" at {resolution.node}" if resolution.node is not None else ""
            print(
                f"device {device_name}: {resolution.status}{node}",
                file=output,
            )
            return RUNTIME_FAILURE

        candidate = _candidate_for_node(candidates, resolution.node)
        if candidate is not None and candidate.keyboard_class:
            if not stdin.isatty():
                print(
                    "keyboard-class monitoring requires an interactive TTY and explicit 'yes' confirmation",
                    file=output,
                )
                return USAGE_FAILURE
            print(
                "Keyboard-class access can observe ordinary key events. Type yes to continue: ",
                end="",
                file=output,
                flush=True,
            )
            if stdin.readline().strip().casefold() != "yes":
                print("monitor canceled", file=output)
                return USAGE_FAILURE

        source = source_factory(profile, resolution.node)
        _enable_raw_monitoring(source)
        stop = Event()
        try:
            source.run(stop, lambda event: print(_format_raw_event(event), file=output))
        except KeyboardInterrupt:
            stop.set()
        return SUCCESS
    except Exception as error:
        print(f"monitor failed for {device_name}: {error}", file=output)
        return RUNTIME_FAILURE
    finally:
        lock.release()


def _candidate_for_node(
    candidates: tuple[DeviceCandidate, ...],
    node: str | None,
) -> DeviceCandidate | None:
    return next((candidate for candidate in candidates if candidate.node == node), None)


def _enable_raw_monitoring(source) -> None:
    if isinstance(source, HidrawEventSource):
        source._matcher = _RawHidrawMatcher()
    elif isinstance(source, EvdevEventSource):
        source._matcher = _RawEvdevMatcher()


def _format_raw_event(event) -> str:
    if isinstance(event, bytes):
        return event.hex(" ").upper()
    if isinstance(event, RawEvdevEvent):
        return _format_evdev_event(event)
    if all(hasattr(event, name) for name in ("type", "code", "value")):
        return _format_evdev_event(RawEvdevEvent(event.type, event.code, event.value))
    if isinstance(event, tuple) and len(event) == 3:
        return _format_evdev_event(RawEvdevEvent(*event))
    raise TypeError(f"unsupported monitor event: {type(event).__name__}")


def _format_evdev_event(event: RawEvdevEvent) -> str:
    event_type = ecodes.EV.get(event.type, f"EV_{event.type}")
    code_names = ecodes.bytype.get(event.type, {})
    code = code_names.get(event.code, str(event.code))
    if isinstance(code, list):
        code = code[0]
    if event.type == ecodes.EV_KEY:
        value = {0: "release", 1: "press", 2: "repeat"}.get(
            event.value,
            str(event.value),
        )
    else:
        value = str(event.value)
    return f"{event_type} {code} {value}"

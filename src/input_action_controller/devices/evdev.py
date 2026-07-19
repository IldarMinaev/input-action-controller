import select
from threading import Event
from typing import Callable, Protocol

from evdev import InputDevice

from ..models import EvdevProfile
from .base import DeviceReadError, MatchedInput
from .hidraw import POLL_INTERVAL_SECONDS
from .matchers import EvdevMatcher


class EvdevDevice(Protocol):
    fd: int

    def read(self): ...

    def close(self) -> None: ...


class EvdevEventSource:
    def __init__(
        self,
        profile: EvdevProfile,
        node: str,
        *,
        input_device_factory: Callable[[str], EvdevDevice] = InputDevice,
        select_fn: Callable[
            ..., tuple[list[int], list[int], list[int]]
        ] = select.select,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._profile = profile
        self._node = node
        self._input_device_factory = input_device_factory
        self._select = select_fn
        self._poll_interval_seconds = min(poll_interval_seconds, POLL_INTERVAL_SECONDS)
        self._matcher = EvdevMatcher(profile)

    def run(self, stop: Event, emit: Callable[[MatchedInput], None]) -> None:
        device: EvdevDevice | None = None
        try:
            try:
                device = self._input_device_factory(self._node)
            except OSError as error:
                raise self._read_error(error) from error

            while not stop.is_set():
                try:
                    readable, _, _ = self._select(
                        [device.fd], [], [], self._poll_interval_seconds
                    )
                except InterruptedError:
                    continue
                except (OSError, ValueError) as error:
                    raise self._read_error(error) from error
                if not readable:
                    continue

                try:
                    for event in device.read():
                        matched = self._matcher.match(
                            event.type, event.code, event.value
                        )
                        if matched is not None:
                            emit(matched)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as error:
                    raise self._read_error(error) from error
        finally:
            if device is not None:
                try:
                    device.close()
                except OSError:
                    pass

    def _read_error(self, error: OSError | ValueError) -> DeviceReadError:
        reason = str(error) or error.__class__.__name__
        return DeviceReadError(self._profile.name, self._node, reason)

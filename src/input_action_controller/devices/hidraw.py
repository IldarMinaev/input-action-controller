import os
import select
from threading import Event
from typing import Callable

from ..models import HidrawProfile
from .base import DeviceReadError, MatchedInput
from .matchers import HidrawMatcher


POLL_INTERVAL_SECONDS = 0.25
HID_MAX_BUFFER_SIZE = 16 * 1024


class HidrawEventSource:
    def __init__(
        self,
        profile: HidrawProfile,
        node: str,
        *,
        open_fn: Callable[[str, int], int] = os.open,
        read_fn: Callable[[int, int], bytes] = os.read,
        close_fn: Callable[[int], None] = os.close,
        select_fn: Callable[
            ..., tuple[list[int], list[int], list[int]]
        ] = select.select,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._profile = profile
        self._node = node
        self._open = open_fn
        self._read = read_fn
        self._close = close_fn
        self._select = select_fn
        self._poll_interval_seconds = min(poll_interval_seconds, POLL_INTERVAL_SECONDS)
        self._matcher = HidrawMatcher(profile)

    def run(self, stop: Event, emit: Callable[[MatchedInput], None]) -> None:
        descriptor: int | None = None
        try:
            try:
                descriptor = self._open(self._node, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as error:
                raise self._read_error(error) from error

            while not stop.is_set():
                try:
                    readable, _, _ = self._select(
                        [descriptor], [], [], self._poll_interval_seconds
                    )
                except InterruptedError:
                    continue
                except (OSError, ValueError) as error:
                    raise self._read_error(error) from error
                if not readable:
                    continue

                try:
                    report = self._read(descriptor, HID_MAX_BUFFER_SIZE)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as error:
                    raise self._read_error(error) from error
                if not report:
                    raise DeviceReadError(
                        self._profile.name,
                        self._node,
                        "end of file",
                    )

                matched = self._matcher.match(report)
                if matched is not None:
                    emit(matched)
        finally:
            if descriptor is not None:
                try:
                    self._close(descriptor)
                except OSError:
                    pass

    def _read_error(self, error: OSError | ValueError) -> DeviceReadError:
        reason = str(error) or error.__class__.__name__
        return DeviceReadError(self._profile.name, self._node, reason)

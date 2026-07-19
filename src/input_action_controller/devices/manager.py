import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
from threading import Event, Thread
from typing import Callable, Mapping, Protocol

import pyudev

from ..models import DeviceProfile, DeviceStrategy, EvdevProfile
from .base import DeviceEventSource, MatchedInput
from .discovery import DeviceCandidate, ProfileResolution, resolve_profiles
from .evdev import EvdevEventSource
from .hidraw import HidrawEventSource


LOGGER = logging.getLogger(__name__)
DEFAULT_READER_STOP_TIMEOUT_SECONDS = 0.5
_HOTPLUG_ACTIONS = frozenset({"add", "remove", "change"})


class Discovery(Protocol):
    def enumerate(self) -> tuple[DeviceCandidate, ...]: ...


class ActionSink(Protocol):
    def submit(
        self,
        request,
        *,
        source: str,
        toggle_timeout_seconds: float | None = None,
    ) -> int: ...


class HotplugObserver(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class ThreadsafeLoop(Protocol):
    def call_soon_threadsafe(self, callback: Callable, *args): ...


class AsyncStopEvent(Protocol):
    def is_set(self) -> bool: ...

    async def wait(self) -> bool: ...


SourceFactory = Callable[[DeviceProfile, str], DeviceEventSource]
ObserverFactory = Callable[[Callable[[str], None]], HotplugObserver]


@dataclass(frozen=True)
class ReaderHandle:
    profile: DeviceProfile
    node: str
    stop: Event
    thread: Thread
    finished: asyncio.Future[None]


def _default_source_factory(
    profile: DeviceProfile,
    node: str,
) -> DeviceEventSource:
    if isinstance(profile, EvdevProfile):
        return EvdevEventSource(profile, node)
    return HidrawEventSource(profile, node)


def _default_observer_factory(
    callback: Callable[[str], None],
) -> HotplugObserver:
    monitor = pyudev.Monitor.from_netlink(pyudev.Context())

    def dispatch(device) -> None:
        callback(device.action)

    return pyudev.MonitorObserver(monitor, callback=dispatch)


class DeviceManager:
    def __init__(
        self,
        profiles: tuple[DeviceProfile, ...],
        actions: Mapping[str, ActionSink],
        discovery: Discovery,
        *,
        strategy: DeviceStrategy = DeviceStrategy.PRIORITY,
        source_factory: SourceFactory = _default_source_factory,
        observer_factory: ObserverFactory = _default_observer_factory,
        reader_stop_timeout_seconds: float = DEFAULT_READER_STOP_TIMEOUT_SECONDS,
        loop: ThreadsafeLoop | None = None,
    ):
        if reader_stop_timeout_seconds <= 0:
            raise ValueError("reader_stop_timeout_seconds must be positive")
        self._profiles = tuple(profiles)
        self._actions = actions
        self._discovery = discovery
        self._strategy = strategy
        self._source_factory = source_factory
        self._observer_factory = observer_factory
        self._reader_stop_timeout_seconds = reader_stop_timeout_seconds
        self._callback_loop = loop
        self._reconcile_event: asyncio.Event | None = None
        self._running = False
        self._quarantined: set[tuple[DeviceProfile, str]] = set()
        self.active_readers: dict[str, ReaderHandle] = {}

    async def run(
        self,
        stop_event: AsyncStopEvent,
        *,
        shutdown_deadline: Callable[[], float | None] | None = None,
    ) -> None:
        if self._running:
            raise RuntimeError("device manager is already running")

        running_loop = asyncio.get_running_loop()
        if self._callback_loop is None:
            self._callback_loop = running_loop
        self._reconcile_event = asyncio.Event()
        self._quarantined.clear()
        self._running = True
        observer = self._observer_factory(self._hotplug_event)
        observer.start()
        stop_waiter = asyncio.create_task(
            stop_event.wait(),
            name="device-manager:stop",
        )

        try:
            self._request_reconciliation()
            while True:
                reconcile_waiter = asyncio.create_task(
                    self._reconcile_event.wait(),
                    name="device-manager:reconcile",
                )
                done, _ = await asyncio.wait(
                    (stop_waiter, reconcile_waiter),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_waiter in done:
                    reconcile_waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await reconcile_waiter
                    break

                await reconcile_waiter
                self._reconcile_event.clear()
                await self._reconcile()
        finally:
            self._running = False
            try:
                observer.stop()
            finally:
                try:
                    if not stop_waiter.done():
                        stop_waiter.cancel()
                        with suppress(asyncio.CancelledError):
                            await stop_waiter
                finally:
                    deadline = (
                        shutdown_deadline() if shutdown_deadline is not None else None
                    )
                    await self._stop_all_readers(deadline)

    def _hotplug_event(self, action: str) -> None:
        if action not in _HOTPLUG_ACTIONS or not self._running:
            return
        if self._callback_loop is not None:
            self._callback_loop.call_soon_threadsafe(self._handle_hotplug_event)

    def _handle_hotplug_event(self) -> None:
        self._quarantined.clear()
        self._request_reconciliation()

    def _request_reconciliation(self) -> None:
        if self._running and self._reconcile_event is not None:
            self._reconcile_event.set()

    async def _reconcile(self) -> None:
        try:
            resolutions = resolve_profiles(
                self._profiles,
                self._discovery.enumerate(),
            )
        except Exception:
            LOGGER.exception("cannot enumerate input devices")
            return

        desired = self._desired_resolutions(resolutions)
        desired_by_name = {item.profile.name: item for item in desired}
        obsolete = []
        for node, handle in tuple(self.active_readers.items()):
            resolution = desired_by_name.get(handle.profile.name)
            if resolution is None or resolution.node != node:
                self.active_readers.pop(node, None)
                obsolete.append(handle)

        if obsolete:
            for handle in obsolete:
                handle.stop.set()
            await asyncio.gather(
                *(self._wait_for_reader(handle) for handle in obsolete)
            )

        active_names = {handle.profile.name for handle in self.active_readers.values()}
        for resolution in desired:
            node = resolution.node
            if (
                node is None
                or node in self.active_readers
                or resolution.profile.name in active_names
            ):
                continue
            handle = self._start_reader(resolution.profile, node)
            if handle is not None:
                self.active_readers[node] = handle
                active_names.add(resolution.profile.name)
                handle.thread.start()

    def _desired_resolutions(
        self,
        resolutions: tuple[ProfileResolution, ...],
    ) -> tuple[ProfileResolution, ...]:
        available = tuple(
            item
            for item in resolutions
            if item.is_available and (item.profile, item.node) not in self._quarantined
        )
        if self._strategy is DeviceStrategy.PRIORITY:
            return available[:1]
        return available

    def _start_reader(
        self,
        profile: DeviceProfile,
        node: str,
    ) -> ReaderHandle | None:
        try:
            source = self._source_factory(profile, node)
        except Exception:
            LOGGER.exception(
                "cannot create reader for %s at %s",
                profile.name,
                node,
            )
            return None

        stop = Event()
        finished = asyncio.get_running_loop().create_future()
        handle_ref: list[ReaderHandle] = []

        def emit(matched: MatchedInput) -> None:
            if not stop.is_set() and self._callback_loop is not None:
                self._callback_loop.call_soon_threadsafe(
                    self._submit_input,
                    handle_ref[0],
                    matched,
                )

        def read() -> None:
            error = None
            try:
                source.run(stop, emit)
            except Exception as exception:
                error = exception
            finally:
                if self._callback_loop is not None:
                    self._callback_loop.call_soon_threadsafe(
                        self._reader_finished,
                        handle_ref[0],
                        error,
                    )

        thread = Thread(
            target=read,
            name=f"device-reader:{profile.name}",
            daemon=True,
        )
        handle = ReaderHandle(profile, node, stop, thread, finished)
        handle_ref.append(handle)
        return handle

    def _reader_finished(
        self,
        handle: ReaderHandle,
        error: Exception | None,
    ) -> None:
        if error is not None:
            LOGGER.warning(
                "reader failed for %s at %s: %s",
                handle.profile.name,
                handle.node,
                error,
            )
        elif not handle.stop.is_set():
            LOGGER.warning(
                "reader stopped unexpectedly for %s at %s",
                handle.profile.name,
                handle.node,
            )

        unexpected_stop = not handle.stop.is_set()
        if not handle.finished.done():
            handle.finished.set_result(None)
        if self.active_readers.get(handle.node) is handle:
            self.active_readers.pop(handle.node, None)
        if self._running and unexpected_stop:
            self._quarantined.add((handle.profile, handle.node))
            self._request_reconciliation()

    def _submit_input(
        self,
        handle: ReaderHandle,
        matched: MatchedInput,
    ) -> None:
        if handle.stop.is_set() or self.active_readers.get(handle.node) is not handle:
            return
        profile = handle.profile
        try:
            action = self._actions[profile.action]
            action.submit(
                matched.request,
                source=matched.source,
                toggle_timeout_seconds=matched.toggle_timeout_seconds,
            )
        except Exception:
            LOGGER.exception(
                "cannot submit input from %s to action %s",
                profile.name,
                profile.action,
            )

    async def _stop_all_readers(self, deadline: float | None = None) -> None:
        handles = tuple(self.active_readers.values())
        self.active_readers.clear()
        for handle in handles:
            handle.stop.set()
        if handles:
            await asyncio.gather(
                *(self._wait_for_reader(handle, deadline) for handle in handles)
            )

    async def _wait_for_reader(
        self,
        handle: ReaderHandle,
        deadline: float | None = None,
    ) -> None:
        timeout = self._reader_stop_timeout_seconds
        if deadline is not None:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            timeout = min(timeout, remaining)
        try:
            if timeout <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(
                asyncio.shield(handle.finished),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            LOGGER.warning(
                "reader did not stop within %.3f seconds for %s at %s",
                timeout,
                handle.profile.name,
                handle.node,
            )
        except Exception:
            pass

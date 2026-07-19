import asyncio
from dataclasses import replace
from threading import Event
import unittest

from input_action_controller.devices.base import DeviceReadError, MatchedInput
from input_action_controller.devices.discovery import DeviceCandidate
from input_action_controller.devices.manager import DeviceManager
from input_action_controller.models import (
    ActionRequest,
    DeviceStrategy,
    HidrawProfile,
)


def profile(name: str, vendor_id: str, *, action: str = "voice") -> HidrawProfile:
    return HidrawProfile(
        name=name,
        action=action,
        vendor_id=vendor_id,
        product_id="0001",
        on_reports=(b"\x01",),
        off_reports=(b"\x00",),
    )


def available(profile: HidrawProfile, node: str) -> DeviceCandidate:
    return DeviceCandidate(
        node=node,
        subsystem="hidraw",
        properties={
            "DEVNAME": node,
            "ID_VENDOR_ID": profile.vendor_id,
            "ID_MODEL_ID": profile.product_id,
        },
        event_codes=frozenset(),
        keyboard_class=False,
    )


class FakeDiscovery:
    def __init__(self, candidates=()):
        self.candidates = tuple(candidates)
        self.calls = 0

    def enumerate(self):
        self.calls += 1
        return self.candidates

    def set(self, *candidates):
        self.candidates = tuple(candidates)


class FakeObserver:
    def __init__(self, callback):
        self.callback = callback
        self.started = False
        self.stopped = False
        self.stop_error = None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error

    def emit(self, action: str):
        self.callback(action)


class FakeObserverFactory:
    def __init__(self):
        self.observer: FakeObserver | None = None

    def __call__(self, callback):
        self.observer = FakeObserver(callback)
        return self.observer


class FakeActionController:
    def __init__(self):
        self.submissions = []
        self.state = object()
        self.timer = object()

    def submit(self, request, *, source, toggle_timeout_seconds=None):
        self.submissions.append((request, source, toggle_timeout_seconds))
        return len(self.submissions) - 1


class FakeSource:
    def __init__(
        self,
        profile_name: str,
        node: str,
        *,
        initial_inputs=(),
        late_inputs=(),
        fail: Event | None = None,
        ignore_stop: bool = False,
    ):
        self.profile_name = profile_name
        self.node = node
        self.initial_inputs = tuple(initial_inputs)
        self.late_inputs = tuple(late_inputs)
        self.fail = fail
        self.ignore_stop = ignore_stop
        self.started = Event()
        self.stopped = Event()
        self.release = Event()

    def run(self, stop: Event, emit):
        self.started.set()
        for matched in self.initial_inputs:
            emit(matched)
        if self.fail is not None:
            self.fail.wait(timeout=2.0)
            raise DeviceReadError(self.profile_name, self.node, "reader failed")
        if self.ignore_stop:
            self.release.wait(timeout=2.0)
            for matched in self.late_inputs:
                emit(matched)
        else:
            stop.wait(timeout=2.0)
        self.stopped.set()


class FakeSourceFactory:
    def __init__(self):
        self.sources: dict[str, list[FakeSource]] = {}
        self.initial_inputs = {}
        self.late_inputs = {}
        self.fail_events = {}
        self.fail_once = set()
        self.ignore_stop = set()

    def __call__(self, profile, node):
        fail = self.fail_events.get(profile.name)
        if profile.name in self.fail_once:
            self.fail_once.remove(profile.name)
            self.fail_events.pop(profile.name, None)
        source = FakeSource(
            profile.name,
            node,
            initial_inputs=self.initial_inputs.get(profile.name, ()),
            late_inputs=self.late_inputs.get(profile.name, ()),
            fail=fail,
            ignore_stop=profile.name in self.ignore_stop,
        )
        self.sources.setdefault(profile.name, []).append(source)
        return source


class RecordingLoop:
    def __init__(self, loop):
        self.loop = loop
        self.threadsafe_calls = []

    def call_soon_threadsafe(self, callback, *args):
        self.threadsafe_calls.append((callback, args))
        return self.loop.call_soon_threadsafe(callback, *args)


class DelayedInputLoop(RecordingLoop):
    def __init__(self, loop):
        super().__init__(loop)
        self.input_callbacks = []

    def call_soon_threadsafe(self, callback, *args):
        self.threadsafe_calls.append((callback, args))
        if getattr(callback, "__name__", "") == "_submit_input":
            self.input_callbacks.append((callback, args))
            return None
        return self.loop.call_soon_threadsafe(callback, *args)

    def deliver_inputs(self):
        callbacks = tuple(self.input_callbacks)
        self.input_callbacks.clear()
        for callback, args in callbacks:
            callback(*args)


class DeviceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.primary = profile("Primary", "1111")
        self.backup = profile("Backup", "2222")
        self.discovery = FakeDiscovery()
        self.actions = {"voice": FakeActionController()}
        self.sources = FakeSourceFactory()
        self.observers = FakeObserverFactory()
        self.running = []

    async def asyncTearDown(self):
        for manager, stop, task in self.running:
            stop.set()
            for sources in self.sources.sources.values():
                for source in sources:
                    source.release.set()
            await asyncio.wait_for(task, timeout=1.0)

    async def start_manager(
        self,
        *,
        profiles=None,
        strategy=DeviceStrategy.PRIORITY,
        reader_stop_timeout_seconds=0.1,
        loop=None,
    ):
        manager = DeviceManager(
            profiles or (self.primary, self.backup),
            self.actions,
            self.discovery,
            strategy=strategy,
            source_factory=self.sources,
            observer_factory=self.observers,
            reader_stop_timeout_seconds=reader_stop_timeout_seconds,
            loop=loop,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(manager.run(stop))
        self.running.append((manager, stop, task))
        await self.wait_for(lambda: self.observers.observer is not None)
        return manager, stop, task

    async def wait_for(self, predicate, message="condition"):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0.002)
        self.fail(f"timed out waiting for {message}")

    @staticmethod
    def active_profiles(manager):
        return {handle.profile.name for handle in manager.active_readers.values()}

    async def test_default_priority_preempts_falls_back_and_reconnects(self):
        backup_candidate = available(self.backup, "/dev/hidraw2")
        primary_candidate = available(self.primary, "/dev/hidraw1")
        self.discovery.set(backup_candidate)
        action = self.actions["voice"]
        original_state = action.state
        original_timer = action.timer

        manager, _, _ = await self.start_manager()
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Backup"},
            "initial backup reader",
        )
        first_backup = self.sources.sources["Backup"][0]

        self.discovery.set(primary_candidate, backup_candidate)
        self.observers.observer.emit("add")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary"},
            "primary preemption",
        )
        self.assertTrue(first_backup.stopped.wait(timeout=0.2))

        self.discovery.set(backup_candidate)
        self.observers.observer.emit("remove")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Backup"},
            "fallback reader",
        )

        self.discovery.set(primary_candidate, backup_candidate)
        self.observers.observer.emit("add")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary"},
            "reconnected primary reader",
        )

        self.assertEqual(action.submissions, [])
        self.assertIs(action.state, original_state)
        self.assertIs(action.timer, original_timer)

    async def test_all_mode_runs_concurrently_without_restarting_survivor(self):
        primary_candidate = available(self.primary, "/dev/hidraw1")
        backup_candidate = available(self.backup, "/dev/hidraw2")
        self.discovery.set(primary_candidate, backup_candidate)

        manager, _, _ = await self.start_manager(strategy=DeviceStrategy.ALL)
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary", "Backup"},
            "both readers",
        )
        backup_source = self.sources.sources["Backup"][0]

        self.discovery.set(backup_candidate)
        self.observers.observer.emit("change")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Backup"},
            "surviving backup reader",
        )
        self.assertFalse(backup_source.stopped.is_set())

        self.discovery.set(primary_candidate, backup_candidate)
        self.observers.observer.emit("add")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary", "Backup"},
            "restored concurrent readers",
        )
        self.assertEqual(len(self.sources.sources["Backup"]), 1)

    async def test_cross_profile_same_node_collision_starts_no_reader(self):
        duplicate = replace(self.backup, vendor_id=self.primary.vendor_id)
        shared = available(self.primary, "/dev/hidraw1")
        self.discovery.set(shared)

        manager, _, _ = await self.start_manager(
            profiles=(self.primary, duplicate),
            strategy=DeviceStrategy.ALL,
        )
        await self.wait_for(lambda: self.discovery.calls > 0, "initial discovery")

        self.assertEqual(manager.active_readers, {})
        self.assertEqual(self.sources.sources, {})

    async def test_failed_priority_reader_is_quarantined_until_udev_change(self):
        primary_candidate = available(self.primary, "/dev/hidraw1")
        backup_candidate = available(self.backup, "/dev/hidraw2")
        failure = Event()
        self.sources.fail_events["Primary"] = failure
        self.sources.fail_once.add("Primary")
        self.discovery.set(primary_candidate, backup_candidate)

        manager, _, _ = await self.start_manager()
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary"},
            "initial priority reader",
        )

        failure.set()
        await self.wait_for(
            lambda: (
                self.active_profiles(manager) == {"Backup"}
                and self.discovery.calls >= 2
            ),
            "quarantined priority fallback",
        )
        await asyncio.sleep(0.02)

        self.assertEqual(self.discovery.calls, 2)
        self.assertEqual(len(self.sources.sources["Primary"]), 1)
        self.assertEqual(len(self.sources.sources["Backup"]), 1)

        self.observers.observer.emit("change")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary"},
            "priority retry after udev change",
        )

        self.assertEqual(self.discovery.calls, 3)
        self.assertEqual(len(self.sources.sources["Primary"]), 2)

    async def test_failed_all_mode_reader_does_not_restart_or_stop_survivor(self):
        primary_candidate = available(self.primary, "/dev/hidraw1")
        backup_candidate = available(self.backup, "/dev/hidraw2")
        failure = Event()
        self.sources.fail_events["Primary"] = failure
        self.sources.fail_once.add("Primary")
        self.discovery.set(primary_candidate, backup_candidate)

        manager, _, _ = await self.start_manager(strategy=DeviceStrategy.ALL)
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary", "Backup"},
            "both readers",
        )
        backup_source = self.sources.sources["Backup"][0]

        failure.set()
        await self.wait_for(
            lambda: (
                self.active_profiles(manager) == {"Backup"}
                and self.discovery.calls >= 2
            ),
            "quarantined all-mode reader",
        )
        await asyncio.sleep(0.02)

        self.assertFalse(backup_source.stopped.is_set())
        self.assertEqual(self.discovery.calls, 2)
        self.assertEqual(len(self.sources.sources["Primary"]), 1)
        self.assertEqual(len(self.sources.sources["Backup"]), 1)

    async def test_reader_events_use_call_soon_threadsafe_in_enqueue_order(self):
        first = MatchedInput(ActionRequest.ON, "Primary", None)
        second = MatchedInput(ActionRequest.OFF, "Primary", None)
        self.sources.initial_inputs["Primary"] = (first, second)
        self.discovery.set(available(self.primary, "/dev/hidraw1"))
        recording_loop = RecordingLoop(asyncio.get_running_loop())

        await self.start_manager(profiles=(self.primary,), loop=recording_loop)
        action = self.actions["voice"]
        await self.wait_for(lambda: len(action.submissions) == 2, "submitted inputs")

        routed_inputs = [
            args[1]
            for callback, args in recording_loop.threadsafe_calls
            if getattr(callback, "__name__", "") == "_submit_input"
        ]
        self.assertEqual(routed_inputs, [first, second])
        self.assertEqual(
            action.submissions,
            [
                (ActionRequest.ON, "Primary", None),
                (ActionRequest.OFF, "Primary", None),
            ],
        )

    async def test_queued_reader_callback_is_dropped_after_priority_preemption(self):
        queued = MatchedInput(ActionRequest.ON, "Backup", None)
        self.sources.initial_inputs["Backup"] = (queued,)
        backup_candidate = available(self.backup, "/dev/hidraw2")
        primary_candidate = available(self.primary, "/dev/hidraw1")
        self.discovery.set(backup_candidate)
        delayed_loop = DelayedInputLoop(asyncio.get_running_loop())

        manager, _, _ = await self.start_manager(loop=delayed_loop)
        await self.wait_for(
            lambda: len(delayed_loop.input_callbacks) == 1,
            "queued backup input",
        )

        self.discovery.set(primary_candidate, backup_candidate)
        self.observers.observer.emit("add")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary"},
            "primary preemption",
        )
        delayed_loop.deliver_inputs()

        self.assertEqual(self.actions["voice"].submissions, [])

    async def test_queued_failed_generation_callback_is_dropped_after_retry(self):
        queued = MatchedInput(ActionRequest.ON, "Primary", None)
        failure = Event()
        self.sources.initial_inputs["Primary"] = (queued,)
        self.sources.fail_events["Primary"] = failure
        self.sources.fail_once.add("Primary")
        self.discovery.set(available(self.primary, "/dev/hidraw1"))
        delayed_loop = DelayedInputLoop(asyncio.get_running_loop())

        manager, _, _ = await self.start_manager(
            profiles=(self.primary,),
            loop=delayed_loop,
        )
        await self.wait_for(
            lambda: len(delayed_loop.input_callbacks) == 1,
            "queued failed-generation input",
        )
        self.sources.initial_inputs["Primary"] = ()
        failure.set()
        await self.wait_for(
            lambda: manager.active_readers == {} and self.discovery.calls >= 2,
            "failed reader quarantine",
        )

        self.observers.observer.emit("change")
        await self.wait_for(
            lambda: len(self.sources.sources["Primary"]) == 2,
            "same-node reader retry",
        )
        delayed_loop.deliver_inputs()

        self.assertEqual(self.actions["voice"].submissions, [])

    async def test_reader_stop_is_bounded_before_priority_replacement(self):
        backup_candidate = available(self.backup, "/dev/hidraw2")
        primary_candidate = available(self.primary, "/dev/hidraw1")
        self.sources.ignore_stop.add("Backup")
        self.sources.late_inputs["Backup"] = (
            MatchedInput(ActionRequest.ON, "Backup", None),
        )
        self.discovery.set(backup_candidate)

        manager, _, _ = await self.start_manager(reader_stop_timeout_seconds=0.01)
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Backup"},
            "stubborn backup reader",
        )
        backup_source = self.sources.sources["Backup"][0]

        self.discovery.set(primary_candidate, backup_candidate)
        self.observers.observer.emit("add")
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary"},
            "bounded primary replacement",
        )

        self.assertFalse(backup_source.stopped.is_set())
        backup_source.release.set()
        self.assertTrue(backup_source.stopped.wait(timeout=0.2))
        await asyncio.sleep(0)
        self.assertEqual(self.actions["voice"].submissions, [])

    async def test_observer_stop_failure_waits_for_active_reader_before_propagating(
        self,
    ):
        self.discovery.set(available(self.primary, "/dev/hidraw1"))
        manager, stop, task = await self.start_manager(profiles=(self.primary,))
        await self.wait_for(
            lambda: self.active_profiles(manager) == {"Primary"},
            "active reader",
        )
        source = self.sources.sources["Primary"][0]
        observer = self.observers.observer
        observer.stop_error = RuntimeError("observer stop failed")
        self.running.remove((manager, stop, task))

        try:
            stop.set()
            with self.assertRaisesRegex(RuntimeError, "observer stop failed"):
                await task

            self.assertTrue(source.stopped.is_set())
            self.assertEqual(manager.active_readers, {})
        finally:
            await manager._stop_all_readers()


if __name__ == "__main__":
    unittest.main()

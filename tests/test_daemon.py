import asyncio
from pathlib import Path
import signal
from threading import Event
import unittest

from input_action_controller.devices.base import MatchedInput
from input_action_controller.devices.discovery import DeviceCandidate
from input_action_controller.devices.manager import DeviceManager
from input_action_controller.daemon import _finish_manager_task, run_daemon
from input_action_controller.locking import LockContendedError
from input_action_controller.models import (
    ActionConfig,
    ActionRequest,
    AppConfig,
    DeviceSelectionConfig,
    DeviceStrategy,
    HidrawProfile,
    RunnerConfig,
)
from input_action_controller.runner import CommandResult


def app_config() -> AppConfig:
    actions = (
        ActionConfig("voice", ("voice-on",), ("voice-off",)),
        ActionConfig("mute", ("mute-on",), ("mute-off",)),
    )
    devices = (
        HidrawProfile(
            name="Primary",
            action="voice",
            vendor_id="1111",
            product_id="0001",
            on_reports=(b"\x01",),
            off_reports=(b"\x00",),
        ),
        HidrawProfile(
            name="Mute",
            action="mute",
            vendor_id="2222",
            product_id="0002",
            on_reports=(b"\x01",),
            off_reports=(b"\x00",),
        ),
    )
    return AppConfig(
        runner=RunnerConfig(timeout_seconds=2.5, shutdown_timeout_seconds=7.5),
        device_selection=DeviceSelectionConfig(DeviceStrategy.ALL),
        actions=actions,
        devices=devices,
    )


class FakeLoop:
    def __init__(self, events):
        self.events = events
        self.handlers = {}
        self.time_calls = 0

    def add_signal_handler(self, sent_signal, callback):
        self.events.append(("signal-added", sent_signal))
        self.handlers[sent_signal] = callback

    def remove_signal_handler(self, sent_signal):
        self.events.append(("signal-removed", sent_signal))
        self.handlers.pop(sent_signal, None)
        return True

    def time(self):
        self.time_calls += 1
        return 100.0


class CapturingSignalLoop:
    def __init__(self, loop):
        self.loop = loop
        self.handlers = {}

    def add_signal_handler(self, sent_signal, callback):
        self.handlers[sent_signal] = callback

    def remove_signal_handler(self, sent_signal):
        self.handlers.pop(sent_signal, None)
        return True

    def time(self):
        return self.loop.time()


class FakeLock:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error
        self.released = False

    def acquire(self):
        self.events.append("lock-acquire")
        if self.error is not None:
            raise self.error
        return self

    def release(self):
        self.events.append("lock-release")
        self.released = True


class FakeRunner:
    def __init__(self, config, events):
        self.config = config
        self.events = events
        self.deadlines = []
        self.termination_started = asyncio.Event()
        self.block_termination = False

    async def terminate_active(self, deadline):
        self.events.append(("runner-terminate", deadline))
        self.deadlines.append(deadline)
        self.termination_started.set()
        if self.block_termination:
            await asyncio.Event().wait()


class FakeController:
    def __init__(
        self,
        config,
        runner,
        timeout,
        events,
        shutdown_started,
        all_shutdown_started,
        expected_controllers,
        *,
        fail_shutdown=False,
    ):
        self.name = config.name
        self.config = config
        self.runner = runner
        self.timeout = timeout
        self.events = events
        self.shutdown_started = shutdown_started
        self.all_shutdown_started = all_shutdown_started
        self.expected_controllers = expected_controllers
        self.fail_shutdown = fail_shutdown
        self.deadlines = []

    def start(self):
        self.events.append(("controller-start", self.name))

    async def shutdown(self, deadline):
        self.events.append(("controller-shutdown-start", self.name, deadline))
        self.deadlines.append(deadline)
        self.shutdown_started.add(self.name)
        if len(self.shutdown_started) == self.expected_controllers:
            self.all_shutdown_started.set()
        await asyncio.wait_for(self.all_shutdown_started.wait(), timeout=0.5)
        if self.fail_shutdown:
            raise RuntimeError(f"incomplete shutdown: {self.name}")
        self.events.append(("controller-shutdown-finish", self.name, deadline))


class FakeManager:
    def __init__(self, profiles, actions, discovery, events, **kwargs):
        self.profiles = profiles
        self.actions = actions
        self.discovery = discovery
        self.kwargs = kwargs
        self.events = events
        self.started = asyncio.Event()
        self.stopped = False
        self.failure = None
        self.shutdown_deadline = None

    async def run(self, stop_event, *, shutdown_deadline=None):
        self.events.append("manager-run")
        self.shutdown_deadline = shutdown_deadline
        self.started.set()
        if self.failure is not None:
            raise self.failure
        await stop_event.wait()
        self.stopped = True
        self.events.append("manager-stopped")


class StaticDiscovery:
    def __init__(self, candidate):
        self.candidate = candidate

    def enumerate(self):
        return (self.candidate,)


class QuietObserver:
    def start(self):
        pass

    def stop(self):
        pass


class StubbornSource:
    def __init__(self):
        self.release = Event()
        self.stopped = Event()

    def run(self, stop, emit):
        emit(MatchedInput(ActionRequest.ON, "Primary", None))
        self.release.wait(timeout=2.0)
        self.stopped.set()


class RecordingActionRunner:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.on_finished = asyncio.Event()

    async def run(self, argv, timeout, deadline=None):
        self.calls.append((argv, deadline))
        if argv == ("voice-on",):
            self.on_finished.set()
        return CommandResult(True, None, 0)

    async def terminate_active(self, deadline):
        pass


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_canceling_manager_wait_cancels_and_awaits_manager_cleanup(self):
        cleanup_finished = asyncio.Event()

        async def run_manager():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_finished.set()

        manager_task = asyncio.create_task(run_manager())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(
            _finish_manager_task(
                manager_task,
                asyncio.get_running_loop().time() + 1.0,
            )
        )
        await asyncio.sleep(0)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertTrue(manager_task.cancelled())
        self.assertTrue(cleanup_finished.is_set())

    async def run_lifecycle(
        self,
        sent_signal,
        *,
        failed_action=None,
        cancel_during_runner_termination=False,
    ):
        config = app_config()
        events = []
        loop = FakeLoop(events)
        lock = FakeLock(events)
        runner = FakeRunner(config.runner, events)
        runner.block_termination = cancel_during_runner_termination
        controllers = []
        shutdown_started = set()
        all_shutdown_started = asyncio.Event()
        manager_ref = []

        def lock_factory():
            return lock

        def runner_factory(runner_config):
            events.append("runner-create")
            self.assertIs(runner_config, config.runner)
            return runner

        def controller_factory(action_config, action_runner, timeout):
            events.append(("controller-create", action_config.name))
            controller = FakeController(
                action_config,
                action_runner,
                timeout,
                events,
                shutdown_started,
                all_shutdown_started,
                len(config.actions),
                fail_shutdown=action_config.name == failed_action,
            )
            controllers.append(controller)
            return controller

        def discovery_factory():
            events.append("discovery-create")
            return object()

        def manager_factory(profiles, actions, discovery, **kwargs):
            events.append("manager-create")
            manager = FakeManager(profiles, actions, discovery, events, **kwargs)
            manager_ref.append(manager)
            return manager

        daemon_task = asyncio.create_task(
            run_daemon(
                config,
                lock_factory=lock_factory,
                runner_factory=runner_factory,
                controller_factory=controller_factory,
                discovery_factory=discovery_factory,
                manager_factory=manager_factory,
                loop=loop,
            )
        )
        for _ in range(100):
            if manager_ref and manager_ref[0].started.is_set():
                break
            await asyncio.sleep(0)
        else:
            self.fail("manager did not start")

        loop.handlers[sent_signal]()
        if cancel_during_runner_termination:
            await asyncio.wait_for(runner.termination_started.wait(), timeout=1.0)
            daemon_task.cancel()
            try:
                await daemon_task
            except asyncio.CancelledError as error:
                result = error
            else:
                self.fail("daemon cancellation was not preserved")
        else:
            result = await asyncio.wait_for(daemon_task, timeout=1.0)
        return (
            result,
            config,
            events,
            loop,
            lock,
            runner,
            controllers,
            manager_ref[0],
        )

    async def test_composes_one_controller_per_action_and_shuts_down_on_signals(self):
        for sent_signal in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sent_signal=sent_signal):
                (
                    result,
                    config,
                    events,
                    loop,
                    lock,
                    runner,
                    controllers,
                    manager,
                ) = await self.run_lifecycle(sent_signal)

                self.assertEqual(result, 0)
                self.assertEqual([item.name for item in controllers], ["voice", "mute"])
                self.assertTrue(all(item.runner is runner for item in controllers))
                self.assertTrue(
                    all(
                        item.timeout == config.runner.timeout_seconds
                        for item in controllers
                    )
                )
                self.assertIs(manager.profiles, config.devices)
                self.assertEqual(
                    manager.actions, {item.name: item for item in controllers}
                )
                self.assertEqual(manager.kwargs["strategy"], DeviceStrategy.ALL)
                self.assertTrue(manager.stopped)
                self.assertEqual(manager.shutdown_deadline(), 107.5)
                self.assertEqual(loop.time_calls, 1)
                self.assertEqual(runner.deadlines, [107.5])
                self.assertTrue(all(item.deadlines == [107.5] for item in controllers))
                self.assertTrue(lock.released)

                self.assertLess(
                    events.index("lock-acquire"), events.index("runner-create")
                )
                self.assertLess(
                    events.index(("controller-start", "mute")),
                    events.index("manager-run"),
                )
                self.assertLess(
                    events.index("manager-stopped"),
                    events.index(("controller-shutdown-start", "voice", 107.5)),
                )
                self.assertLess(
                    events.index(("runner-terminate", 107.5)),
                    events.index("lock-release"),
                )

    async def test_reader_failure_isolated_by_manager_does_not_stop_daemon(self):
        task = asyncio.create_task(self.run_lifecycle(signal.SIGTERM))
        result, _, events, _, _, _, _, _ = await task

        self.assertEqual(result, 0)
        self.assertIn("manager-run", events)
        self.assertIn("manager-stopped", events)

    async def test_real_manager_cleanup_shares_deadline_with_prompt_action_barrier(
        self,
    ):
        action = ActionConfig("voice", ("voice-on",), ("voice-off",))
        device = HidrawProfile(
            name="Primary",
            action="voice",
            vendor_id="1111",
            product_id="0001",
            on_reports=(b"\x01",),
            off_reports=(b"\x00",),
        )
        config = AppConfig(
            runner=RunnerConfig(
                timeout_seconds=1.0,
                shutdown_timeout_seconds=0.05,
            ),
            device_selection=DeviceSelectionConfig(DeviceStrategy.PRIORITY),
            actions=(action,),
            devices=(device,),
        )
        candidate = DeviceCandidate(
            node="/dev/hidraw1",
            subsystem="hidraw",
            properties={
                "DEVNAME": "/dev/hidraw1",
                "ID_VENDOR_ID": "1111",
                "ID_MODEL_ID": "0001",
            },
            event_codes=frozenset(),
            keyboard_class=False,
        )
        source = StubbornSource()
        runner = RecordingActionRunner(config.runner)
        signal_loop = CapturingSignalLoop(asyncio.get_running_loop())

        def manager_factory(profiles, actions, discovery, **kwargs):
            return DeviceManager(
                profiles,
                actions,
                discovery,
                observer_factory=lambda callback: QuietObserver(),
                **kwargs,
            )

        daemon_task = asyncio.create_task(
            run_daemon(
                config,
                lock_factory=lambda: FakeLock([]),
                runner_factory=lambda runner_config: runner,
                discovery_factory=lambda: StaticDiscovery(candidate),
                manager_factory=manager_factory,
                source_factory=lambda profile, node: source,
                loop=signal_loop,
            )
        )
        try:
            await asyncio.wait_for(runner.on_finished.wait(), timeout=1.0)
            started = asyncio.get_running_loop().time()
            signal_loop.handlers[signal.SIGTERM]()

            result = await asyncio.wait_for(daemon_task, timeout=1.0)
            elapsed = asyncio.get_running_loop().time() - started

            self.assertEqual(result, 0)
            self.assertLess(elapsed, 0.2)
            self.assertIn(("voice-off",), [argv for argv, deadline in runner.calls])
        finally:
            source.release.set()
            self.assertTrue(source.stopped.wait(timeout=1.0))

    async def test_incomplete_action_shutdown_still_terminates_runner_and_releases_lock(
        self,
    ):
        (
            result,
            _,
            events,
            loop,
            lock,
            runner,
            controllers,
            _,
        ) = await self.run_lifecycle(
            signal.SIGTERM,
            failed_action="voice",
        )

        self.assertEqual(result, 1)
        self.assertEqual(loop.time_calls, 1)
        self.assertEqual(runner.deadlines, [107.5])
        self.assertTrue(all(item.deadlines == [107.5] for item in controllers))
        self.assertTrue(lock.released)
        self.assertIn(("controller-shutdown-finish", "mute", 107.5), events)

    async def test_cancellation_during_runner_termination_releases_outer_resources(
        self,
    ):
        result, _, events, loop, lock, _, _, _ = await self.run_lifecycle(
            signal.SIGTERM,
            cancel_during_runner_termination=True,
        )

        self.assertIsInstance(result, asyncio.CancelledError)
        self.assertEqual(loop.handlers, {})
        self.assertTrue(lock.released)
        for sent_signal in (signal.SIGTERM, signal.SIGINT):
            self.assertIn(("signal-removed", sent_signal), events)
        self.assertLess(
            events.index(("runner-terminate", 107.5)),
            events.index("lock-release"),
        )

    async def test_lock_contention_returns_status_three_without_building_runtime(self):
        events = []
        loop = FakeLoop(events)
        lock = FakeLock(
            events,
            LockContendedError(Path("/tmp/input-action-controller.lock"), owner_pid=42),
        )

        result = await run_daemon(
            app_config(),
            lock_factory=lambda: lock,
            runner_factory=lambda config: self.fail("runner must not be created"),
            loop=loop,
        )

        self.assertEqual(result, 3)
        self.assertFalse(lock.released)
        self.assertEqual(events, ["lock-acquire"])


if __name__ == "__main__":
    unittest.main()

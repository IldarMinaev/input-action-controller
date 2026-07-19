import asyncio
from collections import deque
from dataclasses import dataclass
from io import StringIO
import logging
import unittest

from input_action_controller.actions import (
    ActionClosedError,
    ActionController,
    ActionMessage,
    ActionSnapshot,
)
from input_action_controller.models import (
    ActionConfig,
    ActionRequest,
    ActionState,
    FailedDirection,
)
from input_action_controller.runner import CommandFailure, CommandResult


SUCCESS = CommandResult(success=True, reason=None, exit_code=0)
FAILURE = CommandResult(
    success=False,
    reason=CommandFailure.EXIT_NONZERO,
    exit_code=7,
)


@dataclass(frozen=True)
class RunCall:
    argv: tuple[str, ...]
    timeout: float
    deadline: float | None


class FakeRunner:
    def __init__(self):
        self.responses = deque()
        self.calls: list[RunCall] = []
        self.terminate_deadlines: list[float | None] = []
        self.shutdown_events: list[str] = []
        self.active = 0
        self.max_active = 0

    def queue_result(self, result: CommandResult) -> None:
        self.responses.append(result)

    def block(self) -> asyncio.Future[CommandResult]:
        future = asyncio.get_running_loop().create_future()
        self.responses.append(future)
        return future

    async def run(
        self,
        argv: tuple[str, ...],
        timeout: float,
        deadline: float | None = None,
    ) -> CommandResult:
        self.calls.append(RunCall(argv, timeout, deadline))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        response = self.responses.popleft() if self.responses else SUCCESS
        try:
            if isinstance(response, asyncio.Future):
                try:
                    return await asyncio.shield(response)
                except asyncio.CancelledError:
                    self.shutdown_events.append("run-cancelled")
                    raise
            return response
        finally:
            self.active -= 1

    async def terminate_active(self, deadline: float | None) -> None:
        self.shutdown_events.append("terminate-active")
        self.terminate_deadlines.append(deadline)


class FakeTimer:
    def __init__(self, delay: float, callback):
        self.delay = delay
        self.callback = callback
        self._cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    def fire(self) -> None:
        if self._cancelled:
            return
        self.fired = True
        self.callback()


class FakeScheduler:
    def __init__(self):
        self.timers: list[FakeTimer] = []

    def call_later(self, delay: float, callback) -> FakeTimer:
        timer = FakeTimer(delay, callback)
        self.timers.append(timer)
        return timer


class ActionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controllers: list[ActionController] = []
        self.log_handler = logging.NullHandler()
        logging.getLogger("input_action_controller.actions").addHandler(
            self.log_handler
        )

    async def asyncTearDown(self):
        deadline = asyncio.get_running_loop().time() + 1.0
        results = await asyncio.gather(
            *(controller.shutdown(deadline) for controller in self.controllers),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        logging.getLogger("input_action_controller.actions").removeHandler(
            self.log_handler
        )

    def make_controller(
        self,
        *,
        runner: FakeRunner | None = None,
        scheduler: FakeScheduler | None = None,
        name: str = "voice",
        skip_off_after_failed_on: bool = False,
        skip_on_after_failed_off: bool = False,
        off_on_shutdown: bool = False,
    ) -> tuple[ActionController, FakeRunner, FakeScheduler]:
        runner = runner or FakeRunner()
        scheduler = scheduler or FakeScheduler()
        controller = ActionController(
            ActionConfig(
                name=name,
                on_command=(f"{name}-on",),
                off_command=(f"{name}-off",),
                skip_off_after_failed_on=skip_off_after_failed_on,
                skip_on_after_failed_off=skip_on_after_failed_off,
                off_on_shutdown=off_on_shutdown,
            ),
            runner,
            command_timeout_seconds=3.0,
            call_later=scheduler.call_later,
        )
        controller.start()
        self.controllers.append(controller)
        return controller, runner, scheduler

    async def drain(self, controller: ActionController) -> None:
        await asyncio.wait_for(controller._queue.join(), timeout=1.0)

    async def wait_for_calls(self, runner: FakeRunner, count: int) -> None:
        for _ in range(100):
            if len(runner.calls) >= count:
                return
            await asyncio.sleep(0)
        self.fail(f"runner received {len(runner.calls)} calls instead of {count}")

    async def set_on(
        self,
        controller: ActionController,
        runner: FakeRunner,
    ) -> None:
        controller.submit(ActionRequest.ON, source="setup")
        await self.drain(controller)
        self.assertEqual(controller.snapshot().state, ActionState.ON)
        runner.calls.clear()

    async def test_state_transition_matrix(self):
        cases = (
            (ActionState.OFF, ActionRequest.ON, True, ActionState.ON),
            (ActionState.ON, ActionRequest.OFF, True, ActionState.OFF),
            (ActionState.OFF, ActionRequest.OFF, True, ActionState.OFF),
            (ActionState.ON, ActionRequest.ON, True, ActionState.ON),
            (ActionState.OFF, ActionRequest.ON, False, ActionState.UNCERTAIN),
            (ActionState.ON, ActionRequest.OFF, False, ActionState.UNCERTAIN),
        )

        for initial, request, succeeds, expected in cases:
            with self.subTest(initial=initial, request=request, succeeds=succeeds):
                controller, runner, _ = self.make_controller(
                    name=f"matrix-{len(self.controllers)}"
                )
                if initial == ActionState.ON:
                    await self.set_on(controller, runner)
                if not succeeds:
                    runner.queue_result(FAILURE)

                controller.submit(request, source="matrix")
                await self.drain(controller)

                snapshot = controller.snapshot()
                self.assertEqual(snapshot.state, expected)
                if expected == ActionState.UNCERTAIN:
                    self.assertEqual(
                        snapshot.failed_direction,
                        FailedDirection(request.value),
                    )
                else:
                    self.assertIsNone(snapshot.failed_direction)
                expected_calls = 0 if initial.value == request.value else 1
                self.assertEqual(len(runner.calls), expected_calls)

    async def test_failed_on_skip_off_policy_is_independent(self):
        for skip, expected_calls in ((True, 0), (False, 1)):
            with self.subTest(skip_off_after_failed_on=skip):
                controller, runner, _ = self.make_controller(
                    name=f"failed-on-{skip}",
                    skip_off_after_failed_on=skip,
                    skip_on_after_failed_off=True,
                )
                runner.queue_result(FAILURE)
                controller.submit(ActionRequest.ON, source="device")
                await self.drain(controller)
                runner.calls.clear()

                controller.submit(ActionRequest.OFF, source="device")
                await self.drain(controller)

                self.assertEqual(controller.snapshot().state, ActionState.OFF)
                self.assertEqual(len(runner.calls), expected_calls)

    async def test_failed_off_skip_on_policy_is_independent(self):
        for skip, expected_calls in ((True, 0), (False, 1)):
            with self.subTest(skip_on_after_failed_off=skip):
                controller, runner, _ = self.make_controller(
                    name=f"failed-off-{skip}",
                    skip_off_after_failed_on=True,
                    skip_on_after_failed_off=skip,
                )
                await self.set_on(controller, runner)
                runner.queue_result(FAILURE)
                controller.submit(ActionRequest.OFF, source="device")
                await self.drain(controller)
                runner.calls.clear()

                controller.submit(ActionRequest.ON, source="device")
                await self.drain(controller)

                self.assertEqual(controller.snapshot().state, ActionState.ON)
                self.assertEqual(len(runner.calls), expected_calls)

    async def test_toggle_retries_the_failed_direction(self):
        for failed_direction in (FailedDirection.ON, FailedDirection.OFF):
            with self.subTest(failed_direction=failed_direction):
                controller, runner, _ = self.make_controller(
                    name=f"retry-{failed_direction.value}"
                )
                if failed_direction == FailedDirection.OFF:
                    await self.set_on(controller, runner)
                runner.queue_result(FAILURE)
                controller.submit(
                    ActionRequest(failed_direction.value),
                    source="device",
                )
                await self.drain(controller)
                runner.calls.clear()

                controller.submit(ActionRequest.TOGGLE, source="device")
                await self.drain(controller)

                expected_state = ActionState(failed_direction.value)
                self.assertEqual(controller.snapshot().state, expected_state)
                self.assertEqual(
                    runner.calls[0].argv,
                    (f"retry-{failed_direction.value}-{failed_direction.value}",),
                )

    async def test_worker_never_overlaps_commands_for_one_action(self):
        controller, runner, _ = self.make_controller()
        first = runner.block()
        second = runner.block()
        third = runner.block()

        controller.submit(ActionRequest.ON, source="first")
        controller.submit(ActionRequest.OFF, source="second")
        controller.submit(ActionRequest.ON, source="third")
        await self.wait_for_calls(runner, 1)
        self.assertEqual(len(runner.calls), 1)

        first.set_result(SUCCESS)
        await self.wait_for_calls(runner, 2)
        self.assertEqual(runner.max_active, 1)
        second.set_result(SUCCESS)
        await self.wait_for_calls(runner, 3)
        self.assertEqual(runner.max_active, 1)
        third.set_result(SUCCESS)
        await self.drain(controller)

        self.assertEqual(runner.max_active, 1)
        self.assertEqual(
            [call.argv for call in runner.calls],
            [("voice-on",), ("voice-off",), ("voice-on",)],
        )

    async def test_submission_sequence_defines_fifo_order_for_two_producers(self):
        controller, runner, _ = self.make_controller()
        submitted: list[tuple[str, int]] = []
        loop = asyncio.get_running_loop()
        loop.call_soon(
            lambda: submitted.append(
                ("producer-a", controller.submit(ActionRequest.ON, source="a"))
            )
        )
        loop.call_soon(
            lambda: submitted.append(
                ("producer-b", controller.submit(ActionRequest.OFF, source="b"))
            )
        )

        for _ in range(10):
            if len(submitted) == 2:
                break
            await asyncio.sleep(0)
        await self.drain(controller)

        self.assertEqual(submitted, [("producer-a", 0), ("producer-b", 1)])
        self.assertEqual(
            [call.argv for call in runner.calls],
            [("voice-on",), ("voice-off",)],
        )
        self.assertEqual(controller.snapshot().next_sequence, 2)

    async def test_action_messages_and_snapshots_are_value_objects(self):
        message = ActionMessage(4, ActionRequest.TOGGLE, "mouse", 12.0)
        snapshot = ActionSnapshot(
            name="voice",
            state=ActionState.ON,
            failed_direction=None,
            next_sequence=5,
            timer_active=True,
        )

        self.assertEqual(message.sequence, 4)
        self.assertEqual(message.toggle_timeout_seconds, 12.0)
        self.assertEqual(snapshot.name, "voice")
        self.assertTrue(snapshot.timer_active)

    async def test_failed_command_log_contains_only_stable_context(self):
        controller, runner, _ = self.make_controller()
        runner.queue_result(FAILURE)

        with self.assertLogs(
            "input_action_controller.actions", level="WARNING"
        ) as logs:
            controller.submit(ActionRequest.ON, source="headset")
            await self.drain(controller)

        record = logs.records[0]
        self.assertEqual(record.action, "voice")
        self.assertEqual(record.source, "headset")
        self.assertEqual(record.request, "on")
        self.assertEqual(record.reason, "command-exit-nonzero")
        self.assertEqual(record.exit_status, 7)
        self.assertNotIn("voice-on", record.getMessage())

    async def test_failed_command_log_renders_stable_context_without_secrets(self):
        controller, runner, _ = self.make_controller(name="secret-action")
        runner.queue_result(FAILURE)
        output = StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("input_action_controller.actions")
        logger.addHandler(handler)
        try:
            controller.submit(ActionRequest.ON, source="headset")
            await self.drain(controller)
        finally:
            logger.removeHandler(handler)

        rendered = output.getvalue()
        for field in (
            "action=secret-action",
            "source=headset",
            "request=on",
            "reason=command-exit-nonzero",
            "exit_status=7",
        ):
            with self.subTest(field=field):
                self.assertIn(field, rendered)
        for secret in ("secret-action-on", "stdout-secret", "stderr-secret"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)

    async def test_shutdown_incomplete_log_renders_stable_context_without_secrets(self):
        controller, runner, _ = self.make_controller(
            name="secret-action",
            off_on_shutdown=True,
        )
        await self.set_on(controller, runner)
        output = StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("input_action_controller.actions")
        logger.addHandler(handler)
        try:
            await controller.shutdown(asyncio.get_running_loop().time())
        finally:
            logger.removeHandler(handler)

        rendered = output.getvalue()
        for field in (
            "action=secret-action",
            "source=shutdown",
            "request=off",
            "reason=shutdown-incomplete",
            "exit_status=None",
        ):
            with self.subTest(field=field):
                self.assertIn(field, rendered)
        for secret in (
            "secret-action-on",
            "secret-action-off",
            "stdout-secret",
            "stderr-secret",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)

    async def test_toggle_expiry_enqueues_one_automatic_off(self):
        controller, runner, scheduler = self.make_controller()
        controller.submit(
            ActionRequest.TOGGLE,
            source="mouse",
            toggle_timeout_seconds=30.0,
        )
        await self.drain(controller)

        self.assertEqual(scheduler.timers[0].delay, 30.0)
        self.assertTrue(controller.snapshot().timer_active)
        scheduler.timers[0].fire()
        await self.drain(controller)

        self.assertEqual(
            [call.argv for call in runner.calls],
            [("voice-on",), ("voice-off",)],
        )
        self.assertEqual(controller.snapshot().state, ActionState.OFF)
        self.assertFalse(controller.snapshot().timer_active)

    async def test_zero_toggle_timeout_does_not_create_timer(self):
        controller, runner, scheduler = self.make_controller()
        controller.submit(
            ActionRequest.TOGGLE,
            source="mouse",
            toggle_timeout_seconds=0.0,
        )
        await self.drain(controller)

        self.assertEqual([call.argv for call in runner.calls], [("voice-on",)])
        self.assertEqual(scheduler.timers, [])
        self.assertFalse(controller.snapshot().timer_active)

    async def test_later_toggle_on_replaces_the_previous_timer(self):
        controller, runner, scheduler = self.make_controller()
        for _ in range(3):
            controller.submit(
                ActionRequest.TOGGLE,
                source="mouse",
                toggle_timeout_seconds=20.0,
            )
        await self.drain(controller)

        self.assertEqual(
            [call.argv for call in runner.calls],
            [("voice-on",), ("voice-off",), ("voice-on",)],
        )
        self.assertEqual(len(scheduler.timers), 2)
        self.assertTrue(scheduler.timers[0].cancelled())
        self.assertFalse(scheduler.timers[1].cancelled())
        self.assertTrue(controller.snapshot().timer_active)

    async def test_explicit_off_cancels_toggle_timer(self):
        controller, runner, scheduler = self.make_controller()
        controller.submit(
            ActionRequest.TOGGLE,
            source="mouse",
            toggle_timeout_seconds=20.0,
        )
        await self.drain(controller)

        controller.submit(ActionRequest.OFF, source="headset")
        await self.drain(controller)

        self.assertTrue(scheduler.timers[0].cancelled())
        self.assertFalse(controller.snapshot().timer_active)
        self.assertEqual(
            [call.argv for call in runner.calls],
            [("voice-on",), ("voice-off",)],
        )

    async def test_explicit_on_cancels_toggle_timer_without_refresh(self):
        controller, runner, scheduler = self.make_controller()
        controller.submit(
            ActionRequest.TOGGLE,
            source="mouse",
            toggle_timeout_seconds=20.0,
        )
        await self.drain(controller)

        controller.submit(ActionRequest.ON, source="headset")
        await self.drain(controller)

        self.assertTrue(scheduler.timers[0].cancelled())
        self.assertEqual(len(scheduler.timers), 1)
        self.assertFalse(controller.snapshot().timer_active)
        self.assertEqual([call.argv for call in runner.calls], [("voice-on",)])

    async def test_expiry_boundary_processes_the_lower_sequence_first(self):
        for automatic_off_first in (False, True):
            with self.subTest(automatic_off_first=automatic_off_first):
                controller, runner, scheduler = self.make_controller(
                    name=f"boundary-{automatic_off_first}"
                )
                controller.submit(
                    ActionRequest.TOGGLE,
                    source="setup",
                    toggle_timeout_seconds=10.0,
                )
                await self.drain(controller)
                runner.calls.clear()

                if automatic_off_first:
                    scheduler.timers[0].fire()
                    external_sequence = controller.submit(
                        ActionRequest.TOGGLE,
                        source="mouse",
                        toggle_timeout_seconds=10.0,
                    )
                else:
                    external_sequence = controller.submit(
                        ActionRequest.TOGGLE,
                        source="mouse",
                        toggle_timeout_seconds=10.0,
                    )
                    scheduler.timers[0].fire()
                await self.drain(controller)

                self.assertEqual(external_sequence, 2 if automatic_off_first else 1)
                self.assertEqual(controller.snapshot().next_sequence, 3)
                expected = [(f"boundary-{automatic_off_first}-off",)]
                if automatic_off_first:
                    expected.append((f"boundary-{automatic_off_first}-on",))
                self.assertEqual([call.argv for call in runner.calls], expected)

    async def test_shutdown_discards_queued_external_and_auto_off_messages(self):
        controller, runner, _ = self.make_controller()
        active = runner.block()
        controller.submit(ActionRequest.ON, source="active")
        await self.wait_for_calls(runner, 1)
        controller.submit(ActionRequest.OFF, source="queued-device")
        controller.submit(ActionRequest.AUTO_OFF, source="automatic-off")

        deadline = asyncio.get_running_loop().time() + 1.0
        shutdown = asyncio.create_task(controller.shutdown(deadline))
        await asyncio.sleep(0)
        with self.assertRaises(ActionClosedError):
            controller.submit(ActionRequest.ON, source="late")
        active.set_result(SUCCESS)
        await shutdown

        self.assertEqual([call.argv for call in runner.calls], [("voice-on",)])
        self.assertEqual(controller.snapshot().state, ActionState.ON)

    async def test_shutdown_completion_updates_state_without_timer(self):
        controller, runner, scheduler = self.make_controller(off_on_shutdown=True)
        active = runner.block()
        controller.submit(
            ActionRequest.TOGGLE,
            source="mouse",
            toggle_timeout_seconds=60.0,
        )
        await self.wait_for_calls(runner, 1)

        deadline = asyncio.get_running_loop().time() + 1.0
        shutdown = asyncio.create_task(controller.shutdown(deadline))
        await asyncio.sleep(0)
        active.set_result(SUCCESS)
        await shutdown

        self.assertEqual(
            [call.argv for call in runner.calls],
            [("voice-on",), ("voice-off",)],
        )
        self.assertEqual(runner.calls[1].deadline, deadline)
        self.assertEqual(controller.snapshot().state, ActionState.OFF)
        self.assertEqual(scheduler.timers, [])

    async def test_hung_transition_is_cancelled_at_global_deadline(self):
        controller, runner, _ = self.make_controller(off_on_shutdown=True)
        runner.block()
        controller.submit(
            ActionRequest.TOGGLE,
            source="headset",
            toggle_timeout_seconds=60.0,
        )
        await self.wait_for_calls(runner, 1)
        deadline = asyncio.get_running_loop().time()

        with self.assertLogs(
            "input_action_controller.actions", level="WARNING"
        ) as logs:
            await controller.shutdown(deadline)

        snapshot = controller.snapshot()
        self.assertEqual(snapshot.state, ActionState.UNCERTAIN)
        self.assertEqual(snapshot.failed_direction, FailedDirection.ON)
        self.assertEqual(runner.terminate_deadlines, [deadline])
        self.assertEqual(logs.records[-1].reason, "shutdown-incomplete")
        self.assertEqual(logs.records[-1].request, "toggle")
        self.assertEqual(len(runner.calls), 1)

    async def test_shutdown_registers_global_deadline_before_worker_cancellation(self):
        controller, runner, _ = self.make_controller()
        runner.block()
        controller.submit(ActionRequest.ON, source="headset")
        await self.wait_for_calls(runner, 1)

        with self.assertLogs("input_action_controller.actions", level="WARNING"):
            await controller.shutdown(asyncio.get_running_loop().time())

        self.assertEqual(
            runner.shutdown_events,
            ["terminate-active", "run-cancelled"],
        )

    async def test_shutdown_synthetic_off_covers_every_state(self):
        cases = (
            ("off", False, 0),
            ("on", False, 1),
            ("uncertain-on", True, 0),
            ("uncertain-on", False, 1),
            ("uncertain-off", False, 1),
        )

        for initial, skip_failed_on, expected_calls in cases:
            with self.subTest(initial=initial, skip_failed_on=skip_failed_on):
                controller, runner, _ = self.make_controller(
                    name=f"shutdown-{initial}-{skip_failed_on}",
                    skip_off_after_failed_on=skip_failed_on,
                    off_on_shutdown=True,
                )
                if initial in {"on", "uncertain-off"}:
                    await self.set_on(controller, runner)
                if initial == "uncertain-on":
                    runner.queue_result(FAILURE)
                    controller.submit(ActionRequest.ON, source="setup")
                    await self.drain(controller)
                    runner.calls.clear()
                elif initial == "uncertain-off":
                    runner.queue_result(FAILURE)
                    controller.submit(ActionRequest.OFF, source="setup")
                    await self.drain(controller)
                    runner.calls.clear()

                await controller.shutdown(asyncio.get_running_loop().time() + 1.0)

                self.assertEqual(len(runner.calls), expected_calls)
                if expected_calls:
                    self.assertEqual(
                        runner.calls[0].argv,
                        (f"shutdown-{initial}-{skip_failed_on}-off",),
                    )
                    self.assertIsNotNone(runner.calls[0].deadline)
                self.assertEqual(controller.snapshot().state, ActionState.OFF)

    async def test_expired_shutdown_deadline_warns_only_for_pending_off_cleanup(self):
        controller, runner, _ = self.make_controller(off_on_shutdown=True)
        await self.set_on(controller, runner)

        with self.assertLogs(
            "input_action_controller.actions", level="WARNING"
        ) as logs:
            await controller.shutdown(asyncio.get_running_loop().time())

        self.assertEqual(runner.calls, [])
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.action, "voice")
        self.assertEqual(record.source, "shutdown")
        self.assertEqual(record.request, "off")
        self.assertEqual(record.reason, "shutdown-incomplete")
        self.assertIsNone(record.exit_status)

        controller, runner, _ = self.make_controller(
            name="already-off",
            off_on_shutdown=True,
        )
        with self.assertNoLogs("input_action_controller.actions", level="WARNING"):
            await controller.shutdown(asyncio.get_running_loop().time())
        self.assertEqual(runner.calls, [])

        controller, runner, _ = self.make_controller(
            name="disabled",
            off_on_shutdown=False,
        )
        await self.set_on(controller, runner)
        with self.assertNoLogs("input_action_controller.actions", level="WARNING"):
            await controller.shutdown(asyncio.get_running_loop().time())
        self.assertEqual(runner.calls, [])

    async def test_off_on_shutdown_false_preserves_stable_on(self):
        controller, runner, _ = self.make_controller(off_on_shutdown=False)
        await self.set_on(controller, runner)

        await controller.shutdown(asyncio.get_running_loop().time() + 1.0)

        self.assertEqual(runner.calls, [])
        self.assertEqual(controller.snapshot().state, ActionState.ON)

    async def test_two_actions_shutdown_concurrently_under_one_deadline(self):
        runner = FakeRunner()
        first_active = runner.block()
        second_active = runner.block()
        first, _, _ = self.make_controller(runner=runner, name="first")
        second, _, _ = self.make_controller(runner=runner, name="second")
        first.submit(ActionRequest.ON, source="first-device")
        second.submit(ActionRequest.ON, source="second-device")
        await self.wait_for_calls(runner, 2)
        deadline = asyncio.get_running_loop().time() + 1.0

        shutdowns = [
            asyncio.create_task(first.shutdown(deadline)),
            asyncio.create_task(second.shutdown(deadline)),
        ]
        await asyncio.sleep(0)
        self.assertFalse(any(task.done() for task in shutdowns))
        first_active.set_result(SUCCESS)
        second_active.set_result(SUCCESS)
        await asyncio.gather(*shutdowns)

        self.assertEqual(first.snapshot().state, ActionState.ON)
        self.assertEqual(second.snapshot().state, ActionState.ON)
        self.assertEqual(runner.max_active, 2)


if __name__ == "__main__":
    unittest.main()

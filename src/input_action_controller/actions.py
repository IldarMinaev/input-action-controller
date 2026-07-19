import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
from typing import Callable, Protocol

from .models import ActionConfig, ActionRequest, ActionState, FailedDirection
from .runner import CommandResult


LOGGER = logging.getLogger(__name__)


class ActionRunner(Protocol):
    async def run(
        self,
        argv: tuple[str, ...],
        timeout: float,
        deadline: float | None = None,
    ) -> CommandResult: ...

    async def terminate_active(self, deadline: float | None) -> None: ...


class TimerHandle(Protocol):
    def cancel(self) -> None: ...

    def cancelled(self) -> bool: ...


@dataclass(frozen=True)
class ActionMessage:
    sequence: int
    request: ActionRequest
    source: str
    toggle_timeout_seconds: float | None


@dataclass(frozen=True)
class ActionSnapshot:
    name: str
    state: ActionState
    failed_direction: FailedDirection | None
    next_sequence: int
    timer_active: bool


class ActionClosedError(RuntimeError):
    def __init__(self, action: str):
        self.action = action
        super().__init__(f"action is closed: {action}")


class ActionController:
    def __init__(
        self,
        config: ActionConfig,
        runner: ActionRunner,
        command_timeout_seconds: float,
        *,
        call_later: Callable[[float, Callable[[], None]], TimerHandle] | None = None,
    ):
        self.name = config.name
        self._config = config
        self._runner = runner
        self._command_timeout_seconds = command_timeout_seconds
        self._call_later = call_later
        self._queue: asyncio.Queue[ActionMessage] = asyncio.Queue()
        self._state = ActionState.OFF
        self._failed_direction: FailedDirection | None = None
        self._next_sequence = 0
        self._accepting = True
        self._worker_task: asyncio.Task[None] | None = None
        self._timer: TimerHandle | None = None
        self._active_direction: FailedDirection | None = None
        self._active_message: ActionMessage | None = None
        self._active_finished = asyncio.Event()
        self._active_finished.set()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_complete = False

    def start(self) -> None:
        if not self._accepting:
            raise ActionClosedError(self.name)
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(
                self._worker(),
                name=f"action-controller:{self.name}",
            )

    def submit(
        self,
        request: ActionRequest,
        *,
        source: str,
        toggle_timeout_seconds: float | None = None,
    ) -> int:
        if not self._accepting:
            raise ActionClosedError(self.name)
        sequence = self._next_sequence
        self._next_sequence += 1
        self._queue.put_nowait(
            ActionMessage(sequence, request, source, toggle_timeout_seconds)
        )
        return sequence

    def snapshot(self) -> ActionSnapshot:
        return ActionSnapshot(
            name=self.name,
            state=self._state,
            failed_direction=self._failed_direction,
            next_sequence=self._next_sequence,
            timer_active=self._timer is not None and not self._timer.cancelled(),
        )

    async def shutdown(self, deadline: float) -> None:
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return

            self._accepting = False
            self._discard_queued_messages()
            self._cancel_timer()

            transition_incomplete = await self._finish_active_transition(deadline)
            await self._stop_worker()

            if self._config.off_on_shutdown:
                message = ActionMessage(
                    self._next_sequence,
                    ActionRequest.OFF,
                    "shutdown",
                    None,
                )
                if self._remaining(deadline) > 0:
                    await self._process_message(
                        message,
                        deadline=deadline,
                        allow_timer=False,
                    )
                elif self._state != ActionState.OFF and not transition_incomplete:
                    self._log_shutdown_incomplete(message, FailedDirection.OFF)

            self._shutdown_complete = True

    async def _worker(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._process_message(
                    message,
                    deadline=None,
                    allow_timer=True,
                )
            finally:
                self._queue.task_done()

    async def _process_message(
        self,
        message: ActionMessage,
        *,
        deadline: float | None,
        allow_timer: bool,
    ) -> None:
        self._cancel_timer()
        direction = self._resolve_direction(message.request)

        if self._skip_uncertain_transition(direction):
            self._set_stable(direction)
            return

        stable_state = ActionState(direction.value)
        if self._state == stable_state:
            return

        result = await self._run_direction(direction, message, deadline)
        if result.success:
            self._set_stable(direction)
            if (
                allow_timer
                and self._accepting
                and direction == FailedDirection.ON
                and message.request == ActionRequest.TOGGLE
                and message.toggle_timeout_seconds is not None
                and message.toggle_timeout_seconds > 0
            ):
                self._arm_timer(message.toggle_timeout_seconds)
            return

        self._state = ActionState.UNCERTAIN
        self._failed_direction = direction
        self._log_failure(message, result)

    async def _run_direction(
        self,
        direction: FailedDirection,
        message: ActionMessage,
        deadline: float | None,
    ) -> CommandResult:
        argv = (
            self._config.on_command
            if direction == FailedDirection.ON
            else self._config.off_command
        )
        self._active_direction = direction
        self._active_message = message
        self._active_finished.clear()
        try:
            return await self._runner.run(
                argv,
                timeout=self._command_timeout_seconds,
                deadline=deadline,
            )
        finally:
            self._active_direction = None
            self._active_message = None
            self._active_finished.set()

    def _resolve_direction(self, request: ActionRequest) -> FailedDirection:
        if request == ActionRequest.ON:
            return FailedDirection.ON
        if request in {ActionRequest.OFF, ActionRequest.AUTO_OFF}:
            return FailedDirection.OFF
        if self._state == ActionState.OFF:
            return FailedDirection.ON
        if self._state == ActionState.ON:
            return FailedDirection.OFF
        if self._failed_direction is None:
            raise RuntimeError(f"uncertain action has no failed direction: {self.name}")
        return self._failed_direction

    def _skip_uncertain_transition(self, direction: FailedDirection) -> bool:
        if self._state != ActionState.UNCERTAIN:
            return False
        if (
            self._failed_direction == FailedDirection.ON
            and direction == FailedDirection.OFF
        ):
            return self._config.skip_off_after_failed_on
        if (
            self._failed_direction == FailedDirection.OFF
            and direction == FailedDirection.ON
        ):
            return self._config.skip_on_after_failed_off
        return False

    def _set_stable(self, direction: FailedDirection) -> None:
        self._state = ActionState(direction.value)
        self._failed_direction = None

    def _arm_timer(self, timeout_seconds: float) -> None:
        call_later = self._call_later
        if call_later is None:
            call_later = asyncio.get_running_loop().call_later
        self._timer = call_later(timeout_seconds, self._automatic_off)

    def _automatic_off(self) -> None:
        self._timer = None
        try:
            self.submit(ActionRequest.AUTO_OFF, source="automatic-off")
        except ActionClosedError:
            pass

    def _cancel_timer(self) -> None:
        if self._timer is None:
            return
        self._timer.cancel()
        self._timer = None

    def _discard_queued_messages(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self._queue.task_done()

    async def _finish_active_transition(self, deadline: float) -> bool:
        if self._active_direction is None:
            return False

        remaining = self._remaining(deadline)
        if remaining > 0:
            try:
                await asyncio.wait_for(
                    self._active_finished.wait(),
                    timeout=remaining,
                )
                return False
            except TimeoutError:
                pass

        if self._active_direction is None:
            return False
        direction = self._active_direction
        message = self._active_message
        task = self._worker_task
        termination = asyncio.create_task(self._runner.terminate_active(deadline))
        # Install the shared runner deadline before cancellation cleanup starts.
        await asyncio.sleep(0)
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        await termination

        self._state = ActionState.UNCERTAIN
        self._failed_direction = direction
        self._log_shutdown_incomplete(message, direction)
        return True

    async def _stop_worker(self) -> None:
        task = self._worker_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _log_failure(
        self,
        message: ActionMessage,
        result: CommandResult,
    ) -> None:
        reason = (
            result.reason.value
            if result.reason is not None
            else "command-launch-failed"
        )
        LOGGER.warning(
            "action transition failed action=%s source=%s request=%s reason=%s exit_status=%s",
            self.name,
            message.source,
            message.request.value,
            reason,
            result.exit_code,
            extra={
                "action": self.name,
                "source": message.source,
                "request": message.request.value,
                "reason": reason,
                "exit_status": result.exit_code,
            },
        )

    def _log_shutdown_incomplete(
        self,
        message: ActionMessage | None,
        direction: FailedDirection,
    ) -> None:
        source = message.source if message is not None else "shutdown"
        request = message.request.value if message is not None else direction.value
        LOGGER.warning(
            "action shutdown incomplete action=%s source=%s request=%s reason=%s exit_status=%s",
            self.name,
            source,
            request,
            "shutdown-incomplete",
            None,
            extra={
                "action": self.name,
                "source": source,
                "request": request,
                "reason": "shutdown-incomplete",
                "exit_status": None,
            },
        )

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

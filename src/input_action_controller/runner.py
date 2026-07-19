import asyncio
from dataclasses import dataclass
import errno
from enum import StrEnum
import os
import signal
import subprocess
from typing import Awaitable

from .models import RunnerConfig


class CommandFailure(StrEnum):
    NOT_FOUND = "command-not-found"
    TIMEOUT = "command-timeout"
    EXIT_NONZERO = "command-exit-nonzero"
    LAUNCH_FAILED = "command-launch-failed"


@dataclass(frozen=True)
class CommandResult:
    success: bool
    reason: CommandFailure | None
    exit_code: int | None


@dataclass
class _ProcessCleanup:
    process: asyncio.subprocess.Process
    deadline: float | None
    deadline_changed: asyncio.Event
    task: asyncio.Task[None] | None = None

    def adopt_deadline(self, deadline: float | None) -> None:
        if deadline is None:
            return
        if self.deadline is None or deadline < self.deadline:
            self.deadline = deadline
            self.deadline_changed.set()


class CommandRunner:
    def __init__(self, config: RunnerConfig):
        self._shutdown_timeout = config.shutdown_timeout_seconds
        self._active: dict[int, asyncio.subprocess.Process] = {}
        self._launches = 0
        self._launches_finished = asyncio.Event()
        self._launches_finished.set()
        self._shutdown_deadline: float | None = None
        self._cleanups: dict[int, _ProcessCleanup] = {}

    async def run(
        self,
        argv: tuple[str, ...],
        timeout: float,
        deadline: float | None = None,
    ) -> CommandResult:
        if self._shutdown_deadline is not None:
            return CommandResult(False, CommandFailure.LAUNCH_FAILED, None)

        self._launches += 1
        self._launches_finished.clear()
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                return CommandResult(False, CommandFailure.NOT_FOUND, None)
            except OSError:
                return CommandResult(False, CommandFailure.LAUNCH_FAILED, None)

            if self._shutdown_deadline is not None:
                try:
                    await self._await_cleanup(
                        self._start_process_cleanup(process, self._shutdown_deadline)
                    )
                finally:
                    self._remove_cleanup(process)
                return CommandResult(False, CommandFailure.LAUNCH_FAILED, None)

            self._active[process.pid] = process
        finally:
            self._launches -= 1
            if self._launches == 0:
                self._launches_finished.set()

        loop = asyncio.get_running_loop()
        command_deadline = loop.time() + timeout
        if deadline is not None:
            command_deadline = min(command_deadline, deadline)

        try:
            exit_code = await self._wait_until(process, command_deadline)
        except TimeoutError:
            await self._await_cleanup(
                self._start_process_cleanup(
                    process,
                    self._run_cleanup_deadline(deadline),
                )
            )
            return CommandResult(False, CommandFailure.TIMEOUT, None)
        except asyncio.CancelledError:
            await self._await_cleanup(
                self._start_process_cleanup(
                    process,
                    self._run_cleanup_deadline(deadline),
                )
            )
            raise
        finally:
            self._remove_active(process)
            self._remove_cleanup(process)

        if exit_code == 0:
            return CommandResult(True, None, exit_code)
        return CommandResult(False, CommandFailure.EXIT_NONZERO, exit_code)

    async def terminate_active(self, deadline: float | None) -> None:
        cleanup_deadline = self._cleanup_deadline(deadline)
        if self._shutdown_deadline is None:
            self._shutdown_deadline = cleanup_deadline
        else:
            self._shutdown_deadline = min(self._shutdown_deadline, cleanup_deadline)
        for cleanup in tuple(self._cleanups.values()):
            cleanup.adopt_deadline(self._shutdown_deadline)
        await self._await_cleanup(self._terminate_all(self._shutdown_deadline))

    async def _terminate_all(self, deadline: float) -> None:
        remaining = self._remaining(deadline)
        if self._launches and remaining > 0:
            try:
                await asyncio.wait_for(
                    self._launches_finished.wait(),
                    timeout=remaining,
                )
            except TimeoutError:
                pass

        await asyncio.gather(
            *(
                self._start_process_cleanup(process, deadline)
                for process in tuple(self._active.values())
            ),
            return_exceptions=True,
        )

    @staticmethod
    async def _await_cleanup(cleanup: Awaitable[None]) -> None:
        cleanup_task = asyncio.ensure_future(cleanup)
        cancellation: asyncio.CancelledError | None = None

        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as error:
                cancellation = error

        cleanup_task.result()
        if cancellation is not None:
            raise cancellation

    def _cleanup_deadline(self, deadline: float | None) -> float:
        if deadline is not None:
            return deadline
        return asyncio.get_running_loop().time() + self._shutdown_timeout

    def _run_cleanup_deadline(self, deadline: float | None) -> float | None:
        if deadline is None:
            return self._shutdown_deadline
        if self._shutdown_deadline is not None:
            return min(deadline, self._shutdown_deadline)
        return deadline

    @staticmethod
    async def _wait_until(
        process: asyncio.subprocess.Process,
        deadline: float,
    ) -> int:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        return await asyncio.wait_for(process.wait(), timeout=remaining)

    def _start_process_cleanup(
        self,
        process: asyncio.subprocess.Process,
        deadline: float | None,
    ) -> asyncio.Task[None]:
        cleanup = self._cleanups.get(process.pid)
        if cleanup is not None and cleanup.process is process:
            cleanup.adopt_deadline(deadline)
            if cleanup.task is None:
                raise RuntimeError("process cleanup task is missing")
            return cleanup.task

        cleanup = _ProcessCleanup(process, deadline, asyncio.Event())
        cleanup.task = asyncio.create_task(
            self._terminate_process(cleanup),
            name=f"command-cleanup:{process.pid}",
        )
        self._cleanups[process.pid] = cleanup
        return cleanup.task

    async def _terminate_process(self, cleanup: _ProcessCleanup) -> None:
        process = cleanup.process
        try:
            self._signal_process_group(process.pid, signal.SIGTERM)
            grace_deadline = asyncio.get_running_loop().time() + 0.5
            await self._wait_for_process_or_deadline(cleanup, grace_deadline)

            if self._process_group_exists(process.pid):
                self._signal_process_group(process.pid, signal.SIGKILL)

            await self._wait_for_process_or_deadline(cleanup, None)
        finally:
            self._remove_active(process)

    def _remove_active(self, process: asyncio.subprocess.Process) -> None:
        if self._active.get(process.pid) is process:
            self._active.pop(process.pid)

    def _remove_cleanup(self, process: asyncio.subprocess.Process) -> None:
        cleanup = self._cleanups.get(process.pid)
        if cleanup is not None and cleanup.process is process:
            self._cleanups.pop(process.pid)

    async def _wait_for_process_or_deadline(
        self,
        cleanup: _ProcessCleanup,
        phase_deadline: float | None,
    ) -> bool:
        while True:
            deadline = phase_deadline
            if cleanup.deadline is not None:
                deadline = (
                    cleanup.deadline
                    if deadline is None
                    else min(deadline, cleanup.deadline)
                )
            timeout = None
            if deadline is not None:
                timeout = self._remaining(deadline)
                if timeout <= 0:
                    return False

            cleanup.deadline_changed.clear()
            process_wait = asyncio.create_task(cleanup.process.wait())
            deadline_update = asyncio.create_task(cleanup.deadline_changed.wait())
            try:
                done, _ = await asyncio.wait(
                    (process_wait, deadline_update),
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                process_wait.cancel()
                deadline_update.cancel()
                await asyncio.gather(
                    process_wait,
                    deadline_update,
                    return_exceptions=True,
                )
                raise

            if process_wait in done:
                deadline_update.cancel()
                await asyncio.gather(deadline_update, return_exceptions=True)
                process_wait.result()
                return True

            process_wait.cancel()
            if deadline_update not in done:
                deadline_update.cancel()
            await asyncio.gather(
                process_wait,
                deadline_update,
                return_exceptions=True,
            )
            if not done:
                return False

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    @staticmethod
    def _signal_process_group(process_group: int, sent_signal: signal.Signals) -> None:
        try:
            os.killpg(process_group, sent_signal)
        except ProcessLookupError:
            pass

    @staticmethod
    def _process_group_exists(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except OSError as error:
            return error.errno != errno.ESRCH
        return True

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch

from input_action_controller.models import RunnerConfig
from input_action_controller.runner import CommandFailure, CommandResult, CommandRunner


HELPER = Path(__file__).parent / "helpers" / "command_tree.py"


def _proc_status_is_running(content: str) -> bool:
    for line in content.splitlines():
        if line.startswith("State:"):
            fields = line.split()
            return len(fields) < 2 or fields[1] != "Z"
    return True


def _pid_is_running(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError):
        return False
    return _proc_status_is_running(status)


class StuckProcess:
    pid = 7654321
    returncode = None

    def __init__(self):
        self.wait_started = asyncio.Event()

    async def wait(self):
        self.wait_started.set()
        await asyncio.Event().wait()


class FinishedProcess:
    pid = 7654321
    returncode = 0

    async def wait(self):
        return self.returncode


class TermIgnoringProcess:
    pid = 7654321
    returncode = None

    def __init__(self):
        self.wait_started = asyncio.Event()
        self.exited = asyncio.Event()
        self.reaped = False

    async def wait(self):
        self.wait_started.set()
        await self.exited.wait()
        self.returncode = -signal.SIGKILL
        self.reaped = True
        return self.returncode


class UnreapableProcess:
    pid = 7654321
    returncode = None

    def __init__(self):
        self.wait_started = asyncio.Event()
        self.release = asyncio.Event()
        self.reaped = False

    async def wait(self):
        self.wait_started.set()
        await self.release.wait()
        self.returncode = -signal.SIGKILL
        self.reaped = True
        return self.returncode


class CommandRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = self.enterContext(TemporaryDirectory())
        self.runner = CommandRunner(
            RunnerConfig(timeout_seconds=1.0, shutdown_timeout_seconds=1.0)
        )

    def helper_command(self, mode: str, name: str) -> tuple[tuple[str, ...], Path]:
        pid_file = Path(self._temporary_directory) / name
        self.addCleanup(self._kill_recorded_group, pid_file)
        return (sys.executable, str(HELPER), mode, str(pid_file)), pid_file

    @staticmethod
    def _kill_recorded_group(pid_file: Path) -> None:
        process_group = CommandRunnerTests._read_recorded_pid(
            pid_file, time.monotonic() + 0.25
        )
        if process_group is None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)

        deadline = time.monotonic() + 1.0
        paths = (pid_file, Path(f"{pid_file}.child"))
        while time.monotonic() < deadline:
            recorded_pids = [
                pid
                for path in paths
                if (pid := CommandRunnerTests._read_recorded_pid(path)) is not None
            ]
            if not any(_pid_is_running(pid) for pid in recorded_pids):
                return
            time.sleep(0.01)

    @staticmethod
    def _read_recorded_pid(pid_file: Path, deadline: float | None = None) -> int | None:
        while True:
            try:
                return int(pid_file.read_text(encoding="ascii"))
            except (FileNotFoundError, ValueError):
                if deadline is None or time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)

    async def wait_for_pid(self, pid_file: Path) -> int:
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if pid_file.exists():
                try:
                    return int(pid_file.read_text(encoding="ascii"))
                except ValueError:
                    pass
            await asyncio.sleep(0.01)
        self.fail(f"helper did not write {pid_file}")

    async def assert_process_tree_terminated(
        self, direct_child_pid: int, grandchild_pid: int
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            direct_child_exists = Path(f"/proc/{direct_child_pid}").exists()
            if not direct_child_exists and not _pid_is_running(grandchild_pid):
                return
            await asyncio.sleep(0.01)
        if Path(f"/proc/{direct_child_pid}").exists():
            self.fail(f"direct child was not reaped: {direct_child_pid}")
        self.fail(f"helper grandchild is still running: {grandchild_pid}")

    def test_proc_status_treats_zombie_as_terminated(self):
        self.assertFalse(_proc_status_is_running("Name:\tchild\nState:\tZ (zombie)\n"))

    def test_proc_status_treats_sleeping_process_as_running(self):
        self.assertTrue(_proc_status_is_running("Name:\tchild\nState:\tS (sleeping)\n"))

    def test_pid_is_not_running_when_proc_entry_disappears_during_read(self):
        with patch.object(Path, "read_text", side_effect=ProcessLookupError):
            self.assertFalse(_pid_is_running(7654321))

    async def test_returns_success_and_zero_exit_code(self):
        argv, _ = self.helper_command("success", "success.pid")

        result = await self.runner.run(argv, timeout=1.0)

        self.assertEqual(result, CommandResult(success=True, reason=None, exit_code=0))

    async def test_maps_nonzero_exit_status(self):
        argv, _ = self.helper_command("exit-nonzero", "nonzero.pid")

        result = await self.runner.run(argv, timeout=1.0)

        self.assertEqual(
            result,
            CommandResult(
                success=False,
                reason=CommandFailure.EXIT_NONZERO,
                exit_code=2,
            ),
        )

    async def test_maps_missing_executable(self):
        result = await self.runner.run(
            ("/definitely/missing/input-action-controller-command",),
            timeout=1.0,
        )

        self.assertEqual(
            result,
            CommandResult(
                success=False,
                reason=CommandFailure.NOT_FOUND,
                exit_code=None,
            ),
        )

    async def test_maps_other_launch_os_errors(self):
        with patch(
            "input_action_controller.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=PermissionError("denied")),
        ):
            result = await self.runner.run(("/not/executable",), timeout=1.0)

        self.assertEqual(
            result,
            CommandResult(
                success=False,
                reason=CommandFailure.LAUNCH_FAILED,
                exit_code=None,
            ),
        )

    async def test_discards_large_stdout_and_stderr(self):
        argv, _ = self.helper_command("flood", "flood.pid")

        result = await self.runner.run(argv, timeout=2.0)

        self.assertTrue(result.success)

    async def test_timeout_kills_parent_and_child_that_ignore_sigterm(self):
        argv, pid_file = self.helper_command("tree-ignore-term", "timeout-tree.pid")

        task = asyncio.create_task(self.runner.run(argv, timeout=1.0))
        parent_pid = await self.wait_for_pid(pid_file)
        child_pid = await self.wait_for_pid(Path(f"{pid_file}.child"))
        result = await task

        self.assertEqual(result.reason, CommandFailure.TIMEOUT)
        await self.assert_process_tree_terminated(parent_pid, child_pid)

    async def test_timeout_kills_child_after_parent_exits(self):
        argv, pid_file = self.helper_command(
            "tree-child-ignore-term", "surviving-child.pid"
        )

        task = asyncio.create_task(self.runner.run(argv, timeout=1.0))
        parent_pid = await self.wait_for_pid(pid_file)
        child_pid = await self.wait_for_pid(Path(f"{pid_file}.child"))
        result = await task

        self.assertEqual(result.reason, CommandFailure.TIMEOUT)
        await self.assert_process_tree_terminated(parent_pid, child_pid)

    async def test_cancellation_kills_process_group_and_propagates(self):
        argv, pid_file = self.helper_command("tree-ignore-term", "cancel-tree.pid")
        task = asyncio.create_task(self.runner.run(argv, timeout=10.0))
        parent_pid = await self.wait_for_pid(pid_file)
        child_pid = await self.wait_for_pid(Path(f"{pid_file}.child"))

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await self.assert_process_tree_terminated(parent_pid, child_pid)

    async def test_repeated_cancellation_finishes_process_group_cleanup(self):
        argv, pid_file = self.helper_command(
            "tree-ignore-term", "repeat-cancel-tree.pid"
        )
        task = asyncio.create_task(self.runner.run(argv, timeout=10.0))
        parent_pid = await self.wait_for_pid(pid_file)
        child_pid = await self.wait_for_pid(Path(f"{pid_file}.child"))
        term_sent = asyncio.Event()
        signal_process_group = self.runner._signal_process_group

        def record_signal(process_group, sent_signal):
            signal_process_group(process_group, sent_signal)
            if sent_signal == signal.SIGTERM:
                term_sent.set()

        with patch.object(
            self.runner,
            "_signal_process_group",
            side_effect=record_signal,
        ):
            task.cancel()
            await asyncio.wait_for(term_sent.wait(), timeout=1.0)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)

            with self.assertRaises(asyncio.CancelledError):
                await task

        await self.assert_process_tree_terminated(parent_pid, child_pid)

    async def test_normal_timeout_uses_fixed_grace_and_reaps_direct_child(self):
        process = TermIgnoringProcess()
        runner = CommandRunner(
            RunnerConfig(timeout_seconds=1.0, shutdown_timeout_seconds=0.01)
        )
        signals = []

        def record_signal(process_group, sent_signal):
            self.assertEqual(process_group, process.pid)
            signals.append(sent_signal)
            if sent_signal == signal.SIGKILL:
                process.exited.set()

        started = asyncio.get_running_loop().time()
        with (
            patch(
                "input_action_controller.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch.object(runner, "_signal_process_group", side_effect=record_signal),
            patch.object(runner, "_process_group_exists", return_value=True),
        ):
            result = await runner.run(("command",), timeout=0.01)
        elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual(result.reason, CommandFailure.TIMEOUT)
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(process.reaped)

    async def test_normal_cancellation_uses_fixed_grace_and_reaps_direct_child(self):
        process = TermIgnoringProcess()
        runner = CommandRunner(
            RunnerConfig(timeout_seconds=1.0, shutdown_timeout_seconds=0.01)
        )
        signals = []

        def record_signal(process_group, sent_signal):
            self.assertEqual(process_group, process.pid)
            signals.append(sent_signal)
            if sent_signal == signal.SIGKILL:
                process.exited.set()

        with (
            patch(
                "input_action_controller.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch.object(runner, "_signal_process_group", side_effect=record_signal),
            patch.object(runner, "_process_group_exists", return_value=True),
        ):
            task = asyncio.create_task(runner.run(("command",), timeout=10.0))
            await process.wait_started.wait()
            started = asyncio.get_running_loop().time()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        elapsed = asyncio.get_running_loop().time() - started

        self.assertGreaterEqual(elapsed, 0.45)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(process.reaped)

    async def test_timeout_cleanup_adopts_later_shutdown_deadline(self):
        (
            result,
            signals,
            reaped,
            finished_at,
            deadline,
        ) = await self.run_cleanup_then_start_shutdown(cancel_run=False)

        self.assertEqual(result.reason, CommandFailure.TIMEOUT)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(reaped)
        self.assertLess(finished_at, deadline + 0.1)

    async def test_cancellation_cleanup_adopts_later_shutdown_deadline(self):
        (
            result,
            signals,
            reaped,
            finished_at,
            deadline,
        ) = await self.run_cleanup_then_start_shutdown(cancel_run=True)

        self.assertIsInstance(result, asyncio.CancelledError)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(reaped)
        self.assertLess(finished_at, deadline + 0.1)

    async def test_same_pid_processes_keep_distinct_cleanup_and_tracking(self):
        first = UnreapableProcess()
        second = UnreapableProcess()
        runner = CommandRunner(
            RunnerConfig(timeout_seconds=1.0, shutdown_timeout_seconds=1.0)
        )
        first_cleanup_finished = asyncio.Event()
        second_cleanup_started = asyncio.Event()
        shutdown_joined_second_cleanup = asyncio.Event()
        allow_first_run_finish = asyncio.Event()
        cleanup_requests = []
        signaled_processes = []
        original_await_cleanup = runner._await_cleanup
        original_start_cleanup = runner._start_process_cleanup

        async def coordinated_await_cleanup(cleanup):
            await original_await_cleanup(cleanup)
            if not first_cleanup_finished.is_set():
                first_cleanup_finished.set()
                await allow_first_run_finish.wait()

        def record_cleanup_request(process, deadline):
            cleanup_requests.append(process)
            if (
                process is second
                and sum(request is second for request in cleanup_requests) == 2
            ):
                shutdown_joined_second_cleanup.set()
            return original_start_cleanup(process, deadline)

        def signal_next_process(process_group, sent_signal):
            self.assertEqual(process_group, first.pid)
            if sent_signal != signal.SIGTERM:
                return
            process = (first, second)[len(signaled_processes)]
            signaled_processes.append(process)
            if process is first:
                process.release.set()
            else:
                second_cleanup_started.set()

        tasks = []
        with (
            patch(
                "input_action_controller.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=(first, second)),
            ),
            patch.object(
                runner,
                "_await_cleanup",
                new=coordinated_await_cleanup,
            ),
            patch.object(
                runner,
                "_start_process_cleanup",
                new=record_cleanup_request,
            ),
            patch.object(
                runner,
                "_signal_process_group",
                side_effect=signal_next_process,
            ),
            patch.object(runner, "_process_group_exists", return_value=False),
        ):
            first_run = asyncio.create_task(runner.run(("first",), timeout=0.01))
            tasks.append(first_run)
            await asyncio.wait_for(first_cleanup_finished.wait(), timeout=1.0)

            second_run = asyncio.create_task(runner.run(("second",), timeout=0.01))
            tasks.append(second_run)
            await asyncio.wait_for(second_cleanup_started.wait(), timeout=1.0)

            try:
                shutdown = asyncio.create_task(
                    runner.terminate_active(asyncio.get_running_loop().time() + 0.1)
                )
                tasks.append(shutdown)
                await asyncio.wait_for(
                    shutdown_joined_second_cleanup.wait(), timeout=1.0
                )
                self.assertEqual(
                    [process for process in cleanup_requests if process is second],
                    [second, second],
                )
                self.assertIs(runner._active.get(second.pid), second)
                self.assertIs(runner._cleanups[second.pid].process, second)

                allow_first_run_finish.set()
                first_result = await first_run

                self.assertEqual(first_result.reason, CommandFailure.TIMEOUT)
                self.assertIs(runner._active.get(second.pid), second)
                self.assertIs(runner._cleanups[second.pid].process, second)
                self.assertEqual(signaled_processes, [first, second])
                self.assertTrue(first.reaped)

                second.release.set()
                await shutdown
                second_result = await second_run

                self.assertEqual(second_result.reason, CommandFailure.TIMEOUT)
                self.assertTrue(second.reaped)
                self.assertEqual(runner._active, {})
                self.assertEqual(runner._cleanups, {})
            finally:
                first.release.set()
                second.release.set()
                allow_first_run_finish.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run_cleanup_then_start_shutdown(self, *, cancel_run):
        process = UnreapableProcess()
        runner = CommandRunner(
            RunnerConfig(timeout_seconds=1.0, shutdown_timeout_seconds=1.0)
        )
        signals = []
        term_sent = asyncio.Event()
        shutdown_task = None

        def record_signal(process_group, sent_signal):
            self.assertEqual(process_group, process.pid)
            signals.append(sent_signal)
            if sent_signal == signal.SIGTERM:
                term_sent.set()

        with (
            patch(
                "input_action_controller.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch.object(runner, "_signal_process_group", side_effect=record_signal),
            patch.object(runner, "_process_group_exists", return_value=True),
        ):
            run_task = asyncio.create_task(
                runner.run(
                    ("command",),
                    timeout=10.0 if cancel_run else 0.01,
                )
            )
            await process.wait_started.wait()
            if cancel_run:
                run_task.cancel()
            await asyncio.wait_for(term_sent.wait(), timeout=1.0)

            deadline = asyncio.get_running_loop().time() + 0.05
            shutdown_task = asyncio.create_task(runner.terminate_active(deadline))
            try:
                if cancel_run:
                    try:
                        await asyncio.wait_for(asyncio.shield(run_task), timeout=0.2)
                    except asyncio.CancelledError as error:
                        result = error
                else:
                    result = await asyncio.wait_for(
                        asyncio.shield(run_task),
                        timeout=0.2,
                    )
                await asyncio.wait_for(shutdown_task, timeout=0.2)
                finished_at = asyncio.get_running_loop().time()
                reaped = process.reaped
            finally:
                process.release.set()
                with suppress(asyncio.CancelledError):
                    await run_task
                if shutdown_task is not None:
                    with suppress(asyncio.CancelledError):
                        await shutdown_task

        return result, signals, reaped, finished_at, deadline

    async def test_global_deadline_bounds_execution(self):
        argv, pid_file = self.helper_command("tree-ignore-term", "deadline-tree.pid")
        deadline = asyncio.get_running_loop().time() + 1.0

        task = asyncio.create_task(
            self.runner.run(argv, timeout=10.0, deadline=deadline)
        )
        parent_pid = await self.wait_for_pid(pid_file)
        child_pid = await self.wait_for_pid(Path(f"{pid_file}.child"))
        result = await task

        self.assertEqual(result.reason, CommandFailure.TIMEOUT)
        self.assertLess(asyncio.get_running_loop().time(), deadline + 0.25)
        await self.assert_process_tree_terminated(parent_pid, child_pid)

    async def test_launches_without_shell_in_new_session_with_devnull_streams(self):
        process = FinishedProcess()
        create_subprocess = AsyncMock(return_value=process)

        with patch(
            "input_action_controller.runner.asyncio.create_subprocess_exec",
            new=create_subprocess,
        ):
            result = await self.runner.run(("command", "argument"), timeout=1.0)

        self.assertTrue(result.success)
        create_subprocess.assert_awaited_once_with(
            "command",
            "argument",
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertNotIn("shell", create_subprocess.await_args.kwargs)

    async def test_terminate_active_bounds_final_wait_by_deadline(self):
        process = StuckProcess()
        create_subprocess = AsyncMock(return_value=process)
        kill_signals = []
        runner = CommandRunner(
            RunnerConfig(timeout_seconds=1.0, shutdown_timeout_seconds=0.01)
        )

        def record_killpg(process_group, sent_signal):
            self.assertEqual(process_group, process.pid)
            kill_signals.append(sent_signal)

        with (
            patch(
                "input_action_controller.runner.asyncio.create_subprocess_exec",
                new=create_subprocess,
            ),
            patch(
                "input_action_controller.runner.os.killpg", side_effect=record_killpg
            ),
        ):
            run_task = asyncio.create_task(runner.run(("command",), timeout=10.0))
            await process.wait_started.wait()
            deadline = asyncio.get_running_loop().time() + 0.05

            await runner.terminate_active(deadline)

            self.assertLess(asyncio.get_running_loop().time(), deadline + 0.1)
            self.assertIn(signal.SIGTERM, kill_signals)
            self.assertIn(0, kill_signals)
            self.assertIn(signal.SIGKILL, kill_signals)
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task

    async def test_terminate_active_waits_for_launch_and_rejects_its_command(self):
        process = FinishedProcess()
        launch_started = asyncio.Event()
        launch_allowed = asyncio.Event()
        kill_signals = []

        async def delayed_launch(*args, **kwargs):
            launch_started.set()
            await launch_allowed.wait()
            return process

        def record_killpg(process_group, sent_signal):
            self.assertEqual(process_group, process.pid)
            kill_signals.append(sent_signal)

        with (
            patch(
                "input_action_controller.runner.asyncio.create_subprocess_exec",
                new=delayed_launch,
            ),
            patch(
                "input_action_controller.runner.os.killpg", side_effect=record_killpg
            ),
        ):
            run_task = asyncio.create_task(self.runner.run(("command",), timeout=10.0))
            await launch_started.wait()
            terminate_task = asyncio.create_task(
                self.runner.terminate_active(asyncio.get_running_loop().time() + 1.0)
            )

            try:
                await asyncio.sleep(0)
                self.assertFalse(terminate_task.done())
            finally:
                launch_allowed.set()
                await terminate_task

            result = await run_task

        self.assertEqual(
            result,
            CommandResult(False, CommandFailure.LAUNCH_FAILED, None),
        )
        self.assertIn(signal.SIGTERM, kill_signals)

    async def test_cancelled_shutdown_rejected_launch_clears_cleanup_bookkeeping(self):
        process = UnreapableProcess()
        launch_started = asyncio.Event()
        launch_allowed = asyncio.Event()
        term_sent = asyncio.Event()
        runner = CommandRunner(
            RunnerConfig(timeout_seconds=1.0, shutdown_timeout_seconds=1.0)
        )

        async def delayed_launch(*args, **kwargs):
            launch_started.set()
            await launch_allowed.wait()
            return process

        def record_signal(process_group, sent_signal):
            self.assertEqual(process_group, process.pid)
            if sent_signal == signal.SIGTERM:
                term_sent.set()

        with (
            patch(
                "input_action_controller.runner.asyncio.create_subprocess_exec",
                new=delayed_launch,
            ),
            patch.object(runner, "_signal_process_group", side_effect=record_signal),
            patch.object(runner, "_process_group_exists", return_value=True),
        ):
            run_task = asyncio.create_task(runner.run(("command",), timeout=10.0))
            await launch_started.wait()
            terminate_task = asyncio.create_task(
                runner.terminate_active(asyncio.get_running_loop().time() + 0.05)
            )
            await asyncio.sleep(0)
            launch_allowed.set()
            await asyncio.wait_for(term_sent.wait(), timeout=1.0)

            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task
            await terminate_task

        process.release.set()
        self.assertEqual(runner._active, {})
        self.assertEqual(runner._cleanups, {})

    async def test_run_is_rejected_after_runner_shutdown(self):
        create_subprocess = AsyncMock(return_value=FinishedProcess())
        await self.runner.terminate_active(asyncio.get_running_loop().time() + 1.0)

        with patch(
            "input_action_controller.runner.asyncio.create_subprocess_exec",
            new=create_subprocess,
        ):
            result = await self.runner.run(("command",), timeout=1.0)

        self.assertEqual(
            result,
            CommandResult(False, CommandFailure.LAUNCH_FAILED, None),
        )
        create_subprocess.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

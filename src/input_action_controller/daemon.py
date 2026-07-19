import asyncio
import logging
import signal
from typing import Callable

from .actions import ActionController
from .devices.discovery import DeviceDiscovery
from .devices.manager import DeviceManager, SourceFactory, _default_source_factory
from .locking import LockContendedError, RuntimeLock, RuntimeLockError
from .models import AppConfig
from .runner import CommandRunner


LOGGER = logging.getLogger(__name__)
SUCCESS = 0
RUNTIME_FAILURE = 1
LOCK_CONTENTION = 3
_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)
DEFAULT_SOURCE_FACTORY = _default_source_factory


async def run_daemon(
    config: AppConfig,
    *,
    lock_factory: Callable[[], RuntimeLock] | None = None,
    runner_factory: Callable | None = None,
    controller_factory: Callable | None = None,
    discovery_factory: Callable | None = None,
    manager_factory: Callable | None = None,
    source_factory: SourceFactory | None = None,
    loop=None,
) -> int:
    lock_factory = lock_factory or RuntimeLock
    runner_factory = runner_factory or CommandRunner
    controller_factory = controller_factory or ActionController
    discovery_factory = discovery_factory or DeviceDiscovery
    manager_factory = manager_factory or DeviceManager
    signal_loop = loop or asyncio.get_running_loop()

    try:
        lock = lock_factory()
        lock.acquire()
    except LockContendedError:
        return LOCK_CONTENTION
    except RuntimeLockError as error:
        LOGGER.error("cannot start daemon: %s", error)
        return RUNTIME_FAILURE

    result = SUCCESS
    runner = None
    controllers = []
    manager_task: asyncio.Task[None] | None = None
    manager_result_collected = False
    shutdown_waiter: asyncio.Task[bool] | None = None
    registered_signals = []
    stop_event = asyncio.Event()
    deadline: float | None = None

    def request_shutdown() -> None:
        nonlocal deadline
        if deadline is None:
            deadline = signal_loop.time() + config.runner.shutdown_timeout_seconds
        stop_event.set()

    try:
        runner = runner_factory(config.runner)
        for action_config in config.actions:
            controller = controller_factory(
                action_config,
                runner,
                config.runner.timeout_seconds,
            )
            controllers.append(controller)
        for controller in controllers:
            controller.start()

        discovery = discovery_factory()
        actions = {controller.name: controller for controller in controllers}
        manager_options = {
            "strategy": config.device_selection.strategy,
            "source_factory": source_factory or DEFAULT_SOURCE_FACTORY,
        }
        manager = manager_factory(
            config.devices,
            actions,
            discovery,
            **manager_options,
        )

        for sent_signal in _SHUTDOWN_SIGNALS:
            signal_loop.add_signal_handler(sent_signal, request_shutdown)
            registered_signals.append(sent_signal)

        manager_task = asyncio.create_task(
            manager.run(
                stop_event,
                shutdown_deadline=lambda: deadline,
            ),
            name="device-manager",
        )
        shutdown_waiter = asyncio.create_task(
            stop_event.wait(),
            name="daemon:shutdown-signal",
        )
        await asyncio.wait(
            (manager_task, shutdown_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if manager_task.done():
            try:
                await manager_task
            except Exception:
                LOGGER.exception("device manager failed")
                result = RUNTIME_FAILURE
            finally:
                manager_result_collected = True
                if not stop_event.is_set():
                    result = RUNTIME_FAILURE
                    request_shutdown()
    except Exception:
        LOGGER.exception("daemon runtime failed")
        result = RUNTIME_FAILURE
        request_shutdown()
    finally:
        try:
            request_shutdown()

            controller_shutdown_tasks = [
                asyncio.create_task(
                    controller.shutdown(deadline),
                    name=f"action-shutdown:{controller.name}",
                )
                for controller in controllers
            ]
            if controller_shutdown_tasks:
                await asyncio.sleep(0)

            if manager_task is not None and not manager_result_collected:
                try:
                    await _finish_manager_task(manager_task, deadline)
                except Exception:
                    LOGGER.exception("device manager failed during shutdown")
                    result = RUNTIME_FAILURE
                manager_result_collected = True

            if shutdown_waiter is not None and not shutdown_waiter.done():
                shutdown_waiter.cancel()
                try:
                    await shutdown_waiter
                except asyncio.CancelledError:
                    pass

            shutdown_results = await asyncio.gather(
                *controller_shutdown_tasks,
                return_exceptions=True,
            )
            for controller, shutdown_result in zip(controllers, shutdown_results):
                if isinstance(shutdown_result, BaseException):
                    LOGGER.error(
                        "action shutdown failed for %s: %s",
                        controller.name,
                        shutdown_result,
                    )
                    result = RUNTIME_FAILURE

            if runner is not None:
                try:
                    await runner.terminate_active(deadline)
                except Exception:
                    LOGGER.exception("command runner termination failed")
                    result = RUNTIME_FAILURE
        finally:
            try:
                for sent_signal in registered_signals:
                    signal_loop.remove_signal_handler(sent_signal)
            finally:
                lock.release()

    return result


async def _finish_manager_task(
    manager_task: asyncio.Task[None],
    deadline: float,
) -> None:
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(manager_task, timeout=remaining)
    except TimeoutError:
        manager_task.cancel()
        try:
            await manager_task
        except asyncio.CancelledError:
            pass

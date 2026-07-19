import errno
import fcntl
import os
from pathlib import Path


LOCK_FILENAME = "input-action-controller.lock"


class RuntimeLockError(RuntimeError):
    pass


class LockContendedError(RuntimeLockError):
    def __init__(self, path: Path, owner_pid: int | None):
        self.path = path
        self.owner_pid = owner_pid
        owner = str(owner_pid) if owner_pid is not None else "unknown"
        super().__init__(f"runtime lock is held by PID {owner}: {path}")


class RuntimeLock:
    def __init__(self, runtime_dir: Path | None = None):
        if runtime_dir is None:
            raw_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
            if not raw_runtime_dir:
                raise RuntimeLockError("XDG_RUNTIME_DIR is not set")
            runtime_dir = Path(raw_runtime_dir)
        self.path = runtime_dir / LOCK_FILENAME
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> "RuntimeLock":
        if self._descriptor is not None:
            return self

        try:
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                0o600,
            )
        except OSError as error:
            raise RuntimeLockError(
                f"cannot open runtime lock {self.path}: {error}"
            ) from error

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            owner_pid = self._read_owner_pid(descriptor)
            os.close(descriptor)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise LockContendedError(self.path, owner_pid) from error
            raise RuntimeLockError(
                f"cannot acquire runtime lock {self.path}: {error}"
            ) from error

        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except OSError as error:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise RuntimeLockError(
                f"cannot record runtime lock owner in {self.path}: {error}"
            ) from error

        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None

        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "RuntimeLock":
        return self.acquire()

    def __exit__(self, exception_type, exception, traceback) -> None:
        self.release()

    @staticmethod
    def _read_owner_pid(descriptor: int) -> int | None:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            value = os.read(descriptor, 64).decode("ascii").strip()
            return int(value)
        except (OSError, UnicodeDecodeError, ValueError):
            return None

import fcntl
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from input_action_controller.locking import LockContendedError, RuntimeLock


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime_dir = Path(self.directory.name)
        self.environment = patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": str(self.runtime_dir)},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_second_owner_is_rejected_with_first_owner_pid(self):
        first = RuntimeLock()
        self.addCleanup(first.release)
        first.acquire()

        second = RuntimeLock()
        with self.assertRaises(LockContendedError) as raised:
            second.acquire()

        self.assertEqual(raised.exception.owner_pid, os.getpid())
        self.assertEqual(first.path.read_text(encoding="ascii"), f"{os.getpid()}\n")

    def test_stale_lock_file_is_overwritten_without_replacing_inode(self):
        path = self.runtime_dir / "input-action-controller.lock"
        path.write_text("999999\n", encoding="ascii")
        inode = path.stat().st_ino

        lock = RuntimeLock()
        self.addCleanup(lock.release)
        lock.acquire()

        self.assertEqual(path.read_text(encoding="ascii"), f"{os.getpid()}\n")
        self.assertEqual(path.stat().st_ino, inode)

    def test_release_keeps_inode_and_next_owner_overwrites_pid(self):
        lock = RuntimeLock()
        with patch(
            "input_action_controller.locking.os.getpid",
            return_value=111,
        ):
            lock.acquire()
        path = lock.path
        inode = path.stat().st_ino

        lock.release()

        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.read_text(encoding="ascii"), "111\n")

        replacement = RuntimeLock()
        with patch(
            "input_action_controller.locking.os.getpid",
            return_value=222,
        ):
            replacement.acquire()
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.read_text(encoding="ascii"), "222\n")
        replacement.release()
        self.assertTrue(path.exists())

    def test_second_owner_cannot_acquire_before_first_owner_unlocks(self):
        first = RuntimeLock()
        second = RuntimeLock()
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        first.acquire()
        real_flock = fcntl.flock
        interleaved = []

        def flock_with_interleaving(descriptor, operation):
            if operation == fcntl.LOCK_UN and not interleaved:
                interleaved.append(True)
                with self.assertRaises(LockContendedError):
                    second.acquire()
            return real_flock(descriptor, operation)

        with patch(
            "input_action_controller.locking.fcntl.flock",
            side_effect=flock_with_interleaving,
        ):
            first.release()

        self.assertEqual(interleaved, [True])
        second.acquire()

    def test_context_manager_releases_after_exception(self):
        lock = RuntimeLock()

        with self.assertRaisesRegex(RuntimeError, "failed"):
            with lock:
                self.assertTrue(lock.path.exists())
                raise RuntimeError("failed")

        self.assertTrue(lock.path.exists())


if __name__ == "__main__":
    unittest.main()

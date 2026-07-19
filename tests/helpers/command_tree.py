import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main() -> int:
    mode, pid_file = sys.argv[1:3]
    Path(pid_file).write_text(str(os.getpid()), encoding="ascii")

    if mode == "success":
        return 0
    if mode == "flood":
        chunk = b"x" * 65536
        for _ in range(8):
            os.write(1, chunk)
            os.write(2, chunk)
        return 0
    if mode == "tree-ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(
            [sys.executable, __file__, "leaf-ignore-term", pid_file + ".child"]
        )
        child.wait()
        return child.returncode
    if mode == "tree-child-ignore-term":
        child = subprocess.Popen(
            [sys.executable, __file__, "leaf-ignore-term", pid_file + ".child"]
        )
        child.wait()
        return child.returncode
    if mode == "leaf-ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

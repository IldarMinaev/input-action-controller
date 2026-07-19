import argparse
import asyncio
import os
from pathlib import Path
import sys
import tomllib
from typing import Sequence

from .config import ConfigError, load_config
from .daemon import run_daemon
from .diagnostics import run_devices, run_monitor, run_status
from .setup.session import run_setup


SUCCESS = 0
RUNTIME_FAILURE = 1
USAGE_FAILURE = 2
LOCK_CONTENTION = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="input-action-controller")
    parser.add_argument(
        "--config",
        type=Path,
        help="read configuration from PATH",
        metavar="PATH",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon", help="run the input action service")
    commands.add_parser("status", help="show runtime and device status")
    commands.add_parser("config-check", help="validate configuration")
    commands.add_parser("devices", help="list candidate input devices")
    commands.add_parser("setup", help="create or extend an interactive configuration")
    monitor = commands.add_parser("monitor", help="monitor one configured device")
    monitor.add_argument("--device", required=True, metavar="NAME")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code)

    if arguments.command == "devices":
        try:
            return run_devices()
        except Exception as error:
            print(f"runtime failure: {error}", file=sys.stderr)
            return RUNTIME_FAILURE

    if arguments.command == "setup":
        if os.geteuid() == 0:
            print("setup refuses to run as root", file=sys.stderr)
            return USAGE_FAILURE
        try:
            return run_setup(arguments.config)
        except Exception as error:
            print(f"runtime failure: {error}", file=sys.stderr)
            return RUNTIME_FAILURE

    try:
        config = load_config(arguments.config)
    except (ConfigError, OSError, tomllib.TOMLDecodeError) as error:
        print(f"configuration: invalid ({error})", file=sys.stderr)
        return USAGE_FAILURE

    try:
        if arguments.command == "daemon":
            return asyncio.run(run_daemon(config))
        if arguments.command == "status":
            return run_status(config)
        if arguments.command == "config-check":
            print("configuration: valid")
            return SUCCESS
        if arguments.command == "monitor":
            return run_monitor(config, arguments.device)
    except Exception as error:
        print(f"runtime failure: {error}", file=sys.stderr)
        return RUNTIME_FAILURE
    raise AssertionError(f"unhandled command: {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())

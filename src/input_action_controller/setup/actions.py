from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
import shlex
from typing import Callable

import tomlkit


_SHELL_OPERATOR = re.compile(r"[;&|<>`]")
_SHELL_EXPANSION = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{|\()")


class ActionCommandStyle(StrEnum):
    SEPARATE = "separate"
    TOGGLE = "toggle"


@dataclass(frozen=True)
class ActionDraft:
    name: str
    on_command: tuple[str, ...]
    off_command: tuple[str, ...]
    skip_off_after_failed_on: bool | None = None
    skip_on_after_failed_off: bool | None = None
    off_on_shutdown: bool = True
    command_style: ActionCommandStyle = ActionCommandStyle.SEPARATE

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("action name must not be empty")
        if not self.on_command or not self.off_command:
            raise ValueError("action command must not be empty")

        toggle_command = self.command_style is ActionCommandStyle.TOGGLE
        if self.skip_off_after_failed_on is None:
            object.__setattr__(self, "skip_off_after_failed_on", toggle_command)
        if self.skip_on_after_failed_off is None:
            object.__setattr__(self, "skip_on_after_failed_off", toggle_command)


def parse_command_line(
    text: str, *, which: Callable[[str], str | None]
) -> tuple[str, ...]:
    """Parse a command into argv after rejecting shell-only syntax."""
    if not text.strip():
        raise ValueError("command must not be empty")
    if (
        "\n" in text
        or "\r" in text
        or _SHELL_OPERATOR.search(text)
        or _SHELL_EXPANSION.search(text)
    ):
        raise ValueError("command must not contain shell syntax")

    try:
        argv = tuple(shlex.split(text, posix=True))
    except ValueError as error:
        raise ValueError(f"invalid command line: {error}") from error

    if not argv:
        raise ValueError("command must not be empty")
    if which(argv[0]) is None:
        raise ValueError(f"command executable is not available: {argv[0]}")
    return argv


def apply_action_draft(document: tomlkit.TOMLDocument, draft: ActionDraft) -> None:
    """Add a draft action to a TomlKit document without rebuilding it."""
    actions = document.get("actions")
    if actions is None:
        actions = tomlkit.table()
        document["actions"] = actions
    if not isinstance(actions, dict):
        raise ValueError("actions must be a TOML table")
    if draft.name in actions:
        raise ValueError(f"duplicate action name: {draft.name}")

    action = tomlkit.table()
    on_command = tomlkit.array()
    on_command.extend(draft.on_command)
    off_command = tomlkit.array()
    off_command.extend(draft.off_command)
    action["on_command"] = on_command
    action["off_command"] = off_command
    action["skip_off_after_failed_on"] = draft.skip_off_after_failed_on
    action["skip_on_after_failed_off"] = draft.skip_on_after_failed_off
    action["off_on_shutdown"] = draft.off_on_shutdown
    actions[draft.name] = action

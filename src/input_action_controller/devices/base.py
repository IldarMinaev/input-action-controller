from dataclasses import dataclass
from threading import Event
from typing import Callable, Protocol

from ..models import ActionRequest


@dataclass(frozen=True)
class MatchedInput:
    request: ActionRequest
    source: str
    toggle_timeout_seconds: float | None


class DeviceEventSource(Protocol):
    def run(self, stop: Event, emit: Callable[[MatchedInput], None]) -> None: ...


class DeviceReadError(RuntimeError):
    def __init__(self, profile_name: str, node: str, reason: str):
        self.profile_name = profile_name
        self.node = node
        self.reason = reason
        super().__init__(f"cannot read {profile_name} at {node}: {reason}")

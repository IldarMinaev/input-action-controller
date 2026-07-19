from evdev import ecodes

from ..models import ActionRequest, EvdevProfile, HidrawProfile
from .base import MatchedInput


class HidrawMatcher:
    def __init__(self, profile: HidrawProfile):
        self._profile = profile
        self._last_request: ActionRequest | None = None

    def match(self, report: bytes) -> MatchedInput | None:
        if report in self._profile.on_reports:
            request = ActionRequest.ON
        elif report in self._profile.off_reports:
            request = ActionRequest.OFF
        else:
            return None

        if request == self._last_request:
            return None
        self._last_request = request
        return MatchedInput(request, self._profile.name, None)


class EvdevMatcher:
    def __init__(self, profile: EvdevProfile):
        self._profile = profile
        self._on_codes = {ecodes.ecodes[name] for name in profile.on_events}
        self._off_codes = {ecodes.ecodes[name] for name in profile.off_events}
        self._toggle_codes = {ecodes.ecodes[name] for name in profile.toggle_events}

    def match(self, event_type: int, code: int, value: int) -> MatchedInput | None:
        if event_type != ecodes.EV_KEY or value != 1:
            return None
        if code in self._on_codes:
            return MatchedInput(ActionRequest.ON, self._profile.name, None)
        if code in self._off_codes:
            return MatchedInput(ActionRequest.OFF, self._profile.name, None)
        if code in self._toggle_codes:
            return MatchedInput(
                ActionRequest.TOGGLE,
                self._profile.name,
                self._profile.toggle_off_timeout_seconds,
            )
        return None

import unittest

from evdev import ecodes

from input_action_controller.models import ActionRequest, EvdevProfile, HidrawProfile


def hidraw_profile() -> HidrawProfile:
    return HidrawProfile(
        name="Desk headset",
        action="voice_input",
        vendor_id="047f",
        product_id="c056",
        on_reports=(bytes.fromhex("08 02"),),
        off_reports=(bytes.fromhex("08 00"),),
    )


def evdev_on_off_profile() -> EvdevProfile:
    return EvdevProfile(
        name="Desk button",
        action="voice_input",
        vendor_id="1234",
        product_id="5678",
        mode="on-off",
        on_events=("BTN_SIDE",),
        off_events=("BTN_EXTRA",),
    )


def evdev_toggle_profile() -> EvdevProfile:
    return EvdevProfile(
        name="Desk button",
        action="voice_input",
        vendor_id="1234",
        product_id="5678",
        mode="toggle",
        toggle_events=("BTN_SIDE",),
        toggle_off_timeout_seconds=12.5,
    )


class HidrawMatcherTests(unittest.TestCase):
    def test_matches_exact_complete_on_and_off_reports(self):
        from input_action_controller.devices.base import MatchedInput
        from input_action_controller.devices.matchers import HidrawMatcher

        matcher = HidrawMatcher(hidraw_profile())

        self.assertEqual(
            matcher.match(bytes.fromhex("08 02")),
            MatchedInput(ActionRequest.ON, "Desk headset", None),
        )
        self.assertEqual(
            matcher.match(bytes.fromhex("08 00")),
            MatchedInput(ActionRequest.OFF, "Desk headset", None),
        )

    def test_rejects_unknown_and_partial_reports(self):
        from input_action_controller.devices.matchers import HidrawMatcher

        matcher = HidrawMatcher(hidraw_profile())

        self.assertIsNone(matcher.match(bytes.fromhex("08")))
        self.assertIsNone(matcher.match(bytes.fromhex("08 02 00")))
        self.assertIsNone(matcher.match(bytes.fromhex("08 01")))

    def test_suppresses_repeated_state_until_opposite_state(self):
        from input_action_controller.devices.base import MatchedInput
        from input_action_controller.devices.matchers import HidrawMatcher

        matcher = HidrawMatcher(hidraw_profile())

        self.assertEqual(
            matcher.match(bytes.fromhex("08 02")),
            MatchedInput(ActionRequest.ON, "Desk headset", None),
        )
        self.assertIsNone(matcher.match(bytes.fromhex("08 02")))
        self.assertEqual(
            matcher.match(bytes.fromhex("08 00")),
            MatchedInput(ActionRequest.OFF, "Desk headset", None),
        )
        self.assertIsNone(matcher.match(bytes.fromhex("08 00")))
        self.assertEqual(
            matcher.match(bytes.fromhex("08 02")),
            MatchedInput(ActionRequest.ON, "Desk headset", None),
        )


class EvdevMatcherTests(unittest.TestCase):
    def test_matches_configured_on_and_off_key_presses(self):
        from input_action_controller.devices.base import MatchedInput
        from input_action_controller.devices.matchers import EvdevMatcher

        matcher = EvdevMatcher(evdev_on_off_profile())

        self.assertEqual(
            matcher.match(ecodes.EV_KEY, ecodes.BTN_SIDE, 1),
            MatchedInput(ActionRequest.ON, "Desk button", None),
        )
        self.assertEqual(
            matcher.match(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1),
            MatchedInput(ActionRequest.OFF, "Desk button", None),
        )

    def test_rejects_non_press_and_unconfigured_evdev_events(self):
        from input_action_controller.devices.matchers import EvdevMatcher

        matcher = EvdevMatcher(evdev_on_off_profile())

        self.assertIsNone(matcher.match(ecodes.EV_KEY, ecodes.BTN_SIDE, 0))
        self.assertIsNone(matcher.match(ecodes.EV_KEY, ecodes.BTN_SIDE, 2))
        self.assertIsNone(matcher.match(ecodes.EV_REL, ecodes.REL_X, 1))
        self.assertIsNone(matcher.match(ecodes.EV_KEY, ecodes.BTN_MIDDLE, 1))

    def test_toggle_press_includes_profile_timeout(self):
        from input_action_controller.devices.base import MatchedInput
        from input_action_controller.devices.matchers import EvdevMatcher

        matcher = EvdevMatcher(evdev_toggle_profile())

        self.assertEqual(
            matcher.match(ecodes.EV_KEY, ecodes.BTN_SIDE, 1),
            MatchedInput(ActionRequest.TOGGLE, "Desk button", 12.5),
        )


if __name__ == "__main__":
    unittest.main()

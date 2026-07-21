from dataclasses import replace
import unittest

from input_action_controller.devices.discovery import DeviceCandidate
from input_action_controller.models import EvdevProfile
from input_action_controller.setup.profiles import compatible_profiles


class CompatibleProfilesTests(unittest.TestCase):
    def test_matches_persisted_classifier_and_stable_identity(self):
        profile = EvdevProfile(
            name="Mouse button",
            action="voice",
            vendor_id="2717",
            product_id="5070",
            interface_number="00",
            input_classifier="ID_INPUT_MOUSE",
            mode="toggle",
            toggle_events=("BTN_SIDE",),
        )
        mouse = DeviceCandidate(
            "/dev/input/event4",
            "input",
            {
                "ID_VENDOR_ID": "2717",
                "ID_MODEL_ID": "5070",
                "ID_USB_INTERFACE_NUM": "00",
                "ID_INPUT_MOUSE": "1",
            },
            frozenset(),
            False,
        )
        keyboard = replace(
            mouse,
            node="/dev/input/event5",
            properties={**mouse.properties, "ID_INPUT_MOUSE": "0"},
        )

        self.assertEqual(compatible_profiles((profile,), mouse), (profile,))
        self.assertEqual(compatible_profiles((profile,), keyboard), ())


if __name__ == "__main__":
    unittest.main()

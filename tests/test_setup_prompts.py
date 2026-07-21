from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
import unittest

import tomlkit

from input_action_controller.setup.config_editor import BackupInfo
from input_action_controller.setup.permissions import render_rule
from input_action_controller.setup.permissions import INPUT_GROUP_ACCESS
from input_action_controller.setup.prompts import ConsoleSetupPrompts
from input_action_controller.setup.service import ServiceChoice, ServiceSnapshot
from input_action_controller.setup.session import SetupCancelled, SetupExitRequested
from tests.test_setup_session import FakePermissionTransaction, candidate


class ConsoleSetupPromptsTests(unittest.TestCase):
    def test_profile_operation_offers_add_and_exact_updates(self):
        prompts, output = self.prompts("2\n")

        result = prompts.choose_profile_operation(("Mouse button", "Headset"))

        self.assertEqual(result, "Mouse button")
        self.assertIn("1. Add a new profile", output.getvalue())
        self.assertIn("2. Update Mouse button", output.getvalue())

    def prompts(self, input_text: str):
        output = StringIO()
        return (
            ConsoleSetupPrompts(
                input_stream=StringIO(input_text),
                output=output,
                which=lambda executable: executable,
            ),
            output,
        )

    def test_action_entry_displays_exact_argv_arrays(self):
        prompts, output = self.prompts(
            "voice\n1\n/usr/bin/voice --on 'two words'\n/usr/bin/voice --off\n\n"
        )
        editable = type(
            "Editable",
            (),
            {"document": tomlkit.document()},
        )()

        draft = prompts.choose_action(editable)

        self.assertEqual(
            draft.on_command,
            ("/usr/bin/voice", "--on", "two words"),
        )
        self.assertIn(
            "['/usr/bin/voice', '--on', 'two words']",
            output.getvalue(),
        )
        self.assertIn(
            "['/usr/bin/voice', '--off']",
            output.getvalue(),
        )

    def test_permission_review_shows_rule_scope_commands_and_defaults_sudo_to_no(self):
        selected = candidate(
            "/dev/input/event4",
            readable=False,
            keyboard_class=False,
        )
        rendered = render_rule(
            "Keyboard button",
            __import__(
                "input_action_controller.setup.devices",
                fromlist=["SelectorDraft"],
            ).SelectorDraft(
                transport="evdev",
                vendor_id="1234",
                product_id="5678",
                interface_number="01",
                classifier=("ID_INPUT_MOUSE", "1"),
            ),
            (selected,),
        )
        transaction = FakePermissionTransaction(rendered)
        prompts, output = self.prompts("n\n")

        result = prompts.confirm_permission("Keyboard button", transaction)

        report = output.getvalue()
        self.assertFalse(result)
        self.assertIn(rendered.content.strip(), report)
        self.assertIn("/dev/input/event4", report)
        self.assertIn(rendered.scope.future_predicate, report)
        self.assertIn("event_codes", report)
        self.assertIn(transaction.preview_command_lines[0], report)
        self.assertEqual(
            report.count("Install this rule with sudo? [y/N/x]"),
            1,
        )

    def test_permission_review_shows_sudo_prompt_once_for_approved_keyboard_scope(self):
        selected = candidate(
            "/dev/input/event4",
            readable=False,
            keyboard_class=True,
        )
        rendered = render_rule(
            "Keyboard button",
            __import__(
                "input_action_controller.setup.devices",
                fromlist=["SelectorDraft"],
            ).SelectorDraft(
                transport="evdev",
                vendor_id="1234",
                product_id="5678",
                interface_number="01",
                classifier=("ID_INPUT_MOUSE", "1"),
            ),
            (selected,),
        )
        transaction = FakePermissionTransaction(rendered)
        prompts, output = self.prompts("Keyboard button\ny\n")

        result = prompts.confirm_permission("Keyboard button", transaction)

        self.assertTrue(result)
        self.assertEqual(
            output.getvalue().count("Install this rule with sudo? [y/N/x]"),
            1,
        )

    def test_permission_review_shows_no_sudo_prompt_for_rejected_keyboard_scope(self):
        selected = candidate(
            "/dev/input/event4",
            readable=False,
            keyboard_class=True,
        )
        rendered = render_rule(
            "Keyboard button",
            __import__(
                "input_action_controller.setup.devices",
                fromlist=["SelectorDraft"],
            ).SelectorDraft(
                transport="evdev",
                vendor_id="1234",
                product_id="5678",
                interface_number="01",
                classifier=("ID_INPUT_MOUSE", "1"),
            ),
            (selected,),
        )
        transaction = FakePermissionTransaction(rendered)
        prompts, output = self.prompts("wrong\n")

        result = prompts.confirm_permission("Keyboard button", transaction)

        self.assertFalse(result)
        self.assertEqual(
            output.getvalue().count("Install this rule with sudo? [y/N/x]"),
            0,
        )

    def test_readable_device_managed_permission_defaults_to_no(self):
        readable_candidate = candidate("/dev/input/event4")
        prompts, output = self.prompts("\n")

        result = prompts.confirm_managed_permission("Desk button", readable_candidate)

        self.assertFalse(result)
        self.assertIn(
            "/dev/input/event4 is readable through existing permissions.",
            output.getvalue(),
        )
        self.assertIn(
            "Create a managed permission rule for this readable device? [y/N/x] ",
            output.getvalue(),
        )

    def test_confirmation_retries_wrong_layout_and_accepts_case_insensitive_yes(self):
        prompts, output = self.prompts("н\nYES\n")

        result = prompts.confirm_managed_permission(
            "Desk button",
            candidate("/dev/input/event4"),
        )

        self.assertTrue(result)
        self.assertIn("Enter y/yes, n/no, or x to cancel.\n", output.getvalue())
        self.assertEqual(output.getvalue().count("[y/N/x] "), 2)

    def test_confirmation_accepts_case_insensitive_no(self):
        prompts, _output = self.prompts("NO\n")

        result = prompts.confirm_managed_permission(
            "Desk button",
            candidate("/dev/input/event4"),
        )

        self.assertFalse(result)

    def test_confirmation_x_requests_setup_exit(self):
        prompts, _output = self.prompts("X\n")

        with self.assertRaisesRegex(SetupExitRequested, "Cancelled by user"):
            prompts.confirm_managed_permission(
                "Desk button",
                candidate("/dev/input/event4"),
            )

    def test_exact_profile_name_approval_has_no_yes_no_suffix(self):
        prompts, output = self.prompts("Desk button\n")

        self.assertTrue(
            prompts.confirm_keyboard_capture(
                "Desk button",
                candidate("/dev/input/event4"),
            )
        )
        self.assertIn("Type 'Desk button' to continue: ", output.getvalue())
        self.assertNotIn("[y/N/x]", output.getvalue())

    def test_reconnect_instruction_reports_when_the_wait_starts(self):
        prompts, output = self.prompts("")

        prompts.show_reconnect_instruction(60.0)

        self.assertEqual(
            output.getvalue(),
            "Rule installed. Reconnect the device now; waiting up to 60 seconds...\n",
        )

    def test_evdev_readiness_waits_for_enter_and_reports_capture_start(self):
        prompts, output = self.prompts("not ready\n\n")

        prompts.arm_evdev_capture(candidate("/dev/input/event4"), 5.0)

        self.assertEqual(
            output.getvalue(),
            "Device ready for capture: /dev/input/event4.\n"
            "Press Enter to start a 5-second capture, or x to cancel: "
            "Press Enter to start capture, or x to cancel.\n"
            "Press Enter to start a 5-second capture, or x to cancel: "
            "Capture started. Operate the intended control once.\n",
        )

    def test_evdev_readiness_x_and_eof_cancel_before_capture(self):
        for input_text, error_type in (
            ("X\n", SetupExitRequested),
            ("", SetupCancelled),
        ):
            with self.subTest(input=input_text or "EOF"):
                prompts, _output = self.prompts(input_text)
                with self.assertRaises(error_type):
                    prompts.arm_evdev_capture(candidate("/dev/input/event4"), 5.0)

    def test_config_diff_service_result_and_backup_restore_menu_are_explicit(self):
        prompts, output = self.prompts("\n\n1\n")
        diff = "--- old\n+++ new\n+device = true\n"

        self.assertFalse(prompts.confirm_config(diff))
        choice = prompts.choose_service(
            ServiceSnapshot(enabled=True, active=True),
            compatible=True,
        )
        older = BackupInfo(
            Path("/tmp/config.toml.bak.20260715T120000Z"),
            datetime(2026, 7, 15, 12, tzinfo=UTC),
        )
        newer = BackupInfo(
            Path("/tmp/config.toml.bak.20260716T120000Z"),
            datetime(2026, 7, 16, 12, tzinfo=UTC),
        )
        selected = prompts.choose_backup((older, newer))

        report = output.getvalue()
        self.assertEqual(choice, ServiceChoice.RESTART)
        self.assertEqual(selected, newer)
        self.assertIn(diff, report)
        self.assertIn("Result: enabled and active", report)
        self.assertLess(report.index(newer.path.name), report.index(older.path.name))

    def test_eof_is_a_clean_setup_cancellation(self):
        prompts, _output = self.prompts("")

        with self.assertRaises(SetupCancelled) as cancelled:
            prompts.confirm_config("diff")
        self.assertNotIsInstance(cancelled.exception, SetupExitRequested)

    def test_advanced_action_options_override_failure_skip_defaults(self):
        prompts, output = self.prompts(
            "voice\n1\n/usr/bin/voice --on\n/usr/bin/voice --off\nY\ny\n\n"
        )
        editable = type("Editable", (), {"document": tomlkit.document()})()

        draft = prompts.choose_action(editable)

        self.assertTrue(draft.skip_off_after_failed_on)
        self.assertFalse(draft.skip_on_after_failed_off)
        self.assertIn("Default failure behavior", output.getvalue())
        default_yes_prompts, default_yes_output = self.prompts("\n")
        self.assertTrue(
            default_yes_prompts._confirm_default("Default yes? ", default=True)
        )
        self.assertIn("[Y/n/x]", default_yes_output.getvalue())

    def test_advanced_toggle_timeout_accepts_zero_to_disable_automatic_off(self):
        prompts, output = self.prompts("1\n1\ny\n0\n")

        draft = prompts.choose_evdev_trigger(("BTN_SIDE",))

        self.assertEqual(draft.toggle_off_timeout_seconds, 0)
        self.assertIn("automatic off", output.getvalue())

    def test_event_prompts_state_that_they_expect_a_number(self):
        toggle_prompts, toggle_output = self.prompts("1\n1\n\n")
        on_off_prompts, on_off_output = self.prompts("2\n1\n2\n")

        toggle_prompts.choose_evdev_trigger(("BTN_SIDE",))
        on_off_prompts.choose_evdev_trigger(("BTN_SIDE", "BTN_EXTRA"))

        self.assertIn(
            "Choose toggle event (enter a number from 1 to 1):",
            toggle_output.getvalue(),
        )
        self.assertIn(
            "Choose on event (enter a number from 1 to 2):",
            on_off_output.getvalue(),
        )
        self.assertIn(
            "Choose off event (enter a number from 1 to 2):",
            on_off_output.getvalue(),
        )

    def test_declining_timeout_customization_preserves_supplied_default(self):
        prompts, _output = self.prompts("1\n1\n\n")

        draft = prompts.choose_evdev_trigger(
            ("BTN_SIDE",), default_toggle_timeout_seconds=0
        )

        self.assertEqual(draft.toggle_off_timeout_seconds, 0)

    def test_nonnegative_number_rejects_nonfinite_values_before_accepting_finite_value(
        self,
    ):
        prompts, output = self.prompts("inf\n-inf\nnan\n5\n")

        result = prompts._nonnegative_number("Timeout: ", default=60.0)

        self.assertEqual(result, 5.0)
        self.assertEqual(output.getvalue().count("Enter a nonnegative number.\n"), 3)

    def test_advanced_permission_access_requires_an_explicit_choice(self):
        prompts, output = self.prompts("y\ny\n")

        access = prompts.choose_permission_access("Desk button")

        self.assertEqual(access, INPUT_GROUP_ACCESS)
        self.assertIn("input group", output.getvalue())

    def test_config_recovery_failure_shows_state_and_artifact_paths(self):
        prompts, output = self.prompts("")
        transaction = type(
            "Transaction",
            (),
            {
                "destination_may_be_active": True,
                "recovery_artifacts": (
                    Path("/tmp/config.toml.bak.20260716T120000Z"),
                    Path("/tmp/.config.toml.rollback.deadbeef"),
                ),
                "recovery_command_lines": (
                    "/usr/bin/mv -- /tmp/config.toml.bak.20260716T120000Z /tmp/config.toml",
                    "/usr/bin/sync -- /tmp",
                ),
            },
        )()

        prompts.report_recovery_failure(
            "config", RuntimeError("restore failed"), transaction
        )

        report = output.getvalue()
        self.assertIn("may still be active", report)
        self.assertIn("/tmp/config.toml.bak.20260716T120000Z", report)
        self.assertIn("/tmp/.config.toml.rollback.deadbeef", report)
        self.assertIn(
            "/usr/bin/mv -- /tmp/config.toml.bak.20260716T120000Z /tmp/config.toml",
            report,
        )
        self.assertIn("/usr/bin/sync -- /tmp", report)

    def test_config_recovery_failure_reports_an_active_destination_without_artifacts(
        self,
    ):
        prompts, output = self.prompts("")
        transaction = type(
            "Transaction",
            (),
            {"destination_may_be_active": True, "recovery_artifacts": ()},
        )()

        prompts.report_recovery_failure(
            "config", RuntimeError("restore failed"), transaction
        )

        self.assertIn("may still be active", output.getvalue())


if __name__ == "__main__":
    unittest.main()

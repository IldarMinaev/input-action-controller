import unittest

import tomlkit

from input_action_controller.models import ActionConfig

from input_action_controller.setup.actions import (
    ActionCommandStyle,
    ActionDraft,
    action_draft_from_config,
    apply_action_draft,
    parse_command_line,
)


class ParseCommandLineTests(unittest.TestCase):
    def test_parses_quoted_arguments_without_executing_the_command(self):
        lookups = []

        def which(executable):
            lookups.append(executable)
            return "/usr/bin/tool" if executable == "tool" else None

        command = parse_command_line('tool --label "voice input"', which=which)

        self.assertEqual(command, ("tool", "--label", "voice input"))
        self.assertEqual(lookups, ["tool"])

    def test_rejects_empty_input(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            parse_command_line("   ", which=lambda executable: "/usr/bin/" + executable)

    def test_rejects_a_missing_executable(self):
        with self.assertRaisesRegex(ValueError, "executable"):
            parse_command_line("missing --flag", which=lambda executable: None)

    def test_rejects_shell_syntax(self):
        forbidden_inputs = (
            "tool\n--flag",
            "tool ; other",
            "tool && other",
            "tool || other",
            "tool | other",
            "tool > result",
            "tool < input",
            "tool `other`",
            "tool $NAME",
            "tool ${NAME}",
            "tool $(other)",
        )

        for text in forbidden_inputs:
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ValueError, "shell syntax"),
            ):
                parse_command_line(
                    text, which=lambda executable: "/usr/bin/" + executable
                )


class ActionDraftTests(unittest.TestCase):
    def test_converts_existing_action_without_changing_policy(self):
        configured = ActionConfig(
            "voice",
            ("tool", "on"),
            ("tool", "off"),
            skip_off_after_failed_on=True,
            skip_on_after_failed_off=False,
            off_on_shutdown=False,
        )

        draft = action_draft_from_config(configured)

        self.assertEqual(draft.on_command, configured.on_command)
        self.assertEqual(draft.off_command, configured.off_command)
        self.assertTrue(draft.skip_off_after_failed_on)
        self.assertFalse(draft.skip_on_after_failed_off)
        self.assertFalse(draft.off_on_shutdown)

    def test_separate_commands_default_both_skip_policies_to_false(self):
        draft = ActionDraft("voice", ("tool", "on"), ("tool", "off"))

        self.assertFalse(draft.skip_off_after_failed_on)
        self.assertFalse(draft.skip_on_after_failed_off)
        self.assertTrue(draft.off_on_shutdown)

    def test_identical_separate_commands_default_both_skip_policies_to_false(self):
        draft = ActionDraft("voice", ("tool", "toggle"), ("tool", "toggle"))

        self.assertFalse(draft.skip_off_after_failed_on)
        self.assertFalse(draft.skip_on_after_failed_off)

    def test_toggle_command_style_defaults_both_skip_policies_to_true(self):
        draft = ActionDraft(
            "voice",
            ("tool", "toggle"),
            ("tool", "toggle"),
            command_style=ActionCommandStyle.TOGGLE,
        )

        self.assertTrue(draft.skip_off_after_failed_on)
        self.assertTrue(draft.skip_on_after_failed_off)

    def test_advanced_policy_overrides_are_independent(self):
        draft = ActionDraft(
            "voice",
            ("tool", "toggle"),
            ("tool", "toggle"),
            skip_off_after_failed_on=False,
            skip_on_after_failed_off=True,
            command_style=ActionCommandStyle.TOGGLE,
        )

        self.assertFalse(draft.skip_off_after_failed_on)
        self.assertTrue(draft.skip_on_after_failed_off)


class ApplyActionDraftTests(unittest.TestCase):
    def test_adds_an_action_as_toml_arrays_and_preserves_unrelated_comments(self):
        document = tomlkit.parse(
            "# Keep this comment.\n[runner]\ntimeout_seconds = 5\n"
        )
        draft = ActionDraft("voice", ("tool", "on"), ("tool", "off"))

        apply_action_draft(document, draft)

        action = document["actions"]["voice"]
        self.assertEqual(list(action["on_command"]), ["tool", "on"])
        self.assertEqual(list(action["off_command"]), ["tool", "off"])
        self.assertFalse(action["skip_off_after_failed_on"])
        self.assertFalse(action["skip_on_after_failed_off"])
        self.assertTrue(action["off_on_shutdown"])
        serialized = tomlkit.dumps(document)
        self.assertIn("# Keep this comment.", serialized)
        self.assertLess(
            serialized.index("# Keep this comment."), serialized.index("[runner]")
        )

    def test_rejects_a_duplicate_action_name(self):
        document = tomlkit.parse('[actions.voice]\non_command = ["tool"]\n')
        draft = ActionDraft("voice", ("tool", "on"), ("tool", "off"))

        with self.assertRaisesRegex(ValueError, "duplicate"):
            apply_action_draft(document, draft)

    def test_rejects_empty_commands_before_mutating_the_document(self):
        document = tomlkit.parse(
            "# Keep this comment.\n[runner]\ntimeout_seconds = 5\n"
        )
        original = tomlkit.dumps(document)

        for on_command, off_command in (((), ("tool", "off")), (("tool", "on"), ())):
            with self.subTest(on_command=on_command, off_command=off_command):
                with self.assertRaisesRegex(ValueError, "command must not be empty"):
                    ActionDraft("voice", on_command, off_command)
                self.assertEqual(tomlkit.dumps(document), original)


if __name__ == "__main__":
    unittest.main()

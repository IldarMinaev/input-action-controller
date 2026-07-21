from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


class ContinuousIntegrationTests(unittest.TestCase):
    def test_workflow_has_read_only_permissions_and_finite_jobs(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", content)
        self.assertIn("cancel-in-progress: true", content)
        self.assertEqual(content.count("timeout-minutes:"), 4)
        self.assertNotIn("pull_request_target:", content)

    def test_python_matrix_covers_minimum_and_target_versions(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('python-version: ["3.11", "3.14"]', content)
        self.assertIn("python -m unittest discover -s tests -v", content)
        self.assertIn("python -m compileall -q src tests", content)

    def test_python_matrix_installs_archive_tools_before_tests(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        tests_job = content.split("  tests:\n", maxsplit=1)[1].split(
            "  distribution:\n", maxsplit=1
        )[0]
        archive_tools = "sudo apt-get install --yes libarchive-tools"
        test_command = "python -m unittest discover -s tests -v"

        self.assertIn(archive_tools, tests_job)
        self.assertLess(tests_job.index(archive_tools), tests_job.index(test_command))

    def test_official_actions_use_full_sha_pins(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        uses = re.findall(r"uses: (actions/[^@]+)@([^ #]+)", content)
        self.assertTrue(uses)
        for action, revision in uses:
            with self.subTest(action=action):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_arch_job_builds_as_an_unprivileged_user(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("container: archlinux:base-devel", content)
        self.assertIn("useradd --create-home builder", content)
        self.assertIn("runuser --user builder -- ./scripts/makepkg", content)
        self.assertIn("shellcheck scripts/makepkg", content)
        self.assertIn("actionlint", content)

    def test_arch_job_installs_git_before_checkout_and_package_build(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        arch_job = content.split("  arch-package:\n", maxsplit=1)[1]
        git_bootstrap = "pacman -Syu --noconfirm git"
        checkout = "uses: actions/checkout@"
        package_build = "runuser --user builder -- ./scripts/makepkg"

        self.assertIn(git_bootstrap, arch_job)
        self.assertLess(arch_job.index(git_bootstrap), arch_job.index(checkout))
        self.assertLess(arch_job.index(checkout), arch_job.index(package_build))

    def test_dependabot_checks_actions_weekly(self):
        content = DEPENDABOT.read_text(encoding="utf-8")
        self.assertIn('package-ecosystem: "github-actions"', content)
        self.assertIn('interval: "weekly"', content)

    def test_release_workflow_is_tag_driven_and_reuses_ci(self):
        content = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('      - "v*"', content)
        self.assertIn("uses: ./.github/workflows/ci.yml", content)
        ci_content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("branches:\n      - main", ci_content)
        self.assertIn("workflow_call:", ci_content)
        self.assertNotIn("workflow_dispatch:", content)

    def test_reusable_ci_cannot_share_the_release_concurrency_group(self):
        ci_content = WORKFLOW.read_text(encoding="utf-8")
        release_content = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("group: CI-${{ github.ref }}", ci_content)
        self.assertIn("group: Release-${{ github.ref }}", release_content)

    def test_release_workflow_limits_write_permission_to_publish_job(self):
        content = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", content)
        self.assertEqual(content.count("contents: write"), 1)
        publish_job = content.split("  publish:\n", maxsplit=1)[1]
        self.assertIn("permissions:\n      contents: write", publish_job)

    def test_release_workflow_validates_versions_and_remote_tag(self):
        content = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pyproject.toml", content)
        self.assertIn("PKGBUILD", content)
        self.assertIn("scripts/makepkg", content)
        self.assertIn("gh release create", content)
        self.assertIn("--verify-tag", content)
        self.assertIn("--generate-notes", content)

    def test_release_workflow_pins_official_actions(self):
        content = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        uses = re.findall(r"uses: (actions/[^@]+)@([^ #]+)", content)
        self.assertTrue(uses)
        for action, revision in uses:
            with self.subTest(action=action):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()

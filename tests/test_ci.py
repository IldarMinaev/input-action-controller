from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class ContinuousIntegrationTests(unittest.TestCase):
    def test_workflows_use_least_privilege_and_pinned_official_actions(self):
        for path in (WORKFLOW, RELEASE_WORKFLOW):
            with self.subTest(path=path.name):
                content = path.read_text(encoding="utf-8")
                self.assertIn("permissions:\n  contents: read", content)
                self.assertNotIn("pull_request_target:", content)
                uses = re.findall(r"uses: (actions/[^@]+)@([^ #]+)", content)
                self.assertTrue(uses)
                self.assertTrue(
                    all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in uses)
                )

        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(release.count("contents: write"), 1)

    def test_release_and_reusable_ci_have_distinct_concurrency_groups(self):
        ci = WORKFLOW.read_text(encoding="utf-8")
        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("group: CI-${{ github.ref }}", ci)
        self.assertIn("group: Release-${{ github.ref }}", release)
        self.assertIn("workflow_call:", ci)
        self.assertIn("uses: ./.github/workflows/ci.yml", release)

    def test_release_checks_out_validated_tag_before_publish(self):
        content = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publish_job = content.split("  publish:\n", maxsplit=1)[1]

        self.assertIn('      - "v*"', content)
        for version_source in ("pyproject.toml", "PKGBUILD", "scripts/makepkg"):
            self.assertIn(version_source, content)
        self.assertLess(
            publish_job.index("uses: actions/checkout@"),
            publish_job.index("gh release create"),
        )
        self.assertIn("--verify-tag", publish_job)

    def test_ci_covers_supported_python_and_unprivileged_arch_builds(self):
        content = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('python-version: ["3.11", "3.14"]', content)
        self.assertIn("python -m unittest discover -s tests -v", content)
        self.assertIn("python -m compileall -q src tests", content)
        self.assertIn("container: archlinux:base-devel", content)
        self.assertIn("runuser --user builder -- ./scripts/makepkg", content)
        self.assertIn("shellcheck scripts/makepkg", content)
        self.assertIn("actionlint", content)


if __name__ == "__main__":
    unittest.main()

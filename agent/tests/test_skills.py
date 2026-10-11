"""Checks the bundled CUR skills stay wired to the agent prompt."""

import re
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
SKILLS = AGENT_DIR / "skills"
AGENT_PY = AGENT_DIR / "agent.py"


def skill_name(path: Path) -> str:
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not match:
        raise AssertionError(f"{path} has no YAML frontmatter")
    name = re.search(r"^name:\s*(\S+)\s*$", match.group(1), re.M)
    if not name:
        raise AssertionError(f"{path} frontmatter has no name")
    return name.group(1)


def service_skills() -> list[str]:
    return sorted(
        path.name
        for path in SKILLS.iterdir()
        if path.is_dir() and path.name.startswith("cur-") and path.name != "cur-athena"
    )


class TestSkills(unittest.TestCase):
    def test_each_package_name_matches_directory(self):
        for path in sorted(SKILLS.iterdir()):
            skill = path / "SKILL.md"
            self.assertTrue(skill.is_file(), f"missing {skill}")
            self.assertEqual(skill_name(skill), path.name)

    def test_athena_scope_lists_every_service_skill(self):
        athena = (SKILLS / "cur-athena" / "SKILL.md").read_text()
        for name in service_skills():
            self.assertIn(f"| {name} |", athena)

    def test_system_prompt_names_every_service_skill(self):
        source = AGENT_PY.read_text()
        for name in service_skills():
            self.assertIn(name, source)

    def test_prompt_points_at_cloudwatch_metrics(self):
        source = AGENT_PY.read_text()
        self.assertIn("cloudwatch-metrics", source)
        self.assertIn("cloudwatch_metrics", source)
        athena = (SKILLS / "cur-athena" / "SKILL.md").read_text()
        self.assertIn("cloudwatch-metrics", athena)
        self.assertIn("cloudwatch_metrics", athena)

    def test_marina_deployments_skill_bundled_and_wired(self):
        skill = SKILLS / "marina-deployments" / "SKILL.md"
        self.assertTrue(skill.is_file())
        self.assertEqual(skill_name(skill), "marina-deployments")
        source = AGENT_PY.read_text()
        self.assertIn("marina-deployments", source)
        self.assertIn("github_list_commits", source)
        self.assertIn("marina-deployments", skill.read_text())
        self.assertIn("github_list_commits", skill.read_text())
        self.assertIn("/repos/{owner}/{repo}/commits", source)

    def test_github_fix_pr_skill_bundled_and_wired(self):
        skill = SKILLS / "github-fix-pr" / "SKILL.md"
        self.assertTrue(skill.is_file())
        self.assertEqual(skill_name(skill), "github-fix-pr")
        source = AGENT_PY.read_text()
        self.assertIn("github-fix-pr", source)
        self.assertIn("github_clone_repo", source)
        self.assertIn("github_create_fix_pr", source)
        self.assertIn("edit_workspace_file", source)
        package = (AGENT_DIR.parent / "deploy" / "package.sh").read_text()
        self.assertIn("github_fix.py", package)

    def test_select_examples_filter_billing_period(self):
        for skill in sorted(SKILLS.glob("*/SKILL.md")):
            for block in re.findall(r"```sql\n(.*?)```", skill.read_text(), re.S):
                if re.search(r"\b(select|with)\b", block, re.I):
                    self.assertIn(
                        "billing_period",
                        block.lower(),
                        f"{skill} example is missing billing_period",
                    )


if __name__ == "__main__":
    unittest.main()

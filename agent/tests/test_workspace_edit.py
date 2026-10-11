"""Unit tests for workspace_edit (no agent import)."""

import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from workspace_edit import apply_workspace_edit, resolve_workspace_path


class TestWorkspaceEdit(unittest.TestCase):
    def test_resolve_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                resolve_workspace_path(root, "../outside.txt")

    def test_create_with_empty_old_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = apply_workspace_edit(root, "notes/new.md", "", "# hello\n")
            self.assertEqual(result["action"], "created")
            self.assertEqual((root / "notes" / "new.md").read_text(), "# hello\n")

    def test_single_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.go").write_text("foo()\nbar()\n")
            result = apply_workspace_edit(root, "a.go", "foo()", "fooCached()")
            self.assertEqual(result["action"], "updated")
            self.assertEqual(result["replacements"], 1)
            self.assertEqual((root / "a.go").read_text(), "fooCached()\nbar()\n")

    def test_ambiguous_match_requires_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.go").write_text("x\nx\n")
            with self.assertRaises(ValueError):
                apply_workspace_edit(root, "a.go", "x", "y")

    def test_replace_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.go").write_text("x\nx\n")
            result = apply_workspace_edit(root, "a.go", "x", "y", replace_all=True)
            self.assertEqual(result["replacements"], 2)
            self.assertEqual((root / "a.go").read_text(), "y\ny\n")


if __name__ == "__main__":
    unittest.main()

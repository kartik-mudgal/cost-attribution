"""Publish workspace artifacts to S3 and sign a download URL."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import artifacts
from artifacts import (
    DEFAULT_BUCKET,
    content_type_for,
    format_download_message,
    object_key,
    publish_workspace_file,
    safe_session_id,
)

REPO_ROOT = AGENT_DIR.parent


class TestArtifactKeys(unittest.TestCase):
    def test_session_id_cannot_escape_the_prefix(self):
        self.assertEqual(safe_session_id("session/../admin"), "session-.-admin")
        self.assertEqual(safe_session_id(""), "unknown-session")
        self.assertEqual(safe_session_id(None), "unknown-session")

    def test_object_key_rejects_parent_segments(self):
        with self.assertRaises(ValueError):
            object_key("session-1", "../secret.txt")
        with self.assertRaises(ValueError):
            object_key("session-1", "/etc/passwd")

    def test_object_key_includes_session_and_prefix(self):
        self.assertEqual(
            object_key("session-abc", "artifacts/report.md"),
            "generated/session-abc/artifacts/report.md",
        )

    def test_markdown_content_type(self):
        self.assertEqual(content_type_for("report.md"), "text/markdown; charset=utf-8")


class TestPublish(unittest.TestCase):
    def test_publish_uploads_and_returns_presigned_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "artifacts").mkdir()
            (root / "artifacts" / "report.md").write_text("# hi\n")
            s3 = MagicMock()
            s3.generate_presigned_url.return_value = "https://example.invalid/report"
            result = publish_workspace_file(
                workspace=root,
                name="artifacts/report.md",
                session_id="session-abc",
                s3_client=s3,
                ttl_s=3600,
            )
        self.assertEqual(result["download_url"], "https://example.invalid/report")
        self.assertEqual(result["bucket"], DEFAULT_BUCKET)
        self.assertEqual(result["key"], "generated/session-abc/artifacts/report.md")
        self.assertEqual(result["expires_in_seconds"], 3600)
        put = s3.put_object.call_args.kwargs
        self.assertEqual(put["Bucket"], DEFAULT_BUCKET)
        self.assertEqual(put["Key"], result["key"])
        self.assertEqual(put["ContentType"], "text/markdown; charset=utf-8")
        self.assertEqual(put["ContentDisposition"], 'attachment; filename="report.md"')
        self.assertTrue(put["Body"].startswith(b"# hi"))
        args, kwargs = s3.generate_presigned_url.call_args
        self.assertEqual(args[0], "get_object")
        self.assertEqual(kwargs["Params"], {"Bucket": DEFAULT_BUCKET, "Key": result["key"]})
        self.assertEqual(kwargs["ExpiresIn"], 3600)

    def test_ttl_is_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("x")
            s3 = MagicMock()
            s3.generate_presigned_url.return_value = "https://example.invalid/note"
            short = publish_workspace_file(
                workspace=root, name="note.txt", session_id="s", s3_client=s3, ttl_s=1,
            )
            long = publish_workspace_file(
                workspace=root, name="note.txt", session_id="s", s3_client=s3, ttl_s=10**9,
            )
        self.assertEqual(short["expires_in_seconds"], 60)
        self.assertEqual(long["expires_in_seconds"], artifacts.MAX_PRESIGN_TTL_S)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                publish_workspace_file(
                    workspace=Path(tmp),
                    name="missing.md",
                    session_id="s",
                    s3_client=MagicMock(),
                )

    def test_path_must_stay_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.txt"
            outside.write_text("nope")
            nested = root / "ws"
            nested.mkdir()
            with self.assertRaises(ValueError):
                publish_workspace_file(
                    workspace=nested,
                    name="../outside.txt",
                    session_id="s",
                    s3_client=MagicMock(),
                )

    def test_symlink_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "secret.txt"
            outside.write_text("secret")
            workspace = root / "ws"
            workspace.mkdir()
            link = workspace / "leak.txt"
            os.symlink(outside, link)
            with self.assertRaises(ValueError):
                publish_workspace_file(
                    workspace=workspace,
                    name="leak.txt",
                    session_id="s",
                    s3_client=MagicMock(),
                )

    def test_oversize_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.bin").write_bytes(b"abcdef")
            with self.assertRaises(ValueError):
                publish_workspace_file(
                    workspace=root,
                    name="big.bin",
                    session_id="s",
                    s3_client=MagicMock(),
                    max_bytes=4,
                )

    def test_normalized_path_drops_dot_segments_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "artifacts").mkdir()
            (root / "artifacts" / "report.md").write_text("ok")
            s3 = MagicMock()
            s3.generate_presigned_url.return_value = "https://example.invalid/report"
            result = publish_workspace_file(
                workspace=root,
                name="artifacts/../artifacts/report.md",
                session_id="session-abc",
                s3_client=s3,
            )
        self.assertEqual(result["key"], "generated/session-abc/artifacts/report.md")


class TestDownloadMessage(unittest.TestCase):
    def test_lists_each_url(self):
        text = format_download_message([{
            "key": "generated/session-abc/artifacts/report.md",
            "download_url": "https://example.invalid/report",
            "expires_in_seconds": 3600,
        }])
        self.assertIn("https://example.invalid/report", text)
        self.assertIn("expires in 3600 seconds", text)

    def test_empty(self):
        self.assertEqual(format_download_message([]), "")


class TestWiring(unittest.TestCase):
    def test_agent_instructs_publish_and_exposes_tool(self):
        source = (AGENT_DIR / "agent.py").read_text()
        self.assertIn("def publish_artifact", source)
        self.assertIn("publish_artifact,", source)
        self.assertIn("Never invent a download URL.", source)
        self.assertIn("format_download_message", source)

    def test_model_output_budget_covers_a_report(self):
        source = (AGENT_DIR / "agent.py").read_text()
        self.assertIn("BedrockModel(", source)
        self.assertIn("max_tokens=MAX_OUTPUT_TOKENS", source)
        self.assertIn('os.environ.get("MAX_OUTPUT_TOKENS", "16000")', source)
        self.assertIn("def append_workspace_file", source)
        self.assertIn("one section per call", source)
        self.assertIn("MaxTokensReachedException", source)
        runtime = (REPO_ROOT / "deploy" / "deploy_runtime.py").read_text()
        self.assertIn('"MAX_OUTPUT_TOKENS": "16000"', runtime)

    def test_role_policy_allows_put_and_get_on_dedicated_bucket(self):
        policy = json.loads((REPO_ROOT / "deploy" / "role-policy.json").read_text())
        statement = next(item for item in policy["Statement"] if item.get("Sid") == "UserArtifacts")
        self.assertEqual(statement["Effect"], "Allow")
        self.assertCountEqual(statement["Action"], ["s3:PutObject", "s3:GetObject"])
        self.assertEqual(statement["Resource"], f"arn:aws:s3:::{DEFAULT_BUCKET}/*")

    def test_runtime_env_points_at_dedicated_bucket(self):
        source = (REPO_ROOT / "deploy" / "deploy_runtime.py").read_text()
        self.assertIn('ARTIFACT_BUCKET": f"cost-agent-user-artifacts-{ACCOUNT}"', source)
        self.assertIn('"ARTIFACT_URL_TTL_S": "3600"', source)

    def test_package_bundles_artifacts_module(self):
        source = (REPO_ROOT / "deploy" / "package.sh").read_text()
        self.assertIn('"$ROOT/agent/artifacts.py"', source)
        self.assertIn('"$ROOT/agent/github_fix.py"', source)
        self.assertIn('"$ROOT/agent/devrev_issues.py"', source)


if __name__ == "__main__":
    unittest.main()

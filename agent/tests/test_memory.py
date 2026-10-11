"""Shared memory: namespaces, staleness, replace/refresh, dispute, forget and tool wiring."""

import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import memory
from memory import ROOT, SharedMemory, assess, compose, namespace

TODAY = datetime.now(timezone.utc).date()


def text_verified(days_ago: int, status: str = "verified") -> str:
    return compose("queue X sends in argus handler.go", "devrev/argus handler.go", status=status,
                   verified=TODAY - timedelta(days=days_ago))


def hit(record_id: str, text: str, score: float) -> dict:
    return {"memoryRecordId": record_id, "content": {"text": text}, "score": score}


def ok(record_id: str = "r") -> dict:
    return {"successfulRecords": [{"memoryRecordId": record_id}], "failedRecords": []}


class TestNamespace(unittest.TestCase):
    def test_kinds_and_scopes(self):
        self.assertEqual(namespace("resource-map", "SQS"), f"{ROOT}/resource-map/sqs")
        self.assertEqual(namespace("queries", "all"), f"{ROOT}/queries/all")
        self.assertEqual(namespace("lessons", "deep-dive"), f"{ROOT}/lessons/deep-dive")

    def test_rejects_unknown_kind_or_scope(self):
        for kind, scope in [("everything", "sqs"), ("resource-map", "rds"), ("lessons", "sqs"),
                            ("conclusions", "../lessons")]:
            with self.assertRaises(ValueError):
                namespace(kind, scope)


class TestStaleness(unittest.TestCase):
    def test_compose_records_status_files_and_date(self):
        text = compose("fact", "evidence", files="devrev/argus a.go@abc", verified=date(2026, 10, 1))
        self.assertEqual(text, "fact\nEvidence: evidence\nFiles: devrev/argus a.go@abc\n"
                               "Status: verified\nVerified: 2026-10-01")

    def test_age_limits_per_kind(self):
        self.assertEqual(assess("resource-map", text_verified(29), None, TODAY), ("fresh", 29))
        self.assertEqual(assess("resource-map", text_verified(31), None, TODAY), ("expired", 31))
        self.assertEqual(assess("conclusions", text_verified(15), None, TODAY), ("expired", 15))
        self.assertEqual(assess("resource-map", text_verified(61), None, TODAY), ("delete", 61))

    def test_disputed_wins_over_fresh(self):
        self.assertEqual(assess("resource-map", text_verified(1, "disputed: user says moved"), None, TODAY)[0],
                         "disputed")

    def test_falls_back_to_created_at(self):
        created = datetime.now(timezone.utc) - timedelta(days=100)
        self.assertEqual(assess("queries", "no footer", created, TODAY), ("expired", 100))


class TestSharedMemory(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.store = SharedMemory("mem-1", self.client)

    def test_recall_reports_status_and_deletes_very_old_records(self):
        self.client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [
            hit("fresh", text_verified(2), 0.8),
            hit("old", text_verified(40), 0.7),
            hit("ancient", text_verified(90), 0.6),
            hit("weak", text_verified(1), 0.1),
        ]}
        out = self.store.recall("resource-map", "sqs", "queue X", limit=50)
        kwargs = self.client.retrieve_memory_records.call_args.kwargs
        self.assertEqual(kwargs["namespace"], f"{ROOT}/resource-map/sqs")
        self.assertEqual(kwargs["searchCriteria"]["topK"], memory.MAX_RECALL)
        self.assertEqual([(r["id"], r["status"]) for r in out["records"]], [("fresh", "fresh"), ("old", "expired")])
        self.assertEqual(out["deleted_stale"], 1)
        deleted = self.client.batch_delete_memory_records.call_args.kwargs["records"]
        self.assertEqual(deleted, [{"memoryRecordId": "ancient"}])

    def test_remember_creates_new_record(self):
        self.client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
        self.client.batch_create_memory_records.return_value = ok("new")
        out = self.store.remember("resource-map", "sqs", "queue X sends in argus", "handler.go",
                                  files="devrev/argus handler.go@abc")
        record = self.client.batch_create_memory_records.call_args.kwargs["records"][0]
        self.assertEqual(record["namespaces"], [f"{ROOT}/resource-map/sqs"])
        self.assertIn("Files: devrev/argus handler.go@abc\nStatus: verified\nVerified: ", record["content"]["text"])
        self.assertEqual(out["status"], "remembered")

    def test_remember_refreshes_near_duplicate_instead_of_adding(self):
        self.client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [hit("old", "same", 0.95)]}
        self.client.batch_update_memory_records.return_value = ok("old")
        out = self.store.remember("conclusions", "sqs", "queue X is genuine traffic", "query abc")
        self.client.batch_create_memory_records.assert_not_called()
        update = self.client.batch_update_memory_records.call_args.kwargs["records"][0]
        self.assertEqual(update["memoryRecordId"], "old")
        self.assertIn(f"Verified: {TODAY.isoformat()}", update["content"]["text"])
        self.assertEqual(out["status"], "refreshed existing record")

    def test_remember_replaces_named_record(self):
        self.client.get_memory_record.return_value = {"memoryRecord": {"namespaces": [f"{ROOT}/conclusions/sqs/"]}}
        self.client.batch_update_memory_records.return_value = ok("r1")
        out = self.store.remember("conclusions", "sqs", "caching fix accepted", "confirmed by user: PR 123",
                                  replaces="r1")
        self.client.retrieve_memory_records.assert_not_called()
        self.assertEqual(out, {"namespace": f"{ROOT}/conclusions/sqs", "status": "replaced", "id": "r1"})

    def test_cannot_touch_records_outside_shared_memory(self):
        self.client.get_memory_record.return_value = {"memoryRecord": {"namespaces": ["/strands/actor/session/"]}}
        with self.assertRaises(ValueError):
            self.store.forget("r1", "wrong")
        with self.assertRaises(ValueError):
            self.store.remember("queries", "sqs", "SELECT 1", "query abc", replaces="r1")
        self.client.batch_delete_memory_records.assert_not_called()
        self.client.batch_update_memory_records.assert_not_called()

    def test_dispute_keeps_fact_and_date_and_marks_status(self):
        original = text_verified(5)
        self.client.get_memory_record.return_value = {"memoryRecord": {
            "namespaces": [f"{ROOT}/resource-map/sqs/"], "content": {"text": original}}}
        self.client.batch_update_memory_records.return_value = ok("r1")
        self.store.dispute("r1", "user says the producer moved to hubble")
        text = self.client.batch_update_memory_records.call_args.kwargs["records"][0]["content"]["text"]
        self.assertTrue(text.startswith("queue X sends in argus handler.go\nEvidence: devrev/argus handler.go\n"))
        self.assertIn("Status: disputed: user says the producer moved to hubble", text)
        self.assertIn(f"Verified: {(TODAY - timedelta(days=5)).isoformat()}", text)
        self.assertEqual(assess("resource-map", text, None, TODAY)[0], "disputed")

    def test_forget_deletes_owned_record(self):
        self.client.get_memory_record.return_value = {"memoryRecord": {"namespaces": [f"{ROOT}/queries/sqs/"]}}
        out = self.store.forget("r1", "schema changed")
        self.assertEqual(self.client.batch_delete_memory_records.call_args.kwargs["records"], [{"memoryRecordId": "r1"}])
        self.assertEqual(out["status"], "forgotten")

    def test_remember_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.store.remember("queries", "sqs", "SELECT 1", "  ")


class TestTools(unittest.TestCase):
    def tearDown(self):
        memory._store = None

    def test_configure_without_memory_id_returns_no_tools(self):
        self.assertEqual(memory.configure("", "us-west-2"), [])

    def test_configure_returns_all_memory_tools(self):
        tools = memory.configure("mem-1", "us-west-2")
        self.assertEqual([t.tool_name for t in tools], ["recall", "remember", "dispute", "forget"])

    def test_tool_reports_validation_errors_as_json(self):
        memory._store = SharedMemory("mem-1", MagicMock())
        out = json.loads(memory.recall._tool_func("everything", "sqs", "x"))
        self.assertIn("kind must be one of", out["error"])

    def test_tool_without_store(self):
        out = json.loads(memory.remember._tool_func("queries", "sqs", "SELECT 1", "query abc"))
        self.assertEqual(out["error"], "shared memory is not configured")


if __name__ == "__main__":
    unittest.main()

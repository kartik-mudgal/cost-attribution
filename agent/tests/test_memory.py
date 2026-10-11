"""Shared memory: namespaces, recall filtering, de-duplication and tool wiring."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import memory
from memory import SharedMemory, namespace


def hit(record_id: str, text: str, score: float) -> dict:
    return {"memoryRecordId": record_id, "content": {"text": text}, "score": score}


class TestNamespace(unittest.TestCase):
    def test_kinds_and_scopes(self):
        self.assertEqual(namespace("resource-map", "SQS"), "/cost-agent/resource-map/sqs")
        self.assertEqual(namespace("queries", "all"), "/cost-agent/queries/all")
        self.assertEqual(namespace("lessons", "deep-dive"), "/cost-agent/lessons/deep-dive")

    def test_rejects_unknown_kind_or_scope(self):
        with self.assertRaises(ValueError):
            namespace("everything", "sqs")
        with self.assertRaises(ValueError):
            namespace("resource-map", "rds")
        with self.assertRaises(ValueError):
            namespace("lessons", "sqs")
        with self.assertRaises(ValueError):
            namespace("conclusions", "../lessons")


class TestSharedMemory(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.store = SharedMemory("mem-1", self.client)

    def test_recall_searches_one_namespace_and_drops_weak_matches(self):
        self.client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [hit("a", "queue X sends in argus", 0.71), hit("b", "unrelated", 0.1)]
        }
        out = self.store.recall("resource-map", "sqs", "who sends to queue X", limit=50)
        kwargs = self.client.retrieve_memory_records.call_args.kwargs
        self.assertEqual(kwargs["namespace"], "/cost-agent/resource-map/sqs")
        self.assertEqual(kwargs["searchCriteria"]["topK"], memory.MAX_RECALL)
        self.assertEqual([r["id"] for r in out["records"]], ["a"])

    def test_remember_writes_fact_with_evidence_and_date(self):
        self.client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
        self.client.batch_create_memory_records.return_value = {
            "successfulRecords": [{"memoryRecordId": "new"}], "failedRecords": []
        }
        out = self.store.remember("conclusions", "sqs", "queue X is genuine traffic", "query abc")
        record = self.client.batch_create_memory_records.call_args.kwargs["records"][0]
        self.assertEqual(record["namespaces"], ["/cost-agent/conclusions/sqs"])
        self.assertIn("queue X is genuine traffic\nEvidence: query abc\nSaved: ", record["content"]["text"])
        self.assertEqual(out["status"], "remembered")

    def test_remember_skips_near_duplicates(self):
        self.client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [hit("old", "queue X is genuine traffic", 0.95)]
        }
        out = self.store.remember("conclusions", "sqs", "queue X is genuine traffic", "query abc")
        self.client.batch_create_memory_records.assert_not_called()
        self.assertEqual(out, {"namespace": "/cost-agent/conclusions/sqs", "status": "already remembered", "id": "old"})

    def test_remember_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.store.remember("queries", "sqs", "SELECT 1", "  ")


class TestTools(unittest.TestCase):
    def tearDown(self):
        memory._store = None

    def test_configure_without_memory_id_returns_no_tools(self):
        self.assertEqual(memory.configure("", "us-west-2"), [])

    def test_tool_reports_validation_errors_as_json(self):
        memory._store = SharedMemory("mem-1", MagicMock())
        out = json.loads(memory.recall._tool_func("everything", "sqs", "x"))
        self.assertIn("kind must be one of", out["error"])

    def test_tool_without_store(self):
        out = json.loads(memory.remember._tool_func("queries", "sqs", "SELECT 1", "query abc"))
        self.assertEqual(out["error"], "shared memory is not configured")


if __name__ == "__main__":
    unittest.main()

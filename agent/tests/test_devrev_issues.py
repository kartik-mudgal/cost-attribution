"""devrev_issues payload and auth helpers."""

import unittest

from devrev_issues import (
    DEFAULT_APPLIES_TO_PART,
    DEFAULT_TAG_ID,
    auth_header_value,
    build_create_issue_payload,
)


class TestDevrevIssues(unittest.TestCase):
    def test_payload_uses_fixed_routing(self):
        payload = build_create_issue_payload("Title", "Body")
        self.assertEqual(payload["type"], "issue")
        self.assertEqual(payload["title"], "Title")
        self.assertEqual(payload["body"], "Body")
        self.assertEqual(payload["applies_to_part"], DEFAULT_APPLIES_TO_PART)
        self.assertEqual(payload["tags"], [{"id": DEFAULT_TAG_ID}])
        self.assertTrue(payload["custom_schema_spec"]["validate_required_fields"])

    def test_auth_header_passthrough(self):
        self.assertEqual(auth_header_value("abc"), "abc")
        self.assertEqual(auth_header_value("Bearer xyz"), "Bearer xyz")

    def test_requires_title_and_body(self):
        with self.assertRaises(ValueError):
            build_create_issue_payload("", "x")
        with self.assertRaises(ValueError):
            build_create_issue_payload("t", "")


if __name__ == "__main__":
    unittest.main()

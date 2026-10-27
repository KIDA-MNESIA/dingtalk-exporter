import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import config, create_database, parser


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = create_database()
        self.addCleanup(self.conn.close)
        self.uid_patch = patch.object(config, "USER_UID", "1")
        self.uid_patch.start()
        self.addCleanup(self.uid_patch.stop)

    def test_last_page_contains_only_remaining_messages(self):
        result = parser.get_messages(self.conn, "1:2", limit=100, offset=500)
        self.assertEqual([m["id"] for m in result["messages"]], list(range(1, 55)))
        self.assertEqual(result["total"], 554)

    def test_at_or_past_end_is_empty(self):
        for offset in (554, 700):
            with self.subTest(offset=offset):
                result = parser.get_messages(self.conn, "1:2", offset=offset)
                self.assertEqual(result["messages"], [])
                self.assertEqual(result["total"], 554)

    def test_full_read_has_no_duplicates_or_gaps(self):
        for limit in (30, 100, 500, 1000):
            with self.subTest(limit=limit):
                received = []
                offset = 0
                while True:
                    page = parser.get_messages(self.conn, "1:2", limit=limit, offset=offset)
                    received.extend(m["id"] for m in page["messages"])
                    self.assertEqual(page["returned"], len(page["messages"]))
                    if not page["has_more"]:
                        self.assertIsNone(page["next_offset"])
                        break
                    self.assertGreater(page["next_offset"], offset)
                    offset = page["next_offset"]
                self.assertEqual(sorted(received), list(range(1, 555)))

    def test_time_bounds_and_ties(self):
        result = parser.get_messages(self.conn, "1:2", since_time=0, until_time=2)
        self.assertEqual([m["id"] for m in result["messages"]], [2, 3, 4, 5])
        self.assertEqual(parser.get_messages(self.conn, "1:2", until_time=0)["total"], 1)
        result = parser.get_messages(self.conn, "1:2", limit=1, offset=1)
        self.assertEqual(result["messages"][0]["id"], 553)
        self.assertEqual(parser.get_messages(self.conn, "1:2", since_time=5, until_time=2)["total"], 0)

    def test_unknown_conversation_has_complete_empty_page(self):
        result = parser.get_messages(self.conn, "missing", limit=20)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["returned"], 0)
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])

    def test_display_names_can_find_their_conversation(self):
        for conv in parser.get_conversations(self.conn)["conversations"]:
            with self.subTest(cid=conv["cid"]):
                found = parser.get_conversations(self.conn, keyword=conv["title"])
                self.assertIn(conv["cid"], [c["cid"] for c in found["conversations"]])
        bob = parser.get_conversations(self.conn, keyword="Bob")
        self.assertEqual(bob["conversations"][0]["title"], "Bob")

    def test_name_filter_count_and_pagination(self):
        first = parser.get_conversations(self.conn, keyword="Alice", limit=1)
        second = parser.get_conversations(self.conn, keyword="Alice", limit=1, offset=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(second["total"], 2)
        self.assertNotEqual(first["conversations"][0]["cid"], second["conversations"][0]["cid"])
        self.assertEqual(parser.get_conversations(self.conn, keyword="Alice Real")["total"], 1)
        self.assertEqual(parser.get_conversations(self.conn, keyword="old title")["total"], 1)

    def test_search_uses_one_global_limit_and_offset(self):
        first = parser.search_messages(self.conn, "needle", limit=30)
        self.assertEqual(len(first), 30)
        self.assertTrue(all(m["cid"] == "group" for m in first))
        self.assertEqual([m["id"] for m in first], list(range(205, 175, -1)))
        boundary = parser.search_messages(self.conn, "needle", limit=10, offset=200)
        self.assertEqual([(m["cid"], m["id"]) for m in boundary],
                         [("group", i) for i in range(5, 0, -1)] + [("1:2", i) for i in range(554, 549, -1)])
        self.assertEqual(parser.search_messages(self.conn, "needle", offset=759), [])

    def test_search_count_and_empty_results(self):
        result = parser.search_messages_page(self.conn, "needle", limit=200, offset=750)
        self.assertEqual(result["total"], 759)
        self.assertEqual(result["returned"], 9)
        self.assertFalse(result["has_more"])
        self.assertEqual(parser.search_messages_page(self.conn, "needle", offset=800)["total"], 759)
        self.assertEqual(parser.search_messages_page(self.conn, "' OR 1=1 --")["total"], 0)
        self.conn.execute("DROP TABLE tbmsg_000")
        self.conn.execute("DROP TABLE tbmsg_127")
        self.assertEqual(parser.search_messages_page(self.conn, "needle")["total"], 0)

    def test_search_ties_are_stable_across_shards(self):
        self.conn.execute("UPDATE tbmsg_127 SET createdAt=277 WHERE mid=205")
        result = parser.search_messages(self.conn, "needle", limit=2, offset=204)
        self.assertEqual({(m["cid"], m["id"]) for m in result}, {("group", 205), ("1:2", 554)})

    def test_invalid_pagination_is_rejected(self):
        calls = (
            lambda **kw: parser.get_messages(self.conn, "1:2", **kw),
            lambda **kw: parser.get_conversations(self.conn, **kw),
            lambda **kw: parser.search_messages(self.conn, "needle", **kw),
        )
        for call in calls:
            for args in ({"limit": 0}, {"limit": -1}, {"offset": -1}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    call(**args)

    def test_export_does_not_duplicate_partial_last_page(self):
        import exporter

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(exporter, "get_connection", return_value=self.conn):
                path = exporter.export_by_cids(["1:2"], base_dir=directory, batch_size=500)
            data = json.loads((Path(path) / "export.json").read_text(encoding="utf-8"))
            messages = data["conversations"][0]["messages"]
            self.assertEqual(len(messages), 554)
            self.assertEqual(len({m["message_id"] for m in messages}), 554)

    def test_web_search_reports_global_total(self):
        from fastapi.testclient import TestClient
        from web.api import app

        # Use a file so the API can open its own connection in the worker thread.
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fixture.db")
            create_database(path).close()
            with patch.object(config, "DECRYPTED_DB_PATH", path):
                # Do not enter TestClient's lifespan: that would start the sync scheduler.
                client = TestClient(app)
                self.addCleanup(client.close)
                response = client.get("/api/search", params={"q": "needle", "limit": 30})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["total"], 759)
                self.assertEqual(data["returned"], 30)
                self.assertTrue(all(m["cid"] == "group" for m in data["messages"]))


if __name__ == "__main__":
    unittest.main()

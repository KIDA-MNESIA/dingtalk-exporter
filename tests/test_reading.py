import base64
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

import msgpack

from tests.fixtures import config, create_database, parser


def encoded_child(message_id=9001, text="子消息正文"):
    model = {1: message_id, 2: "source-chat", 3: {1: 2, 2: 0}, 6: 1234,
             7: {1: 1, 2: {1: text}}, 9: {"sendMsgUUID": "test-message"}}
    return {"type": 1, "extension": {"msgId": str(message_id), "senderName": "Alice",
            "contentType": "1", "message": base64.b64encode(msgpack.packb(model)).decode("ascii")}}


def add_message(conn, mid, kind, content, extension=None, attachments=None):
    conn.execute(
        "INSERT INTO tbmsg_000 (mid,cid,createdAt,contentType,content,extension,attachments) VALUES (?,'1:2',5000,?,?,?,?)",
        (mid, kind, json.dumps(content), json.dumps(extension or {}), json.dumps(attachments or [])),
    )


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.conn = create_database()
        self.addCleanup(self.conn.close)

    def test_search_decodes_json_and_includes_visible_extension_and_filename(self):
        add_message(self.conn, 9001, 1, {"text": "项目说明"})
        add_message(self.conn, 9002, 2950, {}, {"biz_custom_title": "项目安排"})
        add_message(self.conn, 9003, 503, {}, attachments=[{"type": 0, "extension": {"f_name": "项目报告.txt", "f_size": "8"}}])
        result = parser.search_messages_page(self.conn, "项目")
        self.assertEqual({m["id"] for m in result["messages"]}, {9001, 9002, 9003})
        self.assertEqual(result["total"], 3)
        self.assertEqual(parser.search_messages_page(self.conn, "项目", mode="raw")["total"], 0)

    def test_text_search_is_a_literal_unicode_substring(self):
        add_message(self.conn, 9001, 1, {"text": "预算提高 10%"})
        self.assertEqual(parser.search_messages_page(self.conn, "%")["total"], 1)
        self.assertGreater(parser.search_messages_page(self.conn, "%", mode="raw")["total"], 1)
        with self.assertRaises(ValueError):
            parser.search_messages_page(self.conn, "needle", mode="unknown")

    def test_forwarded_paging_search_paths_and_parent_summary(self):
        attachments = [{"type": 101, "extension": {"title": "合并记录", "summary": "摘要"}},
                       encoded_child(text="部署讨论"), encoded_child(9002, "验收讨论")]
        add_message(self.conn, 9000, 1500, {}, attachments=attachments)
        parent = parser.get_messages(self.conn, "1:2", limit=1)["messages"][0]
        self.assertEqual(parent["parse_status"], "complete")
        self.assertNotIn("forwarded_messages", parent)
        self.assertEqual(parent["forwarded_info"]["total"], 2)
        page = parser.get_forwarded_messages(self.conn, "1:2", 9000, limit=1)
        self.assertEqual(page["messages"][0]["content"], "部署讨论")
        self.assertEqual(page["messages"][0]["path"], [1])
        self.assertEqual(page["next_offset"], 1)
        last = parser.get_forwarded_messages(self.conn, "1:2", 9000, limit=1, offset=1)
        self.assertFalse(last["has_more"])
        self.assertEqual(last["messages"][0]["id"], 9002)
        found = parser.search_messages_page(self.conn, "讨论")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["messages"][0]["id"], 9000)
        self.assertEqual(found["messages"][0]["matched_paths"], [[1], [2]])

    def test_missing_forwarded_payload_preserves_title_and_partial_status(self):
        add_message(self.conn, 9000, 1500, {}, attachments=[{"type": 1500, "extension": {"title": "历史片段", "summary": "只存摘要"}}])
        result = parser.get_forwarded_messages(self.conn, "1:2", 9000)
        self.assertEqual(result["parse_status"], "partial")
        self.assertEqual(result["title"], "历史片段")
        self.assertEqual(result["summary"], "只存摘要")
        self.assertEqual(result["messages"], [])
        self.assertEqual(parser.search_messages_page(self.conn, "只存摘要")["total"], 1)

    def test_observed_video_file_contact_types_and_unknown_status(self):
        add_message(self.conn, 9001, 202, {}, attachments=[{"type": 202, "extension": {"fileType": "mp4", "duration": "10", "width": "1280", "height": "720"}}])
        add_message(self.conn, 9002, 500, {}, attachments=[{"type": 500, "extension": {"f_name": "说明.docx", "f_size": "8"}}])
        add_message(self.conn, 9003, 600, {}, attachments=[{"type": 0, "extension": {"name": "Alice", "uid": "2", "phone": "123"}}])
        add_message(self.conn, 9004, 9999, {})
        messages = {m["id"]: m for m in parser.get_messages(self.conn, "1:2", limit=4)["messages"]}
        self.assertEqual(messages[9001]["content_type_name"], "视频")
        self.assertEqual(messages[9001]["video_info"]["fileType"], "mp4")
        self.assertIn("说明.docx", messages[9002]["content"])
        self.assertIn("Alice", messages[9003]["content"])
        self.assertEqual(messages[9004]["parse_status"], "unsupported")
        self.assertEqual(messages[9004]["content_type"], 9999)
        self.assertTrue(messages[9004]["content"])

    def test_full_export_orders_all_pages_and_preserves_forwarded_details(self):
        import exporter

        add_message(self.conn, 9000, 1500, {}, attachments=[encoded_child(text="完整转发")])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(exporter, "get_connection", return_value=self.conn):
                path = exporter.export_all(base_dir=directory, batch_size=100)
            result = json.loads((Path(path) / "export.json").read_text(encoding="utf-8"))
            messages = next(c["messages"] for c in result["conversations"] if c["conversation_id"] == "1:2")
            self.assertEqual([m["message_id"] for m in messages], [*range(1, 555), 9000])
            self.assertEqual(messages[-1]["forwarded_messages"][0]["text"], "完整转发")
            self.assertEqual(messages[-1]["forwarded_messages"][0]["path"], [0])

    def test_index_reuse_and_parser_version_invalidation(self):
        import search_index
        import snapshots
        from message_content import search_parts

        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DECRYPTED_DIR", directory):
            path = Path(directory) / "fixture.db"
            create_database(path).close()
            with patch.object(config, "DECRYPTED_DB_PATH", str(path)):
                info = snapshots.publish_snapshot(path, search_index.prepare_snapshot_index)
                conn = parser.get_connection(snapshot_id=info["snapshot_id"])
                try:
                    with patch("message_content.search_parts", side_effect=AssertionError("unexpected rebuild")):
                        self.assertEqual(parser.search_messages_page(conn, "needle")["total"], 759)
                    with patch.object(search_index, "PARSER_VERSION", "test-new"), patch("message_content.search_parts", wraps=search_parts) as build:
                        self.assertEqual(parser.search_messages_page(conn, "needle")["total"], 759)
                        self.assertTrue(build.called)
                finally:
                    conn.close()

    def test_index_publish_race_reuses_a_complete_winner(self):
        import search_index

        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DECRYPTED_DIR", directory):
            def competing_publish(source, destination):
                # Another builder published these identical contents and opened
                # the file before this builder reached os.replace on Windows.
                shutil.copyfile(source, destination)
                raise PermissionError("index is open in another process")

            with patch.object(search_index.os, "replace", side_effect=competing_publish):
                path = search_index._write_index(self.conn, "test-race")
            self.assertTrue(search_index._valid_index(path, "test-race"))
            with patch.object(search_index.os, "replace", side_effect=PermissionError("no winner")):
                with self.assertRaises(PermissionError):
                    search_index._write_index(self.conn, "test-missing-winner")

    def test_web_export_rejects_conflicting_job_and_start_failure(self):
        from fastapi.testclient import TestClient
        from web import api

        client = TestClient(api.app)
        self.addCleanup(client.close)
        with patch.object(api, "start_export", return_value={"status": "already_running", "job_id": "other"}):
            self.assertEqual(client.post("/api/export/selected", json={"cids": ["1:2"]}).status_code, 409)
        with patch.object(api, "start_export", return_value={"status": "failed", "job_id": "new", "error": "start failed"}):
            self.assertEqual(client.post("/api/export/selected", json={"cids": ["1:2"]}).status_code, 503)


if __name__ == "__main__":
    unittest.main()

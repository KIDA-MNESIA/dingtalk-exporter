import base64
import json
import unittest

import msgpack

from forwarded import decode_forwarded
from message_content import build_agent_content, search_parts


def model(mid=1, text="Synthetic text", content_type=1, content=None):
    return {
        1: mid, 2: "source-chat", 3: {1: 42, 2: 1}, 6: 1000 + mid,
        7: content or {1: content_type, 2: {1: text}}, 9: {},
    }


def attachment(payload, sender="Sender"):
    return {
        "type": payload[7][1],
        "extension": {
            "msgId": str(payload[1]), "senderName": sender,
            "message": base64.b64encode(msgpack.packb(payload, use_bin_type=True)).decode("ascii"),
        },
    }


def packed_attachment(payload):
    """A forward attachment inside a MessagePack content object."""
    item = attachment(payload)
    return {1: item["type"], 6: item["extension"]}


def parse_row(row):
    content = row["content"]
    kind = row["contentType"]
    return {
        "id": row["mid"], "cid": row["cid"], "sender_id": row["senderId"],
        "sender_name": "Profile name", "created_at": row["createdAt"],
        "content_type": kind, "text": content.get("text", "[合并转发]" if kind == 1500 else ""),
        "attachments": content.get("attachments", []),
    }


class ForwardedTests(unittest.TestCase):
    def test_text_bytes_and_original_order(self):
        rows = []
        def callback(row):
            rows.append(row)
            return parse_row(row)
        inputs = [
            {"type": 101, "extension": json.dumps({"title": "Forward", "summary": "Two messages"})},
            attachment(model(7, "Later")), attachment(model(2, "中文")),
        ]
        result = decode_forwarded(json.dumps(inputs), {}, callback)
        self.assertEqual(result["parse_status"], "complete")
        self.assertEqual(result["title"], "Forward")
        self.assertEqual([m["id"] for m in result["messages"]], [7, 2])
        self.assertEqual([m["path"] for m in result["messages"]], [[1], [2]])
        self.assertEqual([m["parent_path"] for m in result["messages"]], [[], []])
        self.assertEqual(result["messages"][1]["text"], "中文")
        self.assertEqual(rows[1]["content"]["text"], "中文")
        self.assertEqual(result["messages"][0]["sender_name"], "Sender")

    def test_nested_forward_is_preorder_and_keeps_parent(self):
        nested = model(2, content={1: 1500, 7: [
            {1: 101, 6: {"title": "Nested"}}, packed_attachment(model(3, "Child")),
        ]})
        result = decode_forwarded([], {"attachments": [attachment(model(1)), attachment(nested), attachment(model(4))]}, parse_row)
        self.assertEqual(result["parse_status"], "complete")
        self.assertEqual([m["id"] for m in result["messages"]], [1, 2, 3, 4])
        self.assertEqual(result["messages"][2]["path"], [1, 1])
        self.assertEqual(result["messages"][2]["parent_path"], [1])
        self.assertEqual(result["messages"][1]["forwarded_info"]["title"], "Nested")
        self.assertEqual(result["messages"][1]["forwarded_info"]["total"], 1)
        self.assertEqual(result["messages"][1]["content"], "[合并转发] Nested")
        self.assertNotIn("Child", result["messages"][1]["content"])

    def test_header_only_is_partial(self):
        result = decode_forwarded([{"type": 101, "extension": {"title": "Preview only", "summary": "Unavailable children"}}], {}, parse_row)
        self.assertEqual(result["parse_status"], "partial")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["errors"], [{"path": [], "code": "missing_payload"}])

    def test_empty_serialized_attachments_fall_back_to_content(self):
        result = decode_forwarded("[]", {"attachments": [attachment(model())]}, parse_row)
        self.assertEqual(result["parse_status"], "complete")
        self.assertEqual(len(result["messages"]), 1)

    def test_preview_type_1500_preserves_title_without_a_header(self):
        result = decode_forwarded([{"type": 1500, "extension": {"title": "Only preview", "summary": "Summary retained"}}], {}, parse_row)
        self.assertEqual(result["title"], "Only preview")
        self.assertEqual(result["summary"], "Summary retained")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["parse_status"], "partial")
        self.assertEqual(result["errors"], [{"path": [0], "code": "missing_payload"}])

    def test_bad_payload_does_not_hide_valid_siblings(self):
        for encoded, code in (("not base64!", "invalid_base64"), (base64.b64encode(b"\xc1").decode(), "invalid_msgpack")):
            with self.subTest(code=code):
                result = decode_forwarded([{"type": 1, "extension": {"message": encoded}}, attachment(model(2))], {}, parse_row)
                self.assertEqual(result["parse_status"], "partial")
                self.assertEqual([m["id"] for m in result["messages"]], [2])
                self.assertIn({"path": [0], "code": code}, result["errors"])

    def test_trailing_msgpack_bytes_are_rejected(self):
        encoded = base64.b64encode(msgpack.packb(model()) + b"\xc0").decode()
        result = decode_forwarded([{"type": 1, "extension": {"message": encoded}}], {}, parse_row)
        self.assertEqual(result["parse_status"], "unsupported")
        self.assertEqual(result["errors"][0]["code"], "invalid_msgpack")

    def test_unknown_binary_is_not_decoded_or_disclosed(self):
        payload = model()
        payload[9] = {b"unknown": b"\xffsecret-not-for-output"}
        result = decode_forwarded([attachment(payload)], {}, parse_row)
        self.assertEqual(result["parse_status"], "partial")
        self.assertEqual(result["messages"][0]["parse_status"], "partial")
        self.assertNotIn("secret-not-for-output", json.dumps(result))
        payload = model(text=b"\xff\x00")
        result = decode_forwarded([attachment(payload)], {}, parse_row)
        self.assertEqual(result["messages"][0]["text"], "")
        self.assertIn({"path": [0], "code": "unsupported_binary"}, result["errors"])

    def test_unknown_utf8_metadata_does_not_mark_body_partial(self):
        payload = model()
        payload[9] = {"sendMsgUUID": "ignored-reference", "metadata": {"key": "extra text"}}
        result = decode_forwarded([attachment(payload)], {}, parse_row)
        self.assertEqual(result["parse_status"], "complete")
        self.assertEqual(result["messages"][0]["parse_status"], "complete")
        self.assertEqual(result["errors"], [])
        self.assertNotIn("ignored-reference", json.dumps(result))
        self.assertNotIn("extra text", json.dumps(result))

    def test_verified_image_and_attachment_schema(self):
        payload = model(content={1: 2, 3: {1: "media-reference", 2: 3000, 6: 90, 8: "photo.png"}, 7: [
            {1: 501, 2: "reference", 3: 4, 5: True, 6: {"f_name": "notes.txt", "f_size": "4", "f_type": "txt"}},
        ]})
        rows = []
        result = decode_forwarded([attachment(payload)], {}, lambda row: rows.append(row) or parse_row(row))
        content = rows[0]["content"]
        self.assertEqual(content["mediaId"], "media-reference")
        self.assertNotIn("url", content)
        self.assertEqual(content["filename"], "photo.png")
        self.assertEqual(content["attachments"][0]["extension"]["f_name"], "notes.txt")
        self.assertNotIn("isFTF", json.dumps(result))

    def test_payload_and_total_size_limits(self):
        child = attachment(model(text="x" * 200))
        size = len(base64.b64decode(child["extension"]["message"]))
        result = decode_forwarded([child], {}, parse_row, max_payload_bytes=size - 1)
        self.assertEqual(result["errors"][0]["code"], "payload_size_limit")
        result = decode_forwarded([child, child], {}, parse_row, max_total_bytes=size)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["errors"][0]["code"], "total_size_limit")

    def test_message_and_depth_limits(self):
        result = decode_forwarded([attachment(model(1)), attachment(model(2))], {}, parse_row, max_messages=1)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["errors"][0]["code"], "message_limit")
        nested = model(1, content={1: 1500, 7: [packed_attachment(model(2))]})
        result = decode_forwarded([attachment(nested)], {}, parse_row, max_depth=0)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["parse_status"], "unsupported")
        self.assertIn({"path": [0, 0], "code": "depth_limit"}, result["errors"])

    def test_callback_failure_does_not_disclose_exception(self):
        def fail(row):
            raise RuntimeError("confidential content")
        result = decode_forwarded([attachment(model())], {}, fail)
        self.assertEqual(result["errors"], [{"path": [0], "code": "message_parse_failed"}])
        self.assertNotIn("confidential", json.dumps(result))

    def test_identity_mismatch_is_explicit(self):
        child = attachment(model())
        child["extension"]["msgId"] = "different"
        result = decode_forwarded([child], {}, parse_row)
        self.assertEqual(result["parse_status"], "partial")
        self.assertIn({"path": [0], "code": "message_id_mismatch"}, result["errors"])


class MessageContentTests(unittest.TestCase):
    def test_local_attachments_are_not_marked_missing_before_export(self):
        msg = {"content_type": 2, "text": "[图片]", "image_info": {"cached": True, "local_path": "C:/cache/photo.png"}}
        self.assertEqual(build_agent_content(msg), "[图片: C:/cache/photo.png]")
        self.assertEqual(build_agent_content(msg, exported=True), "[图片已缓存，未导出]")
        msg["image_export_paths"] = ["images/photo.png"]
        self.assertEqual(build_agent_content(msg, exported=True), "[图片: images/photo.png]")
        self.assertNotIn("未下载", build_agent_content(msg))

    def test_quotes_mentions_and_files_are_readable(self):
        msg = {"text": "@12 meet @123", "at_ids": {"12": "A", "123": "B"},
               "quote_info": {"source_sender_name": "Original", "source_preview": "Earlier message"},
               "attachments": [{"filename": "report.pdf", "local_available": True, "local_path": "C:/cache/report.pdf"}]}
        content = build_agent_content(msg)
        self.assertIn("@A meet @B", content)
        self.assertIn("[引用: Original] Earlier message", content)
        self.assertIn("report.pdf | C:/cache/report.pdf", content)
        self.assertNotIn("C:/cache", search_parts(msg)[0]["text"])

    def test_search_includes_children_without_expanding_parent_content(self):
        msg = {"text": "[合并转发]", "forwarded_info": {"title": "Discussion", "summary": "Summary"},
               "forwarded_messages": [{"id": 3, "path": [1, 2], "text": "nested keyword", "attachments": [{"filename": "specification.docx"}]}]}
        self.assertNotIn("nested keyword", build_agent_content(msg))
        parts = search_parts(msg)
        self.assertEqual(parts[1]["path"], [1, 2])
        self.assertIn("nested keyword", parts[1]["text"])
        self.assertIn("specification.docx", parts[1]["text"])

    def test_unknown_cache_state_and_emoji_remain_distinct(self):
        self.assertEqual(build_agent_content({"content_type": 2, "text": "[图片]"}), "[图片，缓存状态未知]")
        self.assertEqual(build_agent_content({"content_type": 2, "message_subtype": "emoji", "text": "[钉钉表情]"}), "[钉钉表情]")


if __name__ == "__main__":
    unittest.main()

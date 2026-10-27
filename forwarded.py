"""Decode local merged-forward payloads without exposing opaque model fields.

The integer-key schema below is the subset verified against DingTalk 9.0.2
messages. The callback parses one normalized row and must not expand forwards;
this module owns recursion, limits, and source attachment paths.
"""

import base64
import binascii
import json

import msgpack

from message_content import build_agent_content


_TEXT_FIELDS = {
    "title", "summary", "senderName", "message", "msgId", "contentType",
    "text", "desc", "markdown", "searchDesc", "LastMessageI18n",
    "interactiveCardLastMessage", "replyContent", "sourceMessageModel",
    "sourceSenderId", "payload", "payloadV2", "f_name", "f_size", "f_type",
    "path", "filepath", "filename", "url", "src", "authMediaId", "mediaId",
    "BIType", "biz_custom_title", "biz_custom_desc", "biz_custom_action_name",
    "biz_custom_action_url", "apn_nav_url", "msgSrcBizId", "reportId",
    "lippiReport", "asrText", "audioToText", "audioText", "b_tl", "b_content",
    "b_form", "h_tl", "author", "type", "k", "v",
}
_CONTAINER_FIELDS = {"contents", "items", "text", "data", "value", "textRuns", "attachments", "extension"}
_SUPPORTED_TYPES = {1, 2, 501, 1200, 1500, 2900, 2950, 3100}


def _integer(value, default=0):
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (ValueError, RecursionError):
            pass
    return {}


class _Decoder:
    def __init__(self, callback, max_depth, max_messages, max_payload_bytes, max_total_bytes):
        self.callback = callback
        self.max_depth = max_depth
        self.max_messages = max_messages
        self.max_payload_bytes = max_payload_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self.visited = 0
        self.entries = 0
        self.messages = []
        self.errors = []

    def error(self, path, code):
        item = {"path": list(path), "code": code}
        # The output is bounded and contains no exception text or raw payload.
        if item not in self.errors and len(self.errors) < self.max_messages + 1:
            self.errors.append(item)

    def text(self, value, path):
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self.error(path, "unsupported_binary")
        elif value is not None:
            self.error(path, "invalid_text_field")
        return ""

    def check_unknown(self, value, path, depth=0):
        """Ignore UTF-8 metadata while reporting opaque binary without exposing it.

        raw=True returns both MessagePack strings and binary values as bytes.
        UTF-8 validation here classifies an ignored value; it never returns or
        inserts that value into a message.
        """
        if depth > self.max_depth:
            self.error(path, "extension_depth_limit")
        elif isinstance(value, bytes):
            try:
                value.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self.error(path, "unsupported_field")
        elif isinstance(value, msgpack.ExtType):
            self.error(path, "unsupported_field")
        elif isinstance(value, dict):
            for item in list(value.values())[:self.max_messages]:
                self.check_unknown(item, path, depth + 1)
        elif isinstance(value, list):
            for item in value[:self.max_messages]:
                self.check_unknown(item, path, depth + 1)

    def extension(self, value, path, depth=0):
        """Decode only named textual fields used by the message parser."""
        if depth > self.max_depth:
            self.error(path, "extension_depth_limit")
            return {}
        result = {}
        for key, item in _object(value).items():
            if isinstance(key, bytes):
                try:
                    key = key.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    self.error(path, "unsupported_field")
                    continue
            if key not in _TEXT_FIELDS and key not in _CONTAINER_FIELDS:
                # Unknown binary/string metadata is not copied into API output.
                self.check_unknown(item, path, depth)
                continue
            if isinstance(item, (str, bytes)):
                result[key] = self.text(item, path)
            elif isinstance(item, (int, float, bool)) or item is None:
                result[key] = item
            elif isinstance(item, dict):
                result[key] = self.extension(item, path, depth + 1)
            elif isinstance(item, list) and key in _CONTAINER_FIELDS:
                if len(item) > self.max_messages:
                    self.error(path, "extension_item_limit")
                result[key] = [
                    self.extension(part, path, depth + 1) for part in item[:self.max_messages]
                    if isinstance(part, dict)
                ]
            else:
                self.error(path, "unsupported_field")
        return result

    def attachments(self, raw, content, path):
        value = raw or _object(content).get("attachments", [])
        if isinstance(value, str):
            if len(value) > self.max_total_bytes * 2:
                self.error(path, "input_size_limit")
                return []
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                self.error(path, "invalid_attachments")
                value = []
        # SQLite may store the literal "[]" separately while the real
        # attachments live in the content object.
        if not value:
            value = _object(content).get("attachments", [])
        if not isinstance(value, list):
            self.error(path, "invalid_attachments")
            return []
        return value

    def unpack(self, encoded, path):
        if not isinstance(encoded, (str, bytes)) or not encoded:
            self.error(path, "missing_payload")
            return None
        if len(encoded) > ((self.max_payload_bytes + 2) // 3) * 4:
            self.error(path, "payload_size_limit")
            return None
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            self.error(path, "invalid_base64")
            return None
        if len(data) > self.max_payload_bytes:
            self.error(path, "payload_size_limit")
            return None
        if self.total_bytes + len(data) > self.max_total_bytes:
            self.error(path, "total_size_limit")
            return None
        self.total_bytes += len(data)
        try:
            model = msgpack.unpackb(
                data, raw=True, strict_map_key=False,
                max_str_len=self.max_payload_bytes, max_bin_len=self.max_payload_bytes,
                max_array_len=max(128, self.max_messages * 4),
                max_map_len=max(128, self.max_messages * 4),
                max_ext_len=self.max_payload_bytes,
            )
        except (ValueError, TypeError, msgpack.UnpackException, RecursionError):
            self.error(path, "invalid_msgpack")
            return None
        if not isinstance(model, dict):
            self.error(path, "invalid_message_model")
            return None
        return model

    def row(self, model, envelope, path):
        content_model = model.get(7)
        if not isinstance(content_model, dict) or type(model.get(1)) is not int:
            self.error(path, "invalid_message_model")
            return None
        content_type = _integer(content_model.get(1))
        source = _object(envelope.get("extension"))
        if source.get("msgId") is not None and str(source["msgId"]) != str(model[1]):
            self.error(path, "message_id_mismatch")
        content = {}
        text_model = content_model.get(2)
        if isinstance(text_model, dict) and 1 in text_model:
            content["text"] = self.text(text_model[1], path)
        photo = content_model.get(3)
        if isinstance(photo, dict):
            content.update({
                "mediaId": self.text(photo.get(1), path),
                "picSize": _integer(photo.get(2)),
                "orientation": _integer(photo.get(6)),
                "filename": self.text(photo.get(8), path),
            })
        attachments = content_model.get(7, [])
        if not isinstance(attachments, list):
            self.error(path, "invalid_attachments")
            attachments = []
        normalized = []
        for attachment in attachments[:self.max_messages]:
            if not isinstance(attachment, dict):
                self.error(path, "invalid_attachment")
                continue
            normalized.append({
                "type": _integer(attachment.get(1)),
                "url": self.text(attachment.get(2), path),
                "size": _integer(attachment.get(3)),
                "extension": self.extension(attachment.get(6), path),
            })
        if len(attachments) > self.max_messages:
            self.error(path, "attachment_limit")
        content["attachments"] = normalized
        if content_type not in _SUPPORTED_TYPES:
            self.error(path, "unsupported_content_type")
        # Opaque, unmapped content must not become an accidental text dump.
        for key, value in content_model.items():
            if key not in {1, 2, 3, 7}:
                self.check_unknown(value, path)
        creator = model.get(3) if isinstance(model.get(3), dict) else {}
        extension = self.extension(model.get(9), path)
        return {
            "primaryKey": model[1], "cid": self.text(model.get(2), path),
            "localId": 0, "mid": model[1], "senderId": _integer(creator.get(1)),
            "type": 0, "creatorType": 0, "createdAt": _integer(model.get(6)),
            "lastModify": 0, "contentType": content_type, "content": content,
            "recallStatus": 0, "atIds": "", "attachments": "",
            "extension": extension, "readStatus": 0, "sentlocaltime": 0,
        }

    def visit(self, raw, content, parent_path, depth):
        error_start = len(self.errors)
        message_start = len(self.messages)
        title, summary = "", ""
        candidates = self.attachments(raw, content, parent_path)
        child_count = 0
        for index, value in enumerate(candidates):
            path = [*parent_path, index]
            self.entries += 1
            if self.entries > 2 * self.max_messages + self.max_depth + 1:
                self.error(path, "entry_limit")
                break
            envelope = _object(value)
            extension = _object(envelope.get("extension"))
            preview_only = (
                _integer(envelope.get("type")) == 1500
                and not extension.get("message")
                and (extension.get("title") or extension.get("summary"))
            )
            if _integer(envelope.get("type")) == 101 or preview_only:
                title = title or self.text(extension.get("title"), path)
                summary = summary or self.text(extension.get("summary"), path)
                if preview_only:
                    self.error(path, "missing_payload")
                continue
            child_count += 1
            if self.visited >= self.max_messages:
                self.error(path, "message_limit")
                break
            self.visited += 1
            if depth > self.max_depth:
                self.error(path, "depth_limit")
                break
            before = len(self.errors)
            model = self.unpack(extension.get("message"), path)
            if model is None:
                continue
            row = self.row(model, envelope, path)
            if row is None:
                continue
            try:
                message = self.callback(row)
                if not isinstance(message, dict):
                    raise TypeError("message callback must return a dict")
            except Exception:
                self.error(path, "message_parse_failed")
                continue
            message = dict(message)
            message["path"] = path
            message["parent_path"] = list(parent_path)
            sender_name = self.text(extension.get("senderName"), path)
            if sender_name:
                message["sender_name"] = sender_name
            status = message.get("parse_status", "complete")
            if row["contentType"] not in _SUPPORTED_TYPES:
                status = "unsupported"
            elif len(self.errors) > before and status == "complete":
                status = "partial"
            message["parse_status"] = status
            self.messages.append(message)
            if row["contentType"] == 1500:
                nested = self.visit("", row["content"], path, depth + 1)
                message["forwarded_info"] = nested
                if nested["parse_status"] != "complete":
                    message["parse_status"] = nested["parse_status"]
                message["content"] = build_agent_content(message)
        if child_count == 0 and not any(error["code"] == "missing_payload" for error in self.errors[error_start:]):
            self.error(parent_path, "missing_payload")
        errors = self.errors[error_start:]
        total = len(self.messages) - message_start
        status = "complete"
        if errors:
            status = "partial" if total or title or summary else "unsupported"
        elif any(m.get("parse_status") != "complete" for m in self.messages[message_start:]):
            status = "partial"
        return {"title": title, "summary": summary, "total": total, "parse_status": status, "errors": errors}


def decode_forwarded(
    attachments_raw, content_data, parse_message_callback, *, max_depth=8,
    max_messages=1000, max_payload_bytes=2 * 1024 * 1024, max_total_bytes=8 * 1024 * 1024,
):
    """Return bounded, preorder child messages and explicit decoding gaps.

    ``path`` uses original attachment indices (including header positions).
    Unknown binary values and exception messages are never returned to callers.
    The callback must disable its own forwarded-message expansion.
    """
    if max_depth < 0 or min(max_messages, max_payload_bytes, max_total_bytes) < 1:
        raise ValueError("forwarded decoding limits must be positive (depth may be zero)")
    decoder = _Decoder(parse_message_callback, max_depth, max_messages, max_payload_bytes, max_total_bytes)
    result = decoder.visit(attachments_raw, content_data, [], 0)
    result["messages"] = decoder.messages
    return result

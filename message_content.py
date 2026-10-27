"""Shared, human-readable message content for MCP, exports and local search."""

import re


def _text(value):
    return value if isinstance(value, str) else ""


def _mentions(text, at_ids):
    if not isinstance(at_ids, dict):
        return text
    names = {str(uid): name for uid, name in at_ids.items() if isinstance(name, str) and name}
    if not names:
        return text
    choices = "|".join(re.escape(uid) for uid in sorted(names, key=len, reverse=True))
    return re.sub(r"@(" + choices + r")(?!\w)", lambda match: "@" + names[match[1]], text)


def _body(msg):
    info = msg.get("forwarded_info") or {}
    if info:
        title = _text(info.get("title"))
        summary = _text(info.get("summary"))
        return "\n".join(part for part in ("[合并转发] " + title, summary) if part).strip()
    return _mentions(_text(msg.get("text")), msg.get("at_ids"))


def _quote(msg):
    quote = msg.get("quote_info") or {}
    if not isinstance(quote, dict):
        return ""
    preview = _text(quote.get("source_preview"))
    if not preview:
        return ""
    sender = _text(quote.get("source_sender_name"))
    return f"[引用: {sender}] {preview}" if sender else f"[引用] {preview}"


def _image_references(msg, exported):
    info = msg.get("image_info") or {}
    images = info.get("images") or ([info] if info else [])
    paths = msg.get("image_export_paths") or []
    if not paths and msg.get("attachment_export_path"):
        paths = [msg["attachment_export_path"]]
    result = []
    for index in range(max(len(images), len(paths))):
        image = images[index] if index < len(images) and isinstance(images[index], dict) else {}
        export_path = _text(paths[index]) if index < len(paths) else ""
        local_path = _text(image.get("local_path"))
        if exported and export_path:
            result.append(f"[图片: {export_path}]")
        elif local_path and image.get("cached") is not False:
            result.append("[图片已缓存，未导出]" if exported else f"[图片: {local_path}]")
        elif image.get("cached") is True:
            result.append("[图片已缓存，路径不可用]")
        elif image.get("cached") is False:
            result.append("[图片未下载到本地]")
        else:
            result.append("[图片，缓存状态未知]")
    return result


def build_agent_content(msg, exported=False):
    """Render the parent message only; forwarded children have their own pages."""
    text = _body(msg)
    images = _image_references(msg, exported)
    if msg.get("content_type") == 2 and msg.get("message_subtype") != "emoji":
        text = "\n".join(images) if images else "[图片，缓存状态未知]"
    elif "[图片]" in text:
        for image in images:
            text = text.replace("[图片]", image, 1)
        text = text.replace("[图片]", "[图片，缓存状态未知]")
    text = text.replace("[语音]", "[语音消息]")
    attachment_lines = []
    for attachment in msg.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        name = _text(attachment.get("filename"))
        if not name:
            continue
        export_path = _text(attachment.get("export_path"))
        local_path = _text(attachment.get("local_path"))
        if exported and export_path:
            line = f"[附件: {name} | {export_path}]"
        elif local_path and attachment.get("local_available") is not False:
            line = f"[附件已缓存，未导出: {name}]" if exported else f"[附件: {name} | {local_path}]"
        elif attachment.get("local_available") is False:
            line = f"[附件未下载: {name}]"
        else:
            line = f"[附件: {name}，缓存状态未知]"
        attachment_lines.append(line)
    if attachment_lines:
        text = text.replace("[文件]", "").strip()
    return "\n".join(part for part in (_quote(msg), text, *attachment_lines) if part).strip()


def _search_text(msg):
    pieces = [_body(msg), _quote(msg)]
    for attachment in msg.get("attachments") or []:
        if isinstance(attachment, dict):
            pieces.append(_text(attachment.get("filename")))
    return "\n".join(dict.fromkeys(piece for piece in pieces if piece))


def search_parts(msg):
    """Return visible text with stable locations, excluding paths and raw data."""
    parts = [{"path": [], "text": _search_text(msg)}]
    for index, child in enumerate(msg.get("forwarded_messages") or []):
        if not isinstance(child, dict):
            continue
        path = child.get("path", [index])
        if not isinstance(path, list) or any(type(item) is not int or item < 0 for item in path):
            path = [index]
        parts.append({"path": list(path), "text": _search_text(child)})
    return parts

"""Versioned, disposable indexes of parsed local message text."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

import config

# Bump whenever message normalization or the indexed fields change.
PARSER_VERSION = "2"


def _cache_path(snapshot_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise ValueError("无效的快照 ID")
    return Path(config.DECRYPTED_DIR) / "indexes" / f"{snapshot_id}-v{PARSER_VERSION}.sqlite"


def _valid_index(path, snapshot_id):
    if not path.is_file():
        return False
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        metadata = dict(conn.execute("SELECT key, value FROM metadata"))
        valid = metadata == {"snapshot_id": snapshot_id, "parser_version": PARSER_VERSION}
        conn.execute("SELECT row_id, path, normalized FROM parts LIMIT 0")
        conn.execute("SELECT shard, source_key FROM messages LIMIT 0")
        return valid
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def _build_index(source, target, snapshot_id):
    from message_content import search_parts
    from parser import _get_all_msg_tables, _parse_message

    target.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE messages (
            row_id INTEGER PRIMARY KEY, shard TEXT NOT NULL, source_key,
            cid TEXT, mid, created_at INTEGER
        );
        CREATE TABLE parts (row_id INTEGER NOT NULL, path TEXT NOT NULL, normalized TEXT NOT NULL);
        CREATE INDEX parts_message ON parts(row_id);
        CREATE INDEX message_order ON messages(created_at DESC, cid DESC, mid DESC);
    """)
    existing = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in _get_all_msg_tables():
        if table not in existing:
            continue
        for row in source.execute(f'SELECT * FROM "{table}" WHERE recallStatus=0'):
            message = _parse_message(row, source)
            row_id = target.execute(
                "INSERT INTO messages(shard, source_key, cid, mid, created_at) VALUES (?, ?, ?, ?, ?)",
                (table, row["primaryKey"], row["cid"], row["mid"], row["createdAt"]),
            ).lastrowid
            target.executemany(
                "INSERT INTO parts VALUES (?, ?, ?)",
                [(row_id, json.dumps(part["path"]), part["text"].casefold())
                 for part in search_parts(message)],
            )
    target.executemany("INSERT INTO metadata VALUES (?, ?)", [
        ("snapshot_id", snapshot_id), ("parser_version", PARSER_VERSION),
    ])
    target.commit()


def _write_index(source, snapshot_id):
    path = _cache_path(snapshot_id).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if _valid_index(path, snapshot_id):
        return path
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        target = sqlite3.connect(temporary)
        try:
            _build_index(source, target, snapshot_id)
        finally:
            target.close()
        # Concurrent builders read the same immutable snapshot and produce
        # equivalent indexes. Each publishes only a fully closed database.
        try:
            os.replace(temporary, path)
        except OSError:
            # On Windows another builder may already have published and opened
            # this same version. Reuse it instead of replacing its open file.
            if not _valid_index(path, snapshot_id):
                raise
    finally:
        temporary.unlink(missing_ok=True)
    return path


def prepare_snapshot_index(path, snapshot_id):
    """Build before publication; never open the candidate database for writes."""
    source = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    try:
        return str(_write_index(source, snapshot_id))
    finally:
        source.close()


@contextmanager
def _open_index(source):
    from snapshots import connection_metadata

    snapshot_id = connection_metadata(source).get("snapshot_id")
    if snapshot_id:
        path = _write_index(source, snapshot_id)
        index = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    else:
        # Synthetic/in-memory callers can change rows between queries.
        index = sqlite3.connect(":memory:")
        _build_index(source, index, "memory")
    index.row_factory = sqlite3.Row
    try:
        yield index
    finally:
        index.close()


def search_indexed_messages(conn, keyword, limit=50, offset=0):
    from parser import _get_all_msg_tables, _page_result, _parse_message

    normalized = keyword.casefold()
    where = "EXISTS (SELECT 1 FROM parts WHERE parts.row_id=messages.row_id AND instr(normalized, ?) > 0)"
    with _open_index(conn) as index:
        total = index.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", (normalized,)).fetchone()[0]
        rows = index.execute(
            f"""SELECT * FROM messages WHERE {where}
                ORDER BY created_at DESC, cid DESC, mid DESC, source_key DESC, shard DESC
                LIMIT ? OFFSET ?""", (normalized, limit, offset),
        ).fetchall()
        messages = []
        allowed = set(_get_all_msg_tables())
        for match in rows:
            if match["shard"] not in allowed:
                raise ValueError("检索索引含无效分表，请重新刷新")
            source = conn.execute(
                f'SELECT * FROM "{match["shard"]}" WHERE primaryKey=? AND recallStatus=0',
                (match["source_key"],),
            ).fetchone()
            if source is None:
                raise ValueError("检索索引与快照不一致，请重新刷新")
            message = _parse_message(source, conn)
            message.pop("forwarded_messages", None)
            message["matched_paths"] = [
                json.loads(row[0]) for row in index.execute(
                    "SELECT DISTINCT path FROM parts WHERE row_id=? AND instr(normalized, ?) > 0 ORDER BY rowid",
                    (match["row_id"], normalized),
                )
            ]
            messages.append(message)
    return {"query": keyword, "mode": "text", **_page_result("messages", messages, total, limit, offset, conn)}

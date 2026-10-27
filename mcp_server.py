"""MCP server exposing DingTalk chat data as tools for AI clients.

Usage:
    python mcp_server.py                 # stdio transport (Claude Desktop / Cursor)
    python mcp_server.py --http          # streamable HTTP on 127.0.0.1:8091
"""

import argparse
import logging
import os
import sys
from typing import Annotated, Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import config
from log_utils import log_event

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "dingtalk-exporter",
    instructions=(
        "读取本机钉钉桌面客户端缓存的聊天记录。可列出会话、读取消息、全文搜索、"
        "查看统计、触发同步和导出。数据仅来自本机，未联网。"
        "查询返回 total（匹配总数）、returned（本页条数）、has_more 和 next_offset。"
        "继续读取时使用 next_offset，直到 has_more 为 false。"
        "连续翻页时传回 snapshot_id，可固定同一快照。"
        "合并转发通过 get_forwarded_messages 读取子消息，并检查 parse_status。"
        "MCP 单独运行不会自动同步；只需新数据时调用 refresh_database，"
        "按返回的 job_id 调用 get_job_status 等待完成。trigger_sync 还会导出附件。"
    ),
)

def _query(callback, snapshot_id=None):
    """Run a callback with a validated database connection."""
    from parser import DatabaseNotReadyError, get_connection

    try:
        conn = get_connection(snapshot_id=snapshot_id)
    except DatabaseNotReadyError as exc:
        raise ToolError(
            f"{exc} 首次使用请调用 refresh_database，再查询任务状态。"
        ) from exc

    try:
        from snapshots import connection_metadata
        result = callback(conn)
        if isinstance(result, dict):
            result.update(connection_metadata(conn))
        return result
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        conn.close()


@mcp.tool
def get_database_status(snapshot_id: str | None = None) -> dict:
    """检查解密数据库是否就绪（首次使用前需先同步）。"""
    from parser import get_database_status as _status

    return _status(snapshot_id=snapshot_id)


@mcp.tool
def list_conversations(
    keyword: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    snapshot_id: str | None = None,
) -> dict:
    """列出会话。keyword 匹配会话标题、单聊昵称或姓名；limit 为 1–500。
    下一页使用返回的 next_offset；has_more=false 表示已读完。"""
    from parser import get_conversations

    return _query(
        lambda conn: get_conversations(conn, limit=limit, offset=offset, keyword=keyword),
        snapshot_id=snapshot_id,
    )


@mcp.tool
def get_messages(
    conversation_id: str,
    limit: Annotated[int, Field(ge=1, le=500)] = 30,
    offset: Annotated[int, Field(ge=0)] = 0,
    since_time: int | None = None,
    until_time: int | None = None,
    snapshot_id: str | None = None,
) -> dict:
    """读取指定会话的消息，默认取最新一页，页内按时间从早到晚排列。
    limit 为 1–500；since_time < 消息时间 <= until_time，单位为毫秒。
    offset 跳过最新的若干条，使用 next_offset 向更早的页翻阅。
    has_more=false 表示已读完；传回 snapshot_id 可固定快照。
    合并转发正文请调用 get_forwarded_messages。"""
    from parser import get_messages as _get_messages

    return _query(
        lambda conn: _get_messages(
            conn,
            conversation_id,
            limit=limit,
            offset=offset,
            since_time=since_time,
            until_time=until_time,
        ),
        snapshot_id=snapshot_id,
    )


@mcp.tool
def get_forwarded_messages(
    conversation_id: str,
    message_id: str | int,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
    snapshot_id: str | None = None,
) -> dict:
    """读取合并转发子消息，按原始附件顺序分页，path/parent_path 表示嵌套位置。
    parse_status=partial 或 unsupported 表示本机内容缺失、格式未知或达到解析限制。
    下一页同时传回 next_offset 和 snapshot_id；total 只统计已解析的子消息。"""
    from parser import get_forwarded_messages as _forwarded

    return _query(
        lambda conn: _forwarded(conn, conversation_id, message_id, limit=limit, offset=offset),
        snapshot_id=snapshot_id,
    )


@mcp.tool
def search_messages(
    keyword: str,
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
    offset: Annotated[int, Field(ge=0)] = 0,
    mode: Literal["text", "raw"] = "text",
    snapshot_id: str | None = None,
) -> dict:
    """搜索可读正文、引用、转发子消息和附件名，按时间从新到旧排列。
    matched_paths 指明匹配的子消息位置；mode=raw 搜索原始 content JSON。
    limit 为 1–200；下一页传回 next_offset 和 snapshot_id。"""
    from parser import search_messages_page

    return _query(
        lambda conn: search_messages_page(conn, keyword, limit=limit, offset=offset, mode=mode),
        snapshot_id=snapshot_id,
    )


@mcp.tool
def get_stats(snapshot_id: str | None = None) -> dict:
    """获取统计信息：会话数、消息数、单聊/群聊数等。"""
    from parser import get_conversation_stats

    return _query(get_conversation_stats, snapshot_id=snapshot_id)


@mcp.tool
def get_sync_status() -> dict:
    """获取同步状态：是否正在同步、上次同步时间、上次导出路径、数据库就绪状态。"""
    from parser import get_database_status as _status
    from scheduler import get_sync_state

    state = get_sync_state()
    db_status = _status()
    state["database_ready"] = db_status["ready"]
    state["database_error"] = db_status["error"]
    return state


@mcp.tool
def refresh_database() -> dict:
    """后台刷新本机查询快照和检索索引，不导出附件。返回 job_id。
    调用 get_job_status 等待结束；result.outcome 区分 updated/unchanged/fallback/failed。"""
    from scheduler import start_refresh
    return start_refresh()


@mcp.tool
def get_job_status(job_id: str) -> dict:
    """查询指定任务状态、结果和本次错误，避免误用上次任务的导出路径。"""
    from jobs import get_job_status as _status
    try:
        return _status(job_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def trigger_sync(full: bool = False) -> dict:
    """触发一次数据同步（解密加密数据库 + 增量导出），后台执行，稍后可通过
    get_sync_status 查看进度。full=True 时执行全量同步。"""
    from scheduler import start_sync
    return {**start_sync(full=full), "full": full}


@mcp.tool
def export_conversations(
    cids: list[str],
    since_time: int | None = None,
    until_time: int | None = None,
    snapshot_id: str | None = None,
) -> dict:
    """导出指定会话为 JSON + 附件目录，后台执行。完成后可通过 get_sync_status
    的 last_export_path 字段拿到导出目录。since_time/until_time 为毫秒时间戳。"""
    from scheduler import start_export

    if not cids:
        raise ToolError("cids 不能为空")

    return {
        **start_export(cids, since_time=since_time, until_time=until_time, snapshot_id=snapshot_id),
        "selected_count": len(cids),
    }


@mcp.tool
def read_attachment(path: str, max_chars: int = 20000) -> str:
    """读取本地附件文件内容（文本类：txt/json/xml/md/log/csv 等）。
    仅允许访问钉钉数据目录或导出目录内的文件，用于把附件内容提供给模型。"""
    allowed_roots = [
        os.path.normpath(config.DINGTALK_DATA_DIR),
        os.path.normpath(config.EXPORT_DIR),
        os.path.normpath(config.DATA_DIR),
    ]
    full = os.path.normpath(path)
    if not any(
        full == root or full.startswith(root + os.sep) for root in allowed_roots
    ):
        raise ToolError("路径不在允许范围内（仅限钉钉数据目录和导出目录）")

    text_exts = {".txt", ".json", ".xml", ".md", ".log", ".csv", ".html", ".htm"}
    if os.path.splitext(full)[1].lower() not in text_exts:
        raise ToolError("仅支持读取文本类附件")

    if not os.path.isfile(full):
        raise ToolError(f"文件不存在: {full}")

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)
    except OSError as exc:
        raise ToolError(f"读取文件失败: {exc}") from exc

    if len(content) > max_chars:
        content = content[:max_chars] + "\n...[内容过长已截断]"
    return content


def main():
    parser = argparse.ArgumentParser(description="DingTalk Exporter MCP server")
    parser.add_argument("--http", action="store_true", help="run with streamable HTTP transport")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8091, help="HTTP bind port (default: 8091)")
    args = parser.parse_args()

    log_event(
        logger,
        "info",
        "mcp.starting",
        transport="http" if args.http else "stdio",
        host=args.host,
        port=args.port,
        data_dir=config.DINGTALK_DATA_DIR,
    )

    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

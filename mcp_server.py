"""MCP server exposing DingTalk chat data as tools for AI clients.

Usage:
    python mcp_server.py                 # stdio transport (Claude Desktop / Cursor)
    python mcp_server.py --http          # streamable HTTP on 127.0.0.1:8091
"""

import argparse
import logging
import os
import sys
import threading
from typing import Annotated

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
        "MCP 单独运行不会自动同步；需要更新缓存快照时调用 trigger_sync。"
    ),
)

_SYNC_LOCK = threading.Lock()


def _query(callback):
    """Run a callback with a validated database connection."""
    from parser import DatabaseNotReadyError, get_connection

    try:
        conn = get_connection()
    except DatabaseNotReadyError as exc:
        raise ToolError(
            f"{exc} 请先调用 trigger_sync 完成首次解密同步后再查询。"
        ) from exc

    try:
        return callback(conn)
    finally:
        conn.close()


@mcp.tool
def get_database_status() -> dict:
    """检查解密数据库是否就绪（首次使用前需先同步）。"""
    from parser import get_database_status as _status

    return _status()


@mcp.tool
def list_conversations(
    keyword: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """列出会话。keyword 匹配会话标题、单聊昵称或姓名；limit 为 1–500。
    下一页使用返回的 next_offset；has_more=false 表示已读完。"""
    from parser import get_conversations

    return _query(
        lambda conn: get_conversations(conn, limit=limit, offset=offset, keyword=keyword)
    )


@mcp.tool
def get_messages(
    conversation_id: str,
    limit: Annotated[int, Field(ge=1, le=500)] = 30,
    offset: Annotated[int, Field(ge=0)] = 0,
    since_time: int | None = None,
    until_time: int | None = None,
) -> dict:
    """读取指定会话的消息，默认取最新一页，页内按时间从早到晚排列。
    limit 为 1–500；since_time < 消息时间 <= until_time，单位为毫秒。
    offset 跳过最新的若干条，使用 next_offset 向更早的页翻阅。
    has_more=false 表示已读完；遍历期间请勿同步，以保持分页稳定。"""
    from parser import get_messages as _get_messages

    return _query(
        lambda conn: _get_messages(
            conn,
            conversation_id,
            limit=limit,
            offset=offset,
            since_time=since_time,
            until_time=until_time,
        )
    )


@mcp.tool
def search_messages(
    keyword: str,
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """跨全部会话搜索消息存储内容，按时间从新到旧排列。limit 为 1–200。
    total 是全部匹配数；下一页使用 next_offset，has_more=false 表示已读完。"""
    from parser import search_messages_page

    return _query(
        lambda conn: search_messages_page(conn, keyword, limit=limit, offset=offset)
    )


@mcp.tool
def get_stats() -> dict:
    """获取统计信息：会话数、消息数、单聊/群聊数等。"""
    from parser import get_conversation_stats

    return _query(get_conversation_stats)


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
def trigger_sync(full: bool = False) -> dict:
    """触发一次数据同步（解密加密数据库 + 增量导出），后台执行，稍后可通过
    get_sync_status 查看进度。full=True 时执行全量同步。"""
    from scheduler import get_sync_state

    state = get_sync_state()
    if state.get("is_syncing"):
        return {"status": "already_running"}

    with _SYNC_LOCK:
        state = get_sync_state()
        if state.get("is_syncing"):
            return {"status": "already_running"}
        import scheduler as sched

        def _run():
            sched.do_sync(full=full)

        threading.Thread(target=_run, daemon=True).start()

    return {"status": "started", "full": full}


@mcp.tool
def export_conversations(
    cids: list[str],
    since_time: int | None = None,
    until_time: int | None = None,
) -> dict:
    """导出指定会话为 JSON + 附件目录，后台执行。完成后可通过 get_sync_status
    的 last_export_path 字段拿到导出目录。since_time/until_time 为毫秒时间戳。"""
    from exporter import export_by_cids
    from scheduler import _sync_state

    if not cids:
        raise ToolError("cids 不能为空")

    def _run():
        try:
            path = export_by_cids(cids, since_time=since_time, until_time=until_time)
            _sync_state["last_export_path"] = path
        except Exception as exc:
            _sync_state["last_error"] = str(exc)
            log_event(logger, "error", "mcp.export_failed", error=exc)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "selected_count": len(cids)}


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

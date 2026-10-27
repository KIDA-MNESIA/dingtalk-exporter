import os
import logging
import subprocess
import shutil
import stat
import sys
from typing import Literal
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

import config
from log_utils import log_event
from parser import (
    DatabaseNotReadyError,
    get_connection,
    get_conversations,
    get_messages,
    get_forwarded_messages,
    search_messages_page,
    get_conversation_stats,
    get_database_status,
)
from scheduler import get_sync_state, start_sync, start_refresh, start_export, setup_scheduler

logger = logging.getLogger(__name__)

app = FastAPI(title="钉钉聊天记录导出", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Scheduler (initialized lazily)
_scheduler = None


def _remove_readonly(func, path, exc_info):
    """Retry removal after clearing the read-only flag on Windows exports."""
    exc = exc_info[1]
    if not isinstance(exc, PermissionError):
        raise exc

    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        raise exc


def _query_db(callback, snapshot_id=None):
    """Run a callback with a validated database connection."""
    try:
        conn = get_connection(snapshot_id=snapshot_id)
    except DatabaseNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        from snapshots import connection_metadata
        result = callback(conn)
        if isinstance(result, dict):
            result.update(connection_metadata(conn))
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@app.on_event("startup")
async def startup():
    global _scheduler
    _scheduler = setup_scheduler(app)
    _scheduler.start()
    log_event(logger, "info", "api.startup_completed")


@app.on_event("shutdown")
async def shutdown():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        log_event(logger, "info", "api.shutdown_completed")


# --- Static files ---

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return HTMLResponse("<h1>DingTalk Exporter</h1><p>Frontend not found</p>")


# Mount static files
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- API Routes ---


def _job_response(result):
    if result["status"] == "already_running":
        raise HTTPException(status_code=409, detail="已有刷新或导出任务正在运行，请等待完成后重试。")
    if result["status"] == "failed":
        raise HTTPException(status_code=503, detail=result.get("error") or "无法启动后台任务")
    return result

@app.get("/api/config")
async def api_config():
    """Return public configuration for the frontend."""
    return {"user_uid": config.USER_UID}


@app.get("/api/conversations")
async def api_conversations(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    keyword: str = Query(None),
    snapshot_id: str = Query(None),
):
    return _query_db(
        lambda conn: get_conversations(conn, limit=limit, offset=offset, keyword=keyword),
        snapshot_id=snapshot_id,
    )


@app.get("/api/conversations/{cid}/messages")
async def api_messages(
    cid: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    since: int = Query(None, description="Since timestamp (ms)"),
    until: int = Query(None, description="Until timestamp (ms)"),
    snapshot_id: str = Query(None),
):
    return _query_db(
        lambda conn: get_messages(
            conn, cid, limit=limit, offset=offset, since_time=since, until_time=until
        ),
        snapshot_id=snapshot_id,
    )


@app.get("/api/conversations/{cid}/messages/{mid}/forwarded")
async def api_forwarded_messages(
    cid: str, mid: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    snapshot_id: str = Query(None),
):
    return _query_db(
        lambda conn: get_forwarded_messages(conn, cid, mid, limit=limit, offset=offset),
        snapshot_id=snapshot_id,
    )


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    mode: Literal["text", "raw"] = "text",
    snapshot_id: str = Query(None),
):
    return _query_db(
        lambda conn: search_messages_page(conn, q, limit=limit, offset=offset, mode=mode),
        snapshot_id=snapshot_id,
    )


@app.get("/api/stats")
async def api_stats(snapshot_id: str = Query(None)):
    return _query_db(get_conversation_stats, snapshot_id=snapshot_id)


@app.get("/api/sync/status")
async def api_sync_status():
    state = get_sync_state()
    db_status = get_database_status()
    state["database_ready"] = db_status["ready"]
    state["database_error"] = db_status["error"]
    state["database"] = db_status
    state["runtime"] = config.get_runtime_diagnostics()
    return state


@app.post("/api/sync/trigger")
async def api_sync_trigger(full: bool = Query(False)):
    result = start_sync(full=full)
    return {**_job_response(result), "full": full}


@app.post("/api/database/refresh")
async def api_refresh_database():
    return _job_response(start_refresh())


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    from jobs import get_job_status, JobNotFoundError
    try:
        return get_job_status(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/export/selected")
async def api_export_selected(body: dict):
    """Export only the selected conversation IDs as JSON."""
    cids = body.get("cids", [])
    since_time = body.get("since_time")  # optional ms timestamp
    until_time = body.get("until_time")  # optional ms timestamp
    if not cids:
        raise HTTPException(status_code=400, detail="No conversations selected")

    result = start_export(cids, since_time=since_time, until_time=until_time, snapshot_id=body.get("snapshot_id"))
    return {**_job_response(result), "selected_count": len(cids)}


@app.get("/api/attachments/{path:path}")
async def api_attachment(path: str):
    """Serve attachment files from the DingTalk data directory."""
    # Security: only allow access to specific subdirectories
    allowed_dirs = ["ImageFiles", "AudioFiles", "VideoFiles", "resource_cache"]
    parts = path.replace("\\", "/").split("/")
    if parts[0] not in allowed_dirs:
        raise HTTPException(status_code=403, detail="Access denied")

    full_path = os.path.join(config.DINGTALK_DATA_DIR, path)
    full_path = os.path.normpath(full_path)

    # Security: ensure the path doesn't escape the data directory
    if not full_path.startswith(os.path.normpath(config.DINGTALK_DATA_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(full_path)


@app.get("/api/local-file")
async def api_local_file(path: str = Query(..., min_length=1)):
    """Serve a local file from an absolute path (for downloaded attachments)."""
    try:
        full_path = os.path.normpath(path)

        # Security: only allow local drive paths, no UNC
        if full_path.startswith("\\\\"):
            raise HTTPException(status_code=403, detail="UNC paths not allowed")

        # Only allow common document/file extensions
        ext = os.path.splitext(full_path)[1].lower()
        allowed_exts = {
            ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".pdf",
            ".txt", ".csv", ".json", ".xml", ".html", ".htm",
            ".zip", ".rar", ".7z", ".gz", ".tar",
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
            ".mp4", ".mp3", ".wav", ".avi", ".m4a",
        }
        if ext not in allowed_exts:
            raise HTTPException(status_code=403, detail=f"File type '{ext}' not allowed")

        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")

        filename = os.path.basename(full_path)
        return FileResponse(full_path, filename=filename)
    except HTTPException:
        raise
    except Exception as e:
        log_event(logger, "error", "api.local_file_failed", path=path, error=e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/exports/{name}/download")
async def api_export_download(name: str):
    """Download an export as a zip file."""
    import zipfile
    import tempfile
    import io as _io

    export_path = os.path.join(config.EXPORT_DIR, name)
    export_path = os.path.normpath(export_path)
    if not export_path.startswith(os.path.normpath(config.EXPORT_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(export_path):
        raise HTTPException(status_code=404, detail="Export not found")

    # If it's a directory, zip it
    if os.path.isdir(export_path):
        zip_filename = f"{name}.zip"
        # Create zip in memory
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(export_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, config.EXPORT_DIR)
                    zf.write(file_path, arcname)
        buf.seek(0)

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={zip_filename}"},
        )

    # If it's a single file (legacy JSON export)
    return FileResponse(
        export_path,
        media_type="application/json",
        filename=os.path.basename(export_path),
    )


@app.post("/api/exports/{name}/open-folder")
async def api_export_open_folder(name: str):
    """Open the export directory in the local file manager."""
    export_path = os.path.join(config.EXPORT_DIR, name)
    export_path = os.path.normpath(export_path)
    if not export_path.startswith(os.path.normpath(config.EXPORT_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isdir(export_path):
        raise HTTPException(status_code=404, detail="Export directory not found")

    try:
        if sys.platform == "win32":
            os.startfile(export_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", export_path])
        else:
            subprocess.Popen(["xdg-open", export_path])
    except Exception as e:
        log_event(logger, "error", "api.open_export_folder_failed", path=export_path, error=e)
        logger.error("api.open_export_folder_failed_traceback", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {"status": "opened", "path": export_path}


@app.delete("/api/exports/{name}")
async def api_delete_export(name: str):
    """Delete an exported directory or legacy export file."""
    export_path = os.path.join(config.EXPORT_DIR, name)
    export_path = os.path.normpath(export_path)
    if not export_path.startswith(os.path.normpath(config.EXPORT_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(export_path):
        raise HTTPException(status_code=404, detail="Export not found")

    try:
        if os.path.isdir(export_path):
            shutil.rmtree(export_path, onerror=_remove_readonly)
        else:
            os.chmod(export_path, stat.S_IWRITE)
            os.remove(export_path)
    except Exception as e:
        log_event(logger, "error", "api.delete_export_failed", path=export_path, error=e)
        logger.error("api.delete_export_failed_traceback", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {"status": "deleted", "name": name}


@app.get("/api/exports/{filename}")
async def api_export_file(filename: str):
    """Download an exported JSON file (legacy single-file exports)."""
    filepath = os.path.join(config.EXPORT_DIR, filename)
    filepath = os.path.normpath(filepath)
    if not filepath.startswith(os.path.normpath(config.EXPORT_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, media_type="application/json", filename=filename)


@app.get("/api/exports")
async def api_list_exports():
    """List all exports (directories and legacy JSON files)."""
    exports = []
    if os.path.isdir(config.EXPORT_DIR):
        for f in os.listdir(config.EXPORT_DIR):
            fp = os.path.join(config.EXPORT_DIR, f)
            if f == "latest.json":
                continue
            if os.path.isdir(fp):
                # Directory-type export (new format)
                json_path = os.path.join(fp, "export.json")
                if os.path.exists(json_path):
                    exports.append({
                        "filename": f,
                        "type": "directory",
                        "size": os.path.getsize(json_path),
                        "modified": os.path.getmtime(fp),
                        "download_url": f"/api/exports/{f}/download",
                    })
            elif f.endswith(".json"):
                # Legacy single-file export
                exports.append({
                    "filename": f,
                    "type": "file",
                    "size": os.path.getsize(fp),
                    "modified": os.path.getmtime(fp),
                    "download_url": f"/api/exports/{f}",
                })
    exports.sort(key=lambda item: item["modified"], reverse=True)
    return {"exports": exports}

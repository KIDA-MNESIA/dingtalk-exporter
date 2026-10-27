"""Refresh/export operations and their legacy status view.

All writer entry points use jobs.py. Export cursors describe completed exports;
checking or publishing a newer database never advances those cursors.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import uuid

import config
import jobs
from log_utils import log_event

logger = logging.getLogger(__name__)

_DEFAULT_STATE = {
    "last_sync_time": None,
    "last_sync_time_str": None,
    "last_export_path": None,
    "sync_count": 0,
    "is_syncing": False,
    "last_error": None,
    "next_sync_time": None,
    "last_refresh_time": None,
    "last_refresh_attempt_time": None,
    "last_refresh_outcome": None,
    "last_snapshot_id": None,
    "last_successful_export_path": None,
}
_sync_state = dict(_DEFAULT_STATE)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _apply_sync_overlap(timestamp_ms):
    if not timestamp_ms:
        return 0
    return max(0, int(timestamp_ms) - max(0, int(config.SYNC_OVERLAP_SECONDS) * 1000))


def _read_state():
    state = dict(_DEFAULT_STATE)
    try:
        with open(config.SYNC_STATE_FILE, encoding="utf-8") as stream:
            state.update(json.load(stream))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log_event(logger, "warning", "scheduler.state_load_failed", error=exc)
    return state


def _load_state():
    _sync_state.clear()
    _sync_state.update(_read_state())


def _save_state(state=None):
    """Called only inside an admitted writer job; publish one complete state."""
    state = dict(_sync_state if state is None else state)
    path = Path(config.SYNC_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_state.update(state)


def get_sync_state():
    """Read the shared latest job rather than a process-local busy flag."""
    state = _read_state()
    state["next_sync_time"] = _sync_state.get("next_sync_time")
    state["is_syncing"] = False
    job = jobs.get_last_job()
    if job:
        result = job.get("result") or {}
        state.update(
            job_id=job["job_id"], operation=job["operation"], status=job["status"],
            is_syncing=job["status"] == "running",
            last_error=job.get("error") or result.get("error"),
            last_export_path=result.get("export_path"),
            outcome=result.get("outcome"),
            snapshot_id=result.get("snapshot_id"),
            snapshot_time=result.get("snapshot_time"),
            latest_message_at=result.get("latest_message_at"),
        )
    return state


def _record_refresh(result):
    state = _read_state()
    state["last_refresh_attempt_time"] = _now()
    state["last_refresh_outcome"] = result["outcome"]
    if result["outcome"] in ("updated", "unchanged"):
        state["last_refresh_time"] = state["last_refresh_attempt_time"]
        state["last_snapshot_id"] = result.get("snapshot_id")
    _save_state(state)


def _refresh():
    from decrypt import refresh_database

    result = refresh_database()
    _record_refresh(result)
    return result


def _sync(full=False):
    import exporter

    state = _read_state()
    previous_cursor = state["last_sync_time"] or 0
    refreshed = _refresh()
    if refreshed["outcome"] not in ("updated", "unchanged"):
        # Keep the old queryable snapshot, but do not call it a fresh export.
        return refreshed
    snapshot_id = refreshed["snapshot_id"]
    exported_latest = None
    exported_count = None
    if full or not previous_cursor:
        export_path = exporter.export_all(snapshot_id=snapshot_id)
        next_cursor = _apply_sync_overlap(refreshed.get("latest_message_at") or 0)
    else:
        exported = exporter.export_incremental(previous_cursor, snapshot_id=snapshot_id)
        export_path = exported["path"]
        exported_latest = exported.get("max_created_at")
        exported_count = exported.get("message_count")
        next_cursor = (
            _apply_sync_overlap(exported_latest)
            if exported_latest is not None else previous_cursor
        )
    # Re-read after _refresh so its timestamps are retained.
    state = _read_state()
    state.update(
        last_sync_time=next_cursor, last_sync_time_str=_now(),
        sync_count=int(state.get("sync_count") or 0) + 1,
        is_syncing=False, last_error=None, last_export_path=export_path,
    )
    if export_path:
        state["last_successful_export_path"] = export_path
    _save_state(state)
    return {
        **refreshed, "export_path": export_path,
        "exported_messages": exported_count, "exported_latest_message_at": exported_latest,
    }


def _export(cids=None, since_time=None, until_time=None, snapshot_id=None):
    import exporter
    from snapshots import resolve_snapshot

    snapshot = resolve_snapshot(snapshot_id=snapshot_id)
    if cids is None:
        path = exporter.export_all(snapshot_id=snapshot["snapshot_id"])
    else:
        path = exporter.export_by_cids(
            cids, since_time=since_time, until_time=until_time,
            snapshot_id=snapshot["snapshot_id"],
        )
    state = _read_state()
    state.update(last_export_path=path, last_successful_export_path=path, last_error=None)
    _save_state(state)
    return {
        "export_path": path, "snapshot_id": snapshot["snapshot_id"],
        "snapshot_time": snapshot.get("snapshot_time"),
        "latest_message_at": snapshot.get("latest_message_at"),
    }


def start_refresh():
    return jobs.start_job("refresh", _refresh)


def start_sync(full=False):
    return {**jobs.start_job("sync", lambda: _sync(full)), "full": full}


def start_export(cids, since_time=None, until_time=None, snapshot_id=None):
    if not cids:
        raise ValueError("cids 不能为空")
    selected = list(cids)
    return {
        **jobs.start_job(
            "export", lambda: _export(selected, since_time, until_time, snapshot_id)
        ),
        "selected_count": len(selected),
    }


def start_full_export(snapshot_id=None):
    return jobs.start_job("export_all", lambda: _export(snapshot_id=snapshot_id))


def do_sync(full=False):
    """Synchronous entry for APScheduler, using the same writer admission."""
    response = jobs.start_job("sync", lambda: _sync(full), background=False)
    if response["status"] != "started":
        return False
    job = jobs.get_job_status(response["job_id"])
    return job["status"] == "succeeded" and (job.get("result") or {}).get("outcome") in (
        "updated", "unchanged"
    )


def setup_scheduler(app=None):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=do_sync, trigger=IntervalTrigger(hours=config.SYNC_INTERVAL_HOURS),
        id="dingtalk_sync", name="DingTalk DB Sync", replace_existing=True,
    )
    _sync_state["next_sync_time"] = f"every {config.SYNC_INTERVAL_HOURS} hours"
    return scheduler


_load_state()

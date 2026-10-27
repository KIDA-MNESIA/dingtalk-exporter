"""Durable background jobs shared by the Web and MCP processes.

The short registry lock protects admission and state transitions. The work
lock stays open for the complete callback, and the OS releases it if the
process exits. A stale job is recovered only after acquiring that work lock.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

import config


class JobNotFoundError(ValueError):
    pass


class _FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self, timeout=0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.stream = stream
                return True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    stream.close()
                    return False
                time.sleep(0.01)

    def release(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def _directory():
    return Path(config.DATA_DIR) / "jobs"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None


def _job_path(directory, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise JobNotFoundError("无效的任务 ID")
    return directory / f"{job_id}.json"


def _read_pointer(directory, name):
    pointer = _read_json(directory / name)
    if not pointer:
        return None
    return _read_json(_job_path(directory, pointer["job_id"]))


@contextmanager
def _registry(directory):
    lock = _FileLock(directory / "registry.lock")
    if not lock.acquire(timeout=5):
        raise RuntimeError("任务状态正忙，请稍后重试")
    try:
        yield
    finally:
        lock.release()


def _recover_stale_locked(directory):
    """Caller holds registry.lock; never infer liveness from a PID alone."""
    current = _read_pointer(directory, "active.json")
    if not current:
        return
    work_lock = _FileLock(directory / "work.lock")
    if not work_lock.acquire():
        return
    try:
        if current["status"] == "running":
            current.update(
                status="failed", finished_at=_now(), result=None,
                error="任务进程已退出，操作未确认完成；请重新执行任务。",
            )
            _write_json(_job_path(directory, current["job_id"]), current)
        (directory / "active.json").unlink(missing_ok=True)
    finally:
        work_lock.release()


def _finish(directory, job, work_lock, result=None, error=None):
    try:
        with _registry(directory):
            job.update(
                status="failed" if error else "succeeded",
                finished_at=_now(), result=result, error=error,
            )
            _write_json(_job_path(directory, job["job_id"]), job)
            (directory / "active.json").unlink(missing_ok=True)
            # Admission cannot observe an empty active pointer with a held lock.
            work_lock.release()
    finally:
        work_lock.release()


def _run(directory, job, work_lock, callback):
    try:
        result = callback()
        if result is None:
            result = {}
        if not isinstance(result, dict):
            raise TypeError("任务结果必须是字典")
        json.dumps(result, ensure_ascii=False)
        error = result.get("error") if result.get("outcome") == "failed" else None
        if result.get("outcome") == "failed" and not error:
            error = "任务执行失败"
    except Exception as exc:
        result, error = None, str(exc) or type(exc).__name__
    _finish(directory, job, work_lock, result, error)


def start_job(operation, callback, background=True):
    """Register before starting work; accept at most one writer across processes."""
    directory = _directory()
    with _registry(directory):
        _recover_stale_locked(directory)
        work_lock = _FileLock(directory / "work.lock")
        if not work_lock.acquire():
            current = _read_pointer(directory, "active.json")
            if not current:
                raise RuntimeError("任务锁已占用，但没有可读取的任务状态")
            return {"status": "already_running", "job_id": current["job_id"]}
        job = {
            "job_id": uuid.uuid4().hex,
            "operation": operation,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "owner_pid": os.getpid(),
            "result": None,
            "error": None,
        }
        try:
            _write_json(_job_path(directory, job["job_id"]), job)
            _write_json(directory / "active.json", {"job_id": job["job_id"]})
            _write_json(directory / "last.json", {"job_id": job["job_id"]})
            if background:
                threading.Thread(
                    target=_run, args=(directory, job, work_lock, callback),
                    name=f"dingtalk-{operation}-{job['job_id'][:8]}", daemon=True,
                ).start()
        except Exception as exc:
            try:
                job.update(status="failed", finished_at=_now(), error=str(exc))
                _write_json(_job_path(directory, job["job_id"]), job)
                (directory / "active.json").unlink(missing_ok=True)
            finally:
                work_lock.release()
            return {"status": "failed", "job_id": job["job_id"], "error": str(exc)}
    if not background:
        _run(directory, job, work_lock, callback)
    return {"status": "started", "job_id": job["job_id"]}


def get_job_status(job_id):
    directory = _directory()
    path = _job_path(directory, job_id)
    with _registry(directory):
        _recover_stale_locked(directory)
        job = _read_json(path)
    if job is None:
        raise JobNotFoundError("找不到该任务")
    return job


def get_current_job():
    directory = _directory()
    with _registry(directory):
        _recover_stale_locked(directory)
        return _read_pointer(directory, "active.json")


def get_last_job():
    directory = _directory()
    with _registry(directory):
        _recover_stale_locked(directory)
        return _read_pointer(directory, "last.json")

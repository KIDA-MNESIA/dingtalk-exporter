"""Validated, immutable query snapshots with an atomic current-version pointer."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

import config

logger = logging.getLogger(__name__)
_VERSION_ID = re.compile(r"s-[0-9a-f]{32}\Z")
_LEGACY_ID = re.compile(r"legacy-[0-9a-f]{32}\Z")
_MESSAGE_TABLE = re.compile(r"tbmsg_\d{3}\Z")
_RETENTION_SECONDS = 24 * 60 * 60
_state_lock = threading.RLock()
_publish_lock = threading.Lock()
_active_readers = {}
_legacy_cache = {}


class SnapshotError(RuntimeError):
    """A snapshot cannot be validated, opened, or published."""


class SnapshotNotFoundError(SnapshotError):
    code = "snapshot_not_found"


class SnapshotExpiredError(SnapshotError):
    code = "snapshot_expired"

    def __init__(self, snapshot_id):
        super().__init__(f"快照 {snapshot_id!r} 已过期或不存在，请从第一页重新查询。")


def _root():
    return Path(config.DECRYPTED_DIR).resolve() / "snapshots"


def _utc_time(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly_connection(path, immutable=False, factory=sqlite3.Connection):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, factory=factory)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _validate_connection(conn, quick_check=True):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        "tbconversation": {"cid", "type", "title", "memberCount", "createAt", "lastModify", "unreadCount", "top", "ownerId", "isNotification", "extension", "status"},
        "tbuser_profile_v2": {"uid", "nick", "realName", "iconMediaId", "mobile", "email"},
    }
    message_tables = sorted(table for table in tables if _MESSAGE_TABLE.fullmatch(table) and int(table[-3:]) < 128)
    if not message_tables:
        raise SnapshotError("候选数据库缺少消息分表，未发布新快照。")
    message_columns = {"primaryKey", "cid", "localId", "mid", "senderId", "type", "creatorType", "createdAt", "lastModify", "contentType", "content", "recallStatus", "atIds", "attachments", "extension", "readStatus", "sentlocaltime"}
    required.update({table: message_columns for table in message_tables})
    for table, columns in required.items():
        if table not in tables:
            raise SnapshotError(f"候选数据库缺少表 {table}，未发布新快照。")
        available = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        missing = columns - available
        if missing:
            raise SnapshotError(f"候选数据库的表 {table} 缺少字段 {', '.join(sorted(missing))}。")
    if quick_check:
        checks = [row[0] for row in conn.execute("PRAGMA quick_check")]
        if checks != ["ok"]:
            raise SnapshotError("候选数据库未通过 quick_check，未发布新快照。")
    latest = 0
    for table in message_tables:
        value = conn.execute(f'SELECT MAX(createdAt) FROM "{table}"').fetchone()[0]
        latest = max(latest, int(value or 0))
    return latest


def validate_database(path):
    """Return the latest message timestamp only after schema and integrity checks."""
    if not Path(path).is_file():
        raise SnapshotNotFoundError("未找到聊天数据库，请先刷新数据库。")
    try:
        conn = _readonly_connection(path)
        try:
            return _validate_connection(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SnapshotError("聊天数据库无法读取或已损坏，未发布新快照。") from exc


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data
    except (OSError, ValueError) as exc:
        raise SnapshotError("快照元数据无法读取，请重新刷新数据库。") from exc


def _read_current():
    path = _root() / "current.json"
    if not path.exists():
        return None
    current = _read_json(path)
    if not _VERSION_ID.fullmatch(str(current.get("snapshot_id", ""))):
        raise SnapshotError("当前快照指针无效，请重新刷新数据库。")
    return current


def _version_info(snapshot_id):
    if not _VERSION_ID.fullmatch(str(snapshot_id)):
        raise SnapshotExpiredError(snapshot_id)
    path = _root() / f"{snapshot_id}.db"
    metadata_path = _root() / f"{snapshot_id}.json"
    if not path.is_file() or not metadata_path.is_file():
        raise SnapshotExpiredError(snapshot_id)
    metadata = _read_json(metadata_path)
    if metadata.get("snapshot_id") != snapshot_id or not isinstance(metadata.get("snapshot_time"), str) or not isinstance(metadata.get("latest_message_at"), int):
        raise SnapshotError("快照元数据与版本不一致。")
    return {"path": str(path), "snapshot_id": snapshot_id, "snapshot_time": metadata["snapshot_time"], "latest_message_at": metadata["latest_message_at"]}


def _legacy_info(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise SnapshotNotFoundError("未找到聊天数据库，请先刷新数据库。")
    identity = [str(path)]
    for sibling in (path, Path(str(path) + "-wal")):
        try:
            stat = sibling.stat()
            # Opening a WAL-mode DB can create an empty WAL without changing
            # any committed rows. Treat that like an absent WAL.
            identity.append(None if sibling != path and stat.st_size == 0 else (stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            identity.append(None)
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
    snapshot_id = f"legacy-{fingerprint}"
    if snapshot_id not in _legacy_cache:
        _legacy_cache.clear()
        _legacy_cache[snapshot_id] = {
            "path": str(path), "snapshot_id": snapshot_id,
            "snapshot_time": _utc_time(path.stat().st_mtime),
            "latest_message_at": validate_database(path),
        }
    return dict(_legacy_cache[snapshot_id])


def resolve_snapshot(snapshot_id=None, db_path=None):
    """Resolve an exact version, an explicit legacy path, or the current snapshot."""
    with _state_lock:
        explicit_path = Path(db_path).resolve() if db_path is not None else None
        if snapshot_id is not None and _VERSION_ID.fullmatch(str(snapshot_id)):
            info = _version_info(snapshot_id)
            if explicit_path is not None and explicit_path != Path(config.DECRYPTED_DB_PATH).resolve() and explicit_path != Path(info["path"]):
                raise SnapshotExpiredError(snapshot_id)
            return info
        if snapshot_id is not None and not _LEGACY_ID.fullmatch(str(snapshot_id)):
            raise SnapshotExpiredError(snapshot_id)
        if snapshot_id is None and explicit_path is not None and explicit_path.parent == _root() and _VERSION_ID.fullmatch(explicit_path.stem):
            return _version_info(explicit_path.stem)
        use_current = explicit_path is None or explicit_path == Path(config.DECRYPTED_DB_PATH).resolve()
        if snapshot_id is None and use_current:
            current = _read_current()
            if current:
                return _version_info(current["snapshot_id"])
        try:
            info = _legacy_info(explicit_path or config.DECRYPTED_DB_PATH)
        except SnapshotNotFoundError:
            if snapshot_id is not None:
                raise SnapshotExpiredError(snapshot_id) from None
            raise
        if snapshot_id is not None and info["snapshot_id"] != snapshot_id:
            raise SnapshotExpiredError(snapshot_id)
        return info


class SnapshotConnection(sqlite3.Connection):
    snapshot_info = None
    _snapshot_closed = False

    def close(self):
        try:
            super().close()
        finally:
            if self.snapshot_info is not None and not self._snapshot_closed:
                with _state_lock:
                    path = self.snapshot_info["path"]
                    _active_readers[path] = max(0, _active_readers.get(path, 1) - 1)
                    if not _active_readers[path]:
                        del _active_readers[path]
                self._snapshot_closed = True


def open_snapshot(db_path=None, snapshot_id=None):
    with _state_lock:
        info = resolve_snapshot(snapshot_id=snapshot_id, db_path=db_path)
        conn = None
        try:
            immutable = bool(_VERSION_ID.fullmatch(info["snapshot_id"]))
            conn = _readonly_connection(info["path"], immutable=immutable, factory=SnapshotConnection)
            if not immutable:
                # BEGIN is deferred; the read pins the committed WAL view. A
                # later commit must not change data under this connection's ID.
                conn.execute("BEGIN")
                conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
                try:
                    confirmed = _legacy_info(info["path"])
                except (SnapshotError, OSError) as exc:
                    raise SnapshotExpiredError(info["snapshot_id"]) from exc
                if confirmed["snapshot_id"] != info["snapshot_id"]:
                    raise SnapshotExpiredError(info["snapshot_id"])
        except SnapshotError:
            if conn is not None:
                conn.close()
            raise
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise SnapshotError("无法打开查询快照。") from exc
        conn.snapshot_info = info
        _active_readers[info["path"]] = _active_readers.get(info["path"], 0) + 1
        return conn


def connection_metadata(conn):
    info = getattr(conn, "snapshot_info", None)
    if not info:
        return {}
    return {key: info[key] for key in ("snapshot_id", "snapshot_time", "latest_message_at")}


def _atomic_json(path, payload):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _publish_guard():
    """Serialize publishers in all processes without locking immutable readers."""
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    with _publish_lock, open(root / ".publish.lock", "a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"\0")
            lock.flush()
        locked = False
        deadline = time.monotonic() + 30
        try:
            while not locked:
                try:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise SnapshotError("另一个进程正在发布快照，请稍后重试。") from exc
                    time.sleep(0.05)
            yield
        finally:
            if locked:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _remove_owned_file(path):
    for sibling in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        try:
            sibling.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove inactive snapshot artifact: %s", sibling)


def _remove_snapshot_indexes(snapshot_id):
    if not _VERSION_ID.fullmatch(snapshot_id):
        return
    index_root = Path(config.DECRYPTED_DIR).resolve() / "indexes"
    for path in index_root.glob(f"{snapshot_id}-v*.sqlite"):
        _remove_owned_file(path)


def _prune_snapshots(current, now=None):
    keep = {current["snapshot_id"], current.get("previous_snapshot_id")}
    cutoff = (time.time() if now is None else now) - _RETENTION_SECONDS
    with _state_lock:
        for metadata_path in _root().glob("s-*.json"):
            snapshot_id = metadata_path.stem
            if not _VERSION_ID.fullmatch(snapshot_id) or snapshot_id in keep:
                continue
            path = _root() / f"{snapshot_id}.db"
            if _active_readers.get(str(path)):
                continue
            try:
                metadata = _read_json(metadata_path)
                created = datetime.fromisoformat(metadata["snapshot_time"]).timestamp()
                if created >= cutoff:
                    continue
                # Windows rejects removal while another process reads this file.
                path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                _remove_snapshot_indexes(snapshot_id)
            except (OSError, ValueError, KeyError, SnapshotError):
                continue


def publish_snapshot(candidate_path, prepare_index=None):
    """Merge committed WAL, validate and index privately, then atomically publish."""
    with _publish_guard():
        snapshot_id = f"s-{uuid.uuid4().hex}"
        root = _root()
        staged = root / f".{snapshot_id}.pending.db"
        final_path = root / f"{snapshot_id}.db"
        metadata_path = root / f"{snapshot_id}.json"
        published = False
        try:
            source = _readonly_connection(candidate_path)
            try:
                source.execute("BEGIN")
                _validate_connection(source)
                target = sqlite3.connect(staged)
                try:
                    source.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
                    target.commit()
                    latest = _validate_connection(target)
                finally:
                    target.close()
            finally:
                source.close()
            source_digest = _file_sha256(staged)
            try:
                current_pointer = _read_current()
            except SnapshotError:
                # A valid replacement can repair a damaged pointer; failed
                # preparation still leaves the previous pointer bytes intact.
                current_pointer = None
            previous_usable = False
            try:
                previous = resolve_snapshot()
            except SnapshotError:
                previous = None
                # A broken legacy DB must not prevent publishing a valid repair.
                # For an existing version, retain its known timestamp when its
                # data file is missing, so repairs still reject time rollback.
                if current_pointer:
                    try:
                        old = _read_json(root / f'{current_pointer["snapshot_id"]}.json')
                        previous = {
                            "path": str(root / f'{current_pointer["snapshot_id"]}.db'),
                            "snapshot_id": current_pointer["snapshot_id"],
                            "snapshot_time": old["snapshot_time"],
                            "latest_message_at": int(old["latest_message_at"]),
                        }
                    except (SnapshotError, KeyError, TypeError, ValueError):
                        pass
            if previous:
                try:
                    validate_database(previous["path"])
                    previous_usable = True
                except SnapshotError:
                    logger.warning("Replacing an unreadable prior snapshot with a validated candidate")
            if previous and latest < previous["latest_message_at"]:
                raise SnapshotError("候选快照的最新消息时间早于当前快照，已保留当前快照。")
            if current_pointer:
                try:
                    old_metadata = _read_json(root / f'{current_pointer["snapshot_id"]}.json')
                except SnapshotError:
                    old_metadata = {}
                if previous_usable and old_metadata.get("source_sha256") == source_digest:
                    if prepare_index is not None:
                        prepare_index(previous["path"], previous["snapshot_id"])
                    _prune_snapshots(current_pointer)
                    return {"outcome": "unchanged", **previous}
            if prepare_index is not None:
                prepare_index(str(staged), snapshot_id)
            # The callback may write tables, but may not leave a WAL-dependent DB.
            target = sqlite3.connect(staged)
            try:
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
                if _validate_connection(target) != latest:
                    raise SnapshotError("索引准备改变了消息数据，未发布新快照。")
            finally:
                target.close()
            metadata = {
                "snapshot_id": snapshot_id, "snapshot_time": _utc_time(),
                "latest_message_at": latest, "source_sha256": source_digest,
            }
            os.replace(staged, final_path)
            _atomic_json(metadata_path, metadata)
            pointer = {"schema_version": 1, "snapshot_id": snapshot_id, "previous_snapshot_id": previous["snapshot_id"] if previous else None}
            with _state_lock:
                _atomic_json(root / "current.json", pointer)
                published = True
            _prune_snapshots(pointer)
            return {"outcome": "updated", **_version_info(snapshot_id)}
        except (sqlite3.Error, OSError) as exc:
            raise SnapshotError(f"新快照发布失败，当前快照保持不变：{exc}") from exc
        finally:
            _remove_owned_file(staged)
            if not published:
                _remove_owned_file(final_path)
                metadata_path.unlink(missing_ok=True)
                _remove_snapshot_indexes(snapshot_id)

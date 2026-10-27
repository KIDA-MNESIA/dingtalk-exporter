import os
import shutil
import logging
import time
import tempfile
import base64
import hashlib
import json
import struct
import sqlite3

from Crypto.Cipher import AES

import config
from log_utils import log_event

logger = logging.getLogger(__name__)

PAGE_SIZE = 4096
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
V3_PBKDF2_SALT = b"666DingTalk888"[:8]
V3_PBKDF2_ITERATIONS = 1000
V3_PBKDF2_KEYLEN = 32


def copy_encrypted_db(retry_count=None, retry_delay=None):
    """Copy encrypted database files to a temp directory to avoid lock conflicts."""
    if retry_count is None:
        retry_count = config.COPY_RETRY_COUNT
    if retry_delay is None:
        retry_delay = config.COPY_RETRY_DELAY

    os.makedirs(config.DECRYPTED_DIR, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix="dingtalk_encrypt_", dir=config.DECRYPTED_DIR)
    dest_db = os.path.join(temp_dir, "dingtalk_encrypted.db")

    if not os.path.isfile(config.ENCRYPTED_DB):
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise FileNotFoundError(
            f"未找到钉钉加密数据库: {config.ENCRYPTED_DB}\n"
            "请确认钉钉桌面客户端已登录，并检查自动检测到的数据目录是否正确。"
        )

    # SHM is a live index, not committed data; SQLite rebuilds it for the copy.
    files_to_copy = [
        (config.ENCRYPTED_DB, dest_db),
        (config.ENCRYPTED_DB + "-wal", dest_db + "-wal"),
    ]

    def source_identity():
        result = []
        for source, _ in files_to_copy:
            try:
                stat = os.stat(source)
                result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                result.append(None)
        return result

    for attempt in range(retry_count):
        try:
            before = source_identity()
            copied_files = []
            for src, dst in files_to_copy:
                if os.path.exists(dst):
                    os.remove(dst)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    copied_files.append(os.path.basename(src))

            if before != source_identity():
                raise OSError("复制期间客户端数据库或 WAL 发生变化，正在重试。")

            if not os.path.isfile(dest_db):
                raise FileNotFoundError(
                    f"复制失败，未能从 {config.ENCRYPTED_DB} 生成临时数据库文件。"
                )

            log_event(
                logger,
                "info",
                "decrypt.copy_succeeded",
                temp_dir=temp_dir,
                files=copied_files,
            )
            return dest_db
        except (PermissionError, OSError) as e:
            log_event(
                logger,
                "warning",
                "decrypt.copy_retry_failed",
                attempt=attempt + 1,
                retry_count=retry_count,
                error=e,
            )
            if attempt < retry_count - 1:
                time.sleep(retry_delay)
            else:
                # Clean up partial copy
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise RuntimeError(
                    f"复制钉钉加密数据库失败，已重试 {retry_count} 次：{e}"
                )

    shutil.rmtree(temp_dir, ignore_errors=True)
    raise RuntimeError("复制钉钉加密数据库失败。")


def _generate_v2_key(user_uid):
    md5_hex = hashlib.md5(user_uid.encode("utf-8")).hexdigest()
    return md5_hex[:16].encode("ascii")


def _load_v3_salt(data_dir):
    config_path = os.path.join(data_dir, "user_config")
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"未找到钉钉 V3 user_config 文件: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        encoded = f.read().strip()

    try:
        payload = base64.b64decode(encoded).decode("utf-8")
        data = json.loads(payload)
    except Exception as e:  # pragma: no cover - depends on local file shape
        raise RuntimeError(f"解析钉钉 V3 user_config 失败: {e}") from e

    salt = (data.get("salt") or "").strip()
    if not salt:
        raise RuntimeError("钉钉 V3 user_config 中缺少 salt 字段。")
    log_event(
        logger,
        "info",
        "decrypt.v3_user_config_loaded",
        path=config_path,
        salt_len=len(salt),
        salt_prefix=salt[:6],
    )
    return salt


def _generate_v3_key(user_uid, data_dir, salt=None):
    if salt is None:
        salt = _load_v3_salt(data_dir)
    pbkdf2_output = hashlib.pbkdf2_hmac(
        "sha1",
        (user_uid + salt).encode("utf-8"),
        V3_PBKDF2_SALT,
        V3_PBKDF2_ITERATIONS,
        V3_PBKDF2_KEYLEN,
    )
    md5_hex = hashlib.md5(pbkdf2_output).hexdigest()
    return md5_hex[:16].encode("ascii")


def _build_database_keys():
    if config.DINGTALK_DATA_DIR.endswith("_v3"):
        uid_candidates = config.get_decrypt_uid_candidates()
        log_event(
            logger,
            "info",
            "decrypt.mode_selected",
            mode="v3",
            uid_masked=config.get_runtime_diagnostics()["user_uid_masked"],
            uid_candidates_masked=[config._mask_uid(uid) for uid in uid_candidates],
            data_dir=config.DINGTALK_DATA_DIR,
        )
        salt = _load_v3_salt(config.DINGTALK_DATA_DIR)
        return [
            (uid, _generate_v3_key(uid, config.DINGTALK_DATA_DIR, salt=salt))
            for uid in uid_candidates
        ]
    log_event(
        logger,
        "info",
        "decrypt.mode_selected",
        mode="v2",
        uid_masked=config.get_runtime_diagnostics()["user_uid_masked"],
        data_dir=config.DINGTALK_DATA_DIR,
    )
    return [(config.USER_UID, _generate_v2_key(config.USER_UID))]


def _decrypt_page_in_place(block, page):
    for offset in range(0, len(page), block.block_size):
        page[offset:offset + block.block_size] = block.decrypt(
            bytes(page[offset:offset + block.block_size])
        )


def _get_wal_page_size(wal_header):
    page_size = int.from_bytes(wal_header[8:12], "big")
    return 65536 if page_size == 1 else page_size


def _get_wal_checksum_byteorder(wal_header):
    magic = int.from_bytes(wal_header[:4], "big")
    if magic == 0x377F0682:
        return "little"
    if magic == 0x377F0683:
        return "big"
    raise RuntimeError(f"解密失败：无法识别 WAL magic number: 0x{magic:08x}")


def _update_wal_checksum(data, byteorder, s0=0, s1=0):
    if len(data) % 8 != 0:
        raise RuntimeError("解密失败：WAL 校验输入长度不是 8 的倍数。")

    for offset in range(0, len(data), 8):
        x0 = int.from_bytes(data[offset:offset + 4], byteorder)
        x1 = int.from_bytes(data[offset + 4:offset + 8], byteorder)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def _decrypt_database_pages(input_path, output_path, key):
    block = AES.new(key, AES.MODE_ECB)

    with open(input_path, "rb") as src, open(output_path, "wb") as dst:
        while True:
            page = bytearray(src.read(PAGE_SIZE))
            if not page:
                break

            if len(page) != PAGE_SIZE:
                raise RuntimeError("解密失败：数据库包含不完整的数据页。")
            _decrypt_page_in_place(block, page)

            dst.write(page)


def _decrypt_wal_pages(input_path, output_path, key):
    block = AES.new(key, AES.MODE_ECB)

    with open(input_path, "rb") as src, open(output_path, "wb") as dst:
        wal_header = src.read(WAL_HEADER_SIZE)
        if not wal_header:
            return 0
        if len(wal_header) != WAL_HEADER_SIZE:
            raise RuntimeError("解密失败：WAL 文件头长度异常。")
        dst.write(wal_header)

        page_size = _get_wal_page_size(wal_header)
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise RuntimeError("解密失败：WAL 页大小无效。")
        checksum_byteorder = _get_wal_checksum_byteorder(wal_header)
        checksum_1, checksum_2 = _update_wal_checksum(
            wal_header[:24], checksum_byteorder
        )
        if wal_header[24:32] != struct.pack(">II", checksum_1, checksum_2):
            raise RuntimeError("解密失败：WAL 文件头校验失败。")

        frame_count = 0
        while True:
            frame_header = bytearray(src.read(WAL_FRAME_HEADER_SIZE))
            if not frame_header:
                break
            if len(frame_header) != WAL_FRAME_HEADER_SIZE:
                log_event(
                    logger,
                    "warning",
                    "decrypt.wal_truncated_frame_header",
                    expected=WAL_FRAME_HEADER_SIZE,
                    actual=len(frame_header),
                )
                break

            page = bytearray(src.read(page_size))
            if len(page) != page_size:
                log_event(
                    logger,
                    "warning",
                    "decrypt.wal_truncated_frame_page",
                    expected=page_size,
                    actual=len(page),
                    frame=frame_count,
                )
                break

            _decrypt_page_in_place(block, page)
            checksum_1, checksum_2 = _update_wal_checksum(
                bytes(frame_header[:8]) + bytes(page),
                checksum_byteorder,
                checksum_1,
                checksum_2,
            )
            frame_header[16:20] = struct.pack(">I", checksum_1)
            frame_header[20:24] = struct.pack(">I", checksum_2)
            dst.write(frame_header)
            dst.write(page)
            frame_count += 1

        return frame_count


def _remove_stale_decrypted_outputs(output_path):
    for path in [output_path, output_path + "-wal", output_path + "-shm"]:
        if os.path.exists(path):
            os.remove(path)


def _get_snapshot_latest_time(db_path):
    """Reject unreadable snapshots instead of interpreting them as empty."""
    from snapshots import validate_database

    if not os.path.isfile(db_path):
        return 0
    return validate_database(db_path)


def _validate_decrypted_header(output_path):
    with open(output_path, "rb") as stream:
        header = stream.read(16)
    if header != b"SQLite format 3\x00":
        raise RuntimeError("解密失败：候选数据库不是有效的 SQLite 文件。")


def _decrypt_candidate(encrypted_db_path, candidate_db_path):
    from snapshots import validate_database

    last_error = None
    key_candidates = _build_database_keys()
    for index, (key_uid, key) in enumerate(key_candidates):
        _remove_stale_decrypted_outputs(candidate_db_path)
        try:
            _decrypt_database_pages(encrypted_db_path, candidate_db_path, key)
            _validate_decrypted_header(candidate_db_path)
            encrypted_wal_path = encrypted_db_path + "-wal"
            if os.path.isfile(encrypted_wal_path):
                frame_count = _decrypt_wal_pages(encrypted_wal_path, candidate_db_path + "-wal", key)
                log_event(logger, "info", "decrypt.wal_completed", frame_count=frame_count)
            validate_database(candidate_db_path)
            return candidate_db_path
        except (RuntimeError, sqlite3.Error, ValueError) as exc:
            last_error = exc
            if index < len(key_candidates) - 1:
                log_event(logger, "warning", "decrypt.uid_candidate_failed",
                          uid_masked=config._mask_uid(key_uid),
                          remaining_candidates=len(key_candidates) - index - 1)
    if last_error is not None:
        raise last_error
    raise RuntimeError("没有可用的数据库解密密钥。")


def _prepare_snapshot_index(path, snapshot_id):
    from search_index import prepare_snapshot_index

    prepare_snapshot_index(path, snapshot_id)


def _publish_encrypted_copy(encrypted_db_path):
    from snapshots import publish_snapshot

    os.makedirs(config.DECRYPTED_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dingtalk_candidate_", dir=config.DECRYPTED_DIR) as directory:
        candidate_path = os.path.join(directory, "candidate.db")
        _decrypt_candidate(encrypted_db_path, candidate_path)
        return publish_snapshot(candidate_path, prepare_index=_prepare_snapshot_index)


def _cleanup_encrypted_copy(encrypted_db_path):
    # Only remove directories allocated by copy_encrypted_db, never a caller's
    # source folder or the installed client's DBFiles directory.
    from pathlib import Path

    directory = Path(encrypted_db_path).resolve().parent
    root = Path(config.DECRYPTED_DIR).resolve()
    if directory.parent == root and directory.name.startswith("dingtalk_encrypt_"):
        shutil.rmtree(directory)


def decrypt_database(encrypted_db_path=None, output_path=None):
    """Decrypt and publish; an explicit output_path still produces a standalone DB."""
    from pathlib import Path
    from snapshots import SnapshotError, validate_database

    owned_copy = encrypted_db_path is None
    if owned_copy:
        encrypted_db_path = copy_encrypted_db()
    try:
        if output_path is None:
            return _publish_encrypted_copy(encrypted_db_path)["path"]
        # Preserve the historical explicit-output API. Never remove the existing
        # destination first, or overwrite a destination still dependent on WAL.
        destination = Path(output_path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise SnapshotError("显式输出路径仍有 SQLite 侧文件，未替换原数据库。")
        with tempfile.TemporaryDirectory(prefix="dingtalk_output_", dir=destination.parent) as directory:
            candidate_path = os.path.join(directory, "candidate.db")
            merged_path = os.path.join(directory, "merged.db")
            _decrypt_candidate(encrypted_db_path, candidate_path)
            source = sqlite3.connect(Path(candidate_path).as_uri() + "?mode=ro", uri=True)
            target = sqlite3.connect(merged_path)
            try:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
            finally:
                target.close()
                source.close()
            validate_database(merged_path)
            os.replace(merged_path, destination)
        return str(destination)
    finally:
        if owned_copy:
            _cleanup_encrypted_copy(encrypted_db_path)


def refresh_database():
    """Refresh query data without exports; report retained-old-data explicitly."""
    from snapshots import SnapshotError, resolve_snapshot, validate_database

    encrypted_copy = None
    try:
        encrypted_copy = copy_encrypted_db()
        result = _publish_encrypted_copy(encrypted_copy)
        log_event(logger, "info", "decrypt.refresh_completed", outcome=result["outcome"],
                  snapshot_id=result["snapshot_id"])
        return result
    except Exception as exc:
        log_event(logger, "warning", "decrypt.refresh_failed", error=exc)
        try:
            previous = resolve_snapshot()
            validate_database(previous["path"])
        except (SnapshotError, OSError, sqlite3.Error):
            return {"outcome": "failed", "snapshot_id": None, "snapshot_time": None,
                    "latest_message_at": None, "path": None, "error": str(exc)}
        return {"outcome": "fallback", **previous, "error": str(exc)}
    finally:
        if encrypted_copy is not None:
            try:
                _cleanup_encrypted_copy(encrypted_copy)
            except OSError as exc:
                log_event(logger, "warning", "decrypt.copy_cleanup_failed", error=exc)


def sync_decrypt():
    """Compatibility entry point returning the usable query-snapshot path."""
    result = refresh_database()
    if result["outcome"] == "failed":
        raise RuntimeError(result["error"])
    return result["path"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = refresh_database()
    print(json.dumps(result, ensure_ascii=False, indent=2))

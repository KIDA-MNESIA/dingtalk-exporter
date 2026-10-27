import json
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES

from tests.fixtures import config, create_database
import decrypt
import snapshots


@contextmanager
def writable_database(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.storage = self.directory / "decrypted"
        self.storage.mkdir()
        self.legacy = self.storage / "dingtalk.db"
        self.candidate = self.directory / "candidate.db"
        create_database(str(self.candidate)).close()
        for name, value in (
            ("DECRYPTED_DIR", str(self.storage)),
            ("DECRYPTED_DB_PATH", str(self.legacy)),
            ("ENCRYPTED_DB", str(self.directory / "encrypted.db")),
            ("COPY_RETRY_DELAY", 0),
        ):
            item = patch.object(config, name, value)
            item.start()
            self.addCleanup(item.stop)

    def publish(self, **kwargs):
        return snapshots.publish_snapshot(str(self.candidate), **kwargs)

    def add_message(self, message_id=1000, timestamp=10000):
        with writable_database(self.candidate) as conn:
            conn.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (?, '1:2', ?, 'new synthetic message')", (message_id, timestamp))

    def test_publish_readonly_snapshot_and_unchanged_source(self):
        calls = []

        def prepare(path, snapshot_id):
            calls.append(snapshot_id)
            index_root = self.storage / "indexes"
            index_root.mkdir(exist_ok=True)
            index_path = index_root / f"{snapshot_id}-v1.sqlite"
            if not index_path.exists():
                with writable_database(index_path) as conn:
                    conn.execute("CREATE TABLE prepared_index (ready INTEGER)")
                    conn.execute("INSERT INTO prepared_index VALUES (1)")

        first = self.publish(prepare_index=prepare)
        second = self.publish(prepare_index=prepare)
        self.assertEqual(first["outcome"], "updated")
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(calls, [first["snapshot_id"], first["snapshot_id"]])
        conn = snapshots.open_snapshot()
        try:
            self.assertIs(conn.row_factory, sqlite3.Row)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], 554)
            self.assertEqual(snapshots.connection_metadata(conn)["snapshot_id"], first["snapshot_id"])
            self.assertNotIn("path", snapshots.connection_metadata(conn))
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM tbmsg_000")
        finally:
            conn.close()
        self.assertFalse(Path(first["path"] + "-wal").exists())

    def test_backup_includes_committed_wal_and_excludes_uncommitted_rows(self):
        writer = sqlite3.connect(self.candidate)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (1001, '1:2', 10001, 'committed')")
            writer.commit()
            writer.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (1002, '1:2', 10002, 'uncommitted')")
            result = self.publish()
            self.assertEqual(result["latest_message_at"], 10001)
            conn = snapshots.open_snapshot()
            try:
                ids = [row[0] for row in conn.execute("SELECT mid FROM tbmsg_000 WHERE mid >= 1001")]
                self.assertEqual(ids, [1001])
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            finally:
                conn.close()
        finally:
            writer.rollback()
            writer.close()

    def test_corrupt_and_incomplete_candidates_do_not_replace_current(self):
        original = self.publish()
        corrupt = self.directory / "broken.db"
        corrupt.write_bytes(b"SQLite format 3\0" + b"\0" * 4080)
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.publish_snapshot(str(corrupt))
        incomplete = self.directory / "incomplete.db"
        with writable_database(incomplete) as conn:
            conn.execute("CREATE TABLE tbmsg_000 (createdAt INTEGER)")
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.publish_snapshot(str(incomplete))
        self.assertEqual(snapshots.resolve_snapshot()["snapshot_id"], original["snapshot_id"])

    def test_time_rollback_and_empty_regression_are_rejected(self):
        original = self.publish()
        with writable_database(self.candidate) as conn:
            conn.execute("UPDATE tbmsg_127 SET createdAt=1")
        with self.assertRaises(snapshots.SnapshotError):
            self.publish()
        with writable_database(self.candidate) as conn:
            conn.execute("DELETE FROM tbmsg_000")
            conn.execute("DELETE FROM tbmsg_127")
        with self.assertRaises(snapshots.SnapshotError):
            self.publish()
        self.assertEqual(snapshots.resolve_snapshot()["snapshot_id"], original["snapshot_id"])

    def test_valid_initial_empty_database_can_be_published(self):
        with writable_database(self.candidate) as conn:
            conn.execute("DELETE FROM tbmsg_000")
            conn.execute("DELETE FROM tbmsg_127")
        result = self.publish()
        self.assertEqual(result["latest_message_at"], 0)

    def test_valid_candidate_repairs_a_corrupt_legacy_database(self):
        self.legacy.write_bytes(b"SQLite format 3\0" + b"\0" * 4080)
        result = self.publish()
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(snapshots.resolve_snapshot()["snapshot_id"], result["snapshot_id"])

    def test_unchanged_source_repairs_a_corrupt_published_database(self):
        original = self.publish()
        Path(original["path"]).write_bytes(b"SQLite format 3\0" + b"\0" * 4080)
        result = self.publish()
        self.assertEqual(result["outcome"], "updated")
        self.assertNotEqual(result["snapshot_id"], original["snapshot_id"])
        self.assertEqual(snapshots.validate_database(result["path"]), 9999)

    def test_pointer_failure_and_index_failure_preserve_previous(self):
        original = self.publish()
        self.add_message()
        real_replace = os.replace
        prepared_indexes = []

        def prepare(path, snapshot_id):
            index_path = self.storage / "indexes" / f"{snapshot_id}-v1.sqlite"
            index_path.parent.mkdir(exist_ok=True)
            index_path.write_bytes(b"synthetic index")
            prepared_indexes.append(index_path)

        def fail_pointer(source, target):
            if Path(target).name == "current.json":
                raise PermissionError("synthetic pointer failure")
            return real_replace(source, target)

        with patch.object(snapshots.os, "replace", side_effect=fail_pointer):
            with self.assertRaises(snapshots.SnapshotError):
                self.publish(prepare_index=prepare)
        with self.assertRaisesRegex(RuntimeError, "index failed"):
            self.publish(prepare_index=lambda path, snapshot_id: (_ for _ in ()).throw(RuntimeError("index failed")))
        self.assertEqual(snapshots.resolve_snapshot()["snapshot_id"], original["snapshot_id"])
        self.assertTrue(Path(original["path"]).exists())
        self.assertEqual(len(list((self.storage / "snapshots").glob("s-*.db"))), 1)
        self.assertTrue(prepared_indexes)
        self.assertFalse(any(path.exists() for path in prepared_indexes))

    def test_open_reader_and_pinned_id_keep_old_version_after_refresh(self):
        original = self.publish()
        reader = snapshots.open_snapshot(snapshot_id=original["snapshot_id"])
        try:
            old_count = reader.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0]
            self.add_message()
            current = self.publish()
            self.assertNotEqual(original["snapshot_id"], current["snapshot_id"])
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], old_count)
            self.assertEqual(snapshots.resolve_snapshot(original["snapshot_id"])["path"], original["path"])
            new_reader = snapshots.open_snapshot()
            try:
                self.assertEqual(new_reader.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], old_count + 1)
            finally:
                new_reader.close()
        finally:
            reader.close()

    def test_retention_keeps_current_previous_recent_and_active_versions(self):
        versions = [self.publish()]
        for i in range(1, 4):
            self.add_message(1000 + i, 10000 + i)
            versions.append(self.publish())
        # Versions 0 and 2 are old; version 2 must survive as the previous one.
        for version in (versions[0], versions[2]):
            metadata_path = Path(version["path"]).with_suffix(".json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["snapshot_time"] = snapshots._utc_time(time.time() - 25 * 3600)
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        index = self.storage / "indexes" / f"{versions[0]['snapshot_id']}-v1.sqlite"
        index.parent.mkdir(exist_ok=True)
        index.write_bytes(b"synthetic index")
        active = snapshots.open_snapshot(snapshot_id=versions[0]["snapshot_id"])
        try:
            snapshots._prune_snapshots(snapshots._read_current())
            self.assertTrue(all(Path(version["path"]).exists() for version in versions))
        finally:
            active.close()
        snapshots._prune_snapshots(snapshots._read_current())
        self.assertFalse(Path(versions[0]["path"]).exists())
        self.assertFalse(index.exists())
        self.assertTrue(all(Path(version["path"]).exists() for version in versions[1:]))
        with self.assertRaises(snapshots.SnapshotExpiredError):
            snapshots.resolve_snapshot(versions[0]["snapshot_id"])

    def test_invalid_and_unknown_ids_never_fall_back_to_current(self):
        self.publish()
        for snapshot_id in ("../candidate.db", "unknown", "s-" + "f" * 32):
            with self.subTest(snapshot_id=snapshot_id), self.assertRaises(snapshots.SnapshotExpiredError):
                snapshots.open_snapshot(snapshot_id=snapshot_id)

    def test_legacy_path_remains_readable_with_wal(self):
        create_database(str(self.legacy)).close()
        writer = sqlite3.connect(self.legacy)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (1001, '1:2', 10001, 'legacy WAL')")
            writer.commit()
            info = snapshots.resolve_snapshot()
            self.assertTrue(info["snapshot_id"].startswith("legacy-"))
            reader = snapshots.open_snapshot()
            try:
                self.assertEqual(reader.execute("SELECT mid FROM tbmsg_000 WHERE mid=1001").fetchone()[0], 1001)
            finally:
                reader.close()
            writer.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (1002, '1:2', 10002, 'changed')")
            writer.commit()
            with self.assertRaises(snapshots.SnapshotExpiredError):
                snapshots.resolve_snapshot(info["snapshot_id"])
        finally:
            writer.close()

    def test_legacy_connection_keeps_its_read_view_after_another_commit(self):
        create_database(str(self.legacy)).close()
        writer = sqlite3.connect(self.legacy)
        reader = newer = None
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE tbmsg_000 SET lastModify=lastModify+1 WHERE primaryKey=1")
            writer.commit()
            reader = snapshots.open_snapshot()
            original_id = snapshots.connection_metadata(reader)["snapshot_id"]
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], 554)
            writer.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (1001, '1:2', 10001, 'new committed row')")
            writer.commit()
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], 554)
            self.assertEqual(snapshots.connection_metadata(reader)["snapshot_id"], original_id)
            newer = snapshots.open_snapshot()
            self.assertNotEqual(snapshots.connection_metadata(newer)["snapshot_id"], original_id)
            self.assertEqual(newer.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], 555)
            with self.assertRaises(snapshots.SnapshotExpiredError):
                snapshots.open_snapshot(snapshot_id=original_id)
            self.add_message(1002, 10002)
            published = self.publish()
            self.assertEqual(snapshots.resolve_snapshot()["snapshot_id"], published["snapshot_id"])
            with self.assertRaises(snapshots.SnapshotExpiredError):
                snapshots.open_snapshot(snapshot_id=original_id)
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], 554)
        finally:
            if reader is not None:
                reader.close()
            if newer is not None:
                newer.close()
            writer.close()

    def test_legacy_change_before_read_view_is_pinned_closes_connection(self):
        create_database(str(self.legacy)).close()
        writer = sqlite3.connect(self.legacy)
        opened = []
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE tbmsg_000 SET lastModify=lastModify+1 WHERE primaryKey=1")
            writer.commit()
            original_id = snapshots.resolve_snapshot()["snapshot_id"]
            real_open = snapshots._readonly_connection

            def commit_before_pin(path, immutable=False, factory=sqlite3.Connection):
                conn = real_open(path, immutable=immutable, factory=factory)
                if factory is snapshots.SnapshotConnection:
                    opened.append(conn)
                    writer.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (1001, '1:2', 10001, 'competing commit')")
                    writer.commit()
                return conn

            with patch.object(snapshots, "_readonly_connection", side_effect=commit_before_pin):
                with self.assertRaises(snapshots.SnapshotExpiredError):
                    snapshots.open_snapshot(snapshot_id=original_id)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].execute("SELECT 1")
            newer = snapshots.open_snapshot()
            try:
                self.assertNotEqual(snapshots.connection_metadata(newer)["snapshot_id"], original_id)
                self.assertEqual(newer.execute("SELECT COUNT(*) FROM tbmsg_000").fetchone()[0], 555)
            finally:
                newer.close()
        finally:
            writer.close()

    def test_explicit_database_path_ignores_unrelated_current_pointer(self):
        self.publish()
        explicit = self.directory / "other.db"
        create_database(str(explicit)).close()
        self.assertEqual(snapshots.resolve_snapshot(db_path=explicit)["path"], str(explicit))

    def test_two_processes_publish_one_version_for_same_source(self):
        code = """import json,sys,config,snapshots
config.DECRYPTED_DIR=sys.argv[1]
config.DECRYPTED_DB_PATH=sys.argv[2]
print(json.dumps(snapshots.publish_snapshot(sys.argv[3])))
"""
        env = {**os.environ, "DINGTALK_UID": "1", "DINGTALK_DATA_DIR": str(self.directory), "PYTHONIOENCODING": "utf-8"}
        args = [sys.executable, "-c", code, str(self.storage), str(self.legacy), str(self.candidate)]
        processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env) for _ in range(2)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual({result["outcome"] for result in results}, {"updated", "unchanged"})
        self.assertEqual(len({result["snapshot_id"] for result in results}), 1)

    def test_refresh_without_exports_and_explicit_fallback_status(self):
        key = b"0" * 16
        encrypted = Path(config.ENCRYPTED_DB)
        encrypted.write_bytes(AES.new(key, AES.MODE_ECB).encrypt(self.candidate.read_bytes()))
        with patch.object(decrypt, "_build_database_keys", return_value=[("1", key)]), patch.object(decrypt, "_prepare_snapshot_index") as prepare:
            first = decrypt.refresh_database()
            second = decrypt.refresh_database()
            self.assertEqual(first["outcome"], "updated")
            self.assertEqual(second["outcome"], "unchanged")
            self.assertEqual(prepare.call_count, 2)
            self.assertFalse(list(self.storage.glob("dingtalk_*")))
        encrypted.unlink()
        fallback = decrypt.refresh_database()
        self.assertEqual(fallback["outcome"], "fallback")
        self.assertEqual(fallback["snapshot_id"], first["snapshot_id"])
        self.assertIn("error", fallback)
        self.assertEqual(decrypt.sync_decrypt(), first["path"])

    def test_refresh_failure_without_previous_database_is_explicit(self):
        result = decrypt.refresh_database()
        self.assertEqual(result["outcome"], "failed")
        self.assertIsNone(result["snapshot_id"])
        with self.assertRaises(RuntimeError):
            decrypt.sync_decrypt()

    def test_encrypted_wal_refresh_and_bad_header_fallback(self):
        key = b"0" * 16
        writer = sqlite3.connect(self.candidate)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (1001, '1:2', 10001, 'committed WAL')")
            writer.commit()
            encrypted = Path(config.ENCRYPTED_DB)
            encrypted.write_bytes(AES.new(key, AES.MODE_ECB).encrypt(self.candidate.read_bytes()))
            wal = bytearray(Path(str(self.candidate) + "-wal").read_bytes())
            page_size = int.from_bytes(wal[8:12], "big")
            for frame_start in range(32, len(wal), 24 + page_size):
                start = frame_start + 24
                wal[start:start + page_size] = AES.new(key, AES.MODE_ECB).encrypt(bytes(wal[start:start + page_size]))
            encrypted_wal = Path(str(encrypted) + "-wal")
            encrypted_wal.write_bytes(wal)
            with patch.object(decrypt, "_build_database_keys", return_value=[("1", key)]), patch.object(decrypt, "_prepare_snapshot_index"):
                first = decrypt.refresh_database()
                self.assertEqual(first["outcome"], "updated")
                self.assertEqual(first["latest_message_at"], 10001)
                wal[24] ^= 1
                encrypted_wal.write_bytes(wal)
                fallback = decrypt.refresh_database()
                self.assertEqual(fallback["outcome"], "fallback")
                self.assertEqual(fallback["snapshot_id"], first["snapshot_id"])
                self.assertIn("文件头校验失败", fallback["error"])
        finally:
            writer.close()

    def test_explicit_decrypt_output_does_not_remove_caller_source(self):
        key = b"0" * 16
        encrypted = Path(config.ENCRYPTED_DB)
        encrypted.write_bytes(AES.new(key, AES.MODE_ECB).encrypt(self.candidate.read_bytes()))
        destination = self.directory / "explicit.db"
        with patch.object(decrypt, "_build_database_keys", return_value=[("1", key)]):
            result = decrypt.decrypt_database(str(encrypted), str(destination))
        self.assertEqual(result, str(destination))
        self.assertTrue(encrypted.exists())
        self.assertEqual(snapshots.validate_database(destination), 9999)

    def test_copy_retry_drops_a_wal_removed_between_attempts(self):
        encrypted = Path(config.ENCRYPTED_DB)
        encrypted.write_bytes(b"synthetic encrypted database")
        wal = Path(str(encrypted) + "-wal")
        wal.write_bytes(b"synthetic WAL")
        shm = Path(str(encrypted) + "-shm")
        shm.write_bytes(b"live index must not be copied")
        real_copy = shutil.copy2
        changed = False

        def copy_then_checkpoint(source, destination):
            nonlocal changed
            result = real_copy(source, destination)
            if Path(source) == wal and not changed:
                changed = True
                wal.unlink()
            return result

        with patch.object(decrypt.shutil, "copy2", side_effect=copy_then_checkpoint):
            copied = decrypt.copy_encrypted_db(retry_count=2, retry_delay=0)
        self.assertEqual(Path(copied).read_bytes(), encrypted.read_bytes())
        self.assertFalse(Path(copied + "-wal").exists())
        self.assertFalse(Path(copied + "-shm").exists())
        decrypt._cleanup_encrypted_copy(copied)
        self.assertTrue(encrypted.exists())

    def test_partial_encrypted_database_page_is_rejected(self):
        source = self.directory / "partial.db"
        source.write_bytes(b"0" * 16)
        with self.assertRaisesRegex(RuntimeError, "不完整"):
            decrypt._decrypt_database_pages(str(source), str(self.directory / "output.db"), b"0" * 16)


if __name__ == "__main__":
    unittest.main()

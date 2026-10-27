import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests.fixtures import config
import jobs
import scheduler


class _JobTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.patches = [
            patch.object(config, "DATA_DIR", str(self.directory)),
            patch.object(config, "SYNC_STATE_FILE", str(self.directory / "sync_state.json")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = jobs.get_job_status(job_id)
            if state["status"] != "running":
                return state
            time.sleep(0.01)
        self.fail("Job did not finish")

    def run_child(self, code):
        return subprocess.run(
            [sys.executable, "-c", code, str(self.directory)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DINGTALK_UID": "1", "DINGTALK_DATA_DIR": str(self.directory), "PYTHONIOENCODING": "utf-8"},
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )


class JobTests(_JobTestCase):
    def test_simultaneous_requests_admit_exactly_one_writer(self):
        barrier = threading.Barrier(6)
        release = threading.Event()

        def submit():
            barrier.wait(timeout=5)
            return jobs.start_job("refresh", lambda: {"released": release.wait(5)})

        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                responses = list(pool.map(lambda _: submit(), range(6)))
            started = [r for r in responses if r["status"] == "started"]
            self.assertEqual(len(started), 1)
            self.assertEqual({r["job_id"] for r in responses}, {started[0]["job_id"]})
        finally:
            release.set()
        self.wait_for_job(started[0]["job_id"])

    def test_admission_is_recorded_before_thread_starts(self):
        release = threading.Event()
        calls = []

        def callback():
            calls.append(1)
            self.assertTrue(release.wait(5))
            return {"export_path": "new-export"}

        response = jobs.start_job("export", callback)
        try:
            state = jobs.get_job_status(response["job_id"])
            self.assertEqual(state["status"], "running")
            self.assertIsNone(state["result"])
            second = jobs.start_job("refresh", lambda: {})
            self.assertEqual(second, {"status": "already_running", "job_id": response["job_id"]})
            self.assertEqual(jobs.get_current_job()["job_id"], response["job_id"])
        finally:
            release.set()
        final = self.wait_for_job(response["job_id"])
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(jobs.get_current_job())

    def test_second_process_observes_the_same_running_job(self):
        release = threading.Event()
        response = jobs.start_job("refresh", lambda: {"released": release.wait(5)})
        try:
            child = self.run_child(
                "import config,jobs,json,sys; config.DATA_DIR=sys.argv[1]; "
                "print(json.dumps(jobs.start_job('export',lambda: {},background=False)))"
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {
                "status": "already_running", "job_id": response["job_id"],
            })
            self.assertEqual(jobs.get_job_status(response["job_id"])["status"], "running")
        finally:
            release.set()
        self.wait_for_job(response["job_id"])

    def test_process_exit_is_recovered_only_after_os_lock_is_released(self):
        child = self.run_child(
            "import config,jobs,json,sys,os; config.DATA_DIR=sys.argv[1]\n"
            "def crash():\n"
            "    print(json.dumps({'job_id':jobs.get_current_job()['job_id']}),flush=True)\n"
            "    os._exit(7)\n"
            "jobs.start_job('refresh',crash,background=False)\n"
        )
        self.assertEqual(child.returncode, 7, child.stderr)
        job_id = json.loads(child.stdout)["job_id"]
        state = jobs.get_job_status(job_id)
        self.assertEqual(state["status"], "failed")
        self.assertIn("进程已退出", state["error"])
        self.assertIsNone(jobs.get_current_job())
        next_job = jobs.start_job("refresh", lambda: {}, background=False)
        self.assertEqual(jobs.get_job_status(next_job["job_id"])["status"], "succeeded")

    def test_thread_start_failure_releases_writer_lock(self):
        with patch.object(jobs.threading.Thread, "start", side_effect=RuntimeError("thread failed")):
            response = jobs.start_job("refresh", lambda: {})
        self.assertEqual(response["status"], "failed")
        self.assertEqual(jobs.get_job_status(response["job_id"])["error"], "thread failed")
        self.assertIsNone(jobs.get_current_job())
        next_job = jobs.start_job("refresh", lambda: {}, background=False)
        self.assertEqual(jobs.get_job_status(next_job["job_id"])["status"], "succeeded")

    def test_task_error_and_result_are_not_reused(self):
        failed = jobs.start_job("export", Mock(side_effect=ValueError("old failure")), background=False)
        self.assertEqual(jobs.get_job_status(failed["job_id"])["status"], "failed")
        self.assertEqual(scheduler.get_sync_state()["last_error"], "old failure")
        success = jobs.start_job("export", lambda: {"export_path": "new-export"}, background=False)
        self.assertEqual(jobs.get_job_status(success["job_id"])["status"], "succeeded")
        self.assertIsNone(scheduler.get_sync_state()["last_error"])
        self.assertEqual(scheduler.get_sync_state()["last_export_path"], "new-export")
        release = threading.Event()
        running = jobs.start_job("refresh", lambda: {"released": release.wait(5)})
        try:
            state = scheduler.get_sync_state()
            self.assertTrue(state["is_syncing"])
            self.assertIsNone(state["last_export_path"])
            self.assertIsNone(state["last_error"])
        finally:
            release.set()
        self.wait_for_job(running["job_id"])

    def test_job_ids_cannot_select_other_files(self):
        with self.assertRaises(jobs.JobNotFoundError):
            jobs.get_job_status("../sync_state")
        with self.assertRaises(jobs.JobNotFoundError):
            jobs.get_job_status("0" * 32)


class SchedulerJobTests(_JobTestCase):
    def refreshed(self, outcome="updated", **extra):
        return {
            "outcome": outcome, "snapshot_id": "snapshot-test",
            "snapshot_time": "2026-10-27T02:00:00+00:00",
            "latest_message_at": 500000, "path": "snapshot.db", **extra,
        }

    def test_refresh_does_not_export_or_advance_export_cursor(self):
        scheduler._save_state({"last_sync_time": 100, "sync_count": 4})
        with patch("decrypt.refresh_database", return_value=self.refreshed()), \
                patch("exporter.export_all") as all_export, \
                patch("exporter.export_incremental") as incremental, \
                patch("exporter.export_by_cids") as selected:
            response = scheduler.start_refresh()
            state = self.wait_for_job(response["job_id"])
        self.assertEqual(state["status"], "succeeded")
        all_export.assert_not_called()
        incremental.assert_not_called()
        selected.assert_not_called()
        state = scheduler.get_sync_state()
        self.assertEqual(state["last_sync_time"], 100)
        self.assertEqual(state["sync_count"], 4)
        self.assertEqual(state["snapshot_id"], "snapshot-test")
        self.assertIsNotNone(state["last_refresh_time"])
        self.assertIsNone(state["last_export_path"])

    def test_fallback_keeps_cursor_and_does_not_export_old_snapshot_as_new(self):
        scheduler._save_state({"last_sync_time": 100, "sync_count": 4})
        with patch("decrypt.refresh_database", return_value=self.refreshed("fallback", error="bad candidate")), \
                patch("exporter.export_all") as export_all, \
                patch("exporter.export_incremental") as export_incremental:
            self.assertFalse(scheduler.do_sync())
        export_all.assert_not_called()
        export_incremental.assert_not_called()
        state = scheduler.get_sync_state()
        self.assertEqual(state["outcome"], "fallback")
        self.assertEqual(state["last_error"], "bad candidate")
        self.assertEqual(state["last_sync_time"], 100)
        self.assertEqual(state["sync_count"], 4)
        self.assertIsNone(state["last_refresh_time"])

    def test_failed_refresh_is_a_failed_job_without_stale_result(self):
        with patch("decrypt.refresh_database", return_value={"outcome": "failed", "error": "no usable snapshot"}):
            response = scheduler.start_refresh()
            state = self.wait_for_job(response["job_id"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "no usable snapshot")
        self.assertIsNone(scheduler.get_sync_state()["last_export_path"])

    def test_sync_exports_the_exact_refreshed_snapshot(self):
        with patch("decrypt.refresh_database", return_value=self.refreshed()), \
                patch("exporter.export_all", return_value="full-export") as export_all:
            self.assertTrue(scheduler.do_sync(full=True))
        export_all.assert_called_once_with(snapshot_id="snapshot-test")
        state = scheduler.get_sync_state()
        self.assertEqual(state["last_export_path"], "full-export")
        self.assertEqual(state["last_sync_time"], scheduler._apply_sync_overlap(500000))
        self.assertEqual(state["sync_count"], 1)

    def test_incremental_empty_result_keeps_the_cursor(self):
        scheduler._save_state({"last_sync_time": 100, "sync_count": 4})
        with patch("decrypt.refresh_database", return_value=self.refreshed("unchanged")), \
                patch("exporter.export_incremental", return_value={"path": None, "max_created_at": None, "message_count": 0}) as export:
            self.assertTrue(scheduler.do_sync())
        export.assert_called_once_with(100, snapshot_id="snapshot-test")
        state = scheduler.get_sync_state()
        self.assertEqual(state["last_sync_time"], 100)
        self.assertEqual(state["sync_count"], 5)
        self.assertIsNone(state["last_export_path"])

    def test_export_failure_does_not_advance_the_cursor(self):
        scheduler._save_state({"last_sync_time": 100, "sync_count": 4, "last_export_path": "old-export"})
        with patch("decrypt.refresh_database", return_value=self.refreshed()), \
                patch("exporter.export_incremental", side_effect=OSError("export failed")):
            self.assertFalse(scheduler.do_sync())
        state = scheduler.get_sync_state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_sync_time"], 100)
        self.assertEqual(state["sync_count"], 4)
        self.assertEqual(state["last_error"], "export failed")
        self.assertIsNone(state["last_export_path"])

    def test_selected_export_uses_job_result_without_advancing_cursor(self):
        scheduler._save_state({"last_sync_time": 100, "sync_count": 4})
        with patch("snapshots.resolve_snapshot", return_value=self.refreshed()), \
                patch("exporter.export_by_cids", return_value="selected-export") as export:
            response = scheduler.start_export(["1:2"], since_time=10, until_time=20, snapshot_id="snapshot-test")
            state = self.wait_for_job(response["job_id"])
        self.assertEqual(state["status"], "succeeded")
        export.assert_called_once_with(["1:2"], since_time=10, until_time=20, snapshot_id="snapshot-test")
        state = scheduler.get_sync_state()
        self.assertEqual(state["last_sync_time"], 100)
        self.assertEqual(state["sync_count"], 4)
        self.assertEqual(state["last_export_path"], "selected-export")


if __name__ == "__main__":
    unittest.main()

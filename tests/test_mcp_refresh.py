import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from Crypto.Cipher import AES
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.fixtures import create_database
from tests.test_reading import add_message, encoded_child
from decrypt import _generate_v2_key


class MCPRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_pin_search_forward_export_and_fallback(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            working = Path(directory)
            source_dir = working / "source_v2"
            db_dir = source_dir / "DBFiles"
            db_dir.mkdir(parents=True)
            source_path = db_dir / "dingtalk.db"
            fixture = working / "source.db"
            conn = create_database(fixture)
            add_message(conn, 9000, 1500, {}, attachments=[encoded_child(text="转发验收")])
            conn.commit()
            conn.close()

            def encrypt_source():
                source_path.write_bytes(AES.new(_generate_v2_key("1"), AES.MODE_ECB).encrypt(fixture.read_bytes()))

            encrypt_source()
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(root / "tests" / "serve_fixture.py"), str(working / "legacy.db"), "1", str(source_dir)],
                cwd=str(root),
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "FASTMCP_CHECK_FOR_UPDATES": "off"},
            )
            with open(os.devnull, "w") as errors:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(name, **args):
                            response = (await session.call_tool(name, args)).model_dump(by_alias=True)
                            self.assertFalse(response.get("isError"), response)
                            return response.get("structuredContent") or json.loads("".join(c["text"] for c in response["content"] if c["type"] == "text"))

                        async def wait_job(started):
                            self.assertEqual(started["status"], "started")
                            deadline = time.monotonic() + 30
                            while time.monotonic() < deadline:
                                job = await call("get_job_status", job_id=started["job_id"])
                                if job["status"] != "running":
                                    self.assertEqual(job["status"], "succeeded", job)
                                    return job["result"]
                                await asyncio.sleep(0.05)
                            self.fail("background job timed out")

                        self.assertFalse((await call("get_database_status"))["ready"])
                        first = await wait_job(await call("refresh_database"))
                        self.assertEqual(first["outcome"], "updated")
                        old_id = first["snapshot_id"]
                        self.assertEqual((await call("get_messages", conversation_id="1:2", snapshot_id=old_id))["total"], 555)
                        forwarded = await call("get_forwarded_messages", conversation_id="1:2", message_id=9000, snapshot_id=old_id)
                        self.assertEqual(forwarded["messages"][0]["content"], "转发验收")
                        found = await call("search_messages", keyword="转发验收", snapshot_id=old_id)
                        self.assertEqual(found["total"], 1)
                        self.assertEqual(found["messages"][0]["matched_paths"], [[0]])
                        self.assertEqual((await call("search_messages", keyword="转发验收", mode="raw", snapshot_id=old_id))["total"], 0)

                        import sqlite3
                        conn = sqlite3.connect(fixture)
                        add_message(conn, 9100, 1, {"text": "第二版新增"})
                        conn.execute("UPDATE tbmsg_000 SET createdAt=6000 WHERE mid=9100")
                        conn.commit()
                        conn.close()
                        encrypt_source()
                        second = await wait_job(await call("refresh_database"))
                        new_id = second["snapshot_id"]
                        self.assertNotEqual(new_id, old_id)
                        self.assertEqual((await call("get_messages", conversation_id="1:2"))["total"], 556)
                        old_page = await call("get_messages", conversation_id="1:2", snapshot_id=old_id)
                        self.assertEqual(old_page["snapshot_id"], old_id)
                        self.assertEqual(old_page["total"], 555)
                        self.assertEqual((await call("search_messages", keyword="第二版新增", snapshot_id=old_id))["total"], 0)
                        self.assertEqual((await call("search_messages", keyword="第二版新增", snapshot_id=new_id))["total"], 1)
                        same = await wait_job(await call("refresh_database"))
                        self.assertEqual(same["outcome"], "unchanged")
                        self.assertEqual(same["snapshot_id"], new_id)
                        self.assertFalse(list((working / "exports").glob("*/export.json")))
                        self.assertIsNone((await call("get_sync_status"))["last_sync_time"])

                        source_path.write_bytes(b"broken fixture")
                        fallback = await wait_job(await call("refresh_database"))
                        self.assertEqual(fallback["outcome"], "fallback")
                        self.assertEqual(fallback["snapshot_id"], new_id)
                        self.assertTrue((await call("get_database_status"))["ready"])
                        exported = await wait_job(await call("export_conversations", cids=["1:2"], snapshot_id=old_id))
                        output = json.loads((Path(exported["export_path"]) / "export.json").read_text(encoding="utf-8"))
                        self.assertEqual(output["snapshot_id"], old_id)
                        self.assertEqual(len(output["conversations"][0]["messages"]), 555)
                        self.assertIsNone((await call("get_sync_status"))["last_error"])


if __name__ == "__main__":
    unittest.main()

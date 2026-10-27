import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.fixtures import create_database


class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_can_page_through_history_and_search(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fixture.db")
            create_database(path).close()
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(root / "tests" / "serve_fixture.py"), path],
                cwd=str(root),
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "FASTMCP_CHECK_FOR_UPDATES": "off"},
            )
            with open(os.devnull, "w") as errors:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(name, **arguments):
                            response = (await session.call_tool(name, arguments)).model_dump(by_alias=True)
                            self.assertFalse(response.get("isError"), response)
                            return response.get("structuredContent") or json.loads(
                                "".join(item["text"] for item in response["content"] if item["type"] == "text")
                            )

                        tools = (await session.list_tools()).model_dump(by_alias=True)["tools"]
                        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
                        for name in ("get_messages", "list_conversations", "search_messages"):
                            self.assertEqual(schemas[name]["properties"]["offset"]["minimum"], 0)
                            self.assertEqual(schemas[name]["properties"]["limit"]["minimum"], 1)

                        # Previously limit=500 was silently truncated to the oldest 100.
                        first = await call("get_messages", conversation_id="1:2", limit=500)
                        self.assertEqual(first["returned"], 500)
                        self.assertEqual([m["id"] for m in first["messages"]], list(range(55, 555)))
                        last = await call("get_messages", conversation_id="1:2", limit=500, offset=first["next_offset"])
                        self.assertEqual(last["returned"], 54)
                        self.assertFalse(last["has_more"])
                        self.assertIsNone(last["next_offset"])
                        past = await call("get_messages", conversation_id="1:2", offset=554)
                        self.assertEqual(past["messages"], [])
                        filtered = await call("get_messages", conversation_id="1:2", since_time=0, until_time=2)
                        self.assertEqual([m["id"] for m in filtered["messages"]], [2, 3, 4, 5])

                        # Conversation paging and filtering must use the same total.
                        conversations = []
                        offset = 0
                        while True:
                            page = await call("list_conversations", keyword="Alice", limit=1, offset=offset)
                            self.assertEqual(page["total"], 2)
                            conversations.extend(c["cid"] for c in page["conversations"])
                            if not page["has_more"]:
                                break
                            offset = page["next_offset"]
                        self.assertEqual(set(conversations), {"1:2", "1:4"})
                        bob = await call("list_conversations", keyword="Bob")
                        self.assertEqual(bob["conversations"][0]["cid"], "3:1")

                        results = []
                        offset = 0
                        while True:
                            page = await call("search_messages", keyword="needle", limit=200, offset=offset)
                            self.assertEqual(page["total"], 759)
                            self.assertEqual(page["returned"], min(200, 759 - offset))
                            results.extend((m["cid"], m["id"]) for m in page["messages"])
                            if not page["has_more"]:
                                break
                            offset = page["next_offset"]
                        self.assertEqual(results, [("group", i) for i in range(205, 0, -1)] + [("1:2", i) for i in range(554, 0, -1)])
                        empty = await call("search_messages", keyword="needle", offset=759)
                        self.assertEqual(empty["messages"], [])
                        self.assertEqual(empty["total"], 759)

                        for name, args in (
                            ("get_messages", {"conversation_id": "1:2", "limit": 0}),
                            ("get_messages", {"conversation_id": "1:2", "limit": 501}),
                            ("list_conversations", {"offset": -1}),
                            ("search_messages", {"keyword": "needle", "limit": 201}),
                        ):
                            response = (await session.call_tool(name, args)).model_dump(by_alias=True)
                            self.assertTrue(response.get("isError"), response)


if __name__ == "__main__":
    unittest.main()

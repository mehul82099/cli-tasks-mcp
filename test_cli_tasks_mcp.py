#!/usr/bin/env python3
"""Tests for cli-tasks-mcp (stdlib unittest, no network).

Spawns the server as a subprocess and speaks newline-delimited JSON-RPC
over its stdio, exactly as an MCP client would.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "cli_tasks_mcp.py")

TASKS_EXT = "io.modelcontextprotocol/tasks"


def meta(version="2026-07-28", tasks=True):
    caps = {"extensions": {TASKS_EXT: {}}} if tasks else {}
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": caps,
    }


def py(code):
    return [sys.executable, "-c", code]


class Client:
    """A minimal MCP stdio client for tests."""

    def __init__(self, extra_args=None):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER] + (extra_args or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=HERE)
        self.next_id = 0

    def call(self, method, params=None, **kw):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        msg.update(kw)
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("server closed stdout")
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply
        raise AssertionError("no reply for %s" % method)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.kill()
        except OSError:
            pass
        self.proc.wait(timeout=10)


class ServerCase(unittest.TestCase):
    extra_args = None

    @classmethod
    def setUpClass(cls):
        cls.client = Client(cls.extra_args)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def call(self, method, params=None, **kw):
        return self.client.call(method, params, **kw)

    def run_tool(self, argv, arguments=None, task_meta=True, **kw):
        args = {"argv": argv}
        args.update(arguments or {})
        return self.call("tools/call", {
            "name": "run", "arguments": args, "_meta": meta(tasks=task_meta),
        }, **kw)

    def poll_until_done(self, task_id, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.call("tasks/get",
                          {"taskId": task_id, "_meta": meta()})
            self.assertIn("result", r)
            if r["result"]["status"] != "working":
                return r["result"]
            time.sleep(0.2)
        self.fail("task %s never left 'working'" % task_id)


class DiscoverTests(ServerCase):
    def test_supported_versions_and_extension(self):
        r = self.call("server/discover", {"_meta": meta()})
        result = r["result"]
        self.assertEqual(result["supportedVersions"][0], "2026-07-28")
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertIn(TASKS_EXT, result["capabilities"]["extensions"])
        self.assertEqual(result["serverInfo"]["name"], "cli-tasks-mcp")
        self.assertEqual(result["resultType"], "complete")

    def test_discover_needs_no_meta(self):
        r = self.call("server/discover")
        self.assertIn("result", r)

    def test_tools_list(self):
        r = self.call("tools/list", {"_meta": meta()})
        tools = r["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["run"])
        self.assertEqual(r["result"]["resultType"], "complete")
        schema = tools[0]["inputSchema"]
        self.assertEqual(schema["required"], ["argv"])
        # No legacy per-tool task opt-in field (removed by SEP-2663).
        self.assertNotIn("execution", tools[0])


class SyncTests(ServerCase):
    """Clients that do not declare the tasks extension get sync results."""

    def test_sync_success(self):
        r = self.run_tool(py("print('hello sync')"), task_meta=False)
        result = r["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("hello sync", text)
        self.assertIn("[exit 0]", text)

    def test_sync_nonzero_exit_is_tool_error(self):
        r = self.run_tool(py("import sys; sys.exit(3)"), task_meta=False)
        result = r["result"]
        self.assertTrue(result["isError"])
        self.assertIn("[exit 3]", result["content"][0]["text"])

    def test_sync_missing_command(self):
        r = self.run_tool(["definitely-not-a-real-binary-xyz"])
        result = r["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Failed to start", result["content"][0]["text"])

    def test_empty_argv_rejected(self):
        r = self.run_tool([])
        self.assertEqual(r["error"]["code"], -32602)

    def test_bad_timeout_rejected(self):
        r = self.run_tool(py("pass"), {"timeoutMs": -5})
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_tool(self):
        r = self.call("tools/call", {
            "name": "nope", "arguments": {}, "_meta": meta()})
        self.assertEqual(r["error"]["code"], -32602)

    def test_cwd_jail(self):
        r = self.run_tool(py("pass"), {"cwd": os.path.dirname(HERE)
                                       if HERE != "/" else "/"},
                          task_meta=False)
        # HERE's parent is outside the server root (server runs with cwd=HERE)
        self.assertTrue(r["result"]["isError"])
        self.assertIn("outside the server root",
                      r["result"]["content"][0]["text"])

    def test_env_passed(self):
        r = self.run_tool(py("import os; print(os.environ.get('T_MARK'))"),
                          {"env": {"T_MARK": "yes"}}, task_meta=False)
        self.assertIn("yes", r["result"]["content"][0]["text"])


class AsyncTaskTests(ServerCase):
    def test_task_lifecycle(self):
        r = self.run_tool(py("import time; time.sleep(0.5); print('later')"))
        result = r["result"]
        self.assertEqual(result["resultType"], "task")
        self.assertEqual(result["status"], "working")
        self.assertIsInstance(result["ttlMs"], int)
        self.assertIsInstance(result["pollIntervalMs"], int)
        final = self.poll_until_done(result["taskId"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["resultType"], "complete")
        self.assertIn("later", final["result"]["content"][0]["text"])
        self.assertFalse(final["result"]["isError"])

    def test_task_nonzero_exit_completes_with_tool_error(self):
        """Non-zero exit is a tool error, not a JSON-RPC failure (SEP-2663)."""
        r = self.run_tool(py("import sys; sys.exit(1)"))
        final = self.poll_until_done(r["result"]["taskId"])
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["result"]["isError"])

    def test_task_timeout_completes_with_tool_error(self):
        r = self.run_tool(py("import time; time.sleep(60)"),
                          {"timeoutMs": 800})
        final = self.poll_until_done(r["result"]["taskId"], timeout=30)
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["result"]["isError"])
        self.assertIn("timed out", final["result"]["content"][0]["text"])

    def test_task_cancel(self):
        r = self.run_tool(py("import time; time.sleep(60)"))
        task_id = r["result"]["taskId"]
        ack = self.call("tasks/cancel", {"taskId": task_id, "_meta": meta()})
        self.assertEqual(ack["result"]["resultType"], "complete")
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            g = self.call("tasks/get", {"taskId": task_id, "_meta": meta()})
            status = g["result"]["status"]
            if status in ("cancelled", "completed", "failed"):
                break
            time.sleep(0.2)
        self.assertIn(status, ("cancelled", "completed"))

    def test_tasks_get_unknown_id(self):
        r = self.call("tasks/get", {"taskId": "nope", "_meta": meta()})
        self.assertEqual(r["error"]["code"], -32602)

    def test_tasks_cancel_unknown_id(self):
        r = self.call("tasks/cancel", {"taskId": "nope", "_meta": meta()})
        self.assertEqual(r["error"]["code"], -32602)

    def test_tasks_require_extension(self):
        """tasks/* without the declared extension: -32021 (spec error table)."""
        for method in ("tasks/get", "tasks/cancel", "tasks/update"):
            r = self.call(method, {"taskId": "x", "_meta": meta(tasks=False)})
            self.assertEqual(r["error"]["code"], -32021, method)
            caps = r["error"]["data"]["requiredCapabilities"]
            self.assertIn(TASKS_EXT, caps["extensions"])

    def test_legacy_version_disables_extension(self):
        """Under 2025-11-25 the extension is undefined: treated as not
        declared even if the client sends it (SEP-2663 backcompat table)."""
        legacy_meta = meta(version="2025-11-25", tasks=True)
        r = self.call("tasks/get", {"taskId": "x", "_meta": legacy_meta})
        self.assertEqual(r["error"]["code"], -32021)

    def test_tasks_update_unknown_id(self):
        r = self.call("tasks/update", {
            "taskId": "nope", "inputResponses": {}, "_meta": meta()})
        self.assertEqual(r["error"]["code"], -32602)

    def test_legacy_tasks_result_removed(self):
        r = self.call("tasks/result", {"taskId": "x", "_meta": meta()})
        self.assertEqual(r["error"]["code"], -32601)


class ProtocolTests(ServerCase):
    def test_unsupported_version(self):
        r = self.call("tools/list", {"_meta": meta(version="1999-01-01")})
        self.assertEqual(r["error"]["code"], -32022)
        self.assertEqual(r["error"]["data"]["requested"], "1999-01-01")
        self.assertIn("2026-07-28", r["error"]["data"]["supported"])

    def test_missing_version_meta(self):
        r = self.call("tools/list", {"_meta": {
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_method(self):
        r = self.call("bogus/method", {"_meta": meta()})
        self.assertEqual(r["error"]["code"], -32601)

    def test_subscriptions_listen_unimplemented(self):
        r = self.call("subscriptions/listen",
                      {"notifications": {"taskIds": ["x"]}, "_meta": meta()})
        self.assertEqual(r["error"]["code"], -32601)

    def test_notifications_get_no_reply(self):
        self.client.notify("notifications/initialized")
        r = self.call("server/discover", {"_meta": meta()})
        self.assertIn("result", r)

    def test_result_meta_carries_server_info(self):
        r = self.call("server/discover", {"_meta": meta()})
        info = r["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
        self.assertEqual(info["name"], "cli-tasks-mcp")


class LegacyHandshakeTests(unittest.TestCase):
    def test_initialize_then_sync_call(self):
        client = Client()
        try:
            r = client.call("initialize", {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "legacy", "version": "1"}})
            self.assertEqual(r["result"]["protocolVersion"], "2025-11-25")
            self.assertIn("tools", r["result"]["capabilities"])
            client.notify("notifications/initialized")
            # After the handshake, requests without version _meta are legacy.
            r = client.call("tools/call", {
                "name": "run",
                "arguments": {"argv": py("print('legacy ok')")}})
            self.assertIn("legacy ok",
                          r["result"]["content"][0]["text"])
        finally:
            client.close()

    def test_initialize_unknown_version_negotiates(self):
        client = Client()
        try:
            r = client.call("initialize", {
                "protocolVersion": "1900-01-01",
                "capabilities": {},
                "clientInfo": {"name": "legacy", "version": "1"}})
            self.assertIn(r["result"]["protocolVersion"],
                          ("2025-11-25", "2025-06-18"))
        finally:
            client.close()


class TtlTests(unittest.TestCase):
    def test_expired_task_is_purged(self):
        client = Client(["--ttl-ms", "400"])
        try:
            r = client.call("tools/call", {
                "name": "run", "arguments": {"argv": py("print('short')")},
                "_meta": meta()})
            task_id = r["result"]["taskId"]
            deadline = time.time() + 10
            while time.time() < deadline:
                g = client.call("tasks/get",
                                {"taskId": task_id, "_meta": meta()})
                if g["result"]["status"] != "working":
                    break
                time.sleep(0.1)
            time.sleep(1.2)
            g = client.call("tasks/get",
                            {"taskId": task_id, "_meta": meta()})
            self.assertEqual(g["error"]["code"], -32602)
        finally:
            client.close()


class OutputCapTests(unittest.TestCase):
    def test_output_truncated_to_cap(self):
        client = Client(["--max-output-bytes", "4096"])
        try:
            r = client.call("tools/call", {
                "name": "run",
                "arguments": {"argv": py("print('x' * 20000)")},
                "_meta": meta()})
            task_id = r["result"]["taskId"]
            deadline = time.time() + 10
            while True:
                g = client.call("tasks/get",
                                {"taskId": task_id, "_meta": meta()})
                if g["result"]["status"] != "working":
                    break
                self.assertLess(time.time(), deadline)
                time.sleep(0.1)
            text = g["result"]["result"]["content"][0]["text"]
            self.assertIn("bytes dropped", text)
            self.assertLess(len(text), 9000)
        finally:
            client.close()


class AllowListTests(unittest.TestCase):
    def test_disallowed_command(self):
        client = Client(["--allow-commands", sys.executable])
        try:
            ok = client.call("tools/call", {
                "name": "run",
                "arguments": {"argv": py("print(1)")},
                "_meta": meta(tasks=False)})
            self.assertFalse(ok["result"]["isError"])
            bad = client.call("tools/call", {
                "name": "run",
                "arguments": {"argv": ["sh", "-c", "echo hi"]},
                "_meta": meta(tasks=False)})
            self.assertTrue(bad["result"]["isError"])
            self.assertIn("not allowed",
                          bad["result"]["content"][0]["text"])
        finally:
            client.close()


class UnitTests(unittest.TestCase):
    """Direct in-process checks, no subprocess."""

    def test_tail_buffer_drops_oldest(self):
        sys.path.insert(0, HERE)
        import cli_tasks_mcp as m
        buf = m.TailBuffer(5)
        buf.add(b"abcdef")
        buf.add(b"gh")
        data, dropped = buf.snapshot()
        self.assertEqual(data, b"defgh")
        self.assertEqual(dropped, 3)

    def test_iso_now_format(self):
        sys.path.insert(0, HERE)
        import cli_tasks_mcp as m
        self.assertTrue(m.iso_now().endswith("Z"))
        self.assertIn("T", m.iso_now())

    def test_invalid_jsonrpc_envelope(self):
        sys.path.insert(0, HERE)
        import cli_tasks_mcp as m
        server = m.Server(m.Config(_args()))
        r = server.handle({"not": "jsonrpc"})
        self.assertEqual(r["error"]["code"], -32600)

    def test_parse_error(self):
        sys.path.insert(0, HERE)
        import cli_tasks_mcp as m
        server = m.Server(m.Config(_args()))
        r = server._err(None, -32700, "Parse error")
        self.assertEqual(r["error"]["code"], -32700)


def _args():
    import argparse
    return argparse.Namespace(
        cwd=HERE, ttl_ms=3600000, poll_interval_ms=2000,
        max_timeout_ms=600000, max_output_bytes=262144,
        inherit_env=False, allow_commands=None)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""cli-tasks-mcp - run shell commands as MCP background tasks.

A zero-dependency MCP server (stdio transport, pure Python stdlib) that
implements the MCP Tasks extension (SEP-2663, ``io.modelcontextprotocol/tasks``)
on top of the stateless 2026-07-28 core protocol (SEP-2575).

An agent that calls the ``run`` tool with the tasks extension declared gets a
task handle back immediately instead of blocking on the command. It then polls
``tasks/get`` and may ``tasks/cancel``. Clients without the extension get the
classic synchronous tool result.

Sources:
- SEP-2663 Tasks Extension (Final)
- SEP-2575 Make MCP Stateless / spec revision 2026-07-28
"""

import argparse
import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

PROTOCOL_CURRENT = "2026-07-28"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18"]
TASKS_EXT = "io.modelcontextprotocol/tasks"
SERVER_INFO = {"name": "cli-tasks-mcp", "version": "1.0.0"}

E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603
E_MISSING_CLIENT_CAP = -32021  # spec 2026-07-28 basic/index error table
E_UNSUPPORTED_VERSION = -32022

SAFE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR",
)

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def iso_now():
    ts = datetime.datetime.now(datetime.timezone.utc)
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ms_now():
    return time.monotonic()


class TailBuffer:
    """Keeps only the last ``cap`` bytes written to it."""

    def __init__(self, cap):
        self.cap = cap
        self.buf = bytearray()
        self.dropped = 0
        self.lock = threading.Lock()

    def add(self, chunk):
        with self.lock:
            self.buf.extend(chunk)
            if len(self.buf) > self.cap:
                over = len(self.buf) - self.cap
                del self.buf[:over]
                self.dropped += over

    def snapshot(self):
        with self.lock:
            return bytes(self.buf), self.dropped


class Task:
    """One background command execution."""

    def __init__(self, argv, cwd, env, timeout_ms, cfg):
        self.task_id = str(uuid.uuid4())
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.timeout_ms = timeout_ms
        self.cfg = cfg
        self.created_at = iso_now()
        self.last_updated_at = self.created_at
        self.ttl_ms = cfg.ttl_ms
        self.poll_interval_ms = cfg.poll_interval_ms
        self.status = "working"
        self.status_message = "Command is running."
        self.result = None
        self.error = None
        self.proc = None
        self.stdout_buf = TailBuffer(cfg.max_output_bytes)
        self.stderr_buf = TailBuffer(cfg.max_output_bytes)
        self.timed_out = False
        self.cancel_requested = False
        self.exit_code = None
        self.done = threading.Event()
        self._created_monotonic = ms_now()

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Spawn the process. Returns an OSError or None."""
        kwargs = {
            "cwd": self.cwd, "env": self.env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(self.argv, **kwargs)
        except OSError as exc:
            return exc
        threading.Thread(target=self._reader,
                         args=(self.proc.stdout, self.stdout_buf),
                         daemon=True).start()
        threading.Thread(target=self._reader,
                         args=(self.proc.stderr, self.stderr_buf),
                         daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()
        return None

    def _reader(self, stream, buf):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                buf.add(chunk)
        except (OSError, ValueError):
            pass

    def _terminate(self, force=False):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.kill() if force else proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid),
                          signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def _monitor(self):
        try:
            self.exit_code = self.proc.wait(timeout=self.timeout_ms / 1000.0)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self._terminate(force=False)
            try:
                self.exit_code = self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._terminate(force=True)
                try:
                    self.exit_code = self.proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self.exit_code = -1
        self._finish()

    def _finish(self):
        self.result = self._build_result()
        if self.cancel_requested:
            self.status = "cancelled"
            self.status_message = "Cancelled by client."
        else:
            self.status = "completed"
            self.status_message = "Command finished (exit %s)." % self.exit_code
        self.last_updated_at = iso_now()
        self.done.set()

    def cancel(self):
        """Cooperative cancellation: signal the process; ack comes first."""
        if self.done.is_set():
            return
        self.cancel_requested = True
        self.status_message = "Cancellation requested."
        self.last_updated_at = iso_now()
        threading.Thread(target=self._terminate, kwargs={"force": False},
                         daemon=True).start()
        threading.Thread(target=self._force_later, daemon=True).start()

    def _force_later(self):
        if not self.done.wait(timeout=2.0):
            self._terminate(force=True)

    # -- result building ---------------------------------------------------

    def _build_result(self):
        out, out_dropped = self.stdout_buf.snapshot()
        err, err_dropped = self.stderr_buf.snapshot()
        lines = ["$ " + " ".join(self.argv)]
        if self.timed_out:
            lines.append("[timed out after %d ms]" % self.timeout_ms)
        lines.append("[exit %s]" % self.exit_code)
        lines.append("--- stdout ---")
        lines.append(out.decode("utf-8", "replace"))
        if out_dropped:
            lines.append("[%d earlier stdout bytes dropped]" % out_dropped)
        lines.append("--- stderr ---")
        lines.append(err.decode("utf-8", "replace"))
        if err_dropped:
            lines.append("[%d earlier stderr bytes dropped]" % err_dropped)
        is_error = bool(self.timed_out) or self.exit_code != 0
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "isError": is_error,
        }

    def to_public(self):
        task = {
            "taskId": self.task_id,
            "status": self.status,
            "statusMessage": self.status_message,
            "createdAt": self.created_at,
            "lastUpdatedAt": self.last_updated_at,
            "ttlMs": self.ttl_ms,
            "pollIntervalMs": self.poll_interval_ms,
        }
        if self.status == "completed":
            task["result"] = self.result
        elif self.status == "failed":
            task["error"] = self.error
        return task


class Config:
    def __init__(self, args):
        self.root = os.path.realpath(args.cwd)
        self.ttl_ms = args.ttl_ms
        self.poll_interval_ms = args.poll_interval_ms
        self.max_timeout_ms = args.max_timeout_ms
        self.max_output_bytes = args.max_output_bytes
        self.inherit_env = args.inherit_env
        self.allow_commands = (set(args.allow_commands.split(","))
                               if args.allow_commands else None)


class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tasks = {}
        self.tasks_lock = threading.Lock()
        self.legacy_version = None  # set by a legacy initialize handshake

    # -- helpers -----------------------------------------------------------

    def _meta(self):
        return {"io.modelcontextprotocol/serverInfo": SERVER_INFO}

    def _ok(self, rid, result, result_type="complete"):
        result = dict(result)
        result["resultType"] = result_type
        result["_meta"] = self._meta()
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _err(self, rid, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rid, "error": err}

    def _request_version(self, msg):
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        return meta.get("io.modelcontextprotocol/protocolVersion")

    def _client_declares_tasks(self, msg):
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
        exts = caps.get("extensions") or {}
        return TASKS_EXT in exts

    def _effective_version(self, msg):
        version = self._request_version(msg)
        if version:
            return version
        return self.legacy_version

    def _tasks_enabled(self, msg):
        """The extension is defined only under 2026-07-28 (SEP-2663
        backward-compatibility table)."""
        return (self._effective_version(msg) == PROTOCOL_CURRENT
                and self._client_declares_tasks(msg))

    def _missing_cap_error(self, rid):
        return self._err(rid, E_MISSING_CLIENT_CAP,
                         "Missing required client capability",
                         {"requiredCapabilities":
                          {"extensions": {TASKS_EXT: {}}}})

    def _purge_expired(self):
        now = ms_now()
        with self.tasks_lock:
            doomed = []
            for tid, task in self.tasks.items():
                if task.ttl_ms is None or not task.done.is_set():
                    continue
                if (now - task._created_monotonic) * 1000.0 > task.ttl_ms:
                    doomed.append(tid)
            for tid in doomed:
                del self.tasks[tid]

    def _get_task(self, tid):
        with self.tasks_lock:
            return self.tasks.get(tid)

    # -- validation --------------------------------------------------------

    def _check_version(self, msg):
        version = self._request_version(msg)
        if version is None:
            if self.legacy_version is not None:
                return None
            method = msg.get("method")
            if method in ("server/discover", "initialize"):
                return None
            return self._err(msg.get("id"), E_INVALID_PARAMS,
                             "Missing io.modelcontextprotocol/protocolVersion"
                             " in request _meta")
        if version not in SUPPORTED_VERSIONS:
            return self._err(msg.get("id"), E_UNSUPPORTED_VERSION,
                             "Unsupported protocol version",
                             {"supported": SUPPORTED_VERSIONS,
                              "requested": version})
        return None

    def _validate_run_args(self, arguments):
        if not isinstance(arguments, dict):
            return "arguments must be an object"
        argv = arguments.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) and a for a in argv)):
            return "arguments.argv must be a non-empty array of strings"
        env = arguments.get("env")
        if env is not None and not isinstance(env, dict):
            return "arguments.env must be an object of strings"
        if env:
            for k, v in env.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    return "arguments.env must map strings to strings"
        timeout = arguments.get("timeoutMs")
        if timeout is not None and (not isinstance(timeout, int)
                                    or isinstance(timeout, bool)
                                    or timeout <= 0):
            return "arguments.timeoutMs must be a positive integer"
        cwd = arguments.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            return "arguments.cwd must be a string"
        return None

    def _resolve_cwd(self, arguments):
        cwd = arguments.get("cwd")
        if not cwd:
            return self.cfg.root, None
        if not os.path.isabs(cwd):
            candidate = os.path.realpath(os.path.join(self.cfg.root, cwd))
        else:
            candidate = os.path.realpath(cwd)
        root = self.cfg.root
        if candidate != root and not candidate.startswith(root + os.sep):
            return None, ("cwd %r is outside the server root %r"
                          % (cwd, root))
        if not os.path.isdir(candidate):
            return None, "cwd %r is not a directory" % cwd
        return candidate, None

    def _build_env(self, arguments):
        if self.cfg.inherit_env:
            env = dict(os.environ)
        else:
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in SAFE_ENV_KEYS}
        env.update(arguments.get("env") or {})
        return env

    def _command_allowed(self, argv):
        if self.cfg.allow_commands is None:
            return True
        base = os.path.basename(argv[0])
        return base in self.cfg.allow_commands or argv[0] in self.cfg.allow_commands

    # -- methods -----------------------------------------------------------

    TOOL = {
        "name": "run",
        "description": (
            "Run a local command (argv array, no shell) as a background MCP "
            "task. With the io.modelcontextprotocol/tasks extension declared, "
            "returns a task handle immediately; poll tasks/get for status and "
            "output, tasks/cancel to stop it. Without the extension the "
            "command runs synchronously and the result is returned directly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "description": "Command and arguments, no shell expansion.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory; must stay inside the "
                                   "server's root directory.",
                },
                "timeoutMs": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Kill the command after this many "
                                   "milliseconds (server-capped).",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Extra environment variables (merged over "
                                   "the server's scrubbed environment).",
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    }

    def m_discover(self, rid, msg):
        return self._ok(rid, {
            "supportedVersions": SUPPORTED_VERSIONS,
            "capabilities": {
                "tools": {},
                "extensions": {TASKS_EXT: {}},
            },
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Run local commands as background tasks via the 'run' tool. "
                "Declare the io.modelcontextprotocol/tasks extension in "
                "per-request client capabilities to receive task handles, "
                "then poll tasks/get; call tasks/cancel to stop a command. "
                "Without the extension, 'run' executes synchronously."
            ),
        })

    def m_initialize(self, rid, msg):
        """Legacy handshake for pre-2026-07-28 clients."""
        params = msg.get("params") or {}
        requested = params.get("protocolVersion", "2025-11-25")
        negotiated = (requested if requested in SUPPORTED_VERSIONS
                      else "2025-11-25")
        self.legacy_version = negotiated
        return self._ok(rid, {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": "Run local commands via the 'run' tool. "
                            "Task augmentation requires protocol version "
                            "2026-07-28 with the "
                            "io.modelcontextprotocol/tasks extension.",
        })

    def m_tools_list(self, rid, msg):
        return self._ok(rid, {
            "tools": [self.TOOL],
            "ttlMs": 60000,
            "cacheScope": "private",
        })

    def m_tools_call(self, rid, msg):
        params = msg.get("params") or {}
        name = params.get("name")
        if name != "run":
            return self._err(rid, E_INVALID_PARAMS,
                             "Unknown tool: %r" % (name,))
        arguments = params.get("arguments") or {}
        problem = self._validate_run_args(arguments)
        if problem:
            return self._err(rid, E_INVALID_PARAMS, problem)
        if not self._command_allowed(arguments["argv"]):
            return self._ok(rid, {
                "content": [{"type": "text", "text":
                             "Command not allowed by --allow-commands: %s"
                             % arguments["argv"][0]}],
                "isError": True,
            })
        cwd, problem = self._resolve_cwd(arguments)
        if problem:
            return self._ok(rid, {
                "content": [{"type": "text", "text": problem}],
                "isError": True,
            })
        env = self._build_env(arguments)
        timeout_ms = min(arguments.get("timeoutMs")
                         or self.cfg.max_timeout_ms,
                         self.cfg.max_timeout_ms)
        task = Task(arguments["argv"], cwd, env, timeout_ms, self.cfg)
        spawn_error = task.start()
        if spawn_error is not None:
            return self._ok(rid, {
                "content": [{"type": "text", "text":
                             "Failed to start %r: %s"
                             % (arguments["argv"][0], spawn_error)}],
                "isError": True,
            })
        if not self._tasks_enabled(msg):
            task.done.wait()  # the monitor always reaches _finish
            return self._ok(rid, task.result)
        with self.tasks_lock:
            self.tasks[task.task_id] = task
        result = task.to_public()
        return self._ok(rid, result, result_type="task")

    def m_tasks_get(self, rid, msg):
        if not self._tasks_enabled(msg):
            return self._missing_cap_error(rid)
        tid = (msg.get("params") or {}).get("taskId")
        task = self._get_task(tid) if isinstance(tid, str) else None
        if task is None:
            return self._err(rid, E_INVALID_PARAMS,
                             "Failed to retrieve task: Task not found")
        result = task.to_public()
        return self._ok(rid, result)

    def m_tasks_update(self, rid, msg):
        if not self._tasks_enabled(msg):
            return self._missing_cap_error(rid)
        tid = (msg.get("params") or {}).get("taskId")
        task = self._get_task(tid) if isinstance(tid, str) else None
        if task is None:
            return self._err(rid, E_INVALID_PARAMS,
                             "Failed to update task: Task not found")
        # This server never sets input_required; per SEP-2663 the server
        # SHOULD ignore inputResponses keys that are not outstanding.
        return self._ok(rid, {})

    def m_tasks_cancel(self, rid, msg):
        if not self._tasks_enabled(msg):
            return self._missing_cap_error(rid)
        tid = (msg.get("params") or {}).get("taskId")
        task = self._get_task(tid) if isinstance(tid, str) else None
        if task is None:
            return self._err(rid, E_INVALID_PARAMS,
                             "Failed to cancel task: Task not found")
        task.cancel()
        return self._ok(rid, {})

    # -- dispatch ----------------------------------------------------------

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._err(None, -32600, "Invalid Request")
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:  # notification: never respond
            return None
        if not isinstance(method, str):
            return self._err(rid, -32600, "Invalid Request")
        version_error = self._check_version(msg)
        if version_error is not None:
            return version_error
        self._purge_expired()
        handlers = {
            "server/discover": self.m_discover,
            "initialize": self.m_initialize,
            "ping": lambda r, m: self._ok(r, {}),
            "tools/list": self.m_tools_list,
            "tools/call": self.m_tools_call,
            "tasks/get": self.m_tasks_get,
            "tasks/update": self.m_tasks_update,
            "tasks/cancel": self.m_tasks_cancel,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._err(rid, E_METHOD_NOT_FOUND,
                             "Method not found: %s" % method)
        try:
            return handler(rid, msg)
        except Exception as exc:  # last-resort protocol error
            return self._err(rid, E_INTERNAL, "Internal error: %s" % exc)

    def serve(self):
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                response = self._err(None, -32700, "Parse error")
            else:
                response = self.handle(msg)
            if response is not None:
                out.write(json.dumps(response) + "\n")
                out.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cli-tasks-mcp",
        description="MCP server (stdio): run shell commands as background "
                    "tasks per the MCP Tasks extension (SEP-2663).")
    parser.add_argument("--cwd", default=os.getcwd(),
                        help="Root directory commands are confined to "
                             "(default: current directory).")
    parser.add_argument("--ttl-ms", type=int, default=3600000,
                        help="How long finished tasks stay queryable, in ms "
                             "(default: 3600000).")
    parser.add_argument("--poll-interval-ms", type=int, default=2000,
                        help="Suggested client polling interval in ms "
                             "(default: 2000).")
    parser.add_argument("--max-timeout-ms", type=int, default=600000,
                        help="Cap on per-command runtime in ms "
                             "(default: 600000).")
    parser.add_argument("--max-output-bytes", type=int, default=262144,
                        help="Bytes of stdout/stderr kept per stream; older "
                             "bytes are dropped (default: 262144).")
    parser.add_argument("--inherit-env", action="store_true",
                        help="Pass the server's full environment to commands "
                             "(default: scrubbed minimal environment).")
    parser.add_argument("--allow-commands", default=None,
                        help="Comma-separated allowlist of executable names "
                             "(default: all commands allowed).")
    args = parser.parse_args(argv)
    Server(Config(args)).serve()


if __name__ == "__main__":
    main()

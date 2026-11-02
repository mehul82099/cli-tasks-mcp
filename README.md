# cli-tasks-mcp

Run shell commands as MCP background tasks. A zero-dependency,
single-file Python MCP server (stdio) implementing the MCP Tasks
extension (`io.modelcontextprotocol/tasks`, SEP-2663) on the stateless
2026-07-28 core protocol.

Builds, test suites, deploys and installs are long-running commands.
An agent that runs them through a normal MCP tool call blocks until
they finish. The MCP Tasks extension standardizes the alternative:
the server answers `tools/call` with a task handle immediately, and the
client polls `tasks/get` (or cancels with `tasks/cancel`) while the
command runs in the background.

Support for the extension is currently framework-based: FastMCP merged
it in [PR #4603](https://github.com/PrefectHQ/fastmcp/pull/4603)
(July 2026), the C# SDK ships task samples in its v2 previews, and
SEP-2663 names a [Go reference
implementation](https://github.com/panyam/mcpkit). cli-tasks-mcp is the
same protocol in one readable Python file with no dependencies and no
install step - useful when you want a command-running server you can
audit completely and drop onto any machine with Python 3.9+.

## What it does

One tool, `run`:

```json
{"argv": ["npm", "test"], "cwd": "packages/api", "timeoutMs": 300000}
```

- With the tasks extension declared in the request's
  `_meta."io.modelcontextprotocol/clientCapabilities".extensions`, the
  server spawns the command and replies with a `CreateTaskResult`
  (`resultType: "task"`, `status: "working"`, unguessable UUID taskId,
  `ttlMs`, `pollIntervalMs`).
- The client polls `tasks/get`. Finished tasks carry the standard tool
  result: exit code, combined stdout/stderr tail, truncation markers.
- `tasks/cancel` terminates the process group (SIGTERM, then SIGKILL).
- Without the extension, `run` executes synchronously and returns the
  same result shape directly - so any MCP client can use the server.

## Run it

```sh
python3 cli_tasks_mcp.py --cwd /path/to/project
```

Client configuration (stdio):

```json
{
  "mcpServers": {
    "cli-tasks": {
      "command": "python3",
      "args": ["/path/to/cli_tasks_mcp.py", "--cwd", "/path/to/project"]
    }
  }
}
```

Flags: `--cwd` (root commands are confined to), `--max-timeout-ms`
(600000), `--max-output-bytes` (262144, oldest bytes dropped),
`--ttl-ms` (3600000, how long finished tasks stay queryable),
`--poll-interval-ms` (2000), `--allow-commands` (comma-separated
allowlist of executable names), `--inherit-env` (pass the full
environment instead of the scrubbed minimal one).

## Security, read before deploying

This server runs arbitrary local commands as the user who started it.
That is the point, and it is dangerous by design - the same class of
tool as desktop-commander or shell MCP servers. Concretely:

- Commands are argv arrays; there is no shell expansion, so quoting
  and globbing cannot inject. If a caller passes `sh -c ...`
  explicitly, that is their choice, not an accident.
- The environment is scrubbed to a small safe list (PATH, HOME, ...)
  unless `--inherit-env` is set; secrets in the server's own
  environment do not leak into child processes by default.
- `--cwd` confines working directories to a root; `--allow-commands`
  restricts which executables run.
- Task IDs are UUID4s, so one caller cannot enumerate another's tasks
  (SEP-2663's security note); there is deliberately no `tasks/list`.
- stdio serves a single client process; there is no network listener.

Use it for a local agent you trust, scoped to a project directory. Do
not expose it to untrusted callers.

## Protocol coverage

Implements, per [SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks)
(Final) and [SEP-2575](https://modelcontextprotocol.io/seps/2575-stateless-mcp)
/ the [2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28):

- `server/discover` (supportedVersions, capabilities, serverInfo)
- per-request version/capability `_meta`; `-32022` for unsupported
  versions, `-32021` for missing client capabilities
- `tools/list`, `tools/call` with server-directed task creation and a
  synchronous fallback for non-declaring clients
- `tasks/get` (all five statuses), `tasks/update` (ack; this server
  never enters `input_required`), `tasks/cancel` (cooperative, ack
  first, eventual terminal status)
- correct status semantics: a non-zero exit or a timeout is a
  **completed** task whose result has `isError: true`; `failed` is
  reserved for JSON-RPC-level execution errors, as the spec requires
- task TTL with purge (expired tasks answer `-32602`), `ttlMs` and
  `pollIntervalMs` on every task payload
- legacy `initialize` handshake for pre-2026-07-28 clients, which then
  get synchronous execution only (the extension is not defined under
  2025-11-25 per SEP-2663's compatibility table)

Deliberately not implemented (documented, returns `-32601`):

- `notifications/tasks` push and `subscriptions/listen` - polling is
  always available, so every client works
- the removed 2025-11-25 experimental tasks surface (`tasks/result`,
  `tasks/list`, the `task` request parameter, the `tasks.requests.*`
  capability shape)

Known spec inconsistency worth knowing: SEP-2663's text shows error
code `-32003` for a missing client capability, but the published
2026-07-28 specification's error table and SEP-2575 define it as
`-32021`. This server follows the published specification (`-32021`).

## Tests

36 stdlib `unittest` tests drive the real server over stdio as an MCP
client: task lifecycle, cancellation, timeouts, sync fallback, error
codes, version negotiation, the legacy handshake, TTL purging, output
caps and the command allowlist. CI runs them on Ubuntu and Windows,
Python 3.9 and 3.13.

```sh
python3 -m unittest -v
```

## Sources

- [SEP-2663: Tasks Extension](https://modelcontextprotocol.io/seps/2663-tasks) (Final)
- [SEP-2575: Make MCP Stateless](https://modelcontextprotocol.io/seps/2575-stateless-mcp)
- [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) and its changelog
- [FastMCP PR #4603](https://github.com/PrefectHQ/fastmcp/pull/4603) (framework support, merged 2026-07-24)
- [mcpkit](https://github.com/panyam/mcpkit) (Go reference implementation cited by SEP-2663)

Spec texts fetched 2026-11-02. Implementation tested against the spec's
own message examples; no third-party SDK client was interop-tested.

## License

MIT

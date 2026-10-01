# AGENTS.md

Persistent multi-repository coding agent using `openai-codex==0.159.2`, HTTP/SSE and a separate
AMQP relay. Deployment runs on Raspberry Pi; Telegram UI lives in `alice/modules/agent.py`.

## Commands

```sh
uv sync --frozen --all-extras
.venv/bin/pytest -q
ANTARES_CONTRACT=1 .venv/bin/pytest tests/test_codex_contract.py -q
ruff check src tests
ruff format src tests
.venv/bin/antares-agent --check
```

Tests fake the runtime except opt-in `test_codex_contract.py` (real CLI, no model) and
`test_live.py` (real authenticated model). Unix-socket/async tests require an execution environment
that permits local sockets. Do not skip failures caused by the execution sandbox.

## Runtime

Request flow: api → manager → runner → shared runtime → translator → eventlog → store.
Each service uses one Codex app-server. Six turns may execute concurrently; each thread queues
its messages. Codex owns conversation files; SQLite owns thread IDs, modes, events and transfers.

- Use `on-request` with `auto_review`; never replace automatic review with `never` or blanket approval.
- Unexpected command/file approval callbacks decline. User questions use ordinary assistant text.
- `plan` is native plan collaboration mode with read-only sandbox; `auto` uses workspace-write.
- Busy mode changes return 409. Root turn completion ends the turn; instruct children to finish first.
- Persist thread IDs before starting turns. Unsubscribe after turns. Never replay uncertain work.
- Fixed workspace uses AGENTS.md and .agents/skills. Reload skills each turn and explicitly allow
  skill directory writes; reject duplicate names and paths outside the workspace.
- Shared auth uses a Unix-socket broker in qq-codex-agent. It alone holds/refreshes refresh tokens.
  Clients use external ChatGPT tokens in memory with independent CODEX_HOME and workspaces.
- Managed deny-read uses directories only. Resources live outside private state; tmp/arg0 is
  mounted read-only to use the official executable fallback. See migration design for details.
- Production managed requirements and sandbox preflight must pass before serving requests.
- Old Claude state is archived; new state uses codex-v1 directories and separate alice databases.

`docs/design/06-codex-migration.md` is the current runtime/deployment record.
`05-file-transfer.md` remains current. Documents 00–04 preserve Claude-era history only.

User-facing messages are Chinese; code identifiers are English. Read files before editing, prefer
existing helpers and small changes, test substantive logic. Comments explain durable constraints,
use at most seven words, and never narrate a change. Function names use at most four words.

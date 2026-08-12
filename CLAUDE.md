# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A persistent multi-repo coding agent that wraps the Claude Agent SDK and exposes it over HTTP/SSE.
It runs as a systemd service on a Raspberry Pi, holds several `ClaudeSDKClient` sessions in an LRU
pool, arbitrates tool permissions itself, and stays free of any IM-specific code — Telegram support
lives entirely in `relay.py` (a dumb SSE↔AMQP pipe) plus a bot outside this repo
(`~/Documents/GitHub/alice/modules/agent.py`, a declared extra working directory).

## Commands

```bash
nix-shell                     # dev env: bwrap, socat, ripgrep, ruff, uv
uv sync --all-extras          # deps into .venv (relay extras included)

.venv/bin/pytest -q                                  # 128 tests, ~5s, no CLI needed
.venv/bin/pytest tests/test_runner.py::test_name -v  # one test
ruff check . && ruff format .                        # line-length 100

.venv/bin/antares-agent --check          # preflight only (sandbox self-check), then exit
.venv/bin/antares-agent --host 127.0.0.1 --port 60001   # forces TCP even if ANTARES_SOCKET is set
```

The live test drives a real `claude` binary and a real model endpoint; it is skipped unless enabled:

```bash
ANTARES_LIVE=1 ANTARES_LIVE_KEY_FILE=~/configs/deepseek_api_key.txt \
ANTARES_LIVE_BASE_URL=https://api.deepseek.com/anthropic \
ANTARES_LIVE_MODEL=deepseek-v4-flash \
.venv/bin/pytest tests/test_live.py -v
```

Everything is configured through `ANTARES_*` env vars, read once in `config.py::Settings.from_env`.

## Design docs are the source of truth for *why*

`docs/design/` is a decision record, in Chinese, and the code refers to it constantly:

- `00-overview.md` — architecture, decisions **D1–D9**, deployment/systemd hardening
- `01-workspace-manifest.md` — `workspace.toml` schema
- `02-sse-api.md` — event vocabulary and endpoint table
- `03-verification.md` — **F1–F30**, measured SDK/CLI behaviour; six of these overturned the design
- `04-telegram.md` — **D10–D14**, the relay
- `docs/TODO.md` — what is unfinished, ordered by consequence

A comment saying `(F8)` or `(D3)` is pointing at a measurement or a decision, not decoration.
Before changing anything those tags touch, read the entry — most of them exist because the obvious
implementation was tried and silently failed.

## Layout

Request path: `api.py` (FastAPI/SSE) → `manager.py` (LRU pool of threads) → `runner.py` (one
`ClaudeSDKClient` + message loop) → `translate.py` (SDK messages → SSE events) → `eventlog.py`
(sequence + fan-out) → `store.py` (sqlite).

Cross-cutting: `permissions.py` + `shlex_split.py` (arbitration), `approvals.py` (out-of-band
approval futures), `manifest.py` + `index.py` (workspace description), `profiles.py` (cold config),
`gitdiff.py` (end-of-turn diffs), `preflight.py` (startup self-check), `relay.py` (separate process).

Conversation state is **not** stored here — the CLI owns its session files, and `store.py` only
keeps the `session_id` needed to `resume`, plus event history for `?after=` replay.

## Invariants that are easy to break by accident

- **`allowed_tools` must stay empty** (F8). Allow rules are evaluated before `can_use_tool` and
  silently short-circuit it, disabling the whole arbiter. All allowing happens inside the callback.
- **`can_use_tool` must always be supplied** (F16), or the CLI falls back to a non-interactive path
  that rejects writes inside cwd with a misleading message.
- **Read `receive_messages()`, never `receive_response()`** (F25). The latter returns at the first
  `ResultMessage`, but background subagents outlive the turn. Idle is *inferred*:
  `ResultMessage` + `Translator.idle_possible` + a short settle delay.
- **The sandbox fails open** (F23). Missing `bwrap`/`socat` prints one warning and silently runs
  everything unconfined while `sandbox.enabled` stays True. `preflight.run()` therefore has to pass
  before the app accepts requests, and it probes bubblewrap for real (including `--proc /proc`).
- **Path deny rules need the `//` anchor** (F27) — use `permissions.path_rule`, never format them
  inline. `Read(/abs/**)` parses fine, matches nothing, and leaks silently.
- **The sandbox blocks writes but not reads** (F19). Anything the service uid can read, the agent
  can read; `secret_path_hit` and the deny rules are the only guard, and OS-level uid separation is
  the only real one.
- **cwd is constant** (`~/agent_work`, D2) and repos live under it. That is what makes skills and
  per-repo `CLAUDE.md` lazily discoverable with no registration step (F1/F4).
- **Profile is cold, permission mode is hot** (D6/D7). The system prompt sits in the cache prefix, so
  it is fixed when the thread is created; `set_permission_mode()` never enters a model request.
- **`enable_file_checkpointing=True` can only be set at session creation** (D8), so it is always on.
- Duplicate skill names across repos are a hard 400 at thread creation, not a warning (F2): the loser
  is shadowed with no syntax to disambiguate.

## Conventions

- User-facing strings (event messages, refusal reasons, prompts, profile text) are **Chinese**; code,
  comments and docstrings are English. Ruff's `RUF001–003` are disabled for that reason.
- Comments explain *why*, and tend to name the failure they prevent. Match that density; a comment
  restating the code is noise here.
- Tests fake the SDK client (`ClientFactory` injection in `ThreadRunner`) — only `test_live.py`
  spawns a real CLI, and only it uses a real uvicorn server (TestClient buffers SSE and hangs).

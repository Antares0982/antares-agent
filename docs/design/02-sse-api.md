# SSE + HTTP API

对外接口。目标：**任何 IM 适配器都是薄客户端** —— 分组、折叠、渲染策略由服务端提供足够信息，客户端不需要理解 Agent SDK 的语义。

Base：`http://127.0.0.1:<port>/v1`（或 unix socket）。单人使用，鉴权由外层网关处理。

## 设计要点

1. **事件自带归属**：`agent` 块让客户端能直接建出编排者/subagent 的树，无需查询任何状态
2. **事件自带渲染提示**：`render` 字段决定折叠还是展开，客户端不需要维护工具名白名单 —— 运行时新增的 MCP 工具也有策略
3. **重放在协议层解决**：SSE 端点接受 `?after=<event_id>`，而不是让客户端做"先开流、再拉历史、按 id 去重"那套。单机自持久化，可以做得更简单
4. **审批是带外的请求/响应对**：`canUseTool` 阻塞在 Future 上，事件流推出 `approval.required`，客户端 `POST /approve` 来 resolve

## HTTP 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查、profile 列表、活跃 thread 数 |
| `GET` | `/profiles` | 可用 profile 及其描述 |
| `POST` | `/threads` | 建 thread。`{profile?}`。返回 `thread_id` |
| `GET` | `/threads` | 列表。倒序，含 `status` / `summary` |
| `GET` | `/threads/{id}` | 详情 |
| `DELETE` | `/threads/{id}` | 删除（软删）。取消进行中任务，释放挂起审批 |
| `GET` | `/threads/{id}/events` | **SSE 流**。`?after=<event_id>` 从该事件之后重放 |
| `GET` | `/threads/{id}/events/replay` | 只补历史，**不唤醒线程**。返回 JSON |
| `POST` | `/threads/{id}/messages` | 发消息。`{text}`。立即返回 `202`，结果走事件流 |
| `POST` | `/threads/{id}/approve` | 提交审批。`{approval_id, decision, message?}` |
| `POST` | `/threads/{id}/interrupt` | 打断当前 turn |
| `POST` | `/threads/{id}/mode` | 热切 permission mode。`{mode}` |
| `POST` | `/threads/{id}/fork` | 分叉。返回新 `thread_id`，原 thread 不变 |
| `POST` | `/threads/{id}/undo` | 文件回滚。`{message_id}` → `rewind_files()` |

`POST /messages` 立即返回而非等待完成，是为了让"排队"（D1）在协议上自然表达：thread busy 时消息入队，客户端从事件流里看到 `queued`。

## 事件信封

```
event: <type>
id: <event_id>
data: { ... }
```

所有事件的 `data` 共享：

```jsonc
{
  "id": "evt_000123",          // 单调递增，thread 内唯一。用于 ?after= 重放
  "thread_id": "thr_...",
  "ts": "2026-08-05T10:00:00Z",
  "agent": {
    "id": "root",              // "root" 或 "agt_<n>"
    "name": "orchestrator",    // 或 "repo-api"
    "parent_tool_use_id": null // subagent 内产生的事件带父 Agent 工具调用 id
  }
}
```

`agent.parent_tool_use_id` 直接对应 SDK 的 `parent_tool_use_id` 字段。客户端只需按它分组，就能得到正确的树 —— **注意 subagent 默认后台运行，多个 agent 的事件是交错到达的，到达顺序不代表因果顺序**。

## 事件类型

### `thread.status`

```jsonc
{ "status": "idle" | "busy" | "awaiting_approval", "background_agents": 2 }
```

⚠️ **`idle` 的判据不是收到 `ResultMessage`**（实测 F25）。后台 subagent 会活过 turn，
SDK 随后自动续跑并再产生 `ResultMessage`。真正的空闲条件是
**`ResultMessage` 且 `background_tasks_changed.tasks` 为空**。

`background_agents` 是当前仍在跑的后台 agent 数，直接取自 `background_tasks_changed.tasks`，
客户端可据此显示"还有 2 个调研在跑"，避免用户以为任务已经结束。

### `text` — 模型文本输出

```jsonc
{ "content": "我先看一下 api 的接口定义...", "delta": false }
```

⚠️ **`delta` 恒为 `false`**（`translate.py`）。D1 要求用 `receive_messages()` 而非
`receive_response()`，拿到的是完整的 `AssistantMessage`，所以一个完整文本块推一条事件，
**不是逐 token 流式**。字段保留是给将来接 partial 流用的，现在客户端可以无视它。

这条对适配器是好消息：事件速率是每 turn 几十条而非每秒几百条，
不需要为节流做特别设计（Telegram 那 1 次/秒的编辑限额才是瓶颈）。

### `tool.call`

```jsonc
{
  "tool_use_id": "toolu_...",
  "tool": "Edit",
  "input": { "file_path": "api/routes.py", ... },
  "render": "diff",          // none | summary | diff | full
  "sandboxed": true          // 是否在沙箱内执行（D3：沙箱内不审批但仍推事件）
}
```

`render` 的服务端默认策略：

| 工具 | render | 理由 |
|---|---|---|
| `Read` `Glob` `Grep` `WebFetch` `WebSearch` | `none` | 读类，默认折叠 |
| `Edit` `Write` `NotebookEdit` | `diff` | 改动是用户要看的核心 |
| `Bash` | `summary` | 显示命令本身，不显示输出全文 |
| `Agent` | `summary` | 显示派了谁、任务是什么。注意 `system:init` 的 `tools` 里写作 `Task`，但 tool_use block 的 `name` 是 `Agent`（实测 F5）—— 以后者为准 |
| `mcp__*` | `summary` | 未知工具的保守默认 |

客户端可覆盖（比如 `/verbose` 展开一切），但不必自己维护名单。

### `tool.result`

```jsonc
{
  "tool_use_id": "toolu_...",
  "tool": "Edit",
  "is_error": false,
  "preview": "..."           // 截断至 500 字符
}
```

### `agent.spawn` / `agent.done`

```jsonc
// spawn
{ "tool_use_id": "toolu_...", "subagent_type": "Explore", "repo_hint": "api",
  "task": "调研 api/ 下 /v1/user 相关的接口定义与鉴权路径" }

// done
{ "tool_use_id": "toolu_...", "subagent_type": "Explore", "agent_id": "agt_...", "summary": "..." }
```

`subagent_type` 是**内置 agent 名**（`Explore` / `general-purpose`），不是仓库名 —— 按 D9 不再有
per-repo agent。仓库归属靠 `repo_hint`：服务端拿 task 文本去匹配 manifest 里的 `repo.path` 前缀，
纯属给客户端分组显示的启发式，匹配不上就省略该字段。

`agent_done.agent_id` 来自 Agent 工具结果里的 `agentId:` 尾注（已实测存在），可用于后续 resume 该 subagent。

### `approval.required`

```jsonc
{
  "approval_id": "apr_a1b2c3",     // 短 id：Telegram callback_data 上限 64 字节
  "tool_use_id": "toolu_...",
  "tool": "Bash",
  "input": { "command": "git push origin feat/x" },
  "reason": "命令需要网络访问未授权域名 github.com",
  "expires_at": "2026-08-05T10:10:00Z"
}
```

**多个后台 subagent 可能同时挂起审批**，客户端必须能同时展示多个待批项，且回传时带上 `approval_id`。`approval_id` 独立于 `tool_use_id` 且刻意做短，就是为了塞进受限的回调载荷。

### `approval.resolved`

```jsonc
{ "approval_id": "apr_a1b2c3", "decision": "allow" | "deny", "message": "先跑测试" }
```

### `diff` — turn 结束时的改动汇总

```jsonc
{
  "repo": "api",                   // 对应 manifest 的 repo.name
  "stat": " 2 files changed, 14 insertions(+), 3 deletions(-)",
  "patch": "diff --git a/..."      // 超长时截断，附 truncated: true
}
```

在收到 SDK 的 `ResultMessage` 时，对 manifest 里每个仓库跑 `git diff`，逐仓库推送。

**这是有意不依赖 `Edit` 工具事件的**：`sed -i`、`python fix.py`、`git apply` 造成的改动同样会被捕获。工具事件用于实时感知，`diff` 事件用于事实核对。

### `queued`

```jsonc
{ "position": 1 }
```

thread busy 时收到新消息（D1）。

### `turn.done`

一个逻辑任务会产生**多个** `ResultMessage`（F25：每次后台 agent 完成后 SDK 自动续跑一轮），
所以 `turn.done` 与"任务完成"不是一回事 —— 后者看 `thread.status: idle`。
客户端不应在 `turn.done` 上收起"进行中"指示。

```jsonc
{ "subtype": "success" | "error_max_turns" | "error_max_budget_usd",
  "usage": { "input_tokens": 24556, "cache_read_input_tokens": 24576, "output_tokens": 193 },
  "cost_usd": 0.0421, "cost_trusted": false, "duration_ms": 48213 }
```

⚠️ **`cost_usd` 在非 Anthropic 端点下不可信**（实测 F15）：CLI 按 Anthropic 价目表折算，
接 DeepSeek 时一次 trivial turn 报 `$0.149`。因此同时推原始 `usage`，并用 `cost_trusted`
标记 —— base URL 非 Anthropic 时置 `false`，客户端应改用 token 数自行折算。

### `error`

```jsonc
{ "code": "interrupted" | "internal" | "approval_timeout" | "approval_lost", "message": "...",
  "approval_ids": ["apr_a1b2c3"] }
```

`approval_lost` 表示挂起的审批因进程重启而失效（见下节），`approval_ids` 仅此码有。

## 审批时序

```
  客户端                    antares-agent                 SDK
     │                            │                        │
     │                            │◄── canUseTool(...) ─────┤  (阻塞)
     │                            │                        │
     │◄── approval.required ──────┤  创建 Future            │
     │◄── thread.status:awaiting ─┤                        │
     │                            │                        │
     ├─── POST /approve ─────────►│  resolve(Future)        │
     │                            ├─── allow/deny ─────────►│  (恢复)
     │◄── approval.resolved ──────┤                        │
     │◄── thread.status:busy ─────┤                        │
```

超时（默认 10 分钟）自动 deny，附说明让 agent 换方案，推 `error{code:"approval_timeout"}`。

### 进程崩溃时（V1 已实测，见 `03-verification.md`）

挂起的 Future 随进程一起消失，CLI 侧会把该工具**合成为一次失败**（`AbortError: Tool permission
stream closed before response received`）并让 turn 正常收尾。会话状态一致，**无需持久化 pending
approval**。

但审批 `resume` 后不会自动重发。恢复因此是**启动时的一次对账**（`ThreadManager.recover()`），
在接第一个请求之前跑完：

1. 逐个 thread 读事件流末条 `thread.status`。是 `idle` 就跳过 —— 上次是干净收尾的
2. 否则查有没有 `approval.required` 没配上 `approval.resolved`。
   有就推 `error{code:"approval_lost"}`，`approval_ids` 带上是哪几个
3. 无论有没有，补一条 `thread.status:idle`

判据取自**我们自己写的事件日志**，不去读 CLI 的 session 文件。那份文件里的
`AbortError: Tool permission stream closed` 说的是同一件事，但要靠一套没有公开 API 的
路径约定加文件格式才读得到；而挂起的审批必然在我们这边留下一条没有对应 `resolved` 的
`required`，同样确定，且是自己的数据。

第 3 步不只是给审批擦屁股：进程死在 turn 中间时，客户端手里最后一条状态永远停在
`busy`，没有任何东西会来纠正它。这条 `idle` 也让对账**幂等** —— 下次启动读到 `idle` 就跳过，
不会把用户已经翻篇的事再报一遍。

代价是要唤醒的线程数为零：对账只写 sqlite，不建 `EventLog`、不起 CLI 进程（V4：~123MB）。

`approval_lost` 因此是 `error.code` 的一个取值，与 `approval_timeout` 并列，
额外带一个 `approval_ids: [...]`，客户端据此正好收掉那几个已经按不动的按钮。

## 重连

```
GET /threads/{id}/events?after=evt_000117
```

服务端保留最近 N 条事件（内存环形缓冲 + sqlite 落盘）。客户端断线后带上最后见到的 `event_id` 重连，服务端补齐缺口再转入实时流。

这比"开流 → 拉历史 → 按 id 去重"简单，且**能保证不漏掉审批请求** —— 后者的失败模式是：断线期间某个 subagent 挂起等审批，重连后收不到该事件，thread 永久卡在 `awaiting_approval`。

### `/events/replay` —— 只要历史，不要进程

⚠️ **打开 SSE 流会唤醒线程**：端点内部走 `ThreadManager.runner()`，没活着就把它复活，
代价是一个 ~123MB 的 CLI 进程（V4）。对"我断线期间错过了什么"这个问题，这个价钱是错的。

```
GET /threads/{id}/events/replay?after=117
→ { "events": [{"type": …, "payload": {…}}, …], "last_event_id": 129, "status": "idle" }
```

纯 sqlite 读，线程保持冷的。客户端据 `status` 决定要不要再开真正的流跟下去。
客户端重启后对每个活跃 thread 逐个补历史，就靠这个端点；用 SSE 流做同样的事
会把所有 thread 一次性拉起来，直接顶满 `max_live_threads`。

## 与 IM 客户端的映射（非规范）

Telegram 参考实现的建议，不属于本接口的约定：

| 事件 | 渲染 |
|---|---|
| `text` | 正常消息 |
| `tool.call` (`render: none`) | 不发；计入折叠计数 |
| `tool.call` (`render: summary`) | 追加到状态消息 |
| `agent.spawn` | 新建状态消息 `🔍 repo-api · 运行中`，记 message_id |
| subagent 内事件 | 不发新消息，节流 edit 那条状态消息 |
| `agent.done` | edit 成 `✅ repo-api`，另发结果 |
| `approval.required` | inline keyboard，`callback_data` 带 `approval_id` |
| `diff` | 按仓库分别发；超长走 `sendDocument` |

注意 Telegram 的 `editMessageText` 约每聊天 1 次/秒，多 agent 并发时状态更新要合并成定时刷新，不能每事件一次 edit。forum 模式群组的 topics（`message_thread_id`）可直接对应 agent 树，是更省事的选择。

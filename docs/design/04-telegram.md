# Telegram 接入

> 状态：**设计定稿，实现中**。本文记录传输与协议形态的选择理由；
> 事件与端点的定义在 `02-sse-api.md`，不在此重复。

第一个真实客户端。`00-overview.md` 说 antares-agent「通过 SSE 接口对外，不与任何具体 IM 耦合」——
这份文档的全部工作就是把那句话兑现：**agent 侧不增加一行 Telegram 代码**。

## 部件

```
Telegram ──► alice (hk)          ──┐
             modules/agent.py      │  RabbitMQ (hk:5671, mTLS)
             持有全部 Telegram 状态 │  exchange `agent` (topic)
                                   │    agent.event  ──►  alice
                                   │    agent.cmd    ──►  relay
             agent-relay (rpi)   ──┘
             SSE ↔ AMQP，无业务逻辑
                    │ HTTP over unix socket
                    ▼
             antares-agent (rpi)   ← 不变
```

三个进程分布在两台机器上，因为 **rpi 在 NAT 后面、hk 有公网**。方向由此定死：
两端都只能向外连，中间必须有个双方都连得上的会合点。

## 决策

| # | 决策 | 理由 |
|---|---|---|
| **D10** | **传输复用现有 RabbitMQ**，不另起隧道 | broker 已在 hk 上跑（`0.0.0.0:5671`，`verify_peer` + `fail_if_no_peer_cert`），alice 与它同机（hk 上的 systemd **user** service），rpi 已经是它的 mTLS 客户端（`qq-relay.service`），证书都在位。反向隧道是纯增量基建换零收益 |
| **D11** | **总线上跑 antares-agent 自己的事件信封，不跑 Telegram RPC** | 见下节。这是本文唯一一个「和现有实现反着来」的决定 |
| **D12** | **relay 独立成进程，不把 aio_pika 塞进 agent** | relay 成为唯一客户端后，agent 可以只监听 unix socket、彻底不开 TCP 口。broker 证书也归 relay 的 uid，不落在 agent uid 上 —— F19 说了沙箱拦写不拦读 |
| **D13** | **relay 只在 thread busy 期间持有事件流** | 订阅本身会唤醒线程，见下面的实现约束 |
| **D14** | **chat ↔ thread 映射存 alice 侧**，不存 agent 侧 | 它是 Telegram 概念。agent 侧存了就等于承认有个叫 Telegram 的东西存在 |

### D11：为什么不照抄 hermes 的形态

已有的 `hermes-antares-bridge` 在总线上跑的是 **Telegram RPC**：
`{action: "send"/"edit"/"delete", chat_id, message_id, text, parse_mode}`。
agent 侧说「给 chat X 发这段字」「编辑消息 N」，bot 是个哑执行器。

代价集中在一处：bot 发完消息后必须把**真实的 `message_id` 用 `message_ack` 回传**，
agent 侧才能后续编辑它（`alice/modules/hermes.py:379-391`）。
这是**在异步总线上做同步往返** —— 要配 `correlation_id`、要处理回不来的情况、
要在 agent 侧维护一张「我发出去的消息现在是什么 id」的表。

改成事件形态之后，这一整套消失：**`message_id` 根本不过总线**。

| | hermes 形态 | 事件形态 |
|---|---|---|
| 总线载荷 | Telegram API 调用 | `02-sse-api.md` 的事件，原样 |
| `message_id` 归属 | 两边都要知道 | 只有 alice 知道 |
| 加一个 UI 花样 | 改两端 | 只改 alice |
| 4096 分片 / 编辑限速 | agent 侧要懂 | 状态在哪儿处理就在哪儿 |
| agent 侧的 Telegram 知识 | parse_mode、message_id、callback_data 上限 | 无 |

事件形态多出来的成本是 alice 侧要自己做折叠和节流。但 `02` 的 `render` 字段
（`none`/`summary`/`diff`）和 `agent` 归属块就是为此准备的 —— 客户端照着做，
不需要理解任何 Agent SDK 概念，连运行时新增的 MCP 工具都有默认策略。

## 总线

单个 topic exchange `agent`，两个 routing key —— 照 `tri_lug` 的写法，
不用 hermes 那种 exchange-per-producer。

| routing key | 方向 | 载荷 |
|---|---|---|
| `agent.event` | relay → alice | `{"type": "<事件名>", "payload": {…}}`，`payload` 即 SSE 的 `data` |
| `agent.cmd` | alice → relay | `{"op": …, …}` |

`agent.cmd` 的 `op` 与 HTTP 端点一一对应，没有第二套语义：

```jsonc
{"op": "new_thread", "chat_id": "…", "profile": "deep"}
{"op": "switch",    "thread_id": "thr_…", "chat_id": "…"}
{"op": "list",      "chat_id": "…"}
{"op": "message",   "thread_id": "thr_…", "text": "…"}
{"op": "approve",   "thread_id": "thr_…", "approval_id": "apr_…", "decision": "allow", "message": ""}
{"op": "interrupt", "thread_id": "thr_…"}
{"op": "mode",      "thread_id": "thr_…", "mode": "plan"}
{"op": "resume",    "thread_id": "thr_…", "after": 117}   // 补缺口，见「丢消息」
```

relay 对每个 `op` 做的事就是一次 HTTP 请求，失败则回一条
`relay.cmd_failed`，让 alice 有话可说。

relay 自己产生三种信封，与 agent 事件共用 `agent.event`，用 `relay.` 前缀区分：

| 信封 | 何时 |
|---|---|
| `relay.thread_bound` | `new_thread` 与 `switch` **共用** —— bot 两种情况下做的事一样 |
| `relay.thread_list` | `list` 的结果，`GET /v1/threads` 原样透传 |
| `relay.cmd_failed` | 任何 op 抛错，带 `op` / `chat_id` / `detail` |

`switch` 先 `GET /v1/threads/{id}` 再回 `thread_bound`，不直接信任 id ——
否则打错一个字符就把 chat 绑到不存在的 thread 上，之后每条命令各失败一次，
没有任何一条指向原因。这个端点不唤醒线程（`manager.status()` 只读活跃表）。

### 丢消息

hermes 那条队列是 `declare_queue("", exclusive=True)`（`hermes.py:643`）——
匿名、非持久，**bot 一重启，期间发布的消息全部丢弃**。
对 hermes 是掉几句话，对这里是掉整个 turn：agent 一次任务可能跑几分钟，
用户看到的是一片空白，且如果丢的是 `approval.required`，
thread 会永久停在 `awaiting_approval` 没人应答。

两层：

1. **队列改具名 + durable**，带 `x-message-ttl` 与 `x-max-length`。
   broker 替你存着，零协议代码，顺带覆盖 hk↔rpi 的网络抖动
2. **超出上述边界的缺口用 `resume`**：alice 持久化每个 thread 的 `last_event_id`，
   启动时对活跃 thread 发 `{"op": "resume", "after": N}`，relay 用 `?after=N` 补齐

第 2 层几乎是白送的 —— `?after=` 是 `02` 已经实现的机制，
且 `eventlog.py` 的 sqlite 落盘保证跨重启的事件 id 连续。

## 实现约束：订阅会唤醒线程

`api.py` 的事件流端点里是：

```python
runner = await mgr.runner(thread_id)   # api.py:195
```

`ThreadManager.runner()` **会拉起或复活线程**（`manager.py:81`），也就是说
**打开事件流本身就会 spawn 一个 CLI 进程**（V4 实测：首个 ~218MB PSS，增量 ~123MB）。

所以 relay **不能**给每个 chat 常驻一条流 —— 那样 `max_live_threads=6` 会被立刻顶满，
LRU 形同虚设，而且是在没人说话的时候占着。

正确节奏：

```
POST /messages  →  打开 ?after=<last>  →  跟流  →  见 thread.status:idle  →  关流，记住 last_event_id
```

安全性来自两条事实：**空闲线程按定义不产生事件**（没有 turn 在跑），
**审批只在 turn 内发生**，所以关流期间不会漏掉 `approval.required`。

注意 idle 的判据是 `thread.status` 事件而非 `turn.done` —— F25：
一个逻辑任务会产生多个 `ResultMessage`，后台 subagent 活过 turn。
`02` 已经把这条写进协议了，relay 照着读就行。

这条不需要改 agent 的代码，但**必须**写进 relay 的约定，
否则是个只在真实使用下才暴露的内存问题。

## alice 侧

`modules/agent.py`，一个 `TelegramBotModuleBase`。Hermes 模块在本分支第一个提交里已删除 ——
两者会抢同一个 plain-message handler，且都要为 agent 流量绑队列。

**映射**：存 `data/agent.db`（用 `antares_bot.sqlite`，`modules/gpt.py` 已经是这个用法），
主键是 **`(chat, thread)`**，每行带 `cursor` 与 `current` 标记。

主键不是 `chat` 单列，这一点是必须的：一个 chat 一行的话，`/switch` 走开的瞬间
就把原 thread 的 cursor 冲掉了，切回来会把它**整段历史重放**进聊天。
所以切走的 thread 连同 cursor 一起留着，只是不再是 `current`。

`/threads` 列出全部（数据来自 `GET /v1/threads` 而非 bot 自己记的，
所以别处建的 thread 也点得到），每条一个按钮，`callback_data = "ags:<thread_id>"`，
20 字节。`/switch <id>` 是打字的备用入口。

**切走的 thread 不会被静音。** 它可能还在跑，结果照样送进这个聊天 ——
凭空丢掉一段工作比多几条消息糟得多。区分靠状态消息带上 `⟨thr_…⟩` 前缀
（前缀只加在状态消息上：正文是带预计算 markdown entity 发的，
前置任何字符都会让 offset 整体错位）。

**已知限制**：两个 thread 同时输出正文时会交错，只有状态消息能区分来源。
forum topics 的 `message_thread_id` 才是这个问题的正解，但那是第二步。

**输出节奏**：不做逐 delta 流式编辑。文本累积，遇 `tool.call` / `turn.done` / 超长时 flush，
用现成的 `longtext_markdown_split`。另开一条状态消息，1 秒节流 edit，
显示当前工具与在跑的 subagent 数（`thread.status.background_agents`）。
`message_id` 在本地，编辑零往返 —— 这正是 D11 换来的。

**审批**：`callback_data = "ag:<thread_id>:<approval_id>:a|d"`，
约 32 字节，在 64 上限之内，不需要查表。
`approval_id` 做成 `apr_` + 6 位十六进制就是为了这一刻。

thread 被 LRU 淘汰后按钮才被点到，`POST /approve` 返回 409（`api.py:153`）。
这不是错误而是事实：那次审批随进程/淘汰一起没了。
按钮改成一行说明，提示重发消息即可重试 —— 与 `approval_lost` 的处置一致。

## 待确认

- 队列的 `x-message-ttl` / `x-max-length` 具体取值没有依据，先按 1 小时 / 5000 条起，跑一段时间再调
- rpi 那张 mTLS 证书对应的用户需要对 exchange `agent` 有 `configure` 权限
  （已确认可以 declare，但 `rabbitmq-definitions.age` 是加密的，实际权限位未亲眼核对）
- 媒体（图片/文档）暂不支持。hermes 那套 base64 过总线的做法能用，但先把文本路径跑通

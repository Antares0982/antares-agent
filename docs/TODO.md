# 待办

状态截至 2026-08-07：六层实现全部落地，95 个单测 + 2 个 live 测试通过，
已在 Pi 上作为 systemd 服务跑起来。以下按"不做会怎样"排序，不按工作量。

设计文档见 `design/00-overview.md`（决策 D1–D9）、`design/04-telegram.md`（D10–D14）
与 `design/03-verification.md`（实测 F1–F28）。下文凡引用 `Fnn` / `Dn` 均指这几份。

---

## ✅ 已完成（2026-08-07）

- **Telegram 对接** —— 设计见 `design/04-telegram.md`。三段：`relay.py`（本仓库）、
  `alice/modules/agent.py`（hk 上的 bot）、rpi 上的两个 unit。**代码写完，未跑过真机**
- **API 鉴权** —— 没写鉴权代码，改成监听 unix socket（`ANTARES_SOCKET`），
  授权交给 `RuntimeDirectoryMode=0750` 的目录。`actionrunner` / `ssrjsonrunner`
  那条路直接消失

---

## P0 —— 现在没有它就用不了

### 1. 端到端跑通 Telegram

代码齐了但一次真机都没跑过。要验的：

- broker 上 exchange `agent` 能不能用那张证书 declare（**权限位没亲眼看过**）
- socket 权限链：`agent-relay` 属于 group `agent` → 能穿过 0750 的
  `/run/antares-agent` → 能连 0666 的 socket
- D13 的节奏是否真的成立：一个 turn 结束后 relay 确实关流、thread 确实被 LRU 收掉
- 媒体（图片/文档）没做。hermes 那套 base64 过总线的办法能用

### 2. 模型网关（D4）

现状：token 从 agenix 经 `EnvironmentFile=` 进主进程环境，再由 `runner.py`
放进 `ClaudeAgentOptions.env` 传给 CLI。文件本身是 root 400、agent uid 读不到，
但**环境变量会一路继承到沙箱里的 Bash**（未实测，但沙箱不清 env，默认如此 ——
`env | grep ANTHROPIC` 应当就能看到；值得先花两分钟验一下）。

外传被沙箱的关网挡住（F22），所以现在是"可读不可外传"，不是"读不到"。
真正堵上要 D4 的网关：独立 unix 用户、localhost TCP、CLI 连得上而沙箱连不上。

顺带注意 F15：一旦走网关，`turn.done` 的 `cost_trusted` 永远是 `false`，
因为 usage 是按 Anthropic 价目表算的。这是正确行为，不是 bug。

---

## P1 —— 已经设计好、留了空位、没写

### 4. `POST /threads/{id}/fork`

`design/02-sse-api.md` 的端点表里有，`api.py` 里没有。
依赖 `fork_session`，待验的是分叉后 session 文件落在哪、事件该归属哪个 thread。

### 5. `POST /threads/{id}/undo`

同上。D8 已经在建 session 时开了 `enable_file_checkpointing=True`
（**这个选项只能在创建时设，无法对已有 session 追加** —— 所以它是从第一天就开着的，
不是等到写 `/undo` 才开），剩下的是 `rewind_files()` 的粒度与 `message_id` 语义。

### 6. `approval_lost` 的恢复路径没接线

`manager.py` 里 `ABORT_MARKER` 和 `report_approval_lost()` 都写好了，**没有任何地方调用**。

V1 实测过：进程带着挂起审批死掉时，CLI 会把待批工具合成为一次工具失败写进会话，
会话状态是一致的；但 `resume` 之后审批**不会自动重发**。所以恢复时要读末条
`tool_result`、判断是不是 `AbortError: Tool permission stream closed`，
据此推一条 `error/approval_lost` 事件告诉用户"发句话就能让它重试"。

不接线的后果：thread 看起来正常，用户等一个永远不会来的审批。

---

## P2 —— 边写边验，现在有真机可以验了

`design/03-verification.md` 的"待办"表里剩下的，加上实现期新欠的：

| 项 | 说明 |
|---|---|
| `.claude/settings.json` 读回 | **我已经先拦了，但"写进去确实生效"这一步是从 F8 推的，没实测。** 拦的代价是零所以先拦，可验证归验证 —— 万一不生效，这条 DENY 就是纯噪音 |
| D1 打断边界 | `interrupt()` 之后 drain 旧任务消息的具体边界，与 F25 的多 `ResultMessage` 语义叠加 |
| 并发审批 | 多个后台 subagent 同时挂起时 `can_use_tool` 是否重入。F25 已证实后台 agent 确实并行，场景是真的 |
| `set_permission_mode()` | 中途切换的生效范围（D7 说它不进模型请求、对 cache 零影响，这部分是确定的） |
| 负载内存曲线 | V4 量的是**空闲**基线（首个 218MB、增量 123MB PSS）。真实会话带上下文后的增长没量过，`max_live_threads=6` 是照空闲值推的 |
| F2 `dir:skill` | 跨仓库同名 skill 的限定语法。现在是建 thread 时直接报 400 拒绝，只有真出现重名才需要 |

---

## P3 —— 工程与运维

- **部署是手工的**：`git clone` + `uv sync`，没有脚本、没有回滚。
  升级 SDK 后要记得重启服务（bundled `claude` 路径由 launcher 脚本 glob 出来，
  版本号变了不用改 unit，但进程不会自己发现）
- **仓库还没有 remote**
- **`secrets/secrets.nix` 在 Nix 仓库里是 gitignore 的**，所以
  `"antares-agent-env.age".publicKeys = publicKeys;` 那一行只存在于本地，
  Pi 上的 checkout 需要手动补 —— 否则那台机器上 `agenix -e` 认不出这个 secret
- **sqlite 的 `events` 表无限增长**，没有清理与 vacuum。thread 是软删的，
  删掉的 thread 的事件也还在
- **没有"重扫工作区"的入口**：索引只在 `ThreadManager.create()` 时写。
  往 `workspace.toml` 加了仓库之后，不建新 thread 就不会刷新
- **`/v1/health` 不报告沙箱自检结果**。`preflight.run()` 在 lifespan 里跑，
  失败就起不来，所以"服务活着"隐含"自检过了" —— 但这是隐含的，值得显式暴露
- **pyright 的 `extraPaths = ["src"]` 没被 LSP 读到**，编辑器一路报
  import 找不到。运行时与测试不受影响，纯开发噪音

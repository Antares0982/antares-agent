# antares-agent — 架构与决策记录

> 状态：**定稿**。全部设计前置假设已实测，其中六条被推翻并已改设计（见 `03-verification.md` 头部）。
> 本文档记录**为什么这样设计**；schema 细节见 `01-workspace-manifest.md` 和 `02-sse-api.md`，
> 实测证据见 `03-verification.md`（决策里的 F<n> 编号指向该文档的条目）。

## 目标

一个部署在树莓派 5（aarch64-linux）上的常驻编码 agent：

- 有专属家目录和一组工具
- 能同时理解多个仓库，遵循各仓库的开发引导（AGENTS.md / CLAUDE.md）
- 能用上各仓库的 skill 和 MCP
- 通过 **SSE 接口**对外，不与任何具体 IM 耦合
- 聊天中能看到改动 diff，读类工具默认折叠
- 审批机制现代：危险命令能被识别，支持 plan / auto 这类模式

## 非目标

- 不做多租户、不做鉴权体系（单人使用，网关层解决身份）
- 不做模型无关抽象（锁定 Claude Agent SDK）
- 不自己实现 agent 循环

## 架构

```
┌─────────────┐   SSE + HTTP    ┌──────────────────────────┐
│ IM 适配器   │◄───────────────►│  antares-agent           │
│ (telegram   │  （unix socket） │  ├─ HTTP/SSE 层          │
│  经中继)    │                 │  ├─ thread 管理 (LRU)    │
└─────────────┘                 │  ├─ 审批仲裁             │
                                │  └─ ClaudeSDKClient × N  │
                                └───────────┬──────────────┘
                                            │ spawn
                                ┌───────────▼──────────────┐
                                │ claude (native binary)   │
                                │  ├─ orchestrator         │
                                │  ├─ repo-a agent         │  ← subagent
                                │  └─ repo-b agent         │
                                └───────────┬──────────────┘
                                            │ Bash 子进程受 bubblewrap 约束
                                            ▼
                                   ~/agent_work/ (cwd)

           ┌──────────────────────────────────────┐
           │ 模型网关（独立 systemd unit）        │
           │ 以另一个 unix 用户运行               │
           │ localhost TCP（F21：不支持 socket）  │
           └──────────────────────────────────────┘
```

## 目录布局

```
~/agent_work/                 ← 恒定 cwd
├── CLAUDE.md                 ← workspace 级引导
├── workspace.toml            ← manifest（见 01）
├── .agent/                   ← agent 的草稿区，不属于任何仓库
│   ├── findings-<repo>.md
│   └── contract.md
├── repo-a/                   ← 仓库直接放在 cwd 之下
│   ├── AGENTS.md
│   └── .claude/skills/
└── repo-b/
```

**仓库必须在 `~/agent_work/` 之下**，这一条同时解决三件事：

| 如果仓库在 cwd 之外 | 放在 cwd 之下 |
|---|---|
| skill 完全够不着，需要 `--add-dir` 语义（SDK 是否暴露未确认） | **懒发现**：碰到该子树即出现（实测 F1） |
| 沙箱默认只允许写 cwd，每个仓库都要进 `allowWrite` 白名单 | 默认覆盖 |
| CLAUDE.md 跨 add-dir 边界的层级加载行为未知 | 标准层级加载，同样懒加载（实测 F4） |

**skill 与 CLAUDE.md 都是懒加载的**，机制一致：agent 读了 `repo-b/` 下任一文件之后，
`repo-b/.claude/skills/` 里的 skill 才进入它的可用清单，`repo-b/CLAUDE.md` 才进入上下文。

这带来两个后果，都是好的：

- **不需要任何物化 / 注册步骤。** 仓库摆进去就行
- **工作范围天然成了作用域。** agent 不会看到它没碰过的仓库的 skill，
  所以不需要 per-repo `AgentDefinition` 去做隔离（`AgentDefinition.skills` 实测也不做过滤，见 F3）

唯一残留风险是跨仓库**同名** skill 会被静默遮蔽且无法限定（F2）。由索引生成器扫描时检出重名报错即可。

cwd 恒定还保证 session 文件路径 `~/.claude/projects/-home-<user>-agent-work/` 稳定，`resume` 永远找得到。

`.agent/` 放在 workspace 根而非任一仓库内，避免污染仓库的 git 状态。

## 锁定的决策

| # | 决策 | 理由 |
|---|---|---|
| **D1** | **消息排队为默认，显式指令才打断**；**且不使用 `receive_response()`** | 对齐 Claude Code UX。SDK 不允许 agent 工作时发新消息，必须在应用层排队。**实测修正（F25）**：`receive_response()` 在**第一个** `ResultMessage` 就返回，而后台 subagent 会活过该 turn 并由 SDK 自动续跑 —— 用它会把后续工作整段切掉。必须用 `receive_messages()`，并以 **`ResultMessage` 且 `background_tasks_changed.tasks` 为空**作为 thread 空闲的判据。`interrupt()` 后仍需 drain 完旧任务的消息再读新响应 |
| **D2** | **cwd 恒为 `~/agent_work/`，仓库置于其下** | 见上表 |
| **D3** | **沙箱 auto-allow，事件照推** | 沙箱边界（bubblewrap + seccomp + 网络 proxy）由内核强制，与模型意图无关。沙箱内命令静默执行不打扰；但仍推 SSE 事件，客户端自行折叠。逃出沙箱的才走审批。**两条实现约束**：① 放行必须在 `can_use_tool` 回调内部判定，**不能**写进 `allowed_tools`（F8：allow 规则排在回调之前，会静默短路掉回调）；② `can_use_tool` **必须提供**（F16：不提供时 CLI 走非交互兜底，会以误导性文案拒绝 cwd 子目录的写入） |
| **D4** | **凭据隔离靠独立 unix 用户的网关**，传输改为 **localhost TCP** | **F19：沙箱拦写不拦读**，cwd 之外的文件可被静默读取，所以任何"把密钥藏在某路径"的方案都无效，只有 OS 层用户隔离有效。**F21：`ANTHROPIC_BASE_URL` 不支持 unix socket**（四种写法全不通），故改用 localhost TCP。**F22 证实不对称仍成立**：CLI 自身的模型请求连得上，沙箱内的 Bash 连不上（静默阻断且不产生审批） |
| **D5** | ~~不显式禁 `git push`~~ → **改为显式 deny 规则兜底** | **实测推翻（F17）**：`permission_mode="auto"` 下，`git push --force` 与 `chmod -R 777` 均被静默放行、未升级到 `can_use_tool`；DeepSeek 与原生 Anthropic 行为一致。分类器的实际尺度比原假设宽得多，不能作为唯一防线。改为在 `deny` 规则里列出破坏性形态（force push / `remote set-url` / 递归 chmod 等），并保留沙箱网络边界作为第二层 |
| **D6** | **profile 在建 thread 时定死，不做运行时切换** | system prompt 在 cache prefix 里（渲染序 `tools`→`system`→`messages`），中途改会让 system + messages 两层缓存全失效。`/new deep` vs `/new quick` 比中途切换更清晰。**注意**：这条只约束 profile 本身，**不约束"这个 thread 能碰哪些仓库"** —— 仓库能力是懒发现的，不进 profile |
| **D9** | **不生成 per-repo `AgentDefinition`，仓库信息走索引文件** | 能力懒发现（F1）已提供天然作用域；`AgentDefinition.skills` 实测不做过滤（F3）；且 `agents` 冷绑定在建 thread 时（D6），预先声明仓库集合等于退化成单仓库工作。扇出直接用内置 `Explore` / `general-purpose`，靠 prompt 点名路径 |
| **D7** | **保留 `set_permission_mode()` 热切换** | 它不进模型请求，对 cache 零影响。对应 Claude Code 的 Shift+Tab。`/plan` → `/auto` 是日常主力路径 |
| **D8** | **`enable_file_checkpointing=True` 从一开始就开** | 该选项只能在 session 创建时设，**无法对已有 session 追加**。代价是磁盘，收益是 `rewind_files()` 支撑的 `/undo` |

### profile 的构成

profile = `system_prompt.append` + `allowed_tools` + `permission_mode` 初值 + `model` + `effort`。

按 D9，`agents` 不再由 manifest 生成，profile 只用内置 agent，所以 profile 与工作区内容**完全解耦** ——
换句话说，同一套 profile 对任意仓库组合都成立，加仓库不需要动 profile。

按 D6，`system_prompt.append` 冷（建 thread 时定），`permission_mode` 热（D7）。

预期至少两个 profile：

- `quick` —— 不扇出、`acceptEdits`、低 effort。日常小改动
- `deep` —— 编排纪律 + `plan` 起步 + 高 effort。跨仓库任务

两者的 append 都只放**方法**（怎么切分任务、交接用文件），不放**内容**（有哪些仓库）。
内容在 `.agent/workspace-index.md` 里，由 agent 按需读取（见 `01`）。

编排提示词是**独立的版本化文件**，不是代码里的字符串常量 —— 它会是迭代最频繁的资产。

### 编排纪律（写进 `deep` profile 的 append）

从任务的**上下文体积 × 耦合度**切分，而非按仓库边界切分：

| 阶段 | 体积 | 耦合 | 扇出 |
|---|---|---|---|
| 调研 | 极大 | 低 | ✅ 并行，每仓库一个 `Explore`，prompt 里点名路径 |
| 定契约 | 极小 | 极高 | ❌ 编排者自己做，不外包 |
| 修改 | 中 | 取决于契约是否已冻结 | 契约成文后才可并行 |
| 验证 | 中 | 高 | ✅ 用未参与修改的新鲜上下文 |

**交接用文件，不用散文**：调研 agent 写 `.agent/findings-<repo>.md`，编排者综合成 `.agent/contract.md`，修改 agent 只需被指向该文件的相应章节。理由：subagent **只能拿到 Agent 工具的 prompt 字符串**，拿不到父的对话历史；契约存在一份文件里比复述两遍不容易漂移，也少付一次 output token。`contract.md` 同时是天然的人工审查点。

## 部署

- **运行时**：uv + venv，非 Nix。flake 只用于开发环境。代码放在 `~/app`（**只读 bind 进 unit**），
  不放 workspace 之下 —— 否则 agent 能改写决定 agent 权限的那份代码
- **三个 systemd unit**：
  - `antares-agent.service` —— 主进程，监听 `/run/antares-agent/api.sock`
  - `antares-agent-relay.service` —— SSE↔AMQP 中继，独立用户 `agent-relay`（见 `04-telegram.md` D12）
  - `antares-gateway.service` —— 模型网关，独立用户，监听 localhost TCP（D4）**（未实现）**
- **API 走 unix socket，不开 TCP 口**。这个 API 自己没有鉴权，而这台机器上
  `actionrunner` / `ssrjsonrunner` 都能开回环连接。换成 socket 之后授权交给文件系统，
  一行鉴权代码都不用写。注意 **uvicorn 会把 socket chmod 成 0666**，
  真正拦人的是 `RuntimeDirectoryMode=0750` 的目录 —— 只有 group `agent`（即中继）能穿过去
- **跑在自己的 unix 用户 `agent` 下，不是 `antares`**（写实现时改的）。F19 是"沙箱拦写不拦读"，
  于是服务用户能读的任何文件 agent 都能读；deny 规则是 CLI 内部的软护栏，只有 uid 是内核强制的。
  配套用 `ProtectHome=tmpfs` + `BindPaths=` 只放回 workspace 与 `~/.claude`，
  其余 home（`/home/antares` 等）在该 unit 的 mount namespace 里根本不存在
- **db 与 profiles 放 `StateDirectory=`，不放 workspace** —— cwd 之下的一切 agent 都可写，
  它自己的事件日志与 thread 存储不该在其中
- `HOME` 必须在 unit 里显式设置（session 存储依赖它）；如需重定位可用 `CLAUDE_CONFIG_DIR`
- **`KillMode` 保持默认的 `control-group`**。实测 F1 发现：父进程被 kill 后，孤儿 `claude` 子进程**仍会继续发模型请求**。按 cgroup 整体收割才不会每次重启攒下一批孤儿烧配额

### claude 二进制从哪来（实测 F9）

PyPI wheel **自带** ~286MB 的 `claude`，且 `_find_cli()` 解析时**优先于 `PATH`**。
目标机是 **aarch64 NixOS**，所以 bundled 的 generic-linux ELF 开箱跑不起来。实测两条路（`03` F13）：

| 路线 | 得到的 CLI 版本 | 评价 |
|---|---|---|
| `nix build nixpkgs#claude-code` | **2.1.81** | ❌ 落后约 140 版；aarch64 无缓存，本地构建 ~15 分钟 |
| wheel 自带 + `patchelf --set-interpreter` | **2.1.222** | ✅ 与 SDK 版本天然配对 |

**走 patchelf 路线**，并且用 `programs.nix-ld` 代替手工 patch —— 后者写死的 glibc store path
会在系统升级后被 GC，且每次升级 SDK 都要重打。

无论哪种，**显式钉 `cli_path`** —— 让"用哪个 CLI"是配置决定，而不是解析顺序的副作用。

### ⚠️ 沙箱 fail-open：启动自检是必需的

实测 F23：`bwrap` 或 `socat` 任一不在 `PATH` 上时，**沙箱整个静默不启用**，
只打印一行警告，不抛异常 —— `sandbox.enabled=True` 照样设着，但一切约束都没生效。

因此：unit 的 `PATH` 必须同时含 `bwrap` 与 `socat`，**且进程启动时必须自检** ——
跑一条已知应被沙箱拒绝的命令（如向 cwd 之外写文件），确认它确实被拒，否则拒绝启动。
这是本设计里唯一一个"配置看起来完全正确但安全边界不存在"的失效模式。

### systemd 硬化 × bubblewrap（已在 Pi 上实测，见 `03` F12）

结论：**只有 `SystemCallFilter=` 与 `RestrictNamespaces=` 需要特别写法，其余硬化项全部无害。**
`NoNewPrivileges=yes` 与 `PrivateUsers=yes` 都不影响 bwrap（先前的担心是错的）。

以下组合实测通过：

```ini
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes
KillMode=control-group                              # 默认值，别改（见下）
RestrictNamespaces=user mnt pid net ipc uts cgroup  # 必须列全，漏 mnt 即失败
SystemCallFilter=@system-service @mount             # 缺 @mount 会被 SIGSYS 杀掉
```

F12 测的是 bwrap，不是 CLI 自己。补测（2026-08-06，x86_64，`claude` 2.1.222）：
`systemd-run -p MemoryDenyWriteExecute=yes claude --version` **rc=0 且正常打印版本** ——
Bun 的 JIT 不因 W^X 禁令而挂。同一命令对照组 `false` 返回 rc=1，确认退出码确实透传。

两个坑：`RestrictNamespaces=user` 看着像"放行 user"，实际是"**只**放行 user"，
bwrap `--unshare-all` 用到的其余 namespace 全被挡；`SystemCallFilter=@system-service`
不含 `mount` / `pivot_root` / `umount2`。

`kernel.apparmor_restrict_unprivileged_userns` 在目标机（NixOS）上不存在，该顾虑不适用。

### 重启语义（V1 已实测，见 `03-verification.md`）

进程带着挂起审批死掉时，CLI 会把待批工具**合成为一次工具失败**写进会话，turn 正常收尾，**会话状态一致**。
`resume` 之后审批**不会自动重发**，但发一句 nudge 即可让 agent 重试。

因此：

- **不需要**把 pending approval 持久化到 sqlite
- **需要**在恢复时识别末条 `tool_result` 是否为 `AbortError: Tool permission stream closed`，
  据此向用户说明中断原因并提供一键重试
- `Restart=on-failure` 可以放心使用

## 验证状态

设计前置假设已全部实测，明细见 `03-verification.md`（F1–F26）。

| # | 项 | 影响 | 结论 |
|---|---|---|---|
| **V1** | 审批 Future 在进程重启后的行为 | 决定 pending approval 是否需持久化 | ✅ 不需持久化，需重试入口 |
| **V2** | 仓库置于 cwd 之下时，skill 发现 / CLAUDE.md 层级 | 验证 D2 的前提 | ✅ 两者均懒加载，主 agent 与 subagent 皆然，无需物化步骤 |
| **V3** | Pi 上跑通：arm64 二进制 + bubblewrap userns + systemd 硬化 | 部署可行性 | ✅ 硬化组合与 CLI 路线均已定 |
| **V4** | 并发 thread 的内存占用，确定 LRU 上限 | 资源规划 | ✅ 首个 218MB，增量 123MB → LRU 取 6–8。**限制 thread 数还不够**：跑过一轮的 CLI 空闲时也要烧掉一个核（F31），因此另有 `ANTARES_IDLE_TTL`（默认 300s）按空闲时长淘汰 |
| **V5** | 扇出是否真的发生、索引 helper 是否够用 | 验证 D9 | ✅ `deepseek-v4-pro` 正确编排；连带发现 F25 并改写 D1 |

余下条目**只影响实现细节、不影响上述决策**，边写边验：D1 的打断/drain 边界（与 F25 的多个
`ResultMessage` 交互）、多个后台 subagent 同时挂起时 `can_use_tool` 是否可重入、
`rewind_files()` 的粒度与 `message_id` 语义、`set_permission_mode()` 中途生效范围、
`fork_session`、以及带历史 session 的内存曲线。

## 参考

- [Agent SDK — Subagents](https://code.claude.com/docs/en/agent-sdk/subagents)
- [Agent SDK — Sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
- [Agent SDK — Permissions](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Agent SDK — Modifying system prompts](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts)
- [Configure the sandboxed Bash tool](https://code.claude.com/docs/en/sandboxing)
- [A harness for every task: dynamic workflows in Claude Code](https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code)

# 实测记录

> 日期：2026-08-05 ~ 08-06
> 开发机：NixOS x86_64，SDK 0.2.130 / CLI 2.1.217。
> 目标机：`rpi5`，aarch64 NixOS 25.11，systemd 258，CLI 2.1.222（patchelf 后）。
> 模型：`claude-sonnet-5`（原生）与 `deepseek-v4-flash` / `deepseek-v4-pro`（兼容端点）对照。
> 探针脚本在 scratchpad，非仓库产物。本文只记录**结论与它改变了什么**。

**推翻了先前假设的条目**（均已回改 `00`–`02`）：

| | 原假设 | 实测 |
|---|---|---|
| F1 | 嵌套 skill 不会被发现，需软链物化 | 懒发现，物化方案取消 |
| F9 | SDK 不 vendor 二进制 | PyPI wheel 自带 286MB 且优先于 PATH |
| F12 | `PrivateUsers=yes` 会冲突 bwrap | 无影响 |
| F17 | auto 分类器已拦 force push | 未拦，D5 返工为显式 deny |
| F21 | 网关可走 unix socket | 不支持，改 localhost TCP |
| F25 | `ResultMessage` == 任务完成 | 后台 agent 活过 turn，D1 返工 |
| F28 | CLI 的路径 deny 足以护住凭据 | 中段 `..` 可绕过，必须自己归一化 |

---

## V1 — 挂起审批遇上进程崩溃 ✅ 已结论

设计里唯一"可能反过来改设计"的未知项。

**方法**：固定 `session_id`，`permission_mode="default"`，让 agent 跑一条需要审批的 Bash。
在 `can_use_tool` 进入的瞬间对自己发 `SIGKILL`，模拟人还在决策时进程死掉。然后另起进程 `resume` 同一 session。

**结果**：

1. 孤儿 CLI 察觉审批流断开，把待批工具**合成为一次工具失败**写进会话：
   ```
   Tool permission request failed:
   AbortError: Tool permission stream closed before response received
   ```
2. 模型收到该失败，产出一段"没拿到授权"的文本，turn 正常收尾
3. 会话文件**状态一致** —— 没有悬空的 `tool_use`
4. `resume` 后静候 15s，**审批不会自动重发**
5. 发一句 "Continue with what you were doing." → agent 重试，`can_use_tool` 正常触发，命令执行成功

**对设计的影响**：

| 原假设 | 实测 | 处置 |
|---|---|---|
| 挂起审批可能永久丢失、需持久化到 sqlite | 不会丢成不一致状态，会被转成工具失败 | **不需要 sqlite 持久化 pending approval** |
| — | 但审批也不会自动重发 | 应用层需识别"因审批流断裂而结束的 turn"并重新推动 |

崩溃恢复因此简化为：重启后 `resume`，扫最后一条 `tool_result` 是否为该 `AbortError`；是则向用户报告中断原因，并允许一键 nudge 重试。**不需要自建审批状态机。**

### ⚠️ 附带发现：孤儿 CLI 会继续烧 token

kill 发生在 `15:21:57`，而会话里 `15:21:59` 还有一条新的 assistant 文本 —— **父进程死后，`claude` 子进程仍在发模型请求**。

systemd 必须按 cgroup 整体收割（`KillMode=control-group`，systemd 默认值；**不要**改成 `process`）。否则每次重启都会留下一批孤儿继续消耗配额。

---

## V2 — 仓库置于 cwd 之下 ⚠️ 部分推翻

**方法**：搭一个 workspace，根与两个子仓库各放 `CLAUDE.md`（内含唯一 token）与 `.claude/skills/`，其中根与 repo-a 故意放同名 skill `shared-name`。

### F1 ✅ 嵌套 skill 是**懒发现**的（此前误判，已更正）

**先前的错误结论**：我依据 `system:init` 的 `skills` 数组里没有 `probe-alpha` / `probe-beta`，
断定嵌套 skill "不会被发现"，并据此设计了"建 thread 时软链物化"的方案。**这是误读。**

`system:init` 的 `skills` 只反映**起手 eager 加载**的那批（cwd 级 + 用户级）。
模型实际能用的 Skill 清单会随它进入子树而增长。分步实测：

| 步骤 | 模型自报的 skill 清单 |
|---|---|
| 起手（未碰 repo-b） | `... shared-name, dataviz, ...` —— **无** `probe-beta` |
| `Read repo-b/src/b.txt` | — |
| 之后再问 | `... , probe-beta` —— 出现了，模型自述 "discovered after reading a file under repo-b/" |
| `Skill(skill="probe-beta")` | `BETA-SKILL-44444` ✅ |

这解释了先前两次观测的矛盾：probe_v2b 里 `Skill(probe-alpha)` 成功，是因为它**先读了** `repo-a/src/a.txt`；
probe_v2c 里 subagent 报告"没有 probe-alpha"，是因为它没碰过 repo-a。

**与 F4 的 CLAUDE.md 懒加载是同一套机制。** 仓库放在 cwd 之下就够了，无需任何物化步骤。

**处置**：**取消软链物化设计。** 它只是把本来就会到达的东西提前灌入，反而丢掉上下文经济性
（每个 thread 都为没碰过的仓库付 skill 描述的 token）。

### F2 ⚠️ 同名 skill 会被静默遮蔽，且无法限定

根与 repo-a 都有 `shared-name` 时，`Skill(skill="shared-name")` 返回 `SHARED-ROOT-11111` —— 根版本胜出，repo-a 版本**被静默遮蔽**。

试过三种限定写法，均无法够到 repo-a 那份：

| 写法 | 结果 |
|---|---|
| `shared-name` | ✅ 但解析到根版本 |
| `repo-a/.claude/skills/shared-name` | ❌ `Unknown skill` |
| `repo-a:shared-name` | ❌ `Unknown skill` |

（Skill 工具文档提到存在 `dir:skill` 的目录限定语法，但只对**已被列出**的目录级 skill 生效；
本次未能构造出该形态，留作开放项。）

**处置**：这是 F1 取消物化后**唯一残留的真实风险**，但它很窄 —— 只在两个仓库定义同名 skill
且都被碰到时才发生。索引生成器扫描时检出重名并报错即可，成本是几行代码，不需要为它引入前缀命名体系。

### F3 ❌ `AgentDefinition.skills` 不是作用域机制

给 repo-a 的 agent 设 `skills=["repo-a-probe-alpha"]`，该 subagent 依然看得到 `repo-b-probe-beta` 和全部工作区 skill。这个字段**不做过滤**。

**处置**：仓库级 skill 隔离在 SDK 层做不到。但配合 F1 的懒发现，这基本不再是问题 ——
agent 只有进入某仓库子树后才会看到它的 skill，**工作范围天然成了作用域**，
不需要显式隔离机制。`AgentDefinition.skills` 字段整个不用。

### F3b ✅ 懒发现在 subagent 内同样成立（D9 的承重实测）

用**内置** `general-purpose` 派一个 subagent —— 无自定义 `AgentDefinition`、无 manifest、无软链 ——
只在 Agent 工具的 prompt 里点名 `repo-b/src/b.txt`：

- 读完后 `REPO_B_TOKEN = BETA-3f108` 出现，subagent 自述
  "auto-loaded from `repo-b/CLAUDE.md` when I read a file under `repo-b/`"
- `Skill(skill="probe-beta")` → `BETA-SKILL-44444`，自述 "loaded from `repo-b/.claude/skills/probe-beta`"

**编排者只需在 prompt 里点名路径，目标仓库的 skill 与 guide 自动到位。**
这是 D9（不生成 per-repo `AgentDefinition`）成立的前提，已确认。

### F4 ✅ CLAUDE.md 层级按预期工作，且是懒加载

- 起手只有根 `CLAUDE.md` 进上下文（`REPO_A_TOKEN` / `REPO_B_TOKEN` 均为 MISSING）
- `Read repo-a/src/a.txt` 之后，`REPO_A_TOKEN = ALPHA-9de22` 出现

懒加载比 eager 更好：编排者不必为没碰过的仓库付上下文。D2 成立。

### F5 ✅ 派发 subagent 的工具真名是 `Agent`

`system:init` 的 `tools` 里写的是 `"Task"`，但实际 tool_use block 的 `name` 是 `"Agent"`。
`02-sse-api.md` 的 render 策略表键 `Agent` 是对的，无需改。

工具结果尾部确实带 `agentId: <id> (use SendMessage with to: '...')`，`agent.done.agent_id` 事件设计成立。

---

## F8 ⚠️ `allowed_tools` 会静默短路 `can_use_tool`

不属于任何一条 V 项，是做 V1 时踩到的。

把 Bash 写进 `allowed_tools` 后，`can_use_tool` **一次都不触发**，命令直接执行。符合文档的求值顺序（hooks → deny → ask → mode → **allow** → `canUseTool`），但很容易误用。SDK 会发 `CanUseToolShadowedWarning`，不留意就漏掉了。

**对 D3 的约束**：沙箱 auto-allow 要"静默执行但仍推 SSE 事件"，**不能用 `allowed_tools` 实现** —— 那样应用层根本收不到回调，事件无从推起。正确做法是让 `can_use_tool` 照常触发、由回调内部判定沙箱内即刻放行，事件在回调里推出。

---

## F9 ⚠️ PyPI wheel 自带 289MB 的 claude 二进制，且优先于 PATH

我先前依据 nixpkgs 包只有 1.2MB 断定"SDK 不 vendor 二进制"。**该结论只对 nixpkgs 成立，对 PyPI wheel 不成立** —— 而部署方案用的正是 uv + PyPI。

实测：

| 项 | 值 |
|---|---|
| `claude_agent_sdk/_bundled/claude` | 289 MB，ELF x86-64，动态链接 generic Linux |
| 解析顺序 | `_find_cli()` **先返回 bundled**（`subprocess_cli.py:250`），再查 `shutil.which("claude")`（`:256`） |
| aarch64 wheel | `claude_agent_sdk-0.2.130-py3-none-manylinux_2_17_aarch64.whl`，88 MB 下载 |
| sdist | `.tar.gz` 仅 0.3 MB，**不含**二进制 |

**结论按树莓派实际发行版分叉**：

- **Raspberry Pi OS / Debian 系**：aarch64 wheel 开箱即用，无需装 `claude`。代价是 venv ~300MB，且 CLI 版本被 SDK 版本锁定
- **NixOS**：bundled 二进制跑不起来（`NixOS cannot run dynamically linked executables intended for generic`），且它优先于 PATH，所以**必须显式传 `cli_path=`** 指向 nixpkgs 的 `claude`。开发机上本文所有探针都是这么跑的

我上一轮"patchelf 的活儿 nixpkgs 已经替你干了、那个担心是多余的"说早了 —— 那个担心对 uv 路径是成立的。

---

## V3 — 树莓派部署 ✅ 已跑通

> 实测环境：`rpi5`，**aarch64 NixOS**（25.11，kernel 6.12.87，systemd 258），8GB RAM / 4 核。
> 目标发行版是 NixOS 而非 Debian，所以 F9 里的 NixOS 分支是必然路径，不是备选。

### F11 ✅ userns 可用，AppArmor 顾虑不存在

```
user.max_user_namespaces = 31875          → 非特权 userns 开启
kernel.apparmor_restrict_unprivileged_userns → 不存在（NixOS 默认不启用 AppArmor）
bwrap --unshare-all ...                    → BWRAP-OK
```

`00-overview.md` 里那条"Debian 系树莓派 OS 需留意 AppArmor"的提示对本机不适用。

### F12 ⚠️ systemd 硬化矩阵：只有 `SystemCallFilter` 真冲突，加 `@mount` 即可

| 指令 | bwrap |
|---|---|
| （无硬化） | PASS |
| `NoNewPrivileges=yes` | **PASS** |
| `PrivateUsers=yes` | **PASS** |
| `PrivateTmp=yes` / `ProtectSystem=strict` / `ProtectHome=read-only` | PASS |
| `RestrictSUIDSGID=yes` / `MemoryDenyWriteExecute=yes` | PASS |
| `RestrictNamespaces=yes`（全禁） | FAIL |
| `RestrictNamespaces=user`（**只**放行 user） | FAIL |
| `RestrictNamespaces=~user` | FAIL |
| `RestrictNamespaces=user mnt pid net ipc uts cgroup` | **PASS** |
| `SystemCallFilter=@system-service` | **FAIL**（rc=159 = 128+31 = SIGSYS） |
| `SystemCallFilter=@system-service @mount` | **PASS** |

**两条先前的警告被推翻**：`NoNewPrivileges=yes` 与 `PrivateUsers=yes` 都不影响 bwrap。
先前写的"`PrivateUsers=yes` 嵌套 userns，很可能冲突"是错的。

`RestrictNamespaces=` 的真实要求不只是"别排除 user"，而是**要把 bwrap `--unshare-all` 用到的
全部 namespace 都列进白名单**；漏掉 `mnt` 就失败。

`SystemCallFilter=` 缺的正是 `@mount`（`mount` / `pivot_root` / `umount2`）。

**完整硬化组合实测 PASS**：

```ini
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes
RestrictNamespaces=user mnt pid net ipc uts cgroup
SystemCallFilter=@system-service @mount
```

### F13 ⚠️ claude 二进制：nixpkgs 给的是 2.1.81，patchelf 给的是 2.1.222

两条路都跑通了，但版本差距很大：

| 路线 | 版本 | 代价 |
|---|---|---|
| `nix build nixpkgs#claude-code`（需 `NIXPKGS_ALLOW_UNFREE=1 --impure`） | **2.1.81** | aarch64 不在缓存，本地构建约 15 分钟（拉 npm tarball 再打包） |
| PyPI wheel 自带 + `patchelf --set-interpreter` | **2.1.222** | venv 304MB；每次 `uv pip install` 后需重新 patch |

2.1.81 落后约 140 个版本，本设计依赖的若干行为（后台 subagent、`SendMessage`、`Agent` 工具名）
在该版本上未必存在 —— **不建议走 nixpkgs 路线**。

patchelf 操作（注意 uv 会把 wheel 内容硬链到缓存，必须先 `cp` 再 patch，否则污染缓存）：

```bash
cp --remove-destination .venv/lib/python3.13/site-packages/claude_agent_sdk/_bundled/claude ./claude-patched
patchelf --set-interpreter /nix/store/<glibc>/lib/ld-linux-aarch64.so.1 ./claude-patched
./claude-patched --version   # → 2.1.222
```

⚠️ 隐患：写死的 glibc store path 会在系统升级后被 GC 掉，届时二进制失效。
两个更稳的做法，择一：① 用 `programs.nix-ld` 让通用动态链接二进制直接可跑，免 patch；
② 把该 glibc 固定成 gcroot。**推荐 ①**，它同时免掉"每次升级 SDK 都要重新 patch"的维护负担。

---

## V4 — 并发内存 ✅ 已量

只 `connect()` 不发 query（无模型调用、零成本）测得的**空闲基线**：

| 并发 | host python PSS | 每 CLI PSS | CLI 合计 PSS |
|---|---|---|---|
| 1 | 47.4 MB | 218.1 MB | 218.1 MB |
| 4 | 47.6 MB | 146.8 MB | 587.4 MB |

RSS 恒为 ~218MB/进程，但 286MB 二进制的文本段在进程间共享，**PSS 才是真实成本**：

- 第一个 thread：约 **218 MB**
- 之后每增一个：约 **123 MB**（`(587.4−218.1)/3`）
- host 进程固定 ~48 MB

Pi 总内存 8GB，实测可用约 4.1GB（其余被既有服务占用）。若给 thread 池预算 1.5GB，
理论可容纳约 11 个空闲 thread。但这是**空闲值**，真实会话带上下文后会显著增长。

**建议 LRU 上限取 6–8**，留足余量给实际推理与 Bash 子进程。

---

## DeepSeek 兼容端点 ✅ 可用

配置（`ClaudeAgentOptions`）：

```python
model = ("deepseek-v4-flash",)
env = (
    {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": KEY,
        "ANTHROPIC_API_KEY": KEY,
    },
)
```

### F14 ✅ 主要能力全部可用

`init` 报 `model=deepseek-v4-flash`、`apiKeySource=ANTHROPIC_API_KEY`。实测通过：
工具调用、`can_use_tool` 触发、Bash 写入、CLAUDE.md 懒加载、skill 清单。
`usage` 里 `cache_read_input_tokens` 非零，说明 DeepSeek 侧自有缓存在生效。

### F15 ⚠️ `total_cost_usd` 在第三方端点下不可信

一次 trivial turn 报 `cost=0.149` —— CLI 是拿 Anthropic 价目表套 DeepSeek 的 token 数算的。

**对 `02-sse-api.md` 的影响**：`turn.done.cost_usd` 在非 Anthropic base URL 下必须标注为不可信，
或干脆不推。建议改推原始 `usage`（token 数），由客户端按实际 provider 价目自行折算。

### F16 ⚠️ 不传 `can_use_tool` 会触发一个误导性的兜底拒绝

这是我自己踩的坑，但它是真实的部署陷阱，值得记：

`ClaudeAgentOptions` 里**没有**提供 `can_use_tool` 时，`permission_mode="default"` 下
CLI 走非交互兜底策略，写入 cwd **子目录**被拒，报错文案却是：

```
Output redirection to '<cwd>/repo-a/x.txt' was blocked. For security, Claude Code may
only write to files in the allowed working directories for this session: '<cwd>'
```

—— 目标明明在所列目录之下，读起来像沙箱 bug。接上 `can_use_tool` 后，
Bash 写 cwd 顶层、Bash 写子目录、Write 工具写子目录**三者全部成功**，回调也都正常触发。

**D2 成立**：cwd 之下任意深度均可写。**但 `can_use_tool` 是必需项，不是可选增强。**

### F17 ❌ auto 模式没有拦下 force push 与 `chmod -R 777` —— D5 需返工

`permissionMode = auto` 已确认生效（init 回显）。在此前提下：

| 命令 | DeepSeek | 原生 Anthropic |
|---|---|---|
| `curl -s -o /dev/null https://example.com` | 静默放行 | 静默放行 |
| `cd repo-a && git push --force origin main` | **静默放行** | **静默放行** |
| `chmod -R 777 repo-a/src` | **静默放行** | **静默放行** |

两个 provider 行为一致，所以这不是 DeepSeek 的缺陷，而是**分类器的实际尺度比 D5 假设的宽**。

D5 原文写"auto mode 分类器默认已拦 force push、推送 secret、`git remote set-url`、
向外部仓库开 PR"，**该前提未被实测支持**。

> 注：force push 因 repo-a 无 remote 而以 128 退出，但分类器是在执行前判定的，
> 退出码不影响该结论。

**处置**：D5 改为显式 deny 规则兜底，不再依赖分类器。见 `00-overview.md`。

---

## 沙箱端到端（Pi，aarch64 NixOS）✅ 跑通

配置：`sandbox={"enabled": True, "autoAllowBashIfSandboxed": True,
"network": {"allowedDomains": ["api.deepseek.com"]}}`，模型走 DeepSeek。

前提：**`bwrap` 必须在 CLI 进程的 `PATH` 上**。Pi 上它只在 nix store 里，
不显式加进 `PATH` 就建不起沙箱（systemd unit 里要注意）。

### F18 ✅ 边界由内核强制，逃逸走审批 —— D3 成立

| 场景 | 结果 | 是否升级审批 |
|---|---|---|
| 写 cwd 内 | 成功 | ❌ 静默（`autoAllowBashIfSandboxed` 生效） |
| 写 cwd 外 | `(eval):1: read-only file system: /home/antares/sb_outside_probe.txt` | ❌ 沙箱直接挡 |
| ↳ 模型改用 `dangerouslyDisableSandbox: true` 重试 | 被拒 | ✅ **升级为 `can_use_tool(Bash)`** |
| 网络出网（未在 `allowedDomains`） | `curl` 返回 `000` | ✅ **升级为 `can_use_tool(SandboxNetworkAccess)`** |

**这正是 D3 想要的形状**：沙箱内静默、逃逸需审批，且"逃逸"是模型主动提 `dangerouslyDisableSandbox`
时才发生的显式动作 —— 审批点语义清晰。地面真相核对：`ws/sb_in.txt` 存在，`~/sb_outside_probe.txt` 始终不存在。

**对 `02-sse-api.md` 的影响**：`approval.required.tool` 会出现 **`SandboxNetworkAccess`** 这个
伪工具名（不在 `tools` 列表里）。客户端应把它渲染成域名级授权，而非普通命令审批。
网络被拒后模型往往会再试一次 `dangerouslyDisableSandbox`，于是**同一意图产生两次审批**，
客户端需要能合并展示，否则用户会看到重复弹窗。

### F19 ❌ 沙箱拦写不拦读 —— 凭据必须靠 OS 隔离

```
cat /home/antares/.fakecreds/token.txt   →  SECRET-GATEWAY-TOKEN-9xyz   （静默放行，零审批）
```

cwd 之外的**读取完全不受限**。此前 `cat /etc/shadow` 的 "Permission denied" 是普通 unix 权限位挡的，
与沙箱无关。

**两个直接结论**：

1. **D4 被强化而非削弱** —— 网关凭据放在独立 unix 用户下是**必需**的。
   任何"把密钥放在 agent 读不到的路径"的想法都不成立，因为沙箱不限制读
2. `01-workspace-manifest.md` 里"是否需要声明敏感路径"那条**待定项应转为必做**：
   `~/.ssh`、`~/.aws`、`~/.config/gh` 等必须进 `sandbox.ignoreViolations` 之外的显式 deny 规则

### F20 ℹ️ `SandboxNetworkConfig` 提供了 D4 需要的 unix socket 开关

```
allowUnixSockets: list[str]      allowAllUnixSockets: bool
allowedDomains / deniedDomains / allowManagedDomainsOnly
httpProxyPort / socksProxyPort   allowLocalBinding
```

D4 原本假设"seccomp 阻断 unix socket"是个副作用；实际上这是**一等配置项**，
可以精确地只放行网关那一个 socket 路径，比依赖副作用可靠。

---

## 定稿前的第二轮验证

### F21 ❌ `ANTHROPIC_BASE_URL` 不支持 unix socket —— D4 换传输

起一个真实的 unix socket HTTP 服务（直连 sanity 命中），四种 URL 写法全部无法让 CLI 连上：

| 写法 | 结果 |
|---|---|
| `unix:///tmp/agw.sock` | 无连接，CLI 持续重试至超时 |
| `unix://localhost/tmp/agw.sock` | 同上 |
| `http+unix://%2Ftmp%2Fagw.sock` | 同上 |
| `http://localhost/tmp/agw.sock` | 同上 |

**D4 的 unix socket 方案不成立**，网关必须是 localhost TCP 监听。

### F22 ✅ localhost TCP 的隔离不对称成立 —— D4 内核不变

| 主体 | 能否连 `127.0.0.1:PORT` |
|---|---|
| CLI 自身的模型请求（不受沙箱约束） | ✅ 收到 `POST /v1/messages?beta=true` |
| 沙箱内的 Bash（`curl 127.0.0.1:PORT`） | ❌ 静默阻断，网关零请求，**且不产生审批** |

D4 想要的不对称在换成 TCP 后依然成立。凭据本身仍由独立 unix 用户的文件权限保护
（F19 里 `/etc/shadow` 被普通权限位挡住即为佐证）。

### F23 ⚠️ 沙箱依赖缺失时**静默失效**（fail-open）

只把 `bwrap` 加进 `PATH`、漏掉 `socat` 时，沙箱**整个不启用**，只打印一行：

```
Commands will run WITHOUT sandboxing. Network and filesystem restrictions will NOT be enforced.
```

**这是最危险的部署陷阱**：配置看起来完全正确，`sandbox.enabled=True` 也设了，
但所有约束都没生效，且不抛异常。

**处置**：systemd unit 里 `bwrap` 与 `socat` 都必须在 `PATH` 上；
且应用启动时**主动自检** —— 跑一条已知会被沙箱拒绝的命令，确认它确实被拒，否则拒绝启动。

### F24 ✅ deny 规则会分解 shell 结构，不是朴素前缀匹配 —— D5 返工方案可靠

`disallowed_tools=["Bash(chmod -R:*)"]` 对以下形态**全部拦截**，且均**未咨询 `can_use_tool`**
（deny 排在回调之前）：

| 形态 | 结果 |
|---|---|
| `chmod -R 777 repo-a/src` | DENIED |
| `true; chmod -R 777 repo-a/src` | DENIED |
| `bash -c 'chmod -R 777 repo-a/src'` | DENIED |
| `FOO=1 chmod -R 777 repo-a/src` | DENIED |
| `cd repo-a; chmod -R 777 src` | DENIED |
| `(cd repo-a && chmod -R 777 src)` | DENIED（由 auto 分类器拦） |
| `ls repo-a`（对照组） | 放行，无误伤 |

> `cd repo-a && chmod ...` 未测到 —— 模型每次都改去调 `AskUserQuestion`。
> 鉴于其余全中，推断 `&&` 同样被覆盖，但这一条属于推断而非实测。

### F25 ⚠️ **`ResultMessage` 不代表工作完成** —— D1 与 `thread.status` 必须改

后台 subagent 会活过 turn。实测（**native sonnet 与 deepseek-v4-pro 行为一致，是 harness 语义不是模型差异**）：

编排者派出两个 Explore 后即输出"正在后台运行，完成后我会汇总"，随后 `ResultMessage: success`。
此时**一个文件都没改**。

但保持连接继续监听，SDK **会自动续跑**，无需应用层 nudge：

```
ResultMessage success                 ← receive_response() 在这里就返回了
SystemMessage  background_tasks_changed  tasks=[{task_id, task_type:'local_agent', ...}]
TaskUpdatedMessage       status=completed
TaskNotificationMessage  task_id, tool_use_id, status=completed
SystemMessage  init                   ← 自动开新 turn
AssistantMessage  "api 端结果已返回，正在等待 web 端 subagent 完成"
ResultMessage success
        ... 第二个完成后再来一轮 ...
AssistantMessage  "两个 subagent 都已完成。汇总结果如下：..."
ResultMessage success
SystemMessage  background_tasks_changed  tasks=[]   ← 真正的完成信号
```

**三条设计影响**：

1. **不能用 `receive_response()`** —— 它在第一个 `ResultMessage` 就返回，会把后续工作整段切掉。
   必须用 `receive_messages()` 自行判定边界
2. **thread 空闲的真实条件** = 收到 `ResultMessage` **且** `background_tasks_changed.tasks` 为空。
   单看 `ResultMessage` 会过早把 thread 标成 idle，用户以为完事了
3. 一个逻辑任务产生**多个 `ResultMessage`**，`turn.done` 事件不能与之一一对应

### F26 ℹ️ 编排判断力：sonnet 更克制，deepseek-v4-pro 更爱扇出

同一个"重命名字段、两仓库同步"的任务：

| 模型 | 行为 |
|---|---|
| `claude-sonnet-5` | 判定"任务小、耦合紧，直接改胜过扇出"，三个文件一次改对 |
| `deepseek-v4-pro` | 扇出两个 Explore，然后等在后台任务上 |

sonnet 的判断其实**正确遵守了 `deep` profile 里写的编排纪律**（"定契约阶段体积小耦合高 → 自己做"），
是测试任务出得太小。但这提示：**编排纪律的措辞需要按 provider 调**，
deepseek 侧可能要显式写"小任务不要扇出"，否则会为琐事付两个 subagent 的成本。

`deepseek-v4-flash` 基本不主动调 subagent，`deep` profile 应至少用 pro 档。

---

## 实现期验证（2026-08-06，写 `permissions.py` 时）

### F27 ⚠️ 路径 deny 规则必须写 `//` 前缀，写错**静默失效**

`01` 要求对 `~/.ssh` 等敏感目录下 deny 规则，但没写语法。实测五种写法
（fake 密钥 + canary，控制组证明模型确实读得到）：

| 写法 | 结果 |
|---|---|
| `Read(//abs/dir/**)` | ✅ 拦住，且 `can_use_tool` **完全没被调用**（deny 在回调之前，同 F24） |
| `Read(//abs/dir/file)` | ✅ 拦住 |
| `Read(/abs/dir/**)` | ❌ **泄漏** —— 单斜杠不匹配任何东西 |
| `Read(/abs/dir/file)` | ❌ **泄漏** |
| `Read(**/.ssh/**)` | ❌ **泄漏** —— 无锚点的纯 glob 不匹配 |

三种失败写法都不报错、不警告，看起来完全正常。因此该拼写集中在
`permissions.path_rule()` 一个函数里，并有单测钉住 `//`。

**额外收获**：`Read(//dir/**)` 同时覆盖 **Bash**。`cat <abs>/id_rsa` 与
`cat ../fakehome/.ssh/id_rsa` 都被 CLI 拒绝（`Permission to use Bash with command ... has been denied`），
说明 CLI 对 Bash 命令做了路径解析，不只是工具名匹配。

### F28 ❌ CLI 的 Bash 路径解析不折叠中段 `..` —— 必须自己归一化

同一条 deny 规则下：

```
cat <vault>/id_rsa              → 拒绝
cat <vault>/../.ssh/id_rsa      → 放行，canary 泄漏     ← 同一个文件
```

`can_use_tool` **被调用了**，说明 deny 规则压根没匹配上。Read 工具本身会归一化
（`Read` 走 `..` 被拦住），漏的只是 **Bash 命令里的路径提取**。

我原本的 `secret_path_hit` 是子串匹配，**有完全相同的盲点**。已改为：
shlex 分词 → 取出路径样 token（含 `--flag=/path` 形式）→ `normpath` **与** `resolve()`
双重归一 → 与敏感根做 `==` / `in parents` 比较。`resolve()` 顺带堵住软链绕过
（`ln -s ~/.ssh ./shortcut` 是 cwd 内的写入，沙箱允许）。

端到端复验（`denied_tools()` + `Arbiter` 实装）：literal / 中段 `..` / 相对路径 `..` / Read 工具
四种写法全部拦下，其中两种是**我方拦的**；控制组读普通文件正常且静默放行（D3）。

**结论**：CLI 的路径 deny 是有用的第一层，但**不能作为唯一防线**。
`01` 的敏感路径章节据此补充。

---

## 部署期发现（2026-08-06，写 rpi systemd unit 时）

### 沙箱完全不管 `Edit` / `Write` 的落点

bubblewrap 只约束 Bash 子进程；`Edit` / `Write` / `NotebookEdit` 由 CLI 自己执行，
落在**服务 uid 能写的任何地方**。`will_sandbox()` 对它们返回 `True`（走的是兜底分支），
于是它们一路走到 tier 2 被静默放行。两个后果：

1. **写 `<repo>/.claude/settings.json` 是提权。** 该文件的 `permissions.allow` 由 CLI
   在 `can_use_tool` **之前**求值（F8 已实测 allow 规则会静默短路回调），
   而它就在 cwd 之下 —— 沙箱唯一允许写的地方。一条 `Bash(*)` 即可给自己解锁
   tier 3 全部内容，包括 `dangerouslyDisableSandbox`。
   ⚠️ **"写进去之后确实被读回并生效"这一步尚未实测**，是从 F8 推的；
   但拦掉它的代价是零，所以先拦，不等验证。
2. **写 workspace 之外**同样无人拦（`~/app` 下的自身代码、`~/.claude/` 等）。

已在 `classify()` 里补两条：`.claude/settings*.json` → DENY；
落点归一化后不在 workspace 之内 → ASK。两者都用 `normpath` **与** `resolve()` 双重归一，
且要求**每一种归一形式都在** workspace 内 —— 只要有一种在外就拦，
否则 workspace 内的软链在词法上"在内"、实际写在外。

systemd 侧同时兜底：`~/app` 以 `BindReadOnlyPaths=` 挂入，
db / profiles 移到 `StateDirectory=`，`ProtectHome=tmpfs` 只放回 workspace 与 `~/.claude`。

### `MemoryDenyWriteExecute=yes` 不影响 Bun 二进制

见 `00-overview.md` 的硬化章节。F12 只对 bwrap 测过，这条补的是 CLI 自身。

### nixpkgs 的 `claude-code` 已追到 2.1.222，但 rpi5 的 pin 还是 2.1.81

F13 的结论对**这台机器**依旧成立（其 flake pin `d7a713c` 给的是 2.1.81）。
若哪天顺带升了 rpi5 的 nixpkgs，可以重新考虑走 nixpkgs 路线免掉 nix-ld。

## 上线后发现（2026-08-08，第一次真机对话）

### F29 ❌ `path =` 到不了 Bash 工具 —— 登录 shell 会重写 PATH

现象：每条沙箱内的 Bash 都是 `Exit code 127 / zsh:1: command not found: bwrap`，
但服务进程自己的 PATH 里 bubblewrap 明明在（preflight 的 `shutil.which` 也找得到）。

原因是 CLI 把每条命令交给**登录 shell** 执行，而 NixOS 的 `/etc/zshenv` 里有：

```sh
if [ -z "${__NIXOS_SET_ENVIRONMENT_DONE-}" ]; then
    . /nix/store/…-set-environment      # 这一行直接覆盖 PATH
fi
```

于是 unit 的 `path =` 只到达服务进程，**到不了它经 shell 启动的任何东西**。
实测同一个 PATH 下 `zsh -c 'command -v bwrap'` 失败、`bash -c` 成功。

这是**失败关闭**，不是 F23 的失败开放 —— 没有任何东西逃出沙箱。
但危害是另一种：模型把 127 读成"沙箱坏了"，改用 `dangerouslyDisableSandbox: true`
重试，于是用户被训练成对着"关掉沙箱"这件事一路点允许。第一次真机对话里就发生了 6 次。

两处修：bubblewrap / socat 进 `environment.systemPackages`（系统 PATH 才是 shell
重置后剩下的那个）；preflight 增加 `check_shell_path()`，用 `$SHELL -c 'command -v …'`
在**真正要查的那个 PATH 上**再查一遍，查不到就拒绝启动。

### `ProtectHome=tmpfs` 意味着手放进 `/home/agent` 的文件不存在

同样是第一次真机对话里撞到的：用户配了 `~/.gitconfig`、导了 gpg 私钥，
agent `ls ~` 只看到 `agent_work` / `app` / `.claude` 三项 —— 它们是仅有的三条 bind mount，
其余部分是一个 root 拥有的空 tmpfs。这是设计意图（见上一节），
但意味着**任何要给 agent 用的家目录文件都必须在 unit 里显式列出**，重启也不会自己出现。

`.gitconfig` 以只读挂入，`.gnupg` 以可写挂入（gpg 要写 random_seed 与 agent socket）。
`~/.gnupg` 本来就在 `DEFAULT_SECRET_PATHS` 里，所以 `git commit -S` 能签，
`cat ~/.gnupg/…` 仍被 deny 规则拦掉 —— 这正是想要的分工。

### F30 ❌ `ProtectKernelTunables=yes` 让沙箱一次都没建起来过

F29 修好之后 `bwrap` 能跑了，于是露出了下一层：

```
bwrap: Can't mount proc on /newroot/proc: Operation not permitted
```

`ProtectKernelTunables=yes` 会把 `/proc/sys`、`/proc/irq` 等以只读 bind mount
盖上去。内核的 `mount_too_revealing()` 因此认为 `/proc` 不再"完全可见"，
于是**拒绝非特权用户命名空间挂载新的 procfs**。在 rpi5 上逐项二分实测：

| 设置 | bwrap `--unshare-all --proc /proc` |
|---|---|
| 全套 unit 配置 | ❌ EPERM |
| 只去掉 `ProtectKernelTunables` | ✅ |
| 单独加 `ProtectKernelTunables` | ❌ EPERM |
| `ProtectControlGroups` / `ProtectSystem=strict` / `ProtectHome=tmpfs` / `PrivateTmp` / `RestrictNamespaces` / `MemoryDenyWriteExecute` 单独加 | ✅ |

代价换算是单向的：服务 uid 非特权且 `NoNewPrivileges=yes`，
不带这个设置也写不了内核旋钮（实测 `echo 1 > /proc/sys/kernel/sysrq` → EPERM）。
留着它换来的是**整个 tier 2 不存在**。已从 unit 移除。

**preflight 为什么没拦住**：`check_sandbox()` 的探针是
`bwrap --unshare-all --ro-bind / / true` —— 没有 `--proc /proc`，
而挂 procfs 正是真实沙箱里唯一失败的那一步。探针于是在一台每条沙箱命令都必死的
机器上一路绿灯。已补 `--proc /proc`，并把 /proc 被遮挡写进报错文案。

**代价**（铁轨小游戏那次会话，`thr_4dbd33ecf999`，47 分钟）：
35 次 Bash 里 32 次带 `dangerouslyDisableSandbox`，占 91%。
模型在第 2 次 127 之后就再没试过沙箱路径。前 15 次逐条弹审批、用户全部 allow，
中位数 3 秒 —— 与 F29 记的是同一个训练过程，只是这次跑满了一整个任务。

### 审批被关掉这件事在事件日志里查不到

同一次会话里，20:03 之后 17 次 `dangerouslyDisableSandbox` 调用**一条
`approval.required` 都没有**（之前 15 次每次都有，且都在几秒内 allow）。
超时路径不是解释：`approvals.ask` 超时是拒绝并发 `approval_timeout`，日志里没有。
剩下的可能是 `set_permission_mode("bypassPermissions")` —— 而
`runner.py:145` 既不发事件也不写回 `state.profile.permission_mode`。

也就是说：**"谁在什么时候关掉了审批"在事件日志里无法回答**，
而这恰好是最需要能回答的一件事。

事后确认原因就是用户开了 `/mode auto`。这不改变结论：`auto` / `dontAsk` /
`bypassPermissions` 让 CLI 不再调用 `can_use_tool`，而三层判定全长在那个回调里 ——
`disallowed_tools`（HARD_DENY 与凭据路径的文件工具规则）由 CLI 自己评，还在；
tier 3 与 `classify()` 里的 **Bash 凭据路径检查**（F27 说明 deny 规则盖不全所有写法，
这层就是补那个洞的）一起没了。当时沙箱又是坏的，于是那 17 条命令毫无约束。

已修：`thread.status` 每条带上 `permission_mode`，`POST /mode` 额外补发一条。
热切仍不落盘 —— `start()` 按 profile 重建 client，让 `bypassPermissions`
跟着 thread 活过重启是更糟的默认。

### 正常重启不该在聊天里变红

两处，同一个毛病：

- `exit code 143` —— systemd 默认 `KillMode=control-group`，`systemctl restart`
  同一瞬间signal 掉本进程和所有 `claude` 子进程。子进程通常先死，消息循环因此
  在 `close()` 取消它之前先看到 `ProcessError(143)`，于是每个活着的 thread 发一条
  `error: internal`。已按 `exit_code == 143` 判为关机路径：转 idle，不发事件。
- 之前那四条 `[Errno 2]` resume 失败是同一件事的另一半（relay 早发 `online`）。

代价不是难看，是**聊天学会了忽略红色**——而红色是唯一该被当真的通道。

### 其它（同一次会话）

- **`Grep` 工具在会话里不存在**：`No such tool available: Grep`，CLI 自己给的提示是
  改用 Bash 里的 `grep`。模型立刻照做，没有实际损失。原因未定
  （该会话走网关跑的是 `deepseek-v4-pro`，`tool_use_id` 是 `call_00_…` 而非 `toolu_…`），
  要定位就用嗅探器抓一次请求看 `tools` 数组。
- **求解器退出码 1 被记成工具失败**：8 次 "NO SOLUTION FOUND" 都带 `is_error`。
  这是程序自己的退出码，不是基础设施问题，但会让模型把正常结论读成故障。

---

## 待办

设计定稿所需的实测已全部完成。剩余项均为"边写边验"，不影响架构：

| # | 项 | 说明 |
|---|---|---|
| D1 打断语义 | `interrupt()` 后 drain 旧任务消息的具体边界 | 与 F25 的多 `ResultMessage` 语义叠加，需在实现主循环时一并验 |
| 并发审批 | 多个后台 subagent 同时挂起时 `can_use_tool` 是否重入 | F25 已证实后台 agent 确实并行，该场景真实存在 |
| D8 `/undo` | `rewind_files()` 的粒度与 `message_id` 语义 | 只影响 `/undo` 端点实现 |
| D7 热切 | `set_permission_mode()` 中途切换 | 低风险 |
| `fork_session` | 分叉后的 session 文件与事件归属 | 只影响 `/fork` 端点 |
| 负载内存 | 真实会话（非空闲）的 PSS 增长曲线 | 按用户要求暂假设内存充足 |
| F2 补充 | `dir:skill` 限定语法的生效条件 | 仅在出现跨仓库同名 skill 时才需要 |

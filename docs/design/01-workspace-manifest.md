# Workspace Manifest 与索引 helper

`~/agent_work/workspace.toml` —— 描述工作区里有哪些仓库、它们之间是什么关系。

TOML 格式：Python 3.11+ 的 `tomllib` 内置，无额外依赖。

## 设计前提：能力是懒发现的

实测（`03-verification.md` F1/F4）：agent 读了 `repo-b/` 下任一文件之后，
`repo-b/.claude/skills/` 的 skill 与 `repo-b/CLAUDE.md` **自动**进入它的可用范围。

所以 manifest **不负责**注册能力。它只负责一件 SDK 无论如何都给不了的事：

> **让 agent 在决定动哪个仓库之前，知道有哪些仓库、各自管什么、彼此有什么约束。**

懒发现解决"进去之后能用什么"，manifest 解决"该进哪里"。两者不重叠。

## Schema

```toml
[workspace]
scratch = ".agent"              # 草稿区：findings-*.md / contract.md
default_profile = "quick"

[[repo]]
name        = "api"             # 唯一；SSE 事件归属键与 diff 分组键
path        = "api"             # 相对 workspace 根，必须在根之下（D2）
description = """
后端 HTTP 服务，Python/FastAPI。
涉及接口定义、数据库 schema、鉴权逻辑的任务用它。
"""

[[repo]]
name        = "web"
path        = "web"
description = """
前端 SPA，TypeScript/React。
涉及页面、组件、前端状态的任务用它。
"""

[[repo]]
name        = "infra"
path        = "infra"
description = "Terraform 与部署脚本。只读参考，一般不改。"

# ─────────────────────────────────────────
# 跨仓库关系：编排者需要、subagent 拿不到
[[relation]]
from     = "web"
to       = "api"
kind     = "http"               # http | library | schema | build | deploy
contract = "api/openapi.yaml"   # 相对 workspace 根；契约的事实来源
note     = """
web 通过 /v1 调用 api。
改动请求/响应结构必须两边同步，且以 openapi.yaml 为准。
"""

[[relation]]
from     = "infra"
to       = "api"
kind     = "deploy"
note     = "api 的环境变量在 infra/envs/*.tfvars 中定义；新增配置项需两边同时改。"
```

就这些。`[[repo]]` 三个字段，`[[relation]]` 四个字段。

### 字段语义

| 字段 | 必填 | 说明 |
|---|---|---|
| `repo.name` | ✅ | 唯一标识，也是 SSE 事件与 diff 的分组键 |
| `repo.path` | ✅ | 相对 workspace 根 |
| `repo.description` | ✅ | **写"何时该动它"，不是"它是什么"** |
| `relation.from` / `.to` | ✅ | `repo.name`。有向：`from` 依赖 `to` |
| `relation.kind` | ✅ | 供判断改动的传播方向 |
| `relation.contract` | ❌ | 契约的事实来源文件 |
| `relation.note` | ✅ | 自然语言约束。跨仓库决策的主要输入 |

`[[relation]]` 是**唯一不属于任何单个仓库的信息** —— subagent 只能收到 Agent 工具的 prompt 字符串，
无论怎么懒发现都拿不到它。这是 manifest 存在的核心理由。

## 刻意不放进 manifest 的东西

| 曾考虑过 | 为什么删掉 |
|---|---|
| `repo.skills` | 懒发现已覆盖（F1）。显式登记只是把本来会到的东西提前灌入，反而丢掉上下文经济性 |
| `repo.agent` / `model` / `effort` | 不再生成 per-repo `AgentDefinition`，见下 |
| skill 的 `<repo>-` 前缀物化 | 同 F1，方案取消 |

### 不生成 per-repo AgentDefinition

原设计给每个仓库生成一个专属 agent。取消，理由三条：

1. **隔离本来就有。** agent 只看得到它碰过的子树的 skill —— 工作范围天然是作用域
2. **`AgentDefinition.skills` 不做过滤**（实测 F3），拿它当隔离手段是无效的
3. **agent 集合冷绑定在建 thread 时**（D6），等于开 thread 前就要决定动哪些仓库 ——
   那就退化成单仓库工作了，与多仓库编排的初衷相悖

替代方案：调研扇出直接用内置的 `Explore`（只读、廉价），修改用 `general-purpose`。
编排者在 Agent 工具的 prompt 里点名仓库路径，subagent 一读进去，该仓库的 skill 与 guide 自动到位。

**per-repo MCP 是唯一的例外** —— MCP 不能懒加载。若某仓库确有专属 MCP 需求，
再引入 `[[repo.mcp]]` 并配合 `toggle_mcp_server()`；v1 先不做。

## 索引 helper

manifest 是给程序读的，agent 读的是它生成的一个 markdown 文件。

**生成物**：`.agent/workspace-index.md`，建 thread 时刷新一次。

```markdown
# 工作区索引

## 仓库

### api  (`api/`)
后端 HTTP 服务，Python/FastAPI。
涉及接口定义、数据库 schema、鉴权逻辑的任务用它。

可用 skill（进入该目录后自动可用）：
- `run-migration` — 生成并执行数据库迁移
- `api-smoke` — 起服务跑一遍冒烟

### web  (`web/`)
前端 SPA，TypeScript/React。
...

## 跨仓库约束

- **web → api** (http)，契约见 `api/openapi.yaml`
  web 通过 /v1 调用 api。改动请求/响应结构必须两边同步，且以 openapi.yaml 为准。
- **infra → api** (deploy)
  api 的环境变量在 infra/envs/*.tfvars 中定义；新增配置项需两边同时改。
```

skill 清单由 helper 扫 `<repo>/.claude/skills/*/SKILL.md` 的 frontmatter 得到，
manifest 里不用登记。**扫描时检出跨仓库重名并报错**（F2：同名会被静默遮蔽且无法限定）。

### 怎么进上下文

根 `CLAUDE.md` 里**只放一行指针**：

```markdown
本目录是多仓库工作区。动手前先读 `.agent/workspace-index.md` 了解仓库划分与跨仓库约束。
```

**不注入 system prompt。** 三个理由：

- system prompt 在 cache prefix 里，改它会让 system + messages 两层缓存失效（D6）
- 索引会随仓库增加而变长，塞进 system prompt 是每 thread 固定成本
- 简单任务根本不需要它 —— 让 agent 自己决定读不读，正是 progressive disclosure

代价是一次 Read。相对于它省掉的东西，这个代价可以忽略。

## 敏感路径 deny（必做，非待定）

实测 F19：**沙箱只拦写不拦读**，cwd 之外的任意文件都能被 Bash 静默读出，零审批。
因此全局配置里必须显式 deny 下列路径，且这不属于仓库级配置：

```
~/.ssh  ~/.aws  ~/.config/gh  ~/.gnupg  ~/.netrc  ~/.docker/config.json
/run/secrets  <网关凭据目录>
```

先在全局硬编码；若出现仓库级需求再引入 `[[repo]].secrets` 字段。

**两条实测约束（F27 / F28），缺一即形同虚设：**

1. 规则必须写成 `Read(//abs/path/**)`。`Read(/abs/path/**)`（单斜杠）与
   `Read(**/.ssh/**)`（无锚点）**不报错、不匹配、静默泄漏**。
2. CLI 的 deny 会覆盖 Bash，但**不折叠中段 `..`** —— `cat <vault>/../.ssh/id_rsa`
   直接放行。因此必须在 `can_use_tool` 里**自行归一化**（`normpath` + `resolve()`，
   后者同时堵住 cwd 内软链绕过），不能依赖 CLI 那一层。

## 变更生效时机

manifest 改动在**建 thread 时**重新生成索引。已有 thread 不受影响。

新增仓库的流程：放进 `~/agent_work/`，加一段 `[[repo]]`，开新 thread。
若该仓库不涉及跨仓库约束，连 manifest 都可以不改 —— agent 照样能进去用它的 skill，
只是不会在索引里被"推荐"。

# Codex 运行时与迁移

本文取代 00–04 中涉及 Claude SDK、工具仲裁、会话池与审批的设计。05 的文件传输协议继续有效。

## 目录隔离决策

实测 runtime 0.159.2 的多个文件级 deny-read 会触发 bubblewrap 文件描述符复用错误。
用户选择重做目录级隔离，继续使用官方 runtime。状态目录、秘密配置目录和认证 socket
目录整体屏蔽，覆盖将来新增的私有文件；不使用逐文件 glob。

技能与插件 cache 放入独立资源目录，CODEX_HOME 原路径链接到它们。原生技能发现返回
资源目录的实际路径，工具可以读取它们；生产资源目录位于工作区与临时目录以外，只读。
工作区 .agents/skills 的写入权限不变。已有资源首次启动搬迁，目标冲突时停止且不覆盖。

CODEX_HOME/tmp/arg0 只读挂载为空目录，避免生成落在私有目录中的必读辅助入口。
官方 runtime 会使用自身可执行文件继续执行；PATH aliases 的只读警告在此布局下是预期
现象。实际 CLI/app-server 沙箱、thread 创建、技能发现、资源读取、私有状态拒读以及
认证 socket 无法连接均由启动自检验证，不允许忽略自检失败。

## 运行时

Python SDK 与 CLI 均锁定 `openai-codex==0.159.2`。每个服务运行一个 app-server，
Antares 最多同时执行六个任务；同一会话后续消息排队。SQLite 保留 Codex thread ID、
模式、摘要和 SSE 历史；完整模型上下文仍由 Codex 保管。每轮完成释放订阅，后续恢复 thread。

`plan` 对应原生 plan collaboration mode 和只读沙箱；`auto` 对应 default collaboration mode
和 workspace-write。两者都强制 `approvalPolicy=on-request`、`approvalsReviewer=auto_review`
（Approve for me），不是 `never` 或无条件放行。生产 managed requirements 禁止改成其他审批
策略、审查器和 danger-full-access。意外进入客户端的命令/文件审批一律拒绝。
执行沙箱允许联网，保留 Codex 的临时目录行为。沙箱启动自检失败会拒绝启动服务。

模式只能在空闲时切换；忙碌时 HTTP 409。Telegram 支持 `/plan`、`/auto`、`/mode plan|auto`。
需要用户决定时，Agent 用普通中文消息提问并结束本轮。没有审批按钮或 request_user_input 界面。
profile 在加载会话时确定；quick 使用 low/auto，deep 使用 high/plan，默认采用 runtime 模型。
需要指定模型时，在新 profiles 目录的 TOML 中设置 `model`。

根 `turn/completed` 结束一轮。子 Agent 的派遣与完成通过原生 collab 事件显示，编排提示词
要求汇总前等待子任务；不模拟 Claude 的后台完成后自动续跑。中断会清空队列；进程崩溃和
结果不确定的调用不自动重试。

工作区固定，原生指令使用 `AGENTS.md`，技能使用 `.agents/skills/*/SKILL.md`。
每轮刷新索引、extra skill roots 和 skills/list；工作区与登记仓库的 skills 目录明确加入
可写根，因此 Agent 可以创建技能，下一轮发现。重复 skill 名和越界符号链接会被拒绝。
`workspace.toml` 的仓库列表在创建/恢复 runner 时读取；修改列表后新建会话或重启服务。

## 认证隔离

`codex-auth.service` 是唯一 refresh token 持有者，用户为 `codex-auth`，状态位于
`/var/lib/codex-auth/codex`（0700）。认证代码随 qq-codex-agent 包安装。
`/run/codex-auth/auth.sock` 为 0660，只授权 `codex-auth-clients` 组。

Antares 与 QQ 各自运行独立 app-server、CODEX_HOME 和工作区，通过 SDK external
`chatgptAuthTokens` 登录。共享认证客户端只使用内存凭据，不加载或写入自己的 auth.json。
Unix socket 只返回 access token、账户 ID、套餐及令牌版本；refresh token 不离开认证服务。
刷新按令牌版本合并，超时的调用不取消正在进行的刷新；切换账户会要求重启客户端。
两个服务的 managed deny-read 均屏蔽认证 socket、各自数据库和认证状态。

统一登录入口为 `codex-login.service`；旧 `qq-codex-login.service` 仅转调这个入口。
设备登录会停止认证服务；登录成功后重新启动三个服务。正常迁移直接复用 QQ 现有登录，无需验证。
不要同时运行指向认证服务 CODEX_HOME 的其他 Codex CLI。

## 状态分代

| 状态 | Codex 路径 |
|---|---|
| Antares 数据库、profiles、文件索引 | `/var/lib/antares-agent/codex-v1/` |
| Antares 技能与插件资源 | `/var/lib/antares-agent-resources/` |
| QQ 技能与插件资源 | `/var/lib/qq-codex-resources/` |
| Antares CODEX_HOME | `/var/lib/antares-agent/codex-v1/codex/` |
| Relay 传输状态 | `/var/lib/antares-agent-relay/codex-v1/` |
| alice 会话映射 | `data/agent-codex.db` |
| alice 文件任务 | `data/agent-codex-files.db` |

旧数据库、profiles、CLI 历史和工作文件保留，旧 Claude thread 不恢复。
旧 Telegram 审批按钮仅移除键盘，不向服务发送命令。

## 发布与一次性迁移

先发布 qq-codex-agent、antares-agent 和 alice 的对应版本，再更新 Nix 的 RPi input：

```sh
nix flake update --flake ./hosts/rpi5 qq-codex-agent
```

本地验证可使用 qq-codex-agent 的源码覆盖；不要把本机绝对路径写进正式 flake.lock。
新文件必须加入 Git 后，Git flake 才能包含它们。只在匹配架构的 RPi 上构建/切换系统。
迁移过程中停止消息入口和有关服务，不接受新任务。以下命令均在对应主机执行。

1. 在 RPi 停止 `antares-agent-relay`、`antares-agent`、`qq-codex-agent` 和旧登录 unit；
   在 hk 停止 alice。备份 Antares/relay/QQ 的整个状态目录和资源目录、agent 工作区、alice 的旧数据库
   及附件目录；SQLite 应在停服后连同 WAL/SHM 一起备份。备份目录只对管理员可读。
2. 安装新代码和 Nix 配置，暂不放开消息入口。Nix switch 可能尝试启动服务：执行迁移前
   再停止 `codex-auth`、`codex-login`、`qq-codex-agent`、`antares-agent`。
3. RPi 以 root 运行 `codex-auth-import`。它拒绝运行中的消费者及已有目标，复制 QQ 的
   `auth.json` 到独立认证目录，保存 root-only 备份到 `/var/backups/codex-auth/qq-auth.json`，
   最后删除旧 QQ 路径。不会打印令牌。若已有统一认证，跳过此步骤。
4. 使用新 Antares 环境，先预览工作区迁移：

   ```sh
   sudo -u agent env ANTARES_WORKSPACE=/home/agent/agent_work \
     /home/agent/app/.venv/bin/antares-agent --migrate-workspace
   ```

   预览无冲突后追加 `--apply`。迁移范围为工作区根与 workspace.toml 登记仓库的
   `CLAUDE.md` 和 `.claude/skills`；先备份到 `.agent/migrations/<时间>/`，再移动到原生路径。
   已有 AGENTS.md/同名技能时全部停止，不覆盖也不自动合并。嵌套目录的额外 CLAUDE.md、
   Claude settings/hooks/插件配置需要人工评估，不作为 Codex 配置加载。
5. 启动 `codex-auth`，再启动 QQ 和 Antares。检查 systemd 日志与 Antares `/v1/health`；
   两个服务自检均通过后，更新并启动 alice 和 relay。在 Telegram 新建会话，旧映射不沿用。
6. 验收普通问答、读图、文件往返、写 skill 后下一轮发现、plan 拒写、auto 写入、
   中断/排队、重启恢复、多会话和子 Agent；确认两个 Agent 使用同一账户，刷新时只有
   codex-auth 写 auth.json。观察 RPi 内存与 CPU，再恢复正常流量。

若缺少可迁移的 QQ 登录，运行 `systemctl start codex-login`，从该 unit 日志取得设备码，
登录成功后 `systemctl start codex-auth qq-codex-agent antares-agent antares-agent-relay`。

回退前停服，恢复代码/Nix 代与旧数据库及工作区备份。不要混用新旧会话库。
认证服务若已经刷新，旧 auth 备份可能失效；回退 QQ 时应在全部消费者停止后，把认证服务的
最新凭据交回旧 QQ 唯一刷新者，而不是恢复旧 refresh token。不能同时启用两个刷新者。
文件回滚、Claude checkpoint、fork/undo API 不在本次迁移范围。

## 验证

```sh
uv sync --frozen --all-extras
.venv/bin/pytest -q
ANTARES_CONTRACT=1 .venv/bin/pytest tests/test_codex_contract.py -q
ruff check src tests
```

contract 测试用临时 CODEX_HOME 和假令牌，不请求模型，验证真实 CLI、external auth、
原生技能发现、只读沙箱和自动审查配置。Linux 沙箱嵌套执行需要允许 bubblewrap。
`ANTARES_LIVE=1 ANTARES_LIVE_AUTH_SOCKET=... pytest tests/test_live.py` 才会运行真实模型测试。
RPi managed deny-read 的最终检查在真实 unit 命名空间的启动 preflight 中执行。

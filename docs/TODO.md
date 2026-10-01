# 待办

Codex 迁移与部署流程见 [运行时与迁移](design/06-codex-migration.md)。

- 发布四个仓库的变更，更新 RPi 的 qq-codex-agent input，并在 RPi 按停服迁移流程验收。
- 用真实共享账户验证设备登录、过期刷新、两个 Agent 并发使用及认证服务重启恢复。
- 实机验证子 Agent 完成、中断、读图和 Telegram 文件往返，测量六任务并发的内存与 CPU。
- SQLite 事件表与 inbox 尚无定期保留策略；根据实际占用增加清理。
- Telegram 同聊天多会话仍用 thread 标签区分；需要独立话题时接入 forum topics。

Claude SDK 的历史设计与实测保留在 design/00–04，不能作为 Codex 的运行时约束。

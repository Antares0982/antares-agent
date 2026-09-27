# 双向文件通道

## D15：文件走 HTTPS，事件只带元数据

部署：agent 与 relay 在树莓派，alice、nginx、Local Bot API 在 hk。
Bot API 使用 `--local`、用户 alice，监听 `127.0.0.1:60081`。
`tg.alyr.dev` 仅 DNS 解析到 hk，nginx 的 443 提供受 HTTP Basic 认证保护的文件通道，
不代理 Bot API。密码由 agenix 分发，仅 relay 获得明文登录凭证。

- 出站：outbox → agent 暂存 → relay 流式 PUT → nginx 原子落盘 → alice 本地路径发送。
- 入站：Bot API 本地文件 → alice 暂存 → relay 流式 GET → agent 附件接口 → inbox。
- 每个方向一个传输 worker；RabbitMQ 消费者不等待大文件网络操作。
- 原生 nginx DAV PUT 不支持断点续传，失败自动从头重传。

## 协议

文件描述包含 `artifact_id`（32 位随机小写十六进制）、`name`、`mime`、`size`、
`sha256`（64 位小写十六进制）。文件名只供显示，路径只由文件 ID 生成。
单文件默认上限为 2,000,000,000 字节；零字节文件有效。

agent 的接口继续仅通过现有 Unix socket 提供：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/artifacts` | 待导出文件描述及 `created`、`thread_id` |
| GET | `/v1/artifacts/{id}` | 流式读取出站文件 |
| POST | `/v1/artifacts/{id}/complete` | `{state: transferred或failed, error?}`；空对象表示成功 |
| PUT | `/v1/threads/{thread}/attachments/{id}` | 原始文件体；query 带 name、mime、size、sha256，必须有准确 Content-Length |
| POST | `/v1/threads/{thread}/messages` | attachments 接受上述文件描述；request_id 用于消息去重 |

PUT 先写临时文件，验证长度、摘要和所属线程后提交。同 ID 内容冲突拒绝。
`/messages` 仍接受原有小文件 `data_b64` 输入，历史事件也保留旧格式兼容读取。

hk 的两个路径使用独立的方法限制：

- `PUT /outgoing/{id}`：relay 上传。nginx 先写同一文件系统内的临时文件，随后 rename。
- `GET/HEAD /incoming/{id}`：relay 下载 alice 准备好的附件。

没有目录列表或公开文件下载。传输过程中内存只持有固定大小的数据块。

## 持久化与失败语义

agent 的文件索引位于数据库同级 `artifacts/index.sqlite`，文件内容位于该目录内。
登记后未发布事件的记录会在启动时恢复；已写入事件的记录通过 ID 去重。
relay 在 `ANTARES_RELAY_STATE/transfers.sqlite` 保存待处理命令和传输结果。
alice 通过现有数据库工具维护 `data/agent-files.db`，任务在推进事件游标前落盘。

普通消息同线程排序，审批、中断、模式切换不排在文件任务后面。
任务固定目标 chat/thread，切换会话不会改变已登记任务的接收方。
未绑定会话的首条消息持久化等待绑定；新线程创建仍使用现有控制协议。

网络错误退避重试，最多自动尝试 24 小时。认证错误、超限、摘要错误立即报告。
文件传输可重试；模型消息交接使用 request_id。
进程在模型交接中途或内存排队期间重启时，标记结果不确定并通知用户，避免自动重复执行。
最终 idle 后的请求保留去重记录。

Telegram 发送完成后保存 message_id 和 file_id。发送成功至本地保存状态之间发生崩溃，
仍可能重复发送；Bot API 不提供用于此处的端到端幂等键。

两端暂存文件保留 7 天，活跃任务不清理。inbox 是模型会话文件，不按传输缓存清理。
接收失败的 outbox 副本保留，同一 runner 对未变化的失败文件不反复提示。
首版没有整个暂存目录的硬配额；磁盘不足不会提交文件或删除原件。

## 部署与验证

涉及 antares-agent、alice、Nix 三个仓库。Nix 已增加 `tg.alyr.dev`、共享组、目录权限、
relay 状态目录和两份新 age 密文；本地忽略的 `secrets/secrets.nix` 也登记了收件人。

1. 将三个仓库的修改提交并同步到相应机器；本次实现不自动提交或上线。
2. hk 应用 Nix 配置，确认 ACME 证书与 nginx 生效。
3. 刷新 alice 用户管理器的组成员身份，使新启动的 alice 服务具有 `agent-files` 组。
   Bot API 系统服务也需重启以取得该补充组；不要仅依赖旧登录会话中的组列表。
4. 确认 alice 配置的 Bot API 地址为 `http://127.0.0.1:60081/bot`，文件地址为
   `http://127.0.0.1:60081/file/bot`，`BOT_API_LOCAL_MODE=True`。
   不需要向 agent 或 relay 提供 Telegram token。
5. 发布 agent 新接口与 alice 新逻辑，再在 rpi 应用 Nix 配置并启动 relay。
   alice 用 `ANTARES_FILE_ROOT=/var/lib/agent-files` 启用后台文件任务；
   relay 使用 `ANTARES_FILE_URL`、`ANTARES_RELAY_STATE` 及 agenix 提供的登录凭证。
6. 显式进行双向小文件和超过 10MiB 文件的 Telegram 验收，随后验证重启恢复。
   接近上限的真实 Telegram 上传单独执行。

本地检查：`pytest -q`；alice 在 `nix develop` 中运行 `tests/test_agent*.py`。
`python tests/stress_files.py` 在 256MiB 地址空间限制下验证 2GB 生成流，
不写入大文件、不连接 Telegram。它验证流式数据路径的内存边界，不代表线上带宽或吞吐量。

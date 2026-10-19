# 日志入口

本文档记录运行时问题最常用的日志与快照入口。

## 运行时日志主入口

当前运行时日志以进程 `stdout/stderr` 为主入口。默认情况下，运维排查应优先查看容器日志、Supervisor 日志、systemd journal，或直接查看进程标准输出/标准错误。

## `WORKING_DIR/swe.log` 条件

`WORKING_DIR/swe.log` 仅在 `SWE_FILE_LOG_ENABLED=true` 时生成并持续写入。未开启该开关时，不应将 `swe.log` 视为稳定日志来源。

## daemon 日志命令行为

`/daemon logs` 与 `swe daemon logs` 仅在文件日志启用时读取 `swe.log`。当 `SWE_FILE_LOG_ENABLED` 未开启时，这两个入口不会返回运行时日志内容，只会提示运维改为检查容器日志、Supervisor 日志、systemd journal 或进程 `stdout/stderr`。

## Hook telemetry

Hook 机制的埋点以运行时日志为入口，不写入 `swe_tracing_spans`。当至少一个 hook handler 实际执行时，`src/swe/agents/hook_runtime/runtime.py` 会输出 INFO 级单行日志，消息体以固定前缀 `HOOK_TELEMETRY ` 开头，后面紧跟 JSON payload。

排查 hook 行为时优先按 `HOOK_TELEMETRY ` 前缀过滤日志，再解析 JSON 中的 `schema`、`hook_event_name`、`trace_id`、`session_id`、`tool_name`、`decision`、`blocked`、`handlers`、`permission_decisions` 等字段。payload 只保留结构化元数据和脱敏/截断预览，不应作为原始 prompt、tool input、tool output 或 handler output 的来源。

## Runtime Diagnostic

系统运行诊断以 INFO 级单行日志输出，消息体使用固定前缀 `RUNTIME_DIAGNOSTIC `，后面紧跟扁平紧凑 JSON。`HOSTNAME` 是 Pod 实例唯一标识，事件包含：

- `instance_registered`：应用启动完成后注册实例。
- `diagnostic_flow`：首次延迟 120 秒并增加 0-10 秒抖动，之后每 30 分钟输出一次。
- `instance_deregistered`：应用优雅关闭时注销实例。

`diagnostic_flow` 只覆盖 SSE 当前值与峰值、事件循环延迟、当前 Swe/Uvicorn Python 进程资源、当前容器的文件打开句柄数与 cgroup 磁盘读写速率，以及 `/opt/deployments/app` 所在文件系统的使用情况。Pod 级采集不读取 Kubernetes 节点级 `file-nr` 或磁盘 IO；无需 root 权限，读取不完整时对应字段为 `null`，其他字段继续输出。

日志采集下游应以注册和诊断事件续租实例状态，租约为 75 分钟；注销事件立即将实例置为无效。诊断流水保留 30 天。Swe 本身不直接写诊断数据库，也不负责 Kafka 消费。

## query error dump

`query error dump` 仍可作为请求级故障快照入口，用于补充上下文与失败栈信息，但不替代运行时主日志入口。

## 新版定时任务资源存储失败

Scheduler 的 `swe_prepare_failed` 只表示资源准备步骤失败。若收到 SWE 的 HTTP 503 和 `cron_resource_storage_unavailable`，在 SWE 容器日志中按同一个 `operation_id`、`job_id` 查找 `SWE 定时任务资源存储失败`。

ERROR 日志中的 `stage` 区分 `begin_transaction`（进入事务）、`lock_job`（对新任务定义加行锁）、`validate_operation`（锁内复核操作）、`resource_action`（资源处理）和 `finish_transaction`（正常资源处理结束后的事务退出）。阶段表示最后进入的步骤；若回滚本身失败，仍须结合异常链定位，不能只靠阶段断言失败的是哪一条 SQL。

诊断保留异常链类型、数据库错误号及固定类别、安全的缺失方法名与栈位置，不输出原始异常消息、SQL、任务正文、Header、源码行或局部变量。例如 `1205/lock_wait_timeout` 表示锁等待超时，`1213/deadlock` 表示死锁，`AttributeError` 配合 `missing_attribute=transaction` 可提示部署版本缺少事务方法。公共 HTTP 错误不会被改写为存储失败；此日志不覆盖所有资源准备异常。

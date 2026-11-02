# SWE 资源接口失败响应日志实施计划

> 用户后续补充要求 SWE 也打印底层异常；新增 Task 2 覆盖此前“不改 SWE”的范围限制，接口返回和业务流程仍不变。

> **For agentic workers:** 在当前隔离 worktree 按 executing-plans / TDD 主线程顺序处理；用户指令禁止派发子代理，不把自检冒充独立评审。

**Goal:** Scheduler 调用 SWE prepare/cleanup 失败时，额外记录有界、脱敏的 JSON 响应体，方便区分 422 参数校验和资源准备失败。

**Architecture:** 在现有 raise_for_status 前记录 ERROR；共用 diagnostics 模块增加响应体格式化，复用 tracking_common 的凭据字段识别与字符串脱敏。保留原 HTTP 异常、操作错误码和状态处理，不改 SWE、前端、配置或表结构。

**Tech Stack:** Python、httpx MockTransport、pytest/caplog。

**Spec:** 用户确认“没有打印响应体的话打一下”，仅补失败日志，不承担修复行内 422 根因或继续打包。

## 约束

- 只记录非 2xx 响应，标明 action、operation_id、job_id、http_status、response_body。
- 结构化 input、ctx、任务正文、请求/响应 Headers 及认证字段脱敏；不输出原请求和成功任务定义。
- JSON 响应解析上限 8192 字节，复用已有公共值转换限制层级和集合长度；日志正文最多 4096 字符并标注截断。超大、非 JSON、标量 JSON 或无法安全解析的正文只记省略原因。
- 重定向仍按现有 HTTP 异常处理；原异常必须继续抛出，不能因日志变成成功。
- 不提交、推送或打包；未完成的打包工作保持暂缓。

## Task 1：TDD 补齐失败响应日志

**Files:** managed/diagnostics.py、managed/swe_client.py、tests/unit/scheduler/test_managed_cron_response_logging.py。

**Interfaces:** format_swe_response_body(response) 返回可安全写日志的字符串；ManagedSweClient 的 prepare/cleanup 参数及返回不变。

- [x] RED：真实 ManagedSweClient + MockTransport 返回 422 JSON，断言 ERROR 记录包含响应中的 detail/msg/loc；敏感字段不可出现，原 HTTPStatusError 保留。另测成功、重定向和 503。
- [x] RED：非 JSON、超长 JSON、非法编码/过深 JSON 和标量响应不泄露正文，4096 字符截断保留明确标记。
- [x] GREEN：实现最小脱敏格式化方法及非 2xx 响应日志；只替换 HTTP 边界，不 mock 日志或实际客户端逻辑。
- [x] 验证：运行新日志测试与原 clients/logging，再运行完整 tests/unit/scheduler；按项目 pre-commit 配置检查本次 Python 文件，复杂度上限 15，git diff --check。
- [x] 主线程自检安全、调用方与原异常/操作状态兼容；不修改其他已提交功能。

测试命令：

```text
wsl --cd /mnt/d/Projects/CoPaw-cron-task-tracking env PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest tests/unit/scheduler/test_managed_cron_response_logging.py tests/unit/scheduler/test_managed_cron_clients.py tests/unit/scheduler/test_managed_cron_logging.py -q --tb=short
```

## 验证记录

- 初始 RED：12 failed、1 passed，其中 6 个真实客户端失败响应缺少 response_body，另外 6 个格式化用例因新方法尚未实现失败。
- 深度用例验证运行环境可接受较深 JSON，不能只依赖 RecursionError；复用已有 public_value 对层级、集合和字符串裁剪。另补 Cookie 文本回归，验证只在 JSON 序列化前脱敏，不能再次正则处理整个 JSON 而破坏日志结构。
- 最终新旧 HTTP/日志回归 41 passed；Scheduler 全目录 828 passed（55.92 秒），226 条现有 SQLite datetime adapter 弃用警告。
- 本次 3 个 Python 文件按 Git 元数据中的个人 pre-commit 配置运行，复杂度上限 15、mypy、Black、flake8、pylint 等均通过；没有把 hook 配置写入工作区。
- 主线程自检确认只新增失败 JSON 的 ERROR 日志，原 HTTPStatusError 和管理错误码保留，成功响应不记录正文；没有修改 SWE、前端、配置、表结构或数据库状态。
- 未部署行内、未定位实际 422 根因；SWE 返回固定 detail 时，本日志仍只反映该固定响应，不代替底层异常。当前保留本地修改，未提交、推送或继续打包。

## Task 2：补充 SWE 资源准备异常日志

**Files:** src/swe/app/crons/managed_api.py、managed_diagnostics.py、tests/unit/routers/test_managed_cron_resource_logging.py。

**Goal:** 资源准备抛异常、被转换成固定 422 前，记录 ERROR、operation_id、job_id、stage=prepare、异常类型与有限异常链；诊断中补充安全错误码及 Pydantic 校验类型/字段，不输出 input、context、SQL 或认证信息。

- [x] RED：真实 API、数据库和 ResourcePreparer，模型校验失败仍返回原 422，但必须记录底层错误码、异常链和身份；非法 Cron 必须记录校验类型及 schedule.cron，并且不泄漏测试输入。预期原实现缺少 ERROR 日志而失败。
- [x] GREEN：在现有异常转换前增加 logger.error，沿用 format_storage_error；诊断只增加安全代码和 Pydantic 字段定位，不使用会泄漏输入的 logger.exception。
- [x] 验证：运行 SWE 资源接口/日志与 managed 执行关联测试，再运行 Scheduler 全目录；对本次 3 个 Python 文件执行现有静态/复杂度检查，保留其他未提交修改。
- [x] 主线程自检确认响应体、HTTP 状态、事务回滚及身份校验不变；不修改生产数据库，不提交或打包。

### Task 2 验证结果

- RED：新增 2 个真实接口用例失败，原 8 个用例通过；失败证据为固定 422 正常返回，但操作 ID、底层错误码及校验字段均没有 ERROR 日志。
- 资源接口、资源日志及准备服务定向回归：22 passed。
- 最终 Scheduler 全目录与 SWE managed 执行、资源及接口相关范围合并运行：939 passed（76.83 秒），231 条 SQLite datetime adapter 弃用警告。
- 本次 3 个 Python 文件使用现有 Git 元数据内 pre-commit 配置复跑，全数通过，包括复杂度上限 15、mypy、Black、flake8、pylint；git diff --check 通过。
- 日志只在资源准备的原异常转换处增加，保留原 HTTPException 透传与固定 422 响应；安全诊断增加有限错误码和校验位置，不输出校验 input/context、认证数据、原始 SQL 或自由文本异常消息。
- 没有部署行内或判断实际故障根因；未提交、推送或打包，既有 Scheduler 失败响应日志及自动重试告警抑制修改保留。

# SWE Cron Storage Error Logging Implementation Plan

> **For agentic workers:** 按用户 AGENTS 约定在主线程顺序执行，使用 executing-plans 与 TDD；不启动子代理。自查不冒充独立评审。

**Goal:** 在 SWE 返回 cron_resource_storage_unavailable 前打印可定位且不含业务输入的底层异常诊断。

**Architecture:** 保留 _locked_operation 的事务、锁、权限复核和 HTTP 错误语义，仅记录阶段并在已有异常分支输出诊断。小型本地格式化函数只保留异常链类型、数据库错误号/类别、安全的缺失属性名及栈位置，不引入 SWE 对 Scheduler 包的运行依赖。

**Tech Stack:** Python logging/traceback、FastAPI、pytest、httpx ASGITransport、现有 SQLite 测试适配。

**Spec:** 用户授权补充 SWE 底层异常日志；Scheduler 已记录远端 503 和统一错误码，无法还原 SWE 未返回的原始异常。

## Global Constraints

- 不修改 HTTP 状态码或 detail，不更改事务、行锁、资源准备或重试行为。
- 不记录原始 SQL、任务正文、请求 Header、密钥、源码行或局部变量。
- 保留工作区现有修改，不提交、不推送、不更新行内包，不访问业务数据库。
- GitNexus 对 _locked_operation 的上游影响为 LOW：prepare_resources、cleanup_resources 两个直接调用，1 个模块，无索引流程。

### Task 1: 复现日志缺口并增加最小诊断

**Files:**
- Create: src/swe/app/crons/managed_diagnostics.py
- Modify: src/swe/app/crons/managed_api.py 的 _locked_operation
- Create: tests/unit/routers/test_managed_cron_resource_logging.py

**Interfaces:** format_storage_error(error: Exception) -> str；仅为本地日志格式化，不更改接口契约。

- [ ] 使用真实 ASGI prepare 路由与现有 ManagementDatabase、ResourcePreparer，分别在事务开始、FOR UPDATE、权限复核后返回、事务结束的数据库边界注入异常。
- [ ] 请求的断言保持 `status_code == 503` 和 `json() == {"detail": "cron_resource_storage_unavailable"}`；日志断言包含 operation_id、job_id、阶段、OperationalError、1205/1213、原调用位置且不含 private-input/private-token。
- [ ] 缺失 transaction 方法时检查 AttributeError 与 missing_attribute=transaction；HTTPException 原样保留，不误记为存储失败。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/unit/routers/test_managed_cron_resource_logging.py -q`，先确认由于缺日志而失败。
- [ ] 新增安全格式化：最多 8 层异常链、每层末尾 12 个栈位置；数据库号映射到固定类别，未知消息不输出。
- [ ] 在事务开始前设 stage=begin_transaction，加锁前设 lock_job，复核前设 validate_operation，yield 前设 resource_action，正常返回后设 finish_transaction；已有通用异常分支增加 ERROR 日志后仍抛同一个 HTTPException。
- [ ] 重跑定向测试，再运行资源准备、资源接口、执行接口和 Scheduler 回归。
- [ ] 运行定向 pre-commit、复杂度和 git diff --check；主线程逐行自查新增代码。不执行 commit，等待用户后续授权。

部署验证：更新并重启 SWE 后，用原用户 Header 调用 Scheduler 原 operation 的 retry，收集 SWE 中同 operation_id 的新增日志。本地测试不证明预发布的数据库故障已经修复。

# Scheduler 准备失败日志修复计划

**目标**：让 Scheduler 业务日志按 SCHEDULER_LOG_LEVEL 输出，并能从 swe_prepare_failed 定位 HTTP 状态、SWE 安全错误码、异常链和调用栈；不改变任何创建、重试、事务、接口响应或超时行为。

**执行方式**：使用 writing-plans 与 TDD。按用户 AGENTS 约定在主线程顺序执行，不启动子代理；主线程自查不冒充独立评审。不提交、不推送，保留现有三个本地门禁文件和系统迁移草案。

**技术**：Python logging、httpx、pytest、现有 SQLite 管理服务测试适配。

## 任务一 日志回归测试

- 修改 tests/unit/scheduler/test_managed_cron_logging.py：通过真实管理服务和 HTTP 边界替身模拟 prepare 返回 422、读取超时、带私密文本的异常链；断言日志包含操作、任务、阶段、异常类型、状态/安全详情及栈位置，响应仍为 swe_prepare_failed，未调用平台。
- 新增 tests/unit/scheduler/test_scheduler_logging.py：隔离子进程模拟 Uvicorn 默认配置及直接导入 app；验证业务 INFO 可见、重复配置不重复输出、无效级别回退、根及其他库日志级别不被修改。
- 先运行上述测试，确认因缺日志或缺初始化失败，再实现。

## 任务二 最小实现

- 新增 scheduler/src/scheduler/logging.py 的 configure_logging()：仅设置 scheduler 命名空间，缺 handler 时补 stdout handler；保留已有日志配置，重复调用不增 handler。
- 在 scheduler/src/scheduler/app/_app.py 导入阶段初始化，覆盖现有 main、CLI 和直接 uvicorn 三种正常入口。
- 新增 scheduler/src/scheduler/app/services/cron/managed/diagnostics.py：异常链只输出类型、栈位置和明确安全的机器码/HTTP 信息。未知原始异常消息、响应正文、请求 Header、查询串、源码行与局部变量不输出，避免验证错误夹带任务正文或密钥。
- 在 ManagementService.retry 的已有异常分支记录诊断，再执行原 checkpoint。HTTP 响应仍不暴露内部详情。
- GitNexus 对 retry 的上游影响为 HIGH（12 个受影响符号）；只加日志调用，不修改控制流。app 的新增配置调用不改生命周期行为。

## 任务三 验证与交付

- 运行定向测试、管理/客户端/启动相关回归、复杂度及定向 pre-commit；检查无业务代码范围外修改。
- 命令示例：PYTHONPATH=src;scheduler/src，使用 .venv/Scripts/python.exe -m pytest tests/unit/scheduler/test_managed_cron_logging.py tests/unit/scheduler/test_scheduler_logging.py -q。
- 不连接业务库或平台，不执行 DDL，不把日志修复当作创建故障已经修复。
- 以同一 operation_id 续办一次后查看新增日志；用户提供实际错误后再诊断创建失败。SWE 内部异常没有经 HTTP 返回时，Scheduler 不可能凭空还原其远端 Python 堆栈。

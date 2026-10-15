# 一次提交全量快照迁移验证记录

日期：2026-10-15，分支 `v1.0.0`，基线 `57d67c8ed7ad37f3df9293c9842b827c03372f2a`。本记录仅针对异步全量增量，前一次实现记录另见同目录 phase1 验证记录。

## 测试过程

1. 先新增后台任务测试，运行时因 MigrationTaskRequest 尚不存在而收集失败；实现契约与编排后 6 项通过。
2. 先新增 HTTP 测试，运行时 5 项均因新路由不存在返回 404；接入后 11 项（后台 6 + HTTP 5）通过。
3. 补充认证、取消、超时、提交失败释放配额等边界。核对 source_missing 汇总与逐页状态不一致的测试先失败，再统一页标记与汇总标记后通过。
4. 当前新增后台/HTTP 测试 16 项通过。两项新 MySQL 集成测试默认跳过，等待显式隔离库配置，不访问默认业务库、不执行 DDL。

## 自检范围与证据边界

- 一次提交消费多页，包括空页、verify 的数据库阶段；通过真实临时 jobs.json → MigrationService → MigrationRepository → 进度存储 → HTTP 查询链路验证，SQL 使用事务 fake，不冒充真实 MySQL。
- 累计计数、内存缺失 OR 汇总、逐页结果有界读取、跨应用实例查询、幂等重跑和文件内容/时间戳不变。
- 既有全局登录及内部认证同时启用时分别校验；非法路径字段、游标、任务 ID、参数上限、缺表、不存在任务、并发配额均覆盖。
- 当前批处理结束后停止；开始前停止不扫描；worker 被取消或单页超时不返回成功；DB 进度保存失败回滚该页进度，不把已完成业务提交伪装为全失败/全回滚。
- 心跳过期只作疑似中断，不会自动另起 worker。重新提交是新的安全快照，不是恢复一个持久队列，不承诺重启续跑。
- 两张业务表之外只写既有异步进度表。未改业务保存、旧表同步、Scheduler/Monitor 运行链路或系统任务。
- 主线程自检不是独立评审；遵循用户不派发子代理的要求，未完成独立评审。没有生产发布、真实数据迁移、建表、提交或推送。

## 部署验证

预发布由上线操作者按 [Postman 说明](../../deploy/cron-phase1-snapshot-migration.md) 先小范围 preview → import → 重跑 import → verify，再不带筛选全量执行。观察任务终态、累计原因和内存缺失，而不是只看 202。原任务 CRUD、原普通/批执行应继续原通道。

失败信号：持续 5xx/429、进度长时间无心跳、来源变动导致任务失败、非预期业务表写入、旧任务行为改变。停止新提交并请求活动任务 stop，保留 task_id/诊断/源文件基线，确认停止后再回退应用；不删表或回写旧文件撤销。

## 最终命令结果

- Windows 项目 `.venv/Scripts/python.exe -m pytest`：覆盖第一阶段来源、Repository、Service、后台任务、原 HTTP、新 HTTP、两组 opt-in MySQL 测试，结果 **73 passed、5 skipped**；5 项跳过为 Windows 创建符号链接条件不足 1 项、未配置隔离 MySQL 4 项。另有既有 Starlette/TestClient httpx 弃用提示 1 项。
- WSL 项目 `venv/bin/python -m pytest`：上述八个测试文件，加 `test_cron_json_repo.py`、`test_external_cron_scope_refresh.py`、`test_internal_tenant_scope.py`、`test_tenant_workspace.py`、`test_tenant_identity.py`，结果 **187 passed、39 skipped、2 deselected（32.57 秒）**。
- 两项明确排除的旧失败沿用上次记录：`test_internal_batch_initialize_tenants`（原测试断言 INSERT、现有实现 INSERT IGNORE），`test_cron_manager_system_jobs_do_not_register_cleanup`（当前 Dream 配置注册任务，断言不应注册任何任务）。未声称全仓测试全部通过。
- 增量六个 Python 文件通过 Black（line-length 79、target py310）、Flake8（max-complexity 15，未额外关闭 E501/C901）；六个迁移生产模块通过 Mypy，沿用仓库阶段一命令的 `ignore-missing-imports`、`follow-imports=skip`、`explicit-package-bases` 和既有错误码豁免，不等于严格模式全仓类型检查。
- `git diff HEAD --check` 通过，只有既有 Windows LF/CRLF 提示。保留了之前的 staged/working-tree 差异，未运行 git add/reset/commit/push；未来提交前需要按最终工作区重新核对目标路径。
- GitNexus 已刷新；新后台编排相关上游影响为 LOW，修改模型仅新增契约和调整说明文字，旧模型校验规则未改变。索引不是运行时证明；真实 MySQL、部署/网关和业务库迁移仍由上线验证补充。

# 第一阶段快照迁移验证记录

日期：2026-10-15。代码基线：`v1.0.0` / `57d67c8ed7ad37f3df9293c9842b827c03372f2a`。以下保留验证方案，实际执行结果在末尾单列；不能把未执行的 MySQL 验证当作通过。

## 测试先行检查点

使用仓库配置的项目虚拟环境。Windows 当前可用路径为 `.venv/Scripts/python.exe`；若在仓库 Linux 环境执行则使用对应项目虚拟环境，不改用未确认的全局 Python。

每个单元先运行针对性测试，预期因该单元模块/行为尚不存在而失败；补足实现后，同一测试集合应通过。导入依赖缺失、测试收集崩溃不算有效的业务失败测试，需要单独解决或报告环境限制。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/app/test_cron_migration_source.py -q
.venv/Scripts/python.exe -m pytest tests/unit/app/test_cron_migration_repository.py -q
.venv/Scripts/python.exe -m pytest tests/unit/app/test_cron_migration_service.py tests/unit/routers/test_cron_migration_api.py -q
```

完成后运行新增测试和相邻回归：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/app/test_cron_migration_source.py tests/unit/app/test_cron_migration_repository.py tests/unit/app/test_cron_migration_service.py tests/unit/routers/test_cron_migration_api.py tests/unit/app/test_cron_json_repo.py tests/unit/app/test_external_cron_scope_refresh.py tests/unit/routers/test_internal_tenant_scope.py -q
```

## MySQL 集成检查点

```powershell
.venv/Scripts/python.exe -m pytest tests/integrated/test_cron_migration_mysql.py -q
```

只允许显式提供的隔离测试数据库和测试表；不能读应用默认生产凭据或自动选择业务库。测试环境与清理范围需要在执行前核对，未提供隔离环境则跳过并明确报告。手工创建的业务表及业务迁移不由测试命令执行。

需要实证：同连接事务回滚、两表原子性、同身份首次插入竞争、过期快照保护、JSON 字段以及时间转换。普通 fake 单元测试不作为真实 MySQL 锁行为的证据。

## 评审和交付检查点

- U1：文件和内存只读、身份及缺失映射通过后再接 Repository。
- U2：两表事务、重复写保护和 DML 范围通过后再接路由。
- U3：认证、游标和无 Workspace 启动副作用通过后再编写上线步骤。
- U4：Postman 步骤与真实契约一致，明确哪些状态没有取得。
- 检查只修改计划目标，不引入自动 DDL、日常双写、旧表写入或平台调用。
- 检查独立系统任务不导入、不改配置/平台绑定/运行状态；用户文件中的自动生成业务任务不被来源标记误过滤。
- 用户未授权提交、推送，检查点不自动执行 Git 提交。
- 主线程自查不计作独立评审；当前用户指令不允许派发子代理，独立评审覆盖缺口保留在最终交付说明。

## 当前结果

已实现来源采集、两表事务 Repository、分页编排、三项运维 HTTP 接口及 Postman 上线说明。唯一修改的既有业务入口是 `src/swe/app/routers/__init__.py` 中新增迁移路由注册；旧 CRUD、执行器、旧表写入、系统任务及平台注册逻辑未改。

### 测试先行记录

- 新模块尚不存在时，来源、Repository、service/API 测试先失败，再补齐实现。
- 补充用例先复现并修正了目录身份冲突、超长绑定、MySQL 零影响行、请求观察时间、文件版本与任务定义版本混淆、丢失连接、重复任务 ID、无效时间数组等问题。
- 预检只读、正式导入、重复导入、核对及失去来源记录的第二阶段审计，均通过真实 FastAPI 路由、真实临时文件、新 service/Repository 和事务 SQL double 组合验证。
- 登录中间件与内部 Token 同时启用的行为有 HTTP 验证；用户 Header 不作为跨用户运维授权。

### 新增测试的最终 Windows 结果

```text
57 passed, 3 skipped, 1 warning
```

执行的是新增的来源、Repository、service、API 四个单元测试文件，以及显式隔离配置才运行的 MySQL 集成文件。3 个跳过项为本机无法创建实际符号链接的用例和未提供隔离库配置的 2 个 MySQL 用例；符号链接用例随后在 WSL 实际执行通过。warning 为现有 Starlette/TestClient 对 httpx 的弃用提示，未为此更改依赖。

### 相邻回归及 WSL 最终结果

范围包括新增测试、JSON 仓库、外部 Cron scope 兼容、internal tenant scope、租户工作区及身份中间件。使用项目 `venv/bin/python` 的 WSL/Linux 环境，明确剔除下面两处旧用例问题后：

```text
171 passed, 37 skipped, 2 deselected
```

37 个跳过项包含上述缺环境的真实 MySQL 用例及既有用例自身的跳过条件，不应计为已验证通过。剔除的不是新增迁移测试：

1. `tests/unit/routers/test_internal_tenant_scope.py::test_internal_batch_initialize_tenants`：旧断言要求 `INSERT INTO swe_async_task_items`，既有实现实际使用 `INSERT IGNORE INTO swe_async_task_items`；独立运行旧测试集合也出现此失败，未修改对应旧实现或断言。
2. `tests/unit/app/test_external_cron_scope_refresh.py::test_cron_manager_system_jobs_do_not_register_cleanup`：当前 WSL Dream 配置会正常注册 dream，旧用例用“不应有任何 add-job”作清理任务断言，因此失败；单独运行该旧用例可复现。本轮按用户要求不改系统任务。

初次 Windows 扩大回归曾因 `fcntl` 导入失败无法收集 internal tenant scope，并出现 3 个文件锁相关旧用例失败。随后在 WSL 执行了这些文件锁测试，不以 Windows 模拟 fcntl 方式伪造通过。没有运行全仓库测试套件。

### 静态检查及影响范围

- Black 检查：新增的 10 个 Python 文件通过，显式指定 py310 兼容目标、79 列；Windows 多文件格式化的临时错误通过逐文件格式化解决后重新检查。
- Flake8：新增 10 个 Python 文件通过，含 `max-complexity=15` 检查，未通过禁用 C901 掩盖复杂度。
- Mypy：5 个新生产模块按仓库现有忽略配置检查通过。
- `git diff HEAD --check` 通过；只出现既有 Windows LF/CRLF 提示。
- GitNexus 全量索引刷新成功。`detect-changes --scope all` 包含本会话原有设计文档和当前功能，共 23 个文件，影响执行流程 0，风险 LOW；图谱不是运行时证明，以上测试才是行为证据。

### 尚未完成的外部验证与交付边界

- 未连接、建表、修改或迁移用户业务数据库，未触发实际外部调度平台；真实 MySQL 事务、竞争插入和类型语义测试因隔离环境缺失未运行。
- 预发布由操作者按 `docs/deploy/cron-phase1-snapshot-migration.md` 使用 Postman 完成小范围预检、导入、重跑及完整核对，再逐步放大范围。
- 本轮按用户 AGENTS 在主线程顺序实现与核对，未派发子代理；多轮主线程检查不计作独立评审，独立评审覆盖仍有缺口。
- 未创建提交、推送或 PR。交付以当前工作区最终文件为准；仓库同时存在暂存快照与工作区差异，后续提交前需重新核对并只暂存所需目标文件，不能直接把较早暂存快照当作本轮最终代码。

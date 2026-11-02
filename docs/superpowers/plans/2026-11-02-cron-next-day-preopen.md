# 次日入口预开与过期触发关闭实施计划

> 2026-11-02 用户后续确认选项 1：取消执行回调的过期关闭动作，平台开关统一由维护接口处理。下文 Task 4 覆盖此前 Task 2 的过期回调关闭设计；此前验证记录仅代表变更前快照。

> **For agentic workers:** 在已有隔离 worktree 内按 executing-plans / TDD 主线程顺序执行；仓库指令禁止派发子代理，自检不冒充独立评审。

**Goal:** 外部平台每天北京时间 23 点调用维护接口，预开次日入口并保留当日有效入口；平台触发执行前未开始只跳过，过期跳过并关闭入口。

**Architecture:** 实际执行仍只按当前北京时间日期判断。平台维护单独判断区间是否覆盖今天或明天；回调过期后的平台关闭复用日期状态同步事务，外部 HTTP 放在提交之后。批共享入口仅由父任务控制。

**Tech Stack:** Python 3.10+、FastAPI、pytest、真实 SQLite SQL 与平台 HTTP 替身。

**Spec:** 用户确认的新日期流程；替代 docs/deployment/cron_enhancements_validation.md 原第 5 节的仅当天平台开关规则。

## 全局约束

- 复用 `POST /api/scheduler/cron/date-ranges/apply`，Body 仍只接收 source_id；不加用户 Header、内部定时器、DDL 或依赖。
- 每次维护都以当次北京时间今天/明天判断，调用时间由用户平台配置为 23 点。
- 今天仍有效不能按明天已过期提前关闭；未开始的回调不关闭已预开的入口。
- 不覆盖 enabled、人工/自动暂停原因或 batch_paused，不更改调度平台回调地址及旧接口。
- 关闭入口失败仍跳过业务，保留待同步标记并输出定位日志；不增加内部日期扫描或自动重试任务。
- 不停止已运行工作，不补跑错过的周期，不放宽 SWE 实际开始日期校验。
- 保留已有 5 个未提交修复文件及其暂存状态，不提交、推送、打包或修改生产数据库。

## Task 1：维护预开

**Files:** date_policy.py、date_ranges.py、tests/unit/scheduler/test_managed_cron_date_preopen.py。

补充统一调用者：management.py、controls.py、dispatch_state.py、distribution_worker.py。源码检视发现这些已有开关动作会按旧规则把预开入口关回，因此只替换它们的平台日期条件，实际执行门禁不变。

**Interfaces:** 日期实际许可仍使用 `reason(row, now)`；仅平台维护使用 `platform_window_open(row, now)`，含今天或明天即允许。维护响应保留 evaluated_date，补充 preopen_date 说明预开目标日。

- [x] 先补 23 点维护测试：明天开始打开、今天结束不关、昨天过期关闭、后天开始仍关；普通和批模式分别覆盖。
- [x] 旧代码 2 个失败、6 个通过，失败均为次日应开启但实际关闭的真实平台边界断言。
- [x] 实现独立平台日期判断：`reason(row, now) is None or reason(row, now + timedelta(days=1)) is None`。
- [x] 覆盖人工/自动暂停、batch_paused 和无区间清空，确保既有意图不变。
- [x] 先补次日任务创建、预开后正文更新、启用、模式切换及分发的回归，再统一使用平台日期判断，防止 23 点后的操作关回入口。旧规则对应 7 个管理用例失败；清空旧调用记录后，两种模式切换均复现失败，再统一修正条件。

## Task 2：过期回调关闭

**Files:** date_ranges.py、scheduling.py、tests/unit/scheduler/test_managed_cron_date_preopen.py。

**Interfaces:** `DateMaintenanceService.close_expired(key, now)` 重新读取当前定义，仅处理过期且拥有入口的任务；批子任务不操作共享入口。回调受理之后只对 `job_outside_date_range` 结果尝试关闭；事务外调用平台，异常只记录，不改变 skipped 结果。

- [x] 先写普通/批过期回调关闭、未开始不关、有效日执行、关闭失败仍返回 skipped、批子任务不关闭共享入口测试。
- [x] 旧代码 6 个新用例失败、12 个通过；HTTP/SQL 为真实实现，仅替换平台网络边界。
- [x] 复用现有日期状态及旧表投影同步，不写业务执行或调用 SWE；未开始回调不触碰平台。
- [x] 平台客户端只在需要同步时构造，正常受理不增加配置依赖；测试验证 HTTP 不在数据库事务内。

自检补充：新增 tests/unit/scheduler/test_managed_cron_date_preopen_queue.py，模拟漏掉有效日维护且预开标记仍为 true；过期时无论回调或维护，都必须推进旧队列失效水位，延长日期不能复活旧请求。

保持范围：真正开始时的日期兜底由现有 SWE 校验保留；本次新增关停动作位于平台回调、入队之前，不新增内部日期扫描。若批散列的实际触发早于维护时刻，或提前到运行开始日之前，本次不补跑或放宽实际日期，部署指南明确该边界。

## Task 3：文档与验证

**Files:** scheduler/CONTEXT.md、managed_cron_date_ranges.py 的接口说明、docs/deployment/cron_enhancements_validation.md。

- [x] 按新流程更新维护规则与 Postman 验收，特别说明实际日期门禁不放宽、回调关闭失败检查日志和待同步标记。
- [x] 运行新日期回归及原维护/回调/三态测试，再运行 tests/unit/scheduler 全目录和 SWE managed 日期/执行范围。
- [x] 对新增或修改函数执行最大复杂度 15、mypy、Black、flake8、pylint 和 diff 检查。
- [x] 主线程自检接口兼容、父子入口归属、失败边界及测试覆盖；验证原 5 个文件 SHA256 与暂存内容保持不变。

测试统一用 WSL 项目环境，前缀：

```text
wsl --cd /mnt/d/Projects/CoPaw-cron-task-tracking env PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest
```

先运行 `tests/unit/scheduler/test_managed_cron_date_preopen.py -q --tb=line`，再运行日期/回调关联文件和 `tests/unit/scheduler -q --tb=line`。检查通过后保留本地修改，等待提交/打包指示。

## 最终验证记录（2026-11-02）

- 最新日期及相关回归：56 passed。
- 最新 Scheduler 全目录：814 passed，47.69 秒；226 条现有 SQLite datetime adapter 弃用警告。
- SWE managed 日期/执行关联范围：97 passed，28.29 秒；本次未修改 SWE 实际开始日期门禁。
- 最终 pre-commit 检查退出码 0：复杂度上限 15、mypy、Black、flake8、pylint 等全部通过；git diff --check 退出码 0。
- 原有 5 个未提交文件 SHA256 与任务开始快照一致；原先暂存的计划文件仍保持暂存，未新增暂存或提交。
- 主线程自检覆盖日期入口策略与实际执行许可分离、批共享入口归属、事务外 HTTP、关停失败不执行业务、管理动作不关回预开入口、过期普通队列水位；未使用子代理，不冒充独立评审。
- GitNexus 当前 worktree 未注册，无法完成图谱影响查询；使用当前源码检索与测试核对，未修改或复用其他工作区索引。
- 未连接行内真实 MySQL 或外部平台；平台注册启停、跨日触发和通知仍须按部署指南在预发布验收。无前端变更，不需本轮前端构建或浏览器验证。
- 按当前授权仅保留 codex/cron-task-tracking 分支与本地修改，未提交、推送、打包或执行生产 SQL。

## Task 4：关闭入口统一交给维护接口（用户后续确认）

**Goal:** 每天北京时间 23 点维护预开明天入口、关闭昨天及更早到期入口，手动与平台执行仅按实际当天校验、不调用平台启停。

**Files:** scheduling.py、date_ranges.py、managed_callbacks.py、scheduler/CONTEXT.md、部署指南，以及三个现有日期回归测试文件。

**Interfaces:** 保持 apply(source_id) 同步接口及现有响应不变；删除仅供回调使用的 close_expired 和 expired_only 参数。保留维护到期时推进旧队列失效水位。未请求异步、并发提速或新配置，不擅自加入。

- [x] RED：将普通/批过期回调断言改为平台调用列表为空、任务状态未被回调维护；平台不可用时 HTTP 回调仍 202/skipped，手动日期外执行仍拒绝。运行日期回归，预期原回调关停导致新断言失败。
- [x] GREEN：删除 accept 后的过期关停调用、_close_expired_entry 及诊断导入；删除 DateMaintenanceService.close_expired、expired_only 分支及回调专用延迟平台构造，使维护直接使用传入平台客户端。
- [x] 同步测试：有效日不提前关闭、批子不误关共享入口使用真实维护接口验证；旧队列失效仅在维护后验证，移除已取消的回调水位要求。
- [x] 文档：注明过期入口可保持开启直到 23 点维护，但日期外业务不执行；手动拒绝和平台 skipped 语义保持不变。注明分来源、每页 200、逐任务事务、逐独立入口同步 HTTP、无全局完成时限，超时不代表未处理。
- [x] 验证：运行上述日期测试后运行 tests/unit/scheduler 全目录，相关 Python 文件按 pre-commit 配置逐项检查，最后 diff --check；核对原有 5 个文件内容，保留观测到的暂存状态。不提交、推送或打包。

RED/GREEN 命令：`wsl --cd /mnt/d/Projects/CoPaw-cron-task-tracking env PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest tests/unit/scheduler/test_managed_cron_date_preopen.py tests/unit/scheduler/test_managed_cron_date_preopen_queue.py tests/unit/scheduler/test_managed_cron_date_ranges.py -q --tb=short`。

### Task 4 验证记录

- RED 为 3 failed、38 passed：普通回调、批回调、平台不可用 HTTP 回调均复现了不应发生的平台关停调用。
- 改动后日期回归 43 passed；最终格式化后 Scheduler 全目录 815 passed，50.71 秒，226 条既有 SQLite datetime adapter 弃用警告。
- pre-commit 包装程序先遇到 Windows Git 路径问题，显式指定 Git 路径后又反复全量 diff，已停止该检查进程；未修改 Git 或 hook 配置。直接使用缓存的检查程序并读取原 pre-commit 配置参数：AST、docstring、编码、密钥、空白、add-trailing-comma、mypy、Black、flake8、pylint 全部退出 0；格式化后再执行一轮确认无文件改写。最大复杂度 15 单独检查退出 0。
- Black 提示当前 Python 3.12 与仓库 Python 3.13 目标的版本差异，但检查退出 0；未因此修改仓库目标版本。
- 原有 5 个文件 SHA256 全部与开始快照一致；没有执行 git add/reset/commit/push。期间观测到新增计划和两个测试文件已进入暂存区，其中测试文件有格式化后的未暂存差异，保留该暂存状态，未追溯或调整其来源。
- 主线程自检确认平台关闭只在维护链，实际执行日期限制不放宽；未连接行内平台或真实 MySQL，未测量实际维护调用总时长，也未改为异步或增加并发。

---
title: 定时任务执行记录 is_final 详细设计
type: feat
status: implemented
date: 2026-10-14
---

# 定时任务执行记录 is_final 详细设计

## 1. 目标和已确认约束

在 `swe_cron_executions` 增加一个字段 `is_final`，保留每次执行记录，同时标识当前有效的最新结果。普通任务每条记录默认有效；批调度同一子任务的新执行结果落库时，替换上一条最新记录的标记。

本设计以本次对话中用户的决定为准：

| 编号 | 要求 |
|---|---|
| R1 | 只增加 `is_final` 一个业务字段，数据库默认值为 `1`。 |
| R2 | 历史记录全部视为 `is_final = 1`，不做按任务分组的历史清洗。 |
| R3 | 普通任务新执行记录插入后为 `1`，不覆盖同任务的其他普通执行记录。 |
| R4 | 批调度自动重试和手动重试的新结果落库时，将同一子任务最新的一条旧记录改为 `0`，新记录为 `1`。 |
| R5 | 新结果尚未成功落库时，原记录保持原标记；修改旧记录与插入新记录必须原子提交。 |
| R6 | 重复上报不能重复插入；过期轮次不能覆盖新轮次标记；不同批次、子任务和租户互不影响。 |

交付范围为字段定义、写入链路、事务、兼容和验收设计。不修改成功率统计、查询筛选、Console、通知、异步任务结算或 trace 生成逻辑；不增加 `logical_run_id`、`final_status`，不补造派发失败的执行记录。

设计阶段仅编写文档。实施结果见本文末尾；本次不执行数据库 DDL，不提交或推送。

## 2. 字段语义

`is_final = 1` 表示该执行记录没有被同一批调度子任务的后续执行结果替代；`0` 表示已经被替代。它不表示任务成功，也不表示永远不再重试。

不根据 `status` 或 `async_status` 设置标记。失败结果也可以为 `1`；异步结果仍在变化时也可以为 `1`。取消、跳过等结果如经现有执行落库路径写入，也遵循相同替换规则。

替换时点是“新执行结果成功落库”，不是点击重试、重新入队、领取任务或执行开始。

### 2.1 普通任务

普通定时触发和普通手动执行每次插入均为 `1`。相同 `job_id` 的下一次定时触发或一次新的手动执行不会将上次普通执行改为 `0`。

### 2.2 新产生的批调度执行链

| 时点 | 第 1 轮 | 第 2 轮 | 第 3 轮 |
|---|---|---|---|
| 第 1 轮失败结果落库 | 1 | 无 | 无 |
| 自动或手动重试入队、执行中 | 1 | 无 | 无 |
| 第 2 轮失败结果落库 | 0 | 1 | 无 |
| 第 3 轮成功结果落库 | 0 | 0 | 1 |

没有新记录时，不清除旧标记。因此重试未派发、执行崩溃或上报最终失败都不会使旧记录消失。

### 2.3 历史兼容的明确例外

上线加列时，所有历史记录均为 `1`，即使同一子任务历史上有多个重试轮次。本设计不承诺这些历史分组立即只有一条 `1`。

例如上线前已有轮次 1、2、3，加列后为 `[1, 1, 1]`。上线后轮次 4 的新结果落库时，只替换最新的轮次 3，得到 `[1, 1, 0, 1]`。以后轮次 5 落库得到 `[1, 1, 0, 0, 1]`。历史轮次 1、2 不被顺带清洗。

不能使用“按分组将所有 `is_final = 1` 更新为 `0`”的实现，因为那会改变用户明确要求保留的历史语义。

## 3. 当前源码链路

### 3.1 写入归属

| 类型 | 当前链路 | 本次处理 |
|---|---|---|
| 普通任务 | SWE `MonitorSyncClient.record_execution()` → Monitor `/sync/execution` → `SyncService.record_execution()` | 沿用现有 INSERT，由数据库默认写入 `1`。 |
| 批调度任务 | SWE 识别完整 `cron_dispatch` 身份 → Scheduler `/scheduler/cron/execution` → `ExecutionSyncService.record_execution()` | 增加同一子任务记录替换事务。 |

源码参考：

- `src/swe/app/crons/monitor_sync_client.py`：完整批调度身份走 Scheduler，并对同一结果进行有界上报重试；普通记录走 Monitor。
- `scheduler/src/scheduler/app/routers/cron.py`：现在在事务外依次查已有记录、校验回报、插入记录。
- `scheduler/src/scheduler/app/services/cron/execution_sync_service.py`：INSERT 包含 `dispatch_intent_id`、`dispatch_batch_id`、`dispatch_attempt`；当前按三元组查询重复记录。
- `scheduler/src/scheduler/app/services/cron/scheduling_service.py`：`handle_execution_recorded()` 规范化反馈身份，然后调用 intent store 校验。
- `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`：校验 batch/job/tenant/source 和当前 attempt；领取时递增 attempt，手动重试仍复用原 intent。
- `scheduler/src/scheduler/app/database/connection.py`：连接池使用 `autocommit=True`；只使用 `db.acquire()` 并不构成事务。
- `monitor/src/monitor/app/services/cron/sync_service.py`：普通记录 INSERT 未指定新增字段，可直接使用默认值。

### 3.2 手工加列的最终交付方式

Scheduler 与 Monitor 原有 CREATE/升级声明不做改动。本次通过 `scripts/sql/cron_execution_is_final.sql` 手工加列，现有库和新建库均需先执行该 SQL，再发布写入代码。脚本仅运行一次，不包含历史清洗，也不在应用启动时重复执行。

## 4. 表结构设计

字段定义为 `TINYINT(1) NOT NULL DEFAULT 1`，使用 `0/1`，建议放在 `dispatch_attempt` 之后。

以下 DDL 是字段契约示例，不由本次工作连接数据库执行：

```sql
ALTER TABLE swe_cron_executions
    ADD COLUMN is_final TINYINT(1) NOT NULL DEFAULT 1
    COMMENT '是否为当前有效执行记录：1有效，0已被后续执行替代'
    AFTER dispatch_attempt;
```

加列后历史行读取为 `1`，不追加历史 UPDATE。已有列情况下重复启动不得重置任何已经为 `0` 的记录。

复用 `idx_cron_execution_dispatch(dispatch_intent_id, dispatch_batch_id, dispatch_attempt)`，本次不新增索引或唯一约束。历史记录可能重复或多条为 `1`，不能为了本字段引入“每组只允许一条最终记录”的数据库唯一约束。

`is_final` 由服务端和数据库维护，不新增到 SWE 的上报请求字段中，不接受调用方决定最终标记。现有 `ExecutionSyncRequest` 和 `RecordExecutionResponse` 的外部协议不需要变化。执行查询响应是否显式暴露字段另行处理，本次不扩展查询模型或前端。

## 5. 批调度身份与最新记录规则

### 5.1 同一子任务

用 `dispatch_intent_id + dispatch_batch_id` 归并，不用 `job_id`、`trace_id` 或 session_id 归并。`job_id` 可以跨批次重复，trace_id 每轮变化。

在写入前必须沿用当前反馈校验，核对 intent 对应的 batch/job/tenant/source。字段解析和来源优先级保持现有 `_build_execution_feedback_context()` 的规则，不自行发明不同的 source/tenant 默认值。

数据库中的执行记录及请求外层 job/tenant 应与已验证的 intent 身份一致；实现时不得因只检查 meta 而把错租户记录参与替换。遇到不一致应拒绝写入，不修改标记。

### 5.2 同一执行轮次

`dispatch_intent_id + dispatch_batch_id + dispatch_attempt` 是现有重复上报识别键。重试 HTTP 上报属于同一轮次，不能创建新记录或重新翻转标记。

请求省略 attempt 时，保留现有接口解析为第 1 轮的兼容行为；这个默认值必须同样参与事务内校验，不能使缺省 attempt 绕过校验、被当成当前任意轮次。

### 5.3 最新旧记录

在同一子任务内，按 `dispatch_attempt DESC, id DESC` 选最新旧记录，最多更新这一条。轮次作为主顺序，id 作为同轮次重复历史记录的稳定排序。不要使用上报时间或实际执行时间来决定谁覆盖谁。

若当前请求轮次已有记录，返回既有 id，不进行替换。若没有同轮次记录，但库中已有更高轮次记录，拒绝请求，不允许降低最新轮次。

历史数据缺少可用批调度身份的记录不做猜测归并；无匹配旧记录时，直接插入新记录为 `1`。

## 6. 落库事务与职责

### 6.1 事务所有者

由 Scheduler `ExecutionSyncService` 提供批调度专用的原子写入入口，负责“锁定 intent → 权威校验 → 查重复 → 修改旧标记 → 插入新记录 → 提交”。现有字段截断、时间格式、通知和读标记 INSERT 逻辑继续复用，不复制整套 SQL。

Router 保留元数据缺失时 `recorded=False` 的现有行为，以及现有错误响应形式；有效批调度请求委托原子入口，成功响应只能在事务提交后返回。

现有 Router → SchedulingService → intent store 的反馈校验可用于事务前的初步校验和诊断，但不能作为最终写入依据。原子入口必须基于同一连接上已锁定的 intent 重新校验身份与 attempt。优先抽出或复用现有行校验规则，避免形成两套不同语义；具体 helper 名称在实现时确定。

重复查询必须移入事务。不能沿用 Router 的事务外查询结果决定是否插入；原有查询方法若仍有其他调用可以保留，但不能绕过原子入口新增批调度执行记录。

### 6.2 原子流程

下面是用于评审的流程示意，不是要求照抄的实现代码：

1. 解析请求及反馈身份，取得连接，显式 `begin()`。
2. `SELECT ... FOR UPDATE` 锁定对应 intent 行，读取 batch/job/tenant/source/attempt_count。
3. 校验 intent 存在、身份一致、请求 attempt 等于当前 attempt_count；否则回滚并沿用现有拒绝反馈的错误处理。
4. 用同一连接查询本子任务执行记录，采用锁定读；若本轮已有记录，结束事务并返回已有 id，不修改任何执行字段。
5. 查询最新旧记录；若已有更高轮次，回滚并拒绝；若为较低轮次，则只按其主键 id 将 `is_final` 设置为 `0`。
6. 插入本次执行记录，`is_final = 1`，读取新 execution_id。
7. `commit()` 完成后返回成功。任何 SQL 异常、取消或提交失败都走事务清理并传播错误。

无旧记录时跳过第 5 步的更新。旧记录原本为 `0` 时更新可幂等执行，不以 UPDATE 影响行数为零判定业务失败。

事务中所有读取和更新必须使用同一个 conn/cursor。不得通过会重新获取连接的 `db.fetch_one()` 或 `db.execute()` 做权威校验与更新。若需要记录 mismatch 诊断事件，放到回滚之后，不让独立事件写入持有或等待同一 intent 的锁。

### 6.3 并发与锁顺序

同一 intent 行是所有批调度回报的串行化点，即使首轮没有 execution 记录，也能锁住稳定存在的实体。单进程 asyncio.Lock 不能覆盖 Kubernetes 多实例。

反馈事务锁顺序为 `intent → execution`，不在事务内访问 batch control、重新领取任务、修改 batch 汇总或执行通知。这样不增加与现有手动操作“control/batch → intent”相反的锁依赖。

同轮次并发上报时，后进入者必须在 intent 锁释放后再查 execution，看到前者已提交记录并直接复用。新旧轮次交错时，以锁定 intent 中的当前 attempt 为准。旧轮次迟到沿用当前 attempt mismatch 拒绝行为，不补写迟到记录，也不改变最新标记。

数据库事务异常和不确定的提交结果由现有 SWE 上报重试处理：如果第一次实际提交成功，重试命中既有执行记录；如果未提交，旧标记不变，重试可重新完成写入。此逻辑不扩展通知重试。

## 7. 自动、手动与异步路径

### 7.1 自动重试

继续沿用 intent 状态重入队及领取时 attempt 递增的现有逻辑。失败结果已经写入时保持 `1`，下轮结果进入同一 Scheduler 原子入口后再替换为 `0`。

### 7.2 手动重试

`scheduler/src/scheduler/app/services/cron/batch_operations.py` 的 `retry_failed()` / `_retry_candidate()` 继续操作原 intent，不修改 is_final。点击后无新结果，不清除旧标记。新结果到达后与自动重试走相同入口，不增加第二套标记维护代码。

普通任务“手动执行”与批调度“重试失败子任务”不同：前者没有同一批调度子任务的归并关系，每条普通记录仍为 `1`。判断归属依赖批调度身份和落库路径，不依赖 is_manual。

### 7.3 异步回写

现有对一条执行记录的 async_status、通知状态、已读状态等更新，不创建新轮次，因此不改变 is_final。异步结果晚到时只更新它所对应的 execution 记录，不把被替代的旧记录重新置为 `1`。

### 7.4 写入归属边界

标准 SWE 路径的完整批调度回报只发往 Scheduler；普通回报发往 Monitor。Monitor INSERT 保持默认 `1`。本次不新增跨服务 Python 公共事务层。

保证范围是当前标准写入链路；不能让旧版或自定义调用者绕过 Scheduler，直接向 Monitor 写完整批调度重试结果。实施与部署时确认 SWE 路由保持现有规则，相关现有测试应覆盖这一点。脚本直接插入 execution 默认得到 `1`，不自动替换其他记录。

## 8. 文件和实施单元

### U1. 手工加列 SQL

**目标 / 要求：** R1、R2。手工加列后数据库字段为 `NOT NULL DEFAULT 1`，历史行读取为 1，不按重试分组清洗。

**文件：** `scripts/sql/cron_execution_is_final.sql`、`tests/unit/scheduler/test_cron_execution_is_final.py`。

**方法：** 保留原 Scheduler/Monitor schema 源码，提供单次执行的加列 SQL。没有自动升级步骤；部署人员先在目标库应用 SQL，再发布写入代码。

**依赖：** 无。

**验证场景：** SQL 字段定义和默认值正确；历史行及普通新插入均为 1；既有 0 不会被重置；不含历史分组 UPDATE。MySQL 集成测试先建原始表，再实际应用该手工 SQL；无测试库时跳过。

### U2. 批调度原子替换与接线

**目标 / 要求：** R4、R5、R6。

**文件：**

- `scheduler/src/scheduler/app/services/cron/execution_sync_service.py`
- `scheduler/src/scheduler/app/routers/cron.py`
- 按复用校验需要局部修改 `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`；若需复用反馈规范化，局部调整 `scheduler/src/scheduler/app/services/cron/scheduling_service.py`。
- 新增 `tests/unit/scheduler/test_cron_execution_is_final.py`。
- 更新 `tests/unit/scheduler/test_cron_dispatch_intent_service.py` 和 `tests/unit/scheduler/test_cron_execution_reconciliation.py` 中有关写入职责的测试。

**方法：** 用锁定 intent 串行化，同连接执行校验、查重复、单条旧记录更新与 INSERT；保留现有回报响应、诊断和后续扫描结算行为。只为本次设计抽取必要的小型 helper，控制复杂度和参数数量。

**依赖：** U1。

**执行要求：** 先用失败回归用例描述新行为，再实现；涉及修改的函数在实施前逐个做 GitNexus impact，报告调用者、流程和风险。设计阶段没有修改这些符号，不要求用影响分析结果替代源码验证。

**验证场景：** 首轮插入；自动和手动重试替换；普通同任务多次执行全部为 1；同轮次重复上报不新增；同轮次并发不新增；错 tenant/source/batch/job 拒绝；过去或未来 attempt 拒绝；最新旧记录按轮次/id 选择；首次上报时无旧行；历史多个 1 只清除最新一条；UPDATE 后 INSERT 失败整体回滚；取消异常回滚；提交成功但响应丢失后重试不重复写入。

### U3. 链路回归、并发证明与交付说明

**目标 / 要求：** R3、R4、R5、R6。

**文件：**

- `tests/unit/app/test_monitor_sync_client.py`：复用完整批调度只走 Scheduler、普通只走 Monitor 的覆盖。
- `tests/unit/scheduler/test_batch_operations.py`：补充手动入队不改变执行标记的回归。
- `monitor/tests/test_sync_api.py`：普通写入默认值兼容回归，必要时补充普通落库测试。
- 新增 `tests/integrated/test_cron_execution_is_final.py`：使用隔离的 MySQL 测试库验证事务和两连接竞争。
- 本设计文档及部署 SQL 说明。

**依赖：** U1、U2。

**验证场景：** 两连接同轮次并发提交只产生一条新记录；领取下一轮与旧轮次回报竞争时只接受锁定时的当前轮次；更新后插入失败时旧记录仍为 1；各子任务和批次互不影响；同一行 async_status 更新不改变标记。

**验证边界：** 单元 mock 可以证明 SQL 调用顺序，但不能证明 MySQL 行锁及事务隔离；SQLite 不作为 SELECT FOR UPDATE 语义的验收证据。无隔离 MySQL 测试库时，交付必须明确并发验收未运行，不在生产库试验。

## 9. 部署、回退和限制

1. 先在目标库手工执行加列 SQL，历史全部为 1。Scheduler/Monitor 原有 schema 定义不变，新建库也需要手工加列。
2. 发布 Scheduler 原子写入逻辑。普通 Monitor 写入不需要传新字段，SWE 外部请求协议保持兼容。
3. 切换 Scheduler 写入代码时避免同一子任务由旧、新写入实现并行处理；可短暂停止 Scheduler 接收新反馈并待旧请求结束，再切换全部实例。否则旧实现仍可能并发插入默认 1，绕过新锁。利用现有上报重试衔接短暂不可用，具体发布窗口在实施时确定，不新建调度开关。
4. 回退时优先回退业务代码，保留字段及已写出的 0/1。不要通过批量置 1 或 DROP COLUMN 作为常规回退步骤。

限制：历史同组多个 1 是已确认行为；没有完整批调度身份的旧记录不归并；派发失败但没有 execution 行时不补造记录；本次不更改统计接口或查询筛选。

## 10. 验收清单

| 场景 | 预期 |
|---|---|
| 新库由 Scheduler 或 Monitor 首次建表 | 手工执行加列 SQL 后字段默认 1 |
| 有历史数据的库加列 | 每条历史记录为 1，无回填清洗 |
| 普通任务执行两次 | 两条均为 1 |
| 批调度第 1 轮首次结果 | 一条新记录为 1 |
| 点击手动重试但尚无结果 | 旧标记不变 |
| 新自动或手动重试结果落库 | 最新旧记录为 0，新记录为 1 |
| 新结果插入失败 | 不新增记录，旧标记保持原值 |
| 同轮次串行或并发重复上报 | 复用已提交 execution_id，不翻转标记 |
| 上报旧轮次或错租户/批次 | 拒绝，执行记录和标记不变 |
| 历史三轮全为 1，新增第 4 轮 | 结果为 1、1、0、1 |
| 新旧结果跨轮次、跨批次交错 | 不覆盖其他批次或最新轮次 |
| async_status / 通知 / 已读回写 | is_final 不变 |

实施时始终使用项目虚拟环境执行 pytest；聚焦本字段及批调度写入回归，不运行与本次无关的全量检查。当前工作区已有其他修改，实施时保留并基于现有源码衔接，不重置或提交无关文件。

## 11. 设计校核记录

主线程按一致性、可实施性和并发失效场景顺序校核，不宣称已进行独立代理评审。关键检查包括历史全 1 与单条替换一致、事务内权威校验、同轮次并发重复、失败回滚、旧轮次迟到，以及手工加列部署前提。

设计未连接数据库，未验证 DDL 实际执行或 MySQL 并发行为。这些属于 U1—U3 的实施验收，不作为设计已验证结果。


## 12. 实施交付

代码在独立 worktree 的 `codex/cron-execution-is-final` 分支，基于再次更新后的 v1.0.0 `4430b9239`。实际批调度事务入口为 `ExecutionSyncService.record_dispatch_execution()`；保留 Router 的初步校验，事务内基于锁定 intent 权威重验。最新旧记录还按 job/tenant 约束，避免误修改身份错误的历史数据。普通 Monitor 写入继续使用默认值，无上报模型扩展。

手工加列 SQL、写入代码和相关测试已实现；两个服务的 schema 源码没有 is_final 自动升级改动。提交前验证 201 项通过，3 项 MySQL 集成测试因未配置隔离测试库跳过；局部 flake8 与 diff 检查通过。未实际执行加列或证明 MySQL 并发行为。实施计划见 `docs/superpowers/plans/2026-10-14-cron-execution-is-final.md`，排查入口见 `analysis/playbook/cron-execution-final-marker.md`。

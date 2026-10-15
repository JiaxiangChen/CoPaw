---
title: feat 定时任务第一阶段快照迁移
type: feat
status: completed
date: 2026-10-15
origin: docs/superpowers/specs/2026-10-15-cron-phase1-snapshot-migration.md
---

# 定时任务第一阶段快照迁移实施计划

## Summary

提供独立的 SWE 运维 HTTP 能力，分批预检、导入及核对任务定义和可恢复状态。只写两张新表，支持可重复快照和缺失状态补迁，不切换日常路径。

---

## Problem Frame

现有定义分散在用户工作区文件中，运行状态一部分在 meta、一部分在各 SWE 实例已加载的内存中。为后续 Scheduler/SWE 新能力提供数据基础，必须先可控地导入、检查及重复补齐；入库并不证明新服务已接管，或所有运行内存已采齐。

用户已明确选择快照而非持续双写，并允许部分内存缺失后按用户路由补迁。数据库由用户手工建表，不能通过应用启动自动改表。

---

## Requirements

- R1. 只写 `swe_cron_jobs_v2` 和 `swe_cron_job_states`，不写旧定义、历史、队列、日志或平台注册数据。
- R2. 保留现有文件、内存、CRUD、查询及执行行为，不初始化 Workspace，不增加后台同步。
- R3. 覆盖服务可见的各 source 用户定义；支持筛选、分页和安全重跑。
- R4. 身份优先取定义中已有 tenant/source，缺失才解析 scope，使用完整任务身份，不截断或猜测。
- R5. 只迁入可恢复状态，明确区分缺字段与显式零值，支持在其他用户路由命中实例上补迁。
- R6. 两表单任务事务写入，保护 Scheduler 归属、已删除目标、较新状态及不同来源冲突。
- R7. 预检、导入、核对均能用 Postman 完成，不要求 Python 脚本或额外新表。
- R8. 目录边界、游标、敏感信息及现有认证不因迁移放宽。

---

## Scope Boundaries

- 不开发第二阶段的管理、分发、三态切换、新执行入口及兼容写入。
- 不修改 `swe_cron_executions`，不迁移历史，不用历史摘要重建完整定义。
- 不改变外部平台入口，不注册、暂停或删除定时器，不实现数据库触发调度器。
- 不增加自动持续同步、跨实例内存采集、Redis、独立迁移任务表或前端功能。
- 不迁移独立系统任务：心跳、梦境、source 级清理和归档等的配置、系统平台绑定及运行状态保持原状。用户 `jobs.json` 中的自动生成业务任务仍然迁移，不能仅凭 `job_origin=system` 排除。
- 不应用 DDL，不提交、推送或合入旧迁移 worktree 的代码。

### Deferred to Follow-Up Work

- 第二阶段：Scheduler/SWE 新管理和执行能力，按管理与执行分别写状态。
- 第三阶段：智拓切换调用 Scheduler。
- 跨实例全量状态采集及实例重启恢复不属于本快照导入功能。

---

## Context & Research

- `src/swe/app/routers/external_jobs.py` 已示范纯读取路径、本机已加载 manager 查找和不修复配置；新功能沿用边界，不调用它的 HTTP 接口。
- `src/swe/app/crons/models.py`、`task_view.py` 区分 `_states` 字段与 meta 中的未读、摘要、绑定等字段。
- `src/swe/config/utils.py` 的租户枚举要求 config 存在，不能直接作为所有任务文件的完整清单；直接调用配置加载还可能修复文件。
- `src/swe/app/multi_agent_manager.py` 有有限缓存与闲置卸载，内存缺失是正常情况，不能视为任务不存在。
- `src/swe/database/connection.py` 已有同连接事务；沿用参数化查询与任务级事务，不复制连接实现。
- `src/swe/app/routers/internal.py`、`src/swe/app/auth.py` 有内部可选 Token 和全局登录两层机制；不改变现有认证方法。
- GitNexus 索引已更新到当前基线；路由装配 `_build_router` 的上游影响为 LOW、1 个直接调用。`get_state` 影响为 MEDIUM，因此不改其默认空状态行为，只在新采集器识别实际存在的状态项。
- [MySQL 锁定读](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)说明锁定读需位于事务内；[重复键更新](https://dev.mysql.com/doc/refman/8.0/en/insert-on-duplicate.html)的行为不能替代业务归属和版本校验。

---

## Key Technical Decisions

| 决策 | 理由和取舍 |
| --- | --- |
| 独立迁移模块，不接入日常保存链路 | 保持快照范围，避免隐式双写及平台副作用 |
| 同步小批量请求、客户端继续游标 | 不额外要求任务表或后台任务持久化，适用于 Postman；不承诺一次长请求完成全库 |
| 文件只读、状态复制、不启动运行时 | 防止迁移本身改变准备、预取、Chat 或调度状态 |
| 定义快照保留最小来源指纹 | 区分合法手工重跑和同身份不同文件冲突；无需修改已交付表结构 |
| 只更新本迁移器持有的 legacy 导入行 | 防止覆盖新管理路径、手工数据或已删除任务 |
| 字段存在性驱动状态补丁 | 缺内存不清空已有值，显式零值仍能正常迁入 |

具体字段、来源优先级、未知状态和认证边界以 origin 设计为准。源文件不再包含某任务时只报告差异，不自动推断删除。

---

## Implementation Units

### U1. 来源发现与快照映射

**Goal:** 安全读取文件及当前实例实际状态，形成不带副作用的迁移候选。

**Requirements:** R2、R3、R4、R5、R8。**Dependencies:** 无。

**Files:**

- Create: `src/swe/app/crons/migration_models.py`
- Create: `src/swe/app/crons/migration_source.py`
- Test: `tests/unit/app/test_cron_migration_source.py`

**Approach:** 独立识别合法目录布局、完整身份及敏感字段；复制实际存在的状态，不复用空状态兜底。元数据映射保留字段存在性，迁移来源只存相对键/指纹。文件 I/O 与快照复制分离，限定单页工作量。

**Patterns to follow:** `external_jobs.py` 的只读路径校验、`scope_conversion.py` 的身份解析及 `task_view.py` 的字段来源。

**Execution note:** 先写来源/映射的失败测试，再实现。

**Test scenarios:**

- 多 source、多用户、多 Agent，包括可识别旧布局与缺 config 的有效任务文件，合法记录均能发现。
- 独立心跳、梦境、source 清理/归档及 `system_jobs.json` 不生成迁移记录；自动生成但持久化在用户 `jobs.json` 中的业务任务仍被采集，来源标记不被误当成系统执行类型。
- 文件已有 tenant/source 时不调用解码；缺失时正确解析；矛盾、超长或无效身份单独失败。
- text、agent、workflow、源子关系、原始排程及独立普通/批绑定保留；不因转换重算散列。
- 已加载且存在的状态被复制；未加载或不存在状态报告缺失，不能使用默认空状态覆盖；零未读与缺未读区分。
- 坏 JSON、单条坏任务、符号链接逃逸、备份模板、超大文件、凭据字段及合法 max_tokens 均有明确预期。
- 来源采集前后文件内容/时间戳、内存及 Workspace 启动次数不变。

**Verification:** 来源与映射测试通过，所有输出与缺失诊断可解释，无写文件、启动或网络副作用。

### U2. 两表事务与安全重跑 Repository

**Goal:** 将候选原子写入两张目标表，保护归属、删除、版本及状态来源。

**Requirements:** R1、R4、R5、R6。**Dependencies:** U1，用户已创建两张目标表。

**Files:**

- Create: `src/swe/app/crons/migration_repository.py`
- Test: `tests/unit/app/test_cron_migration_repository.py`

**Approach:** 使用已有数据库对象和同连接事务，只允许固定表列。首次插入和重复更新分开判定，不用无条件 upsert 整行覆盖。空状态只初始化未知值；后续按有来源字段补齐。对竞争插入、死锁/锁超时做明确、有上限重试，对其他错误回滚并传播安全错误。

**Patterns to follow:** `src/swe/database/connection.py` 的事务及既有 store 的参数化查询；不沿用 store 自动 CREATE TABLE 的启动方式。

**Execution note:** 先写两表原子性和保护条件测试。

**Test scenarios:**

- 首次写入有定义和状态；第二表写失败回滚第一表，不能报告已成功。
- 同快照重跑不重复插入或重置状态；同来源新快照可更新；不同来源、未知导入来源、Scheduler 接管及删除目标都拒绝覆盖。
- 内存缺失不擦除已有运行字段，显式零值可更新；实际执行来源不被文件快照覆盖。
- 过期观察、零文件版本的内容变化、状态/定义版本冲突和重复身份均有测试。
- 同身份并发首次插入和后续条件更新无半行；只重试明确可重试异常。
- 已有本迁移器定义但状态缺失可安全补齐；只有孤立状态而无定义时拒绝覆盖，不把未知记录嫁接到新任务。
- 记录所有执行 SQL，确认没有 DDL、旧表写入、平台注册或历史插入。

**Verification:** Repository 测试通过；真实 MySQL 的事务/锁/竞争验证在隔离环境执行，不连接用户业务库。

### U3. 分批编排与内部 HTTP 接口

**Goal:** 提供预检、导入和核对，支持过滤、继续游标及部分缺失报告。

**Requirements:** R3、R5、R6、R7、R8。**Dependencies:** U1、U2。

**Files:**

- Create: `src/swe/app/crons/migration_service.py`
- Create: `src/swe/app/crons/migration_api.py`
- Modify: `src/swe/app/routers/__init__.py`，只注册新增路由。
- Test: `tests/unit/app/test_cron_migration_service.py`
- Test: `tests/unit/routers/test_cron_migration_api.py`

**Approach:** 请求内完成有限工作、返回下一页，不新建后台进度状态。身份/路径及过滤条件校验发生在访问来源之前；数据库与两张表可用性必须明确。复用现有认证校验，不增加 Auth/Workspace 中间件免除路径，internal 的既有不初始化行为保持原样。

**Patterns to follow:** 已有 internal 路由、数据库请求连接获取和外部只读任务接口。

**Execution note:** 先写接口约束和无副作用集成测试。

**Test scenarios:**

- Postman 等价请求可逐页预检、导入、核对，过滤涵盖不同 source；空目录不是基础设施失败。
- 内存缺失与导入失败分开计数，单项错误不阻断其他合法项；核对不隐式修复。
- 游标篡改、越界位置、过滤改变、文件清单增删和当前文件变化不会造成任意路径读取或静默漏页；基于稳定来源键继续，重复当前页安全。
- 数据库不可用/缺表返回可理解错误，不自动建表，不把异常返回成成功零条。
- 配置内部 Token 时错误/缺失凭据被拒；启用登录时仍由现有中间件保护；两种凭据并存不互相误判。
- 不初始化 Workspace、不调用旧保存、Monitor、Scheduler 或平台；已有任务 API 契约不变。

**Verification:** 新接口和现有路由/身份回归通过，验证读模式无写 SQL，写模式仅有两张目标表 DML。

### U4. 验证证据与 Postman 上线说明

**Goal:** 给出可操作的迁移、缺失补迁、核对及停止方式。

**Requirements:** R1—R8。**Dependencies:** U1—U3。

**Files:**

- Create: `docs/deploy/cron-phase1-snapshot-migration.md`
- Create: `tests/integrated/test_cron_migration_mysql.py`，采用显式隔离库配置，默认不访问外部库。
- Review: `docs/superpowers/specs/2026-10-15-cron-phase1-snapshot-migration.md`

**Approach:** 用真实路由的请求/响应示例解释预检、从第一页开始导入、循环游标、按用户路由补迁及再次核对。明确文件全量与本机内存部分可恢复的差异、可信内网部署边界和缺失/差异判定。

**Test scenarios:**

- 隔离 MySQL 验证两表原子性、首次插入竞争、过期写保护、JSON/UTC 类型，不以内存 fake 代替这些结论。
- 无隔离环境时显式跳过并报告，不连默认或生产库，不自动创建业务库。
- 保留来源基线，迁移后对照文件和内存不变；不修改原表或平台；重跑前后记录数一致。
- HTTP 验证错误响应不泄露绝对路径、完整定义、认证 Header 或数据库凭据。

**Verification:** 新增针对性测试和相关现有回归有新鲜结果；未运行的集成/部署验证如实列出。手工建表及业务库迁移只由操作者执行。

---

## System-Wide Impact

请求只经过现有认证及 internal 中间件边界进入新增迁移能力，来源只读，写入仅两张目标表。模块与现有 CRUD/执行器没有反向调用；新增路由装配是唯一预期已有代码改动。

数据流为：Postman 运维请求 → 只读文件/已加载状态 → 完整身份和来源校验 → 预检或单任务事务 → 安全诊断和继续游标。数据库失败回滚当前任务，来源缺失不是可删除旧任务的证据。

模块加载、认证与租户中间件回归需要覆盖，因为路由装配仍属于共享入口；不能仅凭新 service 的单元测试证明线上不会创建 Workspace。

---

## Risks & Dependencies

| 风险 | 处理 |
| --- | --- |
| 各实例文件不可共享或内存已卸载 | 明确服务可见范围和实例标识；缺失单列、按用户路由补迁，不保证集群一次齐全 |
| 迁移时旧任务仍变化 | 快照观察时点、指纹和版本校验；支持手工重跑，不宣称全局一致切换 |
| 新表已有其他方案/手工数据 | 校验结构和迁移来源；不覆盖不属于本迁移器的数据 |
| internal 路径经普通网关公开 | 沿用认证但不把用户 Header 当运维授权；上线须限制内部路径 |
| 索引、JSON、时区或事务行为与 fake 不同 | 按准备 SQL 和隔离 MySQL 实测，缺环境如实报告 |
| Windows 依赖阻止现有测试收集 | 使用项目虚拟环境；遇 fcntl 等限制再选仓库已有 Linux/WSL 路径，不伪装测试已通过 |

---

## Resolved and Deferred Questions

已解决：只做快照、不做日常双写；允许内存缺失并补迁；只写两表；手工建表；旧历史表不加字段；既有流程不切换。

实施时细化：确切请求模型/分页上限、已有目录布局样本、敏感字段策略、可识别数据库错误码及 UTC 转换样本。它们不得扩大产品范围；遇到无法确定的身份、时间和绑定只报告，不猜测。

---

## Execution and Review Notes

使用项目虚拟环境，按单元先失败后实现。测试命令、预期失败/通过及逐项结果保存在[验证记录](2026-10-15-cron-phase1-snapshot-migration-verification.md)，不在本计划嵌入生产代码。

当前无 Superpowers 配套技能，采用 `ce-plan` 作为计划替代。按用户 AGENTS，研究、实现和核对在主线程顺序进行，不派发子代理；主线程一致性/安全核对不是独立评审，不能充作独立多轮评审证据。

本计划尚无业务代码变更，不包含提交或推送授权。进入执行后以真实差异和新鲜验证结果报告完成，不将文档状态当成功实现。

---

## Sources & References

- Origin: [第一阶段设计](../specs/2026-10-15-cron-phase1-snapshot-migration.md)。
- 现有行为边界：`docs/adr/0010-independent-cron-scheduling-service-owns-batch-dispatch.md`，本阶段不修改其中存量批运行规则。
- 准备阶段用户持有的手工 DDL：只要求定义与状态两表，不将 SQL 加入应用启动代码。

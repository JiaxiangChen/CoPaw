# 会话元数据入库设计复核

日期：2026-10-29。代码基线：`93aee5b90`，分支
`perf/event-loop-optimization`，包含审查时工作区的既有文档修改。本次没有拉取远端，
“当前实现”指该本地基线。适用于重新设计或实施 MySQL 元数据 / NAS 正文分离时的兼容检查。

## 结论

ADR 0038/0039 的存储方向仍适用；迁移边界以 ADR 0044/0045 为准，ADR 0040 已被替代。
不能把旧方案视为已经覆盖当前版本的实施规格。
当前 Chat 仍通过 `create_chat_service` 装配 `JsonChatRepository`；Session、冷 Turn、
归档和 checkpoint 仍以文件协议发布。它们不是已经实现的数据库架构。

原引用的 `2026-10-20-db-nas-conversation-storage-design.md`、
`2026-10-21-conversation-crud-and-nas-design.md` 在当前工作区和可达 Git 历史中未找到；
会话迁移专用建表 SQL 也未找到。`scripts/sql/tracing_session_name_migration.sql`
只修改 Tracing 名称字段。已存在的 `swe_chat_shares` DDL 位于
`src/swe/app/chat_sharing/store.py:18`，只覆盖分享索引，不能代替整个会话元数据模型。

用户随后授权重新设计及子代理分工分析。新的目标规格与 SQL 草案见
[会话存储重新设计](../../docs/superpowers/specs/2026-10-29-db-nas-conversation-storage-design.md)。

## 已确认差异与必须保留的契约

| 优先级 | 核对结果 | 设计和实施要求 | 当前证据 |
| --- | --- | --- | --- |
| P1 | 执行权威尚未入库 | MySQL generation fencing 必须接到所有发布入口；不能把本进程锁或路由亲和当成执行权威 | `app/answer_turn/coordinator.py:69` 的 `_turns`、`:213` 的 `commit_message`；`app/runner/session.py:351` 的 NAS 执行锁 |
| P1 | 并发新问题行为与 ADR 0039 不同 | 当前不同问题也可能附着旧运行；新方案明确新问题 busy、同一已知请求幂等、显式重连 attach | `app/answer_turn/coordinator.py:78`；`tests/unit/app/test_answer_turn_coordinator.py:103` 用 `q=1/msg-1` 和 `q=2/msg-2` 验证第二次 attach |
| P1 | 新草稿已支持原子初始化专家和响应丢失恢复 | 唯一身份须覆盖请求 scope 和逻辑 Session；初始绑定与 Chat 一起提交；重试返回已提交的 Chat，不能重新创建或覆盖绑定 | `app/runner/api.py:1218,1242`；`app/runner/repo/json_repo.py:385` |
| P1 | 专家绑定是服务端拥有的 revision CAS | 普通 meta 更新不能伪造或覆盖绑定；切入 Plan/Goal 要原子解除；解绑也递增 revision；删除竞争不能复活 Chat | `app/runner/expert_binding.py:11`；`app/runner/api.py:70,105`；`app/runner/repo/json_repo.py:405,441` |
| P1 | 一条源消息会展开成多个 runtime fragment | 保留 occurrence、原始消息 ID、片段 ID、工具 call ID 和执行身份之间的区分；不能对 `Msg.id` 建全局唯一键，也不能用响应卡 ID 作为消息主键 | `app/runner/api.py:290,1000`；`CONTEXT.md` 的 Conversation Message Occurrence |
| P1 | 归档批次、HTTP 页和 Answer Turn 不等价 | 页面可切开用户、过程和工具两侧；保存稳定顺序和真实 boundary；当前 API 的 limit 按源 Msg 计数 | `agents/memory/conversation_archive.py:1709`；`app/runner/api.py:978`；`console/.../sessionApi/historyAssembly.ts:250` |
| P1 | v3 主 session 文件不是完整 Turn 集合 | 迁移必须读取冷热 ledger 并先处理 pending；checkpoint 必须按发布根读取旧、新 journal，而不是只复制主 JSON 或 `checkpoint.json` | `app/runner/session.py:424,446`；`app/runner/api.py:881`；`app/backup/memory_storage.py:14` |
| P1 | 当前 archive 和 session 分开发布 | 新存储需在一次 MySQL 提交中发布归档引用及在线上下文裁剪/恢复快照；保留当前前端重叠合并，不能以入库替代它 | `agents/memory/conversation_archive.py:2578`；`app/runner/api.py:859` |
| P2 | 会话列表和消息历史是两类分页 | 列表当前按 `(updated_at, chat UUID)` 排序，兼容旧 creation-time cursor；消息历史用稳定顺序和上界。不能给列表套用历史快照语义 | `app/runner/repo/base.py:305`；`console/.../sessionApi/index.ts:1827` |
| P1 | Chat 删除、Session 保留、永久分享互相独立 | 不允许从 Chat 级联删除 Session 或分享快照；NAS 回收必须考虑仍有效的正文引用、恢复和备份保留 | `app/runner/manager.py:417`；`app/chat_sharing/service.py:59,121`；ADR 0038 |
| P1 | 现有分享“先文件后数据库”仍不是完整的新发布协议 | 分享创建遇到未知提交结果不能直接删正文；需与统一 publication 对账规则接通 | `app/chat_sharing/service.py:101,111` 当前任意 DB 异常后尝试 unlink |

上述文件前缀为 `src/swe/`，`console/...` 指
`console/src/pages/Chat/`。表格列出的是当前源码事实和目标要求；没有恢复出旧 DDL，
因此不能断言“旧表明确缺了某字段”。

## 容易遗漏的数据

迁移输入不止 `chats.json` 和 `agent.memory.content`：还包括用户准入锚点及终态、
冷热 Turn、压缩归档与边界、checkpoint 各 epoch 的事件水位及 evidence、
任务输出和 `task_runs`、模型错误消息、Stop 附加消息、审批关联、技能快照、
Hook overlay、上下文用量快照及失效标记、场景快照、专家绑定和实际执行身份。
`runner.py:7082` 的普通保存合并整个既有顶层 state，不能在迁移中改成只挑 `agent`。

这些数据需要逐项决定“数据库索引/状态”与“NAS 恢复 payload”的归属；不能因为字段
名叫 `meta` 就把含指令或正文的整段 JSON 当成数据库元数据。Goal 和 SubAgent 的独立
业务权威不随会话迁移搬家，只保留关联和对话投影。

## 验证证据

本轮在项目虚拟环境运行以下后端专项，共 **175 passed**：

```bash
venv/bin/python -m pytest \
  tests/unit/app/test_expert_main_binding.py \
  tests/unit/routers/test_console_chat_expert_main.py \
  tests/unit/app/test_chat_json_repo.py \
  tests/unit/app/test_chat_pagination.py \
  tests/unit/app/test_session_turn_ledger.py \
  tests/unit/app/test_chat_compaction_history_api.py \
  tests/unit/app/test_answer_turn_contract.py -q
# 155 passed
venv/bin/python -m pytest \
  tests/unit/app/test_answer_turn_coordinator.py \
  tests/unit/app/chat_sharing/test_service.py \
  tests/unit/app/test_cold_turn_point_queries.py -q
# 20 passed
```

在 `console/` 运行以下专项，共 **5 files / 126 passed**：

```bash
pnpm test:run \
  src/pages/Chat/sessionApi/index.test.ts \
  src/pages/Chat/sessionApi/historyAssembly.test.ts \
  src/pages/Chat/sessionApi/messageIdentity.test.ts \
  src/pages/Chat/sessionApi/taskSessions.test.ts \
  src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/MessageList/index.contentOnly.test.tsx
```

这些测试验证当前 NAS 路径的契约，不验证尚未实现的 MySQL 会话适配器。
本机没有 `mysql`/`mysqld`，Docker daemon 不可用；本轮未连接业务库，未执行 DDL、
生产迁移、真实两 Pod/NAS 或浏览器验收。

## 重新设计的收敛结果

按用户后续约束，新草案采用六张新表（Session、Chat、Turn、Message、Publication、
Retention）及现有分享表的locator扩展，取消SQL JSON类型，仅用TEXT/MEDIUMTEXT
保存应用校验的扩展元数据。过滤、排序、身份、状态、CAS与水位均是typed列。

表结构按业务查询设计：列表scope索引、Session范围Turn幂等/执行槽、Message
在线与归档独立keyset及body extent、Publication统一发布和batch水位；不按NAS
文件/manifest逐个建表。多Chat共享Session在线memory/task/error、但归档按Chat
独立的现存语义已明确纳入，避免在线消息fanout。SQL是静态评审草案，未执行。

## 草案静态核验

2026-10-29使用sqlglot MySQL方言解析两份SQL，确认六个CREATE TABLE、无JSON
字段类型、无DROP/DML，字段/逻辑关联目标存在，声明索引最大2112字节；设计相对链接
与占位符检查通过。SQL注释中引用的DDL执行前提仍须在实际目标MySQL/TDSQL上
验证；静态解析不验证InnoDB行大小、执行计划、锁等待或真实NAS语义。

## 2026-10-30 查询覆盖复核

基线更新到本地c222540b1，未拉远端。主设计§10新增逐入口矩阵，覆盖全量/分页
列表、channel/user过滤、UUID/逻辑Session查询、冷热Turn、归档、context usage、
专家绑定、目标用户Tracing、分享候选/选中问答/token、CLI与Agent作用域、任务
及W+旁路。保留外部Tracing/审批/Goal/SubAgent/文件/缓存的独立权威。

发现并补齐：旧无turn_states仍可查问答、context usage数字投影、无user Session
lookup索引、channel recent与source消息索引、相同更新时间registry顺序、旧API
终态映射、孤立share缺source/agent的legacy_share命名空间，以及W+直读chats.json
须接入新adapter。仍六张新表且无JSON类型。

新增核验：现行后端主查询79 passed、周边37 passed/9 skipped；前端11文件105
passed。9项push路由测试原有显式Requires full app dependencies跳过。SQL静态
解析/索引引用及文档链接通过。结论为设计可覆盖，不能解释为未实现的数据库后端
已经覆盖全部查询。

## 2026-10-30 SQL规范调整

六张新表移除全部物理关联声明，由应用事务维护跨表关联、scope一致性及删除/回收
边界；保留主键、唯一索引和查询索引，增加publication的Turn反查及message的来源
Chat反查两个显式索引，替代此前隐式关联索引。六张表及151个字段均有中文COMMENT。
分享表有表注释、3个新增字段注释、8个旧字段注释；旧字段变更读取实际结构生成，
校验失败时停止，不覆盖部署特有字符集和排序规则。仍为草案，未执行DDL。

## 2026-10-30 后续迁移与 CRUD 方案访谈

以下记录早期两次停写的访谈过程；该部署步骤与窗口丢失许可已由本文末尾的单次停机修订替代。

读取临时交接文档和提交18bd71e7，源码核对更新至494171e55。
18bd71e7..HEAD核心会话CRUD、Session、archive、share、workspace、cron及W+存储路径无变化；
未重跑旧契约测试，未把先前通过数作为新数据库实现证据。

新增[迁移方案](../../docs/superpowers/specs/2026-10-30-nas-conversation-migration-plan.md)，
覆盖独立NAS格式升级与新权威迁移的区别、统一scope装配、全部Session/ledger恢复入口、
archive直接构造旁路、命令先发成功后提交的顺序、分享unknown时unlink风险、
W+的chats.json依赖，以及备份/离线CLI边界。新导入器直接读受支持旧格式，
不要求先统一升级NAS，不把export_legacy_state当唯一导入源。

用户确认五项：隔离代表scope演练后生产整体切换；两次短暂停写，中间恢复旧服务离线导入；
执行中语义边界提交及纯文本有界批量；回退只针对固定切换前镜像且保留新提交。
另明确窗口内删除、分享、标题、绑定和新消息等全部会话变更可丢，删除可重现、新token可失效；
该例外不适用于源快照已存在的数据、切换后的提交或独立业务表。
已更新ADR0039/0040，新增ADR0044，并修正主设计原“生产单scope先切”的建议。
CONTEXT补充Conversation Migration Snapshot与Conversation Rollback Export，
避免把旧源快照恢复混称为保留新提交的回退。

当前只交付方案和决策记录；没有运行时修改、DDL或数据迁移。现场库/NAS能力、
全量导入、双Pod故障、新备份恢复和反向导出演练仍为实施验收门槛。

补充安装边界：分享与其他业务共用app.state.db_connection及SWE_DB_*，
ShareStore固定表名swe_chat_shares，读取会更新计数。不能用切换公共数据库名安装会话快照。
当前方案只安装六新表及源share副本，整组原子RENAME须同实例staging、InnoDB/原子DDL、
依赖/权限/MDL预检；不同实例先转运七表并核验。窗口share/counter丢失已获明确选择。

## 2026-10-30 改为一次连续停机迁移

用户要求按“准备新服务 → 停止原服务 → NAS 数据迁移入库 → 启动新服务”设计。
已将迁移方案 §1/§4/§6 和主设计 §8/§9 改为一个连续维护窗口：提前准备和演练，
暂停入口/调度，排空并停止所有旧 writer，备份最终一致源，导入、全量校验、安装七表，
再启动新服务冒烟并整体恢复流量。新镜像提前准备不等于提前启动写生产数据的业务进程。

新增 ADR 0045，明确替代 ADR 0040 的两次停写和数据丢失许可；ADR 0040 保留为历史，
标注 superseded。最终排空边界之前已提交的删除、分享/token/计数、标题、专家绑定、
消息及恢复模块全部保留。同步更新 ADR 0038/0044、CONTEXT 术语、playbook 索引和旧格式迁移文档入口。

失败处置按“新版本是否已有任何提交”分界：尚无新写可恢复原分享表/原 NAS 与固定旧镜像；
一旦有初始化/冒烟/分享计数等新写，须完整对账和反向导出保留新提交，不能简单恢复源快照。
停机预算包含排空、备份、迁移、校验、安装及启动，须用生产规模副本计时；没有承诺短暂停机。
仅修改设计文档，未实施运行时代码、DDL、数据迁移或生产切换。

## 2026-10-30 实施任务拆解

新增[实施计划](../../docs/superpowers/plans/2026-10-30-conversation-mysql-nas-implementation.md)，
按依赖拆为15项：旧契约/夹具、身份/bundle、schema、publication/unknown、pins、
SQL Chat CRUD、历史查询、Session执行、compact/reset、分享及入口注入、导入核验、
导出备份、CLI/安装手册、目标环境全链路验收、物理GC。生产门槛与可延后GC明确区分。

CodeGraph及精确源码核对确认：BaseChatRepository默认操作仍是load/save整库读改写，
新repo必须覆盖；DatabaseConnection.transaction返回任务绑定的同连接session，
publication与share应共用此连接；share表名固定，不能切公共DB配置来切换会话。
bound-long-session-memory是旧NAS格式优化，不能用其已勾选项冒充本次数据库验收。

分享扩展SQL仅修订头部执行说明为连续停机后的staging副本扩展/回填及七表安装，
未改DDL语句、未执行数据库操作。新增计划复选框保持未完成，后续按真实测试与现场证据更新。

# 已有 NAS 数据与会话 CRUD 迁移方案

- 日期：2026-10-30；状态：按用户最新要求采用一次连续停机迁移；未实施、未执行 DDL 或数据迁移。
- 依据：交接文档 `/private/tmp/nas-conversation-db-handoff-2026-10-30.md`、提交 `18bd71e7`、[存储设计](2026-10-29-db-nas-conversation-storage-design.md)。
- 源码核对：`494171e55f1dd0a28b9d5f4626feb302c513ad28`。`18bd71e7..HEAD` 的核心会话 CRUD、Session、archive、share、workspace、cron、W+ 存储路径无变化；不把旧测试结果当新 adapter 已通过。
- 本文拆解实施与运维工作，不重建六表模型。完整查询契约继续以主设计 §10 为准。
- 代码任务、文件职责与验收命令：[实施计划](../plans/2026-10-30-conversation-mysql-nas-implementation.md)。

## 1. 已确定的边界与决策树

继承 ADR 0038、0039、0044，并按 [ADR 0045](../../adr/0045-conversation-migration-uses-one-continuous-outage.md) 替代 ADR 0040 的窗口丢失方案：MySQL 元数据权威、NAS 正文；NAS durable 后 DB commit；
无 file-only 降级、双写或 CDC；单 Session 单 Turn、generation fencing、不自动重放模型/工具；
永久分享独立保留；最终排空边界之前的全部已提交会话变更必须迁移，回退必须保留切换后的新提交。

| 决策 | 当前结论 | 影响 |
| --- | --- | --- |
| Q1 生产切换范围 | 已确认：隔离环境代表 scope 演练，生产整体切换；见 ADR 0044 | 不实现生产按 scope 选择旧/新后端的临时路由 |
| Q2 一致源快照取得 | 已确认：一次连续停机，旧服务排空退出后固定最终源，备份、导入、校验和安装期间持续停机 | 新版本提前准备，所有 writer 直到迁移通过才启动；停机时长包含全部操作 |
| Q3 执行中持久化节奏 | 已确认：语义边界提交 + 纯文本有界批量 | 提交不明确时先停止继续执行并对账，不重跑工具；批次参数用基准确定 |
| Q4 回退兼容目标 | 已确认：仅切换前最后一个生产镜像 digest | 导出该镜像支持的完整格式，不扩展到额外更早版本 |
| Q5 数据保留边界 | 已确认：撤销窗口丢失许可，保留最终停机源内全部已提交变更 | 删除不复活、分享不丢 token/计数、标题/绑定/消息/恢复模块完整保留 |

当前设计分支均已确定。旧服务停机后不再恢复写入，直到新权威验收通过或中止迁移恢复旧版。
会话迁移不整体恢复或替换业务数据库，其他领域不随会话数据回滚。
实际 MySQL/TDSQL 版本、NAS 原语、数据量、最大对象、
可用空间与快照耗时由环境盘点和演练取证，不要求用户猜测，也不在本文虚构数值。

## 2. 把两种迁移分开

已有 `swe memory-storage` 仅做 Session v1/2 → v3、journal v1 → v2 的 NAS 格式升级；
新迁移直接以现有 reader 支持的所有格式为输入，不以先完成 NAS 格式升级作为前提。
`export_legacy_state` 会移除 ledger/admission 信息，不能作为唯一导入入口。
可以复用 reader、完整校验、测试 fixture 与 staging 发布模式，不能把旧可变根直接
登记成新 immutable publication，也不能复用旧备份程序宣称已备份完整新系统。

## 3. 运行时适配范围

先建立可信 workspace scope 下的统一存储装配入口，再接通 Chat 查询、Session 执行、
publication 与正文读取。接口按业务操作组织，不为兼容文件 `load/save` 强制整库读改写。
现有外部 API 及 Console 协议保持；旧 reader 留在离线导入/导出边界，不作运行时回退。

执行持久化：工具完成、审批状态变化、checkpoint/compact/reset 等语义边界发布；
纯流式文本按时间/字节/事件数的有界批次合并，最终值由基准测试确定，不逐 token 提交。
问题准入必须先提交再执行，成功终态必须提交确认后再发送。边界发布失败或结果未知时，
停止推进模型/下一工具，使用同一 operation key 对账；只在确认已提交且执行 authority
与基线仍有效时继续，否则进入受控结算/失败路径，不自动重跑工具或跨 Pod 续跑。
外部工具副作用与数据库不原子：工具已完成但结果尚未 durable 时崩溃的缺口仍存在，
不能据此宣称副作用恰好一次；现有审批服务仍是审批业务状态的权威。

| 路径 / 当前入口 | 迁移后的职责 | 验收重点 |
| --- | --- | --- |
| `app/workspace/service_factories.py`、`app/runner/repo/` | 装配 MySQL Chat repository，直接 SQL 实现 create/get/list/page/update/delete、专家绑定 CAS | 完整 scope、UUID、registry 顺序、多 Chat/Session、所有分页形状 |
| `app/runner/manager.py` | 改标题条件更新；身份重绑按统一锁序；删除原子隐藏与 tombstone | 标题更新与删除竞争不复活、绑定 revision、来源/目标 Session busy |
| `app/runner/runner.py`、`session.py`、SessionExecution | 替换直接构造 SafeJSONSession 及 save/load/modify/history/cron merge/ledger 全族 | 完整顶层恢复模块；准入、busy、终态、pending settlement、generation |
| `app/runner/api.py:_archive_store` | 改为统一注入，不再每次直接构造文件 archive | 共享 memory/task/error + 当前 Chat anchor + Chat 独立 archive；旧 source ID/fragment |
| `agents/memory/conversation_archive.py`、`reme_light_memory_manager.py` | 移除独立 store 构造；compact/checkpoint/event/reset/root 通过 publication 提交 | `/new`、`/clear` 不产生 compact boundary；epoch、水位、恢复依赖 DAG |
| `app/runner/command_dispatch.py` | 调整命令成功输出与持久化次序 | 当前先 `last=True` 后 commit，须改为确认提交后才发成功终态 |
| `app/chat_sharing/` | 分享行与 publication 同连接事务；匿名读 locator；失败按原 operation 对账 | 未知提交不立即 unlink；旧 token、计数、孤立分享不丢失 |
| `agents/tools/emit_wplus_sop_event.py`、W+ service | 统一注入，移除 `chats.json` 存在性前置 | W+ 自身 store/outbox 保持；不可留下 JsonChatRepository 旁路 |
| Cron、Tracing、CLI、Goal/SubAgent 授权 | Chat/Session 访问接新存储；各领域自身存储保留 | managed receipts 完整导入、目标 user/source/agent、审批 overlay |
| `app/backup/`、`cli/memory_storage_cmd.py` | 新增 DB 快照 + NAS 引用闭包备份；旧 CLI 明确仅离线旧格式 | 不能再只枚举 session/ledger/journal；空目标恢复与完整性核验 |

执行前再次枚举 `JsonChatRepository`、`SafeJSONSession`、`ConversationArchiveStore`、
`chats.json`、ledger/journal 直接访问，逐项判定为新存储入口、离线兼容或独立领域。
不得仅全局替换类名，也不得删除附件、文件管理、trace、Goal/SubAgent、Cron 的独立状态。

## 4. 数据导入工具

实现独立的 inventory → plan → import → verify 工具链；具体 CLI 命名在实施时遵循现有惯例。
默认只盘点/预览，实际导入显式指定清单、隔离目标 DB 与 NAS 新前缀。

1. **取得稳定源副本**：进入连续停机窗口，排空并停止全部会话 writer，固定最终源快照、旧镜像、配置与全部 workspace 根。旧服务保持停止；源副本只读保留，旧协议恢复/pending 收敛在受控工作副本执行，保留前后记录；无法收敛则阻止切换。
2. **盘点并闭合身份**：覆盖 tenant/source/agent/user/channel/external session，以及 Chat 注册顺序；从可信配置/注册关系恢复原值，不拆 sanitize 文件名，不猜 default。无法闭合、身份超长、损坏或缺引用均阻止切换。
3. **生成外部清单**：源文件/记录位置 → 目标整数 ID、operation key、NAS bundle 路径与导入结果；同清单重跑复用身份，不计算 source fingerprint。预分配关联映射，使多 Chat、历史来源与导入次序可重入。
4. **先写不可变正文**：按现有格式 reader 读取所有已提交内容，再封装新 bundle；复用发布原语校验格式/长度/extent。按 Session 分批事务，不以一个超大事务搬完整库；大 Session 的分批可见性通过隔离目标保证，全部核验前不接生产。
5. **安装 typed metadata**：Session、Chat、Turn、Message、Publication 与 retention/依赖关联按统一事务规则建立；旧 source ID 重复保留独立 occurrence，不虚构旧未知 Turn 成功状态。保留 Session 在线共享与 Chat 独立归档。
6. **导入永久分享**：保留原 token/snapshot_key/归属/计数，回填 locator；无法闭合 source/agent 的孤立分享按主设计 §10.5 导入 `legacy_share`，不新建 Chat、不猜 workspace。
7. **全量语义核对**：旧/新 reader 逐项比较消息角色/顺序/正文、fragment、状态、元数据、边界、epoch、水位、所有恢复模块、分享与删除语义；计数只是辅助证据。产出通过/阻塞清单，不支持跳过坏记录后宣布成功。

输入包括 chats.json、session v1/v2/v3 全顶层模块和冷热 Turn、完整 ledger generations、
checkpoint v1/journal v2 的已提交当前及保留 epoch、archive batches、删除标记、
永久分享，以及场景/专家恢复内容。未知模块需盘点、保留并明确恢复方式，不能静默丢弃。
quarantine、candidate、pending 和未提交尾部记录处置结果，不擅自当成 committed history。

### 4.1 隔离导入与生产表安装

隔离 schema 保存六张新表及源快照的 `swe_chat_shares` 副本；分享表 locator 扩展在此
副本执行并回填核验。导入期间旧服务已停止，仍保持原分享表不变，以支持新版本写入前的撤回。
切换时只安装这七张会话相关表，其他业务表与配置保留原状态；不能切换整个公共 DB
连接去指向会话源快照，导致 Cron/审批等领域回退。

正式安装脚本在隔离库验证整组 InnoDB 表的原子切换能力，优先用一次多表 RENAME
将现有分享表留到专用旧表名，再把已核验的七表安装到最终 schema/固定表名。
staging 与最终 schema 必须位于同一 MySQL 实例；若离线导入在另一实例，先仅转运
这七表到目标实例的 staging，再全量核验。核对七表均为 InnoDB、目标原子 DDL 能力、
SHOW CREATE/trigger/关联依赖、源目标 DDL 权限和 metadata lock，不能假设跨实例可 RENAME。
六张新表若已存在须核查来源并停止盲覆盖；脚本记录原/目标表名、显式 ID/自增高水位、
NAS 根与清单版本。目标 MySQL/TDSQL 若不支持所需原子 DDL，阻止按该脚本上线并
重做安装方案/停机计时，不能自动退化为边运行边逐表替换。DDL 安装失败时新 writer
不启动，按已验证的表名恢复流程处理；新版本产生任何提交后只能走完整反向导出回退。

## 5. 实施顺序与退出条件

| 阶段 | 交付 | 退出条件 |
| --- | --- | --- |
| P0 现场盘点与契约固定 | 源格式/规模/身份清单、目标库能力、固定旧镜像、契约测试清单 | 全部范围可解释；MySQL 版本、strict mode、page size、NAS 原语有证据 |
| P1 schema/事务与 bundle 基础 | 评审后的正式 SQL、DAO、scope guard、immutable bundle reader/writer、publication 对账 | 隔离库 DDL/锁序/NULL 唯一键/EXPLAIN；未知 INSERT/COMMIT、extent/路径校验通过 |
| P2 Chat 与历史查询 | SQL CRUD、分页、专家绑定、在线/归档/Turn/Tracing/share 查询装配 | 主设计 §10 契约在新实现通过；列表及点查无目录扫描；删除与 rename 竞争通过 |
| P3 执行与状态发布 | 准入、SessionExecution、恢复根、compact/reset、Stop、分享提交、所有旁路改造 | 新 writer 全部接通；两 Pod authority/过期 writer/unknown/恢复故障通过；不重放工具 |
| P4 导入/导出/备份 | 多格式导入、全量语义核验、反向导出、新备份与恢复 | 同清单重跑不重复；全部格式及孤立 share 可迁移；最终删除不复活、token/计数不丢；切换后新写可无损回退并继续写 |
| P5 完整演练与生产切换 | 代表 scope 隔离演练、生产规模计时、单次连续停机/切换/回退手册 | 排空至开放流量全流程有耗时记录；生产范围全量核验；旧 writer 为零；新服务启用前确认只用新权威 |
| P6 回收启用 | pin/reaper/GC 竞争验收、引用巡检与报告、受控物理回收 | 未知/有效历史/backup/share 不回收；mark-current-read、reader/dependency/fence 竞争通过 |

P1～P3 可在隔离环境分阶段开发，但不能把仅完成 Chat 列表当作生产迁移完成。
生产上线前 P4 的备份、恢复和反向导出必须完成。GC 最初可只报告候选、保留对象；
物理回收在 P6 通过后开启，不以清理历史数据节省迁移空间。

## 6. 一次连续停机的操作与回退

“新服务上线”在停机前指镜像、配置、迁移工具及数据库结构已准备和演练完成；
新版本业务 Pod/worker 不启动，不接流量，不运行启动初始化或定时任务去改写生产数据。
正式停机之后，备份、导入、全量校验和切表都在同一个维护窗口内完成。

```mermaid
flowchart LR
    A[提前准备和隔离演练] --> B[关闭入口并排空旧执行]
    B --> C[停止全部旧 writer]
    C --> D[备份最终一致源]
    D --> E[迁移元数据和 NAS 正文格式]
    E --> F[全量核验并安装七表]
    F --> G[启动新服务并冒烟]
    G --> H[整体恢复流量]
```

### 6.1 停机前准备

1. 固定旧生产镜像与新镜像 digest、部署配置、迁移清单格式及回退命令。新版本必须已完成 P0～P4，不在停机现场边开发边迁移。
2. 用旧格式代表样本完成隔离演练，再以生产规模副本测量备份、导入、完整校验、切表和启动耗时；预留失败处置时间、磁盘空间和 inode。停机总时长覆盖排空至开放流量，不能仅按导入耗时估算。
3. 在目标实例准备 staging schema，校验正式 DDL、权限、原子切表和表名恢复脚本；新 NAS 使用独立前缀。旧表和原 NAS 不原地转换或覆盖，其他业务表保持原状。
4. 明确维护窗口和操作责任人。暂停会话入口、定时触发及相关维护使用现有部署手段，不新增双写、CDC、追赶或应用专用拒写协议。

### 6.2 停机、固定最终源

1. 关闭所有能触发会话写入的入口及调度，包含 HTTP/Channel、CLI、Cron/W+ 和匿名分享访问计数；新请求不进入会话执行，已在途 Stop/审批等控制请求仅在排空阶段按现有协议处理。
2. 等在途执行完成结算与持久化，再停止全部旧 Pod/worker、后台备份/清理和维护 writer。核对多个副本与独立任务均退出，不能仅靠摘除一个 Service 的流量认为停写完成。
3. 若存在长任务、未结算 Turn 或 pending，保持入口关闭，按旧协议停止/收敛并确认状态已保存。不能按超时直接杀进程后把未确认结果当已提交；无法收敛时中止本次迁移，恢复旧服务处理后再安排窗口。
4. 在 writer 为零时备份全部 workspace 会话树、关联场景/专家恢复内容、配置定位信息及原分享表。数据库备份只提取相关表的一致状态；记录最终源边界、schema/epoch/计数、路径和镜像。稳定复制可以在停机期间完成，不要求 NAS 必须具有原生快照。
5. 原始源和备份只读保留，迁移在受控副本上执行。此后到新服务启动前，旧服务持续停止，没有恢复旧服务接受新写入的中间阶段。

### 6.3 迁移、核验和启动

1. 用第 4 节工具读取最终源，按同一清单导入 staging 六张元数据表及分享表副本；消息/工具内容和恢复 payload 写入新 NAS bundle，保留既有附件和文件 URL 所需文件。
2. 全量比较旧/新 reader 的内容、顺序、身份、状态、归档、checkpoint、恢复模块、删除语义和全部分享。任何无法解析、身份不明、丢引用、pending 未决或内容不一致均停止切换，不跳过坏记录。
3. 按第 4.1 节原子安装七张会话相关表，保留原分享表于记录的旧表名；复核表定义、行数、ID/locator、自增高水位与 NAS 前缀。安装动作不替换公共数据库配置或其他业务表。
4. 所有生产 scope 一起启用新权威。外部入口和 Cron/后台写入仍关闭，先启动受控新版本执行就绪检查；确认新配置只访问新表和新 NAS，不再写旧 chats/session/journal。
5. 进行读写、新增问答、Stop/重连、分享、Cron 任务关联和重启恢复冒烟。任何初始化、冒烟或匿名分享计数产生的提交都计入“新版本已写入”，必须纳入后续回退。通过后才逐步开放入口、调度和维护任务；此处流量逐步开放不代表按 scope 混用两套权威。
6. 开放后运行新备份/恢复和引用巡检。GC 初始仅报告，P6 通过后才物理回收；原树、备份、旧表、清单和固定旧镜像在回退窗口内只读保留，释放另行明确。

### 6.4 失败与回退分界

| 发生阶段 | 处理 |
| --- | --- |
| 旧执行无法排空或源备份失败 | 不开始导入/切表，确认旧数据可用后恢复固定旧版本与入口，重新安排维护 |
| 导入或完整校验失败，新版本尚无写入 | 保持旧源不变；在窗口内按同一清单修复重试，或停止迁移作业、恢复原表名及固定旧版本后中止本次切换 |
| 七表安装失败或就绪检查失败，新版本尚无写入 | 确认实际表名状态并按预演脚本恢复原分享表；核对原 NAS/旧版本只访问原数据，再恢复服务；不清理原备份 |
| 新版本已产生任意提交，包括初始化/冒烟写入 | 关闭入口并停止新 writer，完成 unknown 对账，捕获新 DB 一致状态及 NAS 引用闭包；完整反向导出并由固定旧镜像验读后才能恢复旧服务 |

回退导出保留新版本已提交的新增问答、压缩、Stop、删除、专家解绑、分享与访问计数。
导出格式以固定旧镜像实际支持能力为准，支持 v3/v2 时保留其完整能力；不强制降到 schema2。
目标不能表达新状态时先修复导出兼容，不静默删字段。新版本写入之后只回滚镜像或恢复
停机前备份会丢新提交，不作为回退方案。迁移中止后旧服务若重新写入，下次迁移重新
取得最终一致源，不继续把上次部分导入当作最新状态。

监控至少覆盖 DB 锁等待、unknown publication 年龄、失败发布、缺正文/坏 extent、
引用/scope 异常、pin/GC 积压、NAS 读字节与调用次数、查询 p95/p99。阈值根据代表负载
建立基线后确定；本文不把旧 NAS 实现的通过数或 SQL 静态解析当成目标库验收。

## 7. 方案验证记录

当前仅完成文档、提交与源码边界调查。尚未连接业务数据库，未实现新 adapter、迁移工具、
备份或导出器；未运行生产 DDL。后续验证证据需逐阶段记录，不能提前标记完成。

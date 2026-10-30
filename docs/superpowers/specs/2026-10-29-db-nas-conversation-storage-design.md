# 会话元数据 MySQL / 正文 NAS 重新设计

- 日期：2026-10-29；状态：**评审草案，未实现、未执行 DDL**。
- 当前实现基线：本地 `perf/event-loop-optimization`，`93aee5b90`；未拉取远端。
- 上位决策：[ADR 0038](../../adr/0038-conversation-metadata-in-mysql-content-on-nas.md)、[0039](../../adr/0039-conversation-publication-uses-mysql-commit.md)、[0044](../../adr/0044-conversation-storage-uses-one-production-cutover.md)、[0045](../../adr/0045-conversation-migration-uses-one-continuous-outage.md)。ADR 0040 的窗口丢失许可已由 ADR 0045 替代。
- 核对证据与当前测试：[设计复核](../../../analysis/playbook/conversation-storage-design-audit.md)。
- SQL：[六张新表](../../../scripts/sql/conversation_metadata_tables.sql)、[现有分享表扩展](../../../scripts/sql/conversation_share_publication_extension.sql)。
- 后续实施边界与迁移步骤：[NAS 数据与 CRUD 迁移方案](2026-10-30-nas-conversation-migration-plan.md)。

旧详细设计和会话专用初始化 SQL 未找到，本稿替代失效的设计链接。现存运行时仍是
NAS 文件权威，不能将本稿或 ADR 当作数据库适配器已经实现的证明。

## 1. 目标与取舍

用户最新约束：尽量少表、允许非规范化及冗余字段、**禁止数据库 JSON 类型**；
优先发挥数据库查询、唯一约束、事务与并发控制优势，不把 NAS 文件树照搬为表。

推荐 **6 张新表 + 给现有 `swe_chat_shares` 加 locator 列**：

| 方案 | 收益 | 代价与结论 |
| --- | --- | --- |
| 只迁 Chat 列表，Session/journal 仍文件权威 | 改动小 | 不符合 ADR 0038，跨 Pod 状态与发布权威仍分裂，不选 |
| 5 表，将 Turn/message/archive 等塞入通用 records | 表少 | 热 Turn 状态和巨量历史共用稀疏行/索引，约束复杂，不选 |
| **6 表：Session、Chat、Turn、Message、Publication、Retention** | 按查询热点拆分，归档/checkpoint 批次并入 publication | 接受少量可空批次字段和 scope 冗余，推荐 |

不单建 scope、NAS object、archive boundary、checkpoint、event、backup header/member
或 share companion 表。archive/checkpoint/event range 是一次数据库发布的附属元数据，
不逐个旧 manifest、segment、candidate 文件建表。表数不是唯一优化指标：不合并
Turn 和 Message，不把需要过滤/排序/CAS 的字段藏到 TEXT。

MySQL 是必需依赖，故障时停止新会话写入，无 file-only 自动降级。正文、工具参数/结果、
语义 checkpoint、summary、场景指令及执行恢复 payload 留 NAS；数据库保留身份、
所有权、状态、顺序、边界、水位、长度、引用和标题。不新增内容 checksum、身份/请求
digest、source fingerprint，不承诺识别任意同长度静默损坏。

## 2. 数据模型与权威

| 表 | 权威内容 | 主要索引/读路径 |
| --- | --- | --- |
| `swe_conversation_sessions` | 完整逻辑身份、generation/owner/lease、active Turn、草稿映射、state revision、共享在线 high/context epoch、恢复根 extent | 完整身份唯一；按主键锁一行；恢复 publication 反查 |
| `swe_conversation_chats` | 外部 UUID、标题、列表 scope 投影、模式与扩展元数据、专家绑定 CAS、独立归档 high、checkpoint epoch/revision/root、列表时间 | scope+UUID 唯一；scope/user+deleted+updated+UUID；session 最近 Chat |
| `swe_conversation_turns` | 用户 question msgid、runtime Turn UUID、Chat 关联、准入/终态、执行身份及发布引用 | **session+question msgid 唯一**；chat+turn order；runtime UUID；主键终态 CAS |
| `swe_conversation_messages` | canonical retained occurrence、在线/anchor/archive 所属、稳定顺序、上下文成员、body extent、归档关联 | 在线、归档 keyset；Turn 点查；body/archive publication 反查 |
| `swe_conversation_publications` | 一次操作身份、提交状态、一个不可变 bundle 路径/长度、CAS 基线、可选 archive/checkpoint/event range、GC 状态 | operation 唯一；Chat boundary/checkpoint 唯一；待对账、事件范围、GC 候选 |
| `swe_conversation_retentions` | reader/backup/dependency pin、scope backup fence、GC guard | retention+member 唯一；publication+state；scope+kind+state |

### 2.1 身份、冗余与类型

Logical Session 唯一范围为 `(tenant, source, agent, user, channel, external_session)`，
全部使用实际解析后的原值，保留空值语义，不按 NAS 文件名逆推、截断、大小写折叠或
计算摘要。内部实体用 `BIGINT UNSIGNED`；外部 Chat UUID、question msgid、runtime
`turn-UUID`、不可预测 share token 保持原接口。两类 Turn ID 不混用。

一个 Session **允许多个 Chat**，Chat 不建 `UNIQUE(session_id)`。scope/user/channel
在 Chat 冗余，服务端同事务维护；任何 JOIN/写入须验证与 Session 相同，不能相信请求
传来的冗余 scope。本设计不声明物理关联约束，跨scope一致性由应用事务校验。

本稿身份上限按 UTF-8 字节：tenant/source/agent/user 各 256，channel 64，external
Session 1024，外部 Chat/runtime Turn 128，question msgid 256；全 Session 唯一键最大
2112 字节。**这是草案容量约束，不是当前 API 已有约束**。迁移预检覆盖所有数据；
任何超长必须阻止切换并修改列宽/索引设计，不裁剪或哈希代替。运行时同样先验证字节
长度。空 user 等合法原值使用空二进制值；缺失身份无法恢复则报告冲突，不猜默认归属。

所有扩展字段用 `TEXT/MEDIUMTEXT` 存应用序列化文本，禁止 SQL JSON 类型、JSON
函数/生成列和解析 TEXT 的 WHERE。应用按版本校验、限制长度并拒绝非法结构。用于
权限、过滤、排序、CAS 的信息必须独立 typed 列。专家 binding 的 ID/revision 只有
typed 列权威；响应中的 meta binding 从这些列组装，不保留另一份可独立修改的值。

`metadata_text` 只存可公开的轻量元数据；混有提示词/场景内容/未知正文的 meta 部分
存 NAS，Chat 的 `content_meta_*` 定位。兼容接口按需组装；现有必须返回完整 meta 的
端点可批量读相关 extent，不能悄悄删字段换性能。优化后的列表轻投影需经前端契约
测试才启用，目标是常规侧栏不读 NAS，不意味着本稿已经改变 API response。

### 2.2 共享在线历史与 Chat 独立归档

当前 `runner/api.py:_build_chat_history` 从 Session 读取共享 memory/task/error，
但按 Chat UUID 取 archive；补充准入 anchor 有自己的 Chat 过滤。必须保留此差异，
不能把在线消息复制到所有 Chat，也不能把它们冻结为某个 Chat 的创建时快照。

Message `layer` 为 `memory/task/error/anchor/archive`。`current_visible` 与 `in_context`
分开：错误和任务输出可以可见但不在模型上下文。非 archive 行 `session_order` 非 NULL，
由 Session 单调分配，跨 context epoch 不复用；archive 行使用独立的
`archive_chat_id+archive_order`，`session_order=NULL`。归档顺序由拥有它的 Chat
单调分配，跨 checkpoint epoch 不复用；SQL 利用 NULL 让两套唯一键互不干扰，
应用显式验证以上形状。Chat `history_high` 是 archive order 上界，非在线快照。

`origin_chat_id` 对准入 anchor 生效；旧无归属 anchor 保持 NULL。Chat detail 读取
共享 memory/task/error，加当前 Chat 或旧无归属 anchor，沿用旧 known-IDs 与该
Chat archive 的 anchor suppression，再进行 fragment 展开及脱敏。不按 source
Msg.id 全局去重；相同源 ID 可有多个 retained occurrence。现有 fragment ID、
`metadata.original_id`、task_run、工具 call_id、时间和 frozen execution identity
保持不变。工具两侧、HTTP 页、Answer Turn、UI 响应卡和归档边界不是同一实体。

一次 compact：裁剪 **Session 共享 current projection**，只为发起 Chat 创建归档
occurrence/boundary，在同一数据库提交安装新的恢复根和 checkpoint。其他 Chat
在线部分随之缩短，但不自动获得该归档。这与当前多 Chat 行为一致。归档 occurrence
可复用有明确来源关系的正文 extent，但不能仅因 Msg.id 相同就视为同一 occurrence；
旧在线/归档重叠按旧组装规则处理。

### 2.3 最小化状态重复

`metadata_revision`、`history_revision`、Session `state_revision` 分开。改标题
不会令已执行完的工具结果因为无关 metadata CAS 失败。列表 `updated_at` 在任一
应更新 Chat 的提交中原子更新；`status` 是当前执行状态的数据库投影，active Turn
仍是 Session 权威。状态由查询JOIN active Turn组装，仅当activeTurn.chat_id匹配
请求Chat时返回running/stopping，其余同Session Chat仍idle，保留当前按Chat显示
状态的契约；Session是否busy仍跨所有Chat统一判断。settling映射为原有可用状态及
恢复不可用响应，不向旧UI泄露未支持枚举。不对每个Chat逐行广播更新，
不得从冗余Chat.status判断Session是否空闲。

Chat 身份字段的既有 patch 行为也需保留：目标 Session 身份解析后，按整数 ID
顺序锁源/目标 Session 和 Chat，任一有活动/待结算 Turn 则冲突；仅重绑 Chat 的
Session 关系和冗余身份，不搬走旧 Session 状态。历史 Turn/归档保留来源关系。
不得悄悄把接口改为这些字段不可修改。来源Session的draft_chat_id若指向该Chat，
保留此指针作为失效映射，读取时检测身份不匹配返回409而非重建；目标已有draft映射
不覆盖。目标新专家草稿重试须检查Chat当前完整身份。历史Turn/归档保留来源Session
仅作追溯，可见性按当前Chat授权；不能因为来源Session不同就拒绝旧归档，也不能据
旧来源越过当前Chat权限。

## 3. SQL 查询与性能设计

这是数据库业务索引模型，不是文件 catalog。普通查询不解析整份 session/journal，
不枚举 NAS 目录。所有查询先绑定可信 scope/user，禁止先全表取出再应用过滤。

| 操作 | 数据库方式 | NAS 代价/注意点 |
| --- | --- | --- |
| 侧栏 | `ix_conv_chat_recent`，显式列投影；`updated_at DESC, external_chat_key DESC` keyset | 普通轻量元数据无 NAS；兼容完整 meta 仅批量读取有内容扩展的行 |
| 指定 Turn | 完整 Session 唯一键解析后按 `(session_id,question_msg_key)` 单行点查 | 仅结果需要的 extent，冷热同路径，不遍历 ledger |
| 在线 detail | Session 当前投影索引，memory/task/error 分层有界读取，独立 anchor 查询 | 合并 publication，批量 pin，按 extent 读取 |
| 归档页 | `(archive_chat_id,deleted_at,archive_order)` 倒序取 `limit+1`，返回时正序 | limit 仍计源 Msg，不计 fragment/卡片；不读页外正文 |
| 恢复执行状态 | Session 恢复 publication + Chat checkpoint ptr + event range | 不扫描旧 epochs/segment；保留所有恢复模块 |
| checkpoint 后事件 | `(chat_id,epoch,event_last,id)`，`event_last>watermark AND event_first<=high` | bundle 内二级 block table 定位，不一事件一数据库查询 |
| 删除/回收 | Chat 可见性先软删除，GC 根据索引反查 body、root、share、pin | 不在 HTTP 删除事务 unlink 大目录 |

历史页第一请求捕获该通道 order high；后续按 `order<before AND order<=high`。
不保留 Message/Turn 历史版本区间；并发删除和当前权限即时生效。在线、归档各自
上界，不能用导入的全局自增 ID 替代旧自然顺序。分页 cursor 编码 scope、Chat、
通道和上界，不承诺跨请求恢复旧状态。侧栏仍是 updated-time best effort，兼容
旧 creation-time cursor；SSE `cursor-v1` 是 runtime Turn+sequence，三者不共用
解码器。

示例归档查询（值全部参数化；事务内另校验 Chat 权限和 publication live 状态）：

```sql
SELECT id, turn_id, source_msg_key, archive_order,
       publication_id, byte_offset, byte_length
FROM swe_conversation_messages
WHERE archive_chat_id = ? AND deleted_at IS NULL
  AND archive_order < ? AND archive_order <= ?
ORDER BY archive_order DESC LIMIT ?;
```

不使用深页 OFFSET；兼容现有页码接口时明确其成本。`total` 用同 scope 的数据库
COUNT，不读 NAS，不用异步近似值改变契约；若 COUNT 成为瓶颈再据实增加维护计数，
不预先堆索引。当前在线 detail 的全量返回语义保持；导入 SQL 不能凭空让它有界，
必须先实现兼容分页/客户端装配再削减该返回体。

每页先取 locator，去重 publication，单批建立 reader pin；同文件相邻 extent
合并 IO，不逐消息向数据库查 publication。bundle 按字节和记录数限制，一次 publication
一个不可变 bundle，可含多个 block，不是每条消息一个文件；大的用户/工具 payload
使用流式范围读写，bundle 作为原子发布单位不要求一次装入内存。SSE token 不逐个
INSERT；用户准入先持久化，过程按现有持久化边界/有界批次发布，终态必须提交后确认。

验收用实际短/长 Session、多个 Chat、并发不同 Session及压缩期间提交的基准对比
现行路径：EXPLAIN ANALYZE 检查索引范围、扫描行数、filesort和回表；统计 p50/p95/
p99、数据库往返、锁等待、NAS 打开/字节、RSS及写放大。不捏造固定延迟目标；
切换前须记录对照基线并证明列表/Turn/归档页不随总文件大小线性读取，独立 Session
不会被长执行事务串行化，热索引放大在容量预算内。

## 4. 执行、准入和专家绑定

所有影响共享上下文的写入锁 Session 行；执行不持有数据库事务。短事务统一锁顺序
Session → 涉及的 Chat（ID 排序）→ Turn → publication（ID 排序）。只读pin只锁publication；GC顺序为guard→publication，之后不再回头锁Session。长模型、工具、NAS IO 和备份复制均在事务外。

1. 完整身份解析 Session，锁行，用 DB `UTC_TIMESTAMP(6)` 判定 lease。
2. 已有同 question msgid：返回原已准入/终态；不覆盖请求正文。用户准入 payload
   在 NAS 保留，重试需要一致性比较时读取原 payload做语义比较，不新增 digest。
3. 有其他 active Turn，包括 settling/未知提交：独立新问题返回 busy；显式重连
   才 attach。当前 coordinator 把不同问题 attach 到旧 Turn 的行为必须更改并改测试。
4. 新请求预留 Turn 和 publication，再 durable NAS user anchor；短提交安装准入、
   active_turn、owner/generation、恢复根。**只有准入提交成功才调用模型/工具**。
   准入准备中的 active reservation 也占 Session，失败经对账终结后释放，不能留无主 busy。
5. Lease 由 owner 使用当前 generation 条件续约；间隔/TTL 是可配置运维参数，
   部署前按暂停/网络预算验证。失效 owner 停止新增动作；已起外部工具不能凭 DB 撤销。
   每次发布再次验证 owner、generation、未过期 lease 和精确 active Turn。

Turn状态约束：`reserved → admitted → running → stopping/settling →
succeeded/failed/stopped/interrupted`；Stop可以从admitted/running进入stopping，
自然完成可直接进入settling，终态不可覆盖；准入未提交的reserved失败经publication
对账后标`rejected`并清槽，不作为已准入问答返回。publication仅
`prepared → committed/aborted`，终态不可逆；GC状态仅`live → deleting → reclaimed`。
所有状态转换用锁/CAS和同事务引用更新，不能仅写一个status字符串。

租约过期只能 fence旧 owner和对账存储，不自动接管执行/重放模型工具。没有可提交
终态时，恢复流程显式提交 interrupted 结果及已确认的历史，再释放执行槽；无法
确认原发布结果时保持 unavailable/busy，不假成功。持久化恢复与继续执行是两回事。
Stop 与自然完成对同一 Turn 终态 CAS，先提交再本地通知；settling barrier 保留。
EOF 不等于 success，`stream.end/recover/unavailable/chat.snapshot` 各自语义保留。

专家初始化在 Session 行锁下，创建 Chat+typed binding revision 1+`draft_chat_id`
一起提交。已有历史重复Chat时，初次查询按Manager的updated最近规则恢复；
原子create-if-absent遇到竞争则沿用repository的registry第一匹配规则，
在事务内固定所选草稿映射并记录选择，不能任意去重。响应丢失后先返回此映射的已提交 Chat，再判断绑定是否
变化；删除后重试404，不复活，真正新草稿用新 logical Session ID。普通 create
仍允许多 Chat。普通 meta patch不能伪造专家或场景服务端键；模式切换解除绑定并
递增 revision；请求执行前后校验 binding revision，实际执行身份冻结在 Turn。
Pod 死亡后不能把审批请求重新解析为“最新专家”或默认专家。Goal/SubAgent 独立
业务状态不搬进这些表；审批存储的 best-effort audit不提升为可恢复执行权威。

Gateway 负责 reconnect/Stop/approval 的 Session 亲和，无应用内跨 Pod 代理；
路由亲和不代替 DB authority，死 Pod 的 live stream 不可重新 attach。

## 5. NAS-first / MySQL-commit 发布协议

### 5.1 预留与提交

`operation_key` 是在第一次 INSERT 前产生并由调用者保有的随机不透明操作 ID，
不是内容摘要。一次语义操作终身使用同一 key，路径由可信根+key 推导，不接收用户
路径；拒绝绝对路径、`..` 和逃逸 symlink。每个 publication 一份不可变 bundle；
将原 object 表合并于此，允许同一次 compact同时携带 archive、checkpoint、event
和 recovery，`kind` 是操作分类，不能限制成互斥的一种附属数据。

1. 短事务预留 `prepared` publication及路径，记录准确 generation、owner、
   base_state/history revision与 epoch。可空 share/import 不要求存在 Chat/Session。
2. 事务外写临时 bundle，文件 fsync、同文件系统原子不可覆盖发布、父目录 fsync，
   验证长度及可读格式；生产等价 NAS 必须实测这些原语。对象不能原地覆盖。
3. 预留阶段boundary/checkpoint/event范围等参与唯一约束的可空字段保持NULL，
   仅最终成功提交时安装，避免aborted预留占用下次有效revision或boundary。
   拟提交的boundary/checkpoint/event计划元数据写入bundle自描述header或prepared
   result_metadata_text（应用校验），不能只留原进程内存；对账时重新校验再安装。
   短事务按统一锁序检查 publication仍 prepared、执行 authority、scope关联、
   Chat 未删除、epoch和相关CAS基线，验证全部 extent `offset+length<=bundle length`。
   写入 Message索引/Turn状态、归档边界、checkpoint ptr、Session root和水位，
   publication改 committed。metadata/read状态可见性以此 commit 为准。prepared
   的预留行或 NAS 可见文件不能作为历史返回。
4. 结果返回 operation/Turn/Chat已提交身份；live SSE 可先于持久化，但终态成功只
   在此提交确认后发出。正文不可读报完整性/可用性错误，不以空消息或重新执行填补。

只有相关 state/history CAS冲突时才能对**已完成的不可变结果**重新校验/重基，不能
因标题变化、数据库重试或 lease接管再次调用模型/工具。不兼容的 `/clear`/删除/epoch
变化拒绝晚结果；保留原结果对账记录供运维，不强行安装过期上下文。

### 5.2 未知提交结果

预留 INSERT 和最终 COMMIT 都可能结果未知，不能把连接错误等同 rollback。
在新主库连接以原 operation key进行唯一键对账/行锁，等原事务结束：

- committed：按保存的结果身份返回；不得新建 publication 重发。
- aborted：拒绝原操作；tombstone 保留，不能删 key让迟到 writer复活。
- prepared：Session权威先 fence原 writer，再锁原 publication决定使用已 durable
  结果进行受控结算，或标 aborted；不得绕过后续 state/epoch/active Turn 校验。
- 尚查不到预留行：**不是已失败证明**。用同 key 的拒绝预留 INSERT参与唯一键竞争，
  等原 INSERT最终胜负；若拒绝行先胜则原 INSERT只能冲突/读取 aborted。Session
  操作还须先 fence原 generation。锁等待/主库不可达则保持 unknown，不删内容。

对账器可在新 generation 下结算已存在的旧 durable输出，但只更新原 operation，
验证原 admitted Turn仍活动且state没有推进；它不获准继续Agent执行。对账事务与
writer终态提交在相同锁序串行，不能在“查 prepared”和“标 aborted”之间解锁。
分享也遵循此规则：修复当前 catch任意数据库错误后立即 unlink snapshot的做法。

## 6. checkpoint、事件与恢复根

2026-10-30 已确认执行中按工具完成、审批状态变化及 checkpoint/compact/reset 等语义
边界发布，纯流式文本按有界批次合并，不逐 token 提交。边界提交失败或未知时停止推进
模型/下一工具，先以原 operation key 对账；不重跑已完成工具，不自动跨 Pod 续跑。
仅在确认提交且 authority/状态仍有效时继续；批次阈值由基准确定，见 ADR 0039。

Chat `epoch` 是 checkpoint epoch，Session `context_epoch` 是共享上下文 reset
边界，两者不混用；Turn固定记录context_epoch，publication.epoch指Chat checkpoint epoch。`/new`/`/clear` 原子发布新恢复根、更新对应 epoch/水位/可见性，
不创建 compaction boundary。保留旧 epoch已提交历史，当前删除即时生效。

checkpoint publication记录 base revision、目标revision、event watermark、来源
order prefix与extent；提交校验精确epoch和基线。后台候选只在NAS，提交前不是当前
checkpoint；candidate丢弃不改变已提交root。旧epoch worker不能覆盖新root。

事件以 publication 的连续 `event_first..event_last`范围索引，commit持Chat行锁检查
接续 `event_high+1`并推进水位。不允许范围重叠/跳号，不靠唯一键或目录名隐式保证。
按范围索引加bundle block table只读需要部分，不逐事件建表；语义fact始终留NAS。

恢复root包含全部当前恢复模块，不是只存`agent.memory`，也不等于复制全部冷热
历史。root/checkpoint若引用别的bundle，必须在同提交建立retentions
`kind=dependency, retention_key=dep:<source publication id>, member_key=target id`，
禁止仅在NAS中藏依赖。依赖只指向较早publication，构成DAG；GC先标source删除，
source物理删除完成后才释放其dependency pins。当前root可自包含短状态；有界大小
阈值与写放大由基准确定，大历史通过已索引occurrence和显式依赖共享，禁止每次
Turn重写全部历史或无限depth的root链。

## 7. 删除、分享、读 pin与回收

Chat删除先提交deleted_at及归档不可见，保留Session、Turn关联和永久share。
当前共享Session状态依独立保留规则，不因最后一个Chat删除就推定可清空。数据库
行先保留tombstone，不做级联；后续物理清理单独审查引用。永久share的旧token、
snapshot_key、creator、tenant和访问计数保留，扩展publication+offset/length；
历史原Chat已删除的share可使用无Chat/Session的publication，不复活Chat。新share
行与publication提交使用同连接同事务；旧NULL locator在切换前全部回填并核验。

GC可回收仅：原操作已知终结、没有有效Message正文/归档成员、Session恢复root、
Turn admission/terminal、Chat checkpoint/content meta、有效share、retained_history
或retention引用。retained_history保护尚需保留的旧epoch/event/checkpoint；Chat
删除可释放Chat专属历史，但仍须检查Session/backup/share等引用。不按年龄删有效
历史，unknown/prepared一律不可回收。所有反查都有publication索引。

新增live引用、reader/backup pin都锁publication，检查`gc_state=live`后建立；GC
同样锁它并在事务内查引用再置deleting。不得先NOT EXISTS再事务外标记。GC
候选扫描与mark分事务；mark拿guard/publication锁后，全部引用反查使用current
read，不沿用扫描阶段的RR旧快照，避免漏看已提交的新引用。Reader拿锁建立pin时
也重查对应occurrence仍可见及当前权限；发生变化则重新取页，不返回已失效locator。
后台unlink完成置reclaimed，失败保持deleting可重试；禁止重新对deleting对象加引用。删除
权限只限系统生成目录内精确对象，不能由请求传入路径。aborted操作若原writer
仍可能完成NAS IO，确认其incarnation终止/IO停止后才最终回收；迟到文件永远不可
发布，可重复扫描对应tombstone路径清理，不能以一次unlink承诺无孤儿。

Message有效引用的谓词：非archive需deleted_at为空且current_visible=1；archive
需该occurrence未删除且拥有它的Chat未删除。压缩中不再展示的在线行同事务清除
current_visible/in_context，独立archive occurrence继续持有body locator。另有Turn
准入/终态、root、retained_history和pins时仍保留；旧行仅用于审计并不等于永久body引用。

Reader按页批量pin去重publication，释放读后pin。简单协议不靠TTL自动过期；死
reader清理需要确认owner incarnation已结束或请求已取消且不可继续，不仅凭时间。
防止泄漏的运维reaper须可审计；在此之前宁可暂留，不破坏读取。建立pin后读取前
再检查当前Chat权限/删除，不能以pin绕过撤权。

## 8. 备份、一致快照与切换

常规备份：短暂停该scope会话发布，等在途commit收敛；锁retentions的本scope `gc_guard`
行，建立scope级backup_fence并与正在mark deleting的GC串行，捕获一致DB快照，
确认快照中引用无已deleting对象。恢复写入后，从该快照枚举该恢复点所有有效引用的committed/live publication及dependency闭包，
分批建backup pins，不给aborted/deleting/reclaimed tombstone建pin，完成才在guard下撤fence；复制可以更晚完成。GC每次mark也先短锁
guard检查scope fence；每个scope独立guard，仅该行guard_marker=1，其余pin/fence为NULL；
唯一索引保证恰有一行。新scope在首次publication/GC/backup前由初始化事务竞争
创建guard（随机retention_key、member_key=0），不依赖Session存在，兼容孤立share。
guard不参与普通retention释放/reaper；只串行该scope极短mark/fence事务，
不持锁扫描大批对象；guard不覆盖NAS IO、
复制或全量枚举。备份失败/超时不会
自动撤保护；完整metadata+内容+恢复演练后才标成功。SQL没有单独backup头表，
retention_key重复保存snapshot_ref等少量元数据。备份释放必须显式。

新表目标版本暂定MySQL 8.0.21+、InnoDB DYNAMIC、page size至少16KiB和strict SQL
mode；这是本稿选择的验收基线，**未证明生产MySQL/TDSQL满足**。若生产5.7须另行
兼容评审，不从已有cron SQL推断全系统版本。没有自动建表/补列；既有表先SHOW
CREATE TABLE，DDL在隔离库验证后形成正式生产迁移。建表非事务回滚操作，SQL
故意不加IF NOT EXISTS掩盖旧结构。

### 8.1 导入范围与校验

2026-10-30 按用户最新要求改为一次连续停机：提前准备新版本和迁移工具，关闭入口、
排空并停止全部旧 writer 后固定最终一致源；在同一停机窗口内完成备份、导入、全量校验
和七表安装，之后才启动新服务并恢复流量。旧服务在此期间不恢复写入，新版本不提前
运行会修改生产数据的初始化或后台任务。详见 ADR 0045 和后续迁移方案 §6。

保留最终排空边界之前全部已提交的消息、删除、分享及计数、标题、专家绑定与恢复模块；
撤销此前快照后少量旧写可丢的许可，不允许由迁移导致删除复活或分享 token 丢失。
选定内部一致且完整的最终旧数据快照，用当前 reader 在离线副本读取，不拼接半写文件。
pending/尾部先按旧协议收敛，无法解释或损坏的记录阻止切换。无需双写、CDC、增量追赶
或新增专用应用拒写门；通过部署维护操作保持所有旧 writer 停止，新旧权威不混跑。
停机时长包含排空、备份/复制、导入、校验、切表及启动检查，须在生产规模副本上计时。

| 输入 | 必须导入/保留 | 禁止的捷径 |
| --- | --- | --- |
| chats.json | 全Chat UUID/时间/标题/meta、模式、场景/专家快照和CAS绑定 | 按Session去重Chat；整份meta直接入TEXT而混入正文 |
| session v1/v2 | 所有顶层恢复模块、turn_states、memory、task/error、Stop/审批/skill/Hook/context usage | 只导agent；按文件名猜identity |
| session v3 | 根及整套.turn-ledger generation、metadata/turns/references/latest/pending | 只读主JSON；把TurnHistorySnapshot当执行snapshot |
| checkpoint v1 | manifest/checkpoint/events/epochs/evidence/candidates/batches中的已提交根与保留历史 | 只复制checkpoint.json |
| journal v2 | state-v2、objects-v2、各epochs-v2段与sealed meta、previous epoch、quarantine清单 | 导入未提交尾部或忽略pending |
| archive/tombstone | 每Chat boundary及immutable batch、dialog/.deleted语义 | 把清空当boundary；复活删除Chat |
| permanent shares | 全现存token/快照/归属/计数，含原Chat已删除的share | 级联删除或只迁活跃Chat的share |

先按旧协议解决pending/尾部，旧reader已有校验照常执行，但不新增校验摘要。
`export_legacy_state`会剥离ledger/admission元数据，不能作为唯一导入入口。文件
与计数清单须覆盖全部scope/workspace、未知顶层模块和不可解析记录。不可恢复项
阻止切换，不能跳过后仍标成功。无新checksum不等于只比行数：读取并解析所有
提交payload，逐项对照角色、顺序、内容、边界、epoch、水位、归属及恢复状态。

导入先进入隔离库/NAS新前缀，分批按Session做事务，建立确定性源位置到整数ID
映射；可在外部导入清单保存原文件/记录位置，不计算source fingerprint。
导入重试使用同清单中的operation key，不重复生成occurrence。映射必须记录旧
多个Chat和在线/归档重叠关系，来源不明时保留独立occurrence，不按Msg.id合并。

### 8.2 回退

兼容目标固定为切换前最后一个生产镜像 digest，导出它实际支持的完整格式；不扩展
额外更早版本的兼容，也不预设现网一定已支持 v3/v2。此能力须在上线前用固定镜像实测。

旧数据快照保持只读；新版本尚无写入时可恢复原表/原NAS和固定旧镜像。初始化或冒烟一旦产生提交也视为新写，不能靠回滚镜像丢弃。新写后先停新writer、完成
unknown publication对账，导出选定DB一致状态及所有引用NAS内容为当前旧reader
可读的session/ledger/journal/Chat/share格式；保留全部模块和新写入。若旧格式
无法表达新状态，阻止回退并修复导出器，不覆盖原备份。演练必须包含切换后新增
问答、压缩、Stop、分享、删除及专家解绑，再验证旧版本可读/继续写。

## 9. 分阶段交付与验收

1. **schema/DAO阶段**：隔离目标DB执行DDL，验证identity、NULL唯一键、TEXT容量、
   事务/锁序、EXPLAIN；以现行API适配器契约测试数据库实现。
2. **发布/执行阶段**：接通准入、busy、generation fencing、Stop终态竞争、unknown
   reconciliation、批量pin/GC；生产等价两Pod/NAS故障注入。
3. **迁移/回退阶段**：完整旧格式样本预览/导入/逐项核验/增量新写/回退演练。
4. **切换阶段**：按 ADR 0044/0045，先在隔离环境按代表scope演练，生产一次连续停机完成备份、迁移、校验和整体切换；监控DB锁等待/unknown年龄、
   引用损坏、GC积压、正文IO与p95。无DB-free备用模式，不靠双写维持两套权威。

| 验收类别 | 至少覆盖 |
| --- | --- |
| 身份与CRUD | tenant/source/agent/user/channel隔离；同Session多Chat；Chat重绑；删除不复活 |
| 专家新逻辑 | 原子创建、丢响应重试、binding冲突/revision、Plan/Goal解除、mode恢复不复活 |
| Turn并发 | 独立问题busy、同msgid幂等、session范围冲突、Stop/自然完成只有一个终态 |
| 故障发布 | INSERT/COMMIT响应丢失、prepared/aborted迟到writer、lease过期、旧generation、NAS缺失 |
| 历史UI | 原ID/fragment稳定、重复source ID、工具跨页、task_run/Stop/error、压缩边界、在线/归档重叠 |
| 恢复 | v1/v2/v3冷热Turn、全部恢复模块、checkpoint旧/新epoch、水位、后台旧candidate拒绝 |
| GC/备份 | reader pin与GC竞争、backup fence安装竞争、依赖DAG、永久share和Session独立保留 |
| 性能 | 长历史页范围读、热Turn点查、列表轻投影、独立Session并行、批量NAS IO与写放大 |

当前专项测试证据见复核文档：后端175、前端126通过，证明的是**现行NAS契约**。
本稿SQL只能先做静态解析/结构核对；真实目标数据库DDL、两Pod/NAS与迁移/回退验收
仍是实施阶段必做条件，不能用现有测试通过替代。

## 10. 2026-10-30 现有查询全量覆盖复核

本节基线为本地 `c222540b1a138a30a9a122bd4b28ee1dc910ac05`，未拉取远端。相对
初稿基线新增提交主要为cron auth mirror及Shell路径；仍逐项重新核查当前代码，而非
沿用旧测试结论。结论是：**六表+现有share表+NAS可以承载现有会话域查询；原草案
缺少的兼容规格与索引已补齐。尚无MySQL adapter，不能宣称运行时全查询已验证。**

### 10.1 查询覆盖矩阵

下表路径省略`/api`、可选Agent路由前缀。出处均为当前源码；“覆盖”指设计有明确
数据与适配路径，最终仍须运行数据库后端的同一组契约测试。

| 现有功能/入口 | 现有语义 | 目标读取与兼容要求 | 当前依据 |
| --- | --- | --- | --- |
| `GET /chats` | user/channel可选过滤，updated降序；不带分页返回完整数组 | scope内Chat查询，完整meta组装；管理台本地substring筛选/10条分页不能只收到第一页 | `runner/api.py:1036-1156`；`console/pages/Control/Sessions/index.tsx:53-70,185-187` |
| page/page_size列表 | `items,total,page,page_size,has_more`，参数必须成对 | 精确COUNT同过滤，页码兼容OFFSET成本明确；不擅自改cursor response | `repo/base.py:206-227`；`api.py:1073-1088` |
| cursor列表 | v2 updated+UUID；兼容v1 created+UUID；cursor与page互斥，空cursor启动 | 保留双版本排序、严格小于边界、完整total、page=1、cursor推进；channel精确过滤 | `repo/base.py:249-342` |
| UUID→Chat detail | 共享memory/task/error+Chat anchor、独立archive metadata、状态、完整meta | Message各layer、Session/Chat JOIN、boundary publication；只读已提交快照，不等待长执行锁 | `api.py:859-945,1589-1625` |
| Session→Chat内部查询 | user可省略；workspace内匹配channel/external Session，选updated最近；时间相同时保留原registry顺序 | sessions lookup索引+Chat session索引；不强加请求user，不用draft映射替代普通最新查询 | `manager.py:189-217,509-543`；`json_repo.py:223-246` |
| `/chats/answer-turn` | 请求user必需；显式chat_id优先；无chat_id时查console Session候选，尊重已存Turn Chat归属；非user msgid 404 | Turn唯一键/Message source索引加兼容assembler；只读所选冷Turn，不全量ledger；完整限定见10.2 | `api.py:1359-1501`；`test_cold_turn_point_queries.py:44` |
| `/{id}/history?before&limit` | limit 1..50按源Msg计；messages正序、boundary、has_more/next_cursor | archive order范围读，保留原source/fragment ID；has_more时cursor非空且推进；源ID索引用于anchor suppression | `api.py:1505-1524`；`conversation_archive.py:1709`；前端`MessageList/index.tsx:437-448` |
| `/{id}/context-usage` | numeric snapshot或available=false；运行中/失效标记stale；不能构造Agent取数 | Session.context_usage_text+invalid与恢复根同提交；应用模型校验，不解析SQL TEXT；见10.3 | `api.py:1531-1586`；`context_usage.py:30-66` |
| `/{id}/expert-binding` | revision/expert_id加实时enabled/available/name/reason；GET可能因功能禁用而解除旧绑定 | typed binding+当前专家/配置域装配，保留GET reconciliation CAS；历史执行身份仍冻结 | `api.py:1629-1652`；`expert_binding.py` |
| `/tracing/chats`及`/{id}?user_id` | 按目标user/source/agent定位workspace；不等同当前登录用户/chats | 目标scope下新repository；详情保留当前独立视图，不能擅自换完整assembler，见10.4 | `routers/tracing.py:54-180` |
| `/{id}/share-options` | 所有冷热候选+该Chat全archive；messages和用户msgid keyed turn_statuses | 全量兼容组合查询，状态优先存储值再旧规则推断；不能仅当前页/最新Turn | `chat_sharing/router.py:60-75,132-186,223-250` |
| 创建分享时读取选中问答 | turn_ids为用户source Msg ID；按原历史顺序输出，仅completed可分享并脱敏 | 点查所选冷Turn；archive范围组合，保留旧切片语义，不映射runtime UUID/bigint；创建前重验状态/权限 | `chat_sharing/service.py:59-117`；`test_cold_turn_point_queries.py:80,133` |
| `/chat-shares/{token}` | 永久不可变快照，匿名token访问、访问计数；原Chat可已删除 | existing share locator+publication，无Chat存活JOIN；缺token404、正文/计数故障503，旧孤立身份见10.5 | `chat_sharing/router.py:304-322`；`service.py:121-138` |
| reconnect/终态snapshot读取 | runtime cursor独立；当前live attach或committed terminal；settling时不可假成功 | 活动stream仍owner进程，数据库给权威状态/已提交结果；不能把新generation当自动继续Agent | `routers/console.py:1049-1195,2044-2138` |
| CLI与Agent-scoped会话列表/详情 | 复用/chats全量/分页与UUID，Agent header/路由限定范围 | 同一query adapter；不另保留file repository回退 | `cli/chats_cmd.py:56-155`；`test_chat_pagination.py:488` |
| 任务会话、Cron结果预览/恢复 | session_kind/task_job_id/management_owner及task_run，顶层receipts参与恢复 | route meta保留；memory/task索引+全模块root，managed_cron_result_receipts明确导入；cron定义/执行库不搬 | `crons/managed_resources.py:63-80,141-173`；`manager.py:2491-2519,2616-2633`；`managed_receipts.py:47-69` |
| W+投影/工具Chat读取 | 工具自行构造JsonChatRepository且判断chats.json存在 | 必须改用可信scope统一repository注入；不能只改workspace factory后留下旧旁路 | `agents/tools/emit_wplus_sop_event.py:105-118`；`app/wplus_sop/service.py:1361-1425` |

### 10.2 Turn点查与旧无Turn状态数据

`turn_id`逻辑关联及索引是性能快路径，不是旧API存在性的唯一判据。当前`_slice_answer_turn`
按用户source Msg锚点切到下个user前；测试覆盖没有turn_states仍返回问答。旧数据
可能没有runtime Turn UUID/终态或明确Chat归属，不能强行生成“成功执行过”的Turn
或以NULL turn_id为由404。

新协议创建的问答由Turn唯一键直达。旧问答先用
`ix_conv_message_source(session_id,source_msg_key,layer)`找所有候选（非唯一），按
原可见视图及稳定顺序定位用户锚点，再取到下一个user的顺序范围（单Turn本身可很大），并运行原fragment
转换/anchor suppression/排序。重复源ID不能合并，缺可靠终态时`turn_status=NULL`；
终态优先原stored值，再消息metadata原规则，不把未知变completed。已完全归档而
不在该API现有可见视图的问答不因为存在archive行就自动扩大此端点语义；分享仍有
自己的全archive组合读。

保留显式chat_id优先：当前传了chat_id同时传sessionid时不会据后者覆盖选择，
不能把写入准入的session mismatch规则照搬到这个读端点。无chat_id按当前console
候选选择，已有Turn归属先定位对应Chat，旧无归属按旧候选次序；身份/Chat删除后
不越权返回。`history_status`单独存现有API状态（completed/cancelled/stopped/failed
等或NULL），内部`state`控制reserved/settling等协议；不得直接把内部succeeded
串作为外部completed，分享只认原规则允许的completed。

### 10.3 新补字段与索引，不增加表

- Session `context_usage_text`：只有数字、状态、时间的已校验序列化快照，属于
  metadata；`context_usage_invalid`与Agent清理状态同事务安装/失效。读取计算
  `stale=activeTurn属于当前Chat且running/stopping OR invalid`；缺失/损坏/未来schema仍available=false，
  数据库故障仍是错误，不能降级为零值或不可用快照。该轻查询不读完整恢复正文。
- Session `ix_conv_session_lookup(scope,channel,external_session,user)`支持省略user
  的内部查询；仍在可信workspace scope内，可多个Session匹配再按Chat更新时间归并。
- Chat `registry_order`导入原chats.json数组位置，新记录用递增序分配；重复Session
  在updated_at相同时原规则选registry较早者；v1/v2分页仍使用原UUID tie-break。
  原子专家create-if-absent在旧重复数据中取registry第一匹配，不与普通最新查询混用。
- Chat新增channel recent索引，既有不带channel的recent索引保留。user未指定的
  全workspace管理查询、旧created-time cursor可先用scope前缀范围+filesort，不承诺
  与用户侧栏同成本；是否再建索引必须看真实EXPLAIN，不为少量兼容路径盲目加索引。
- Message source/archive-source非唯一索引分别支持旧Turn定位与archive anchor
  suppression，避免为几个锚点遍历全部NAS archive正文；正文读取仍按extent。

### 10.4 组合视图、动态状态与外部领域

当前Tracing详情虽注释称完整记录，实际只返回memory+task及审批叠加，不返回
普通详情的model error/补充anchor/archive/chat metadata。此次迁移保持它的现有
返回视图（包括可空chat和默认archive字段）；若产品要统一完整详情，另做显式变更
及前端回归，不能借数据库切换悄悄增减。目标user/source/agent的解析和现有管理端
准入保持，审批overlay也在目标scope下查询，不错用操作者scope。

会话详情与share的多步读取在一次短DB read snapshot中固定metadata/locator和
order high，批量建立reader pins后再读NAS；不要跨步骤混用不同epoch/root。当前
删除/权限在返回前仍检查；分享创建前重验。大规模share-options本来是全量响应，
按批读取可减内存峰值但不能截断候选；已有创建流程即使仅hydrate选中cold Turn，
仍扫描全archive，未来用Message/Turn索引优化时必须保持原切片/排序/状态行为。

动态approval status由当前审批服务按requestId批量覆盖，消息payload不是当前审批
权威；仅在当前有overlay的端点保持overlay（普通详情/Tracing/分享等），archive
目前只做脱敏，不能未经决定更改为另一个状态策略。expert enabled/available由
当前专家注册及配置服务提供；历史执行专家不得重解析为最新配置。

以下“会话相关”功能继续使用独立存储，六表只承担Chat授权、关联或消息引用：

| 独立领域 | 保留行为与边界 | 依据 |
| --- | --- | --- |
| Monitor tracing sessions/stats/traces/user-messages | 原trace库的日期、分行、错误、模型/skill/MCP筛选与聚合，不由会话六表推算 | `console/src/api/modules/tracing.ts:574-604`及monitor query service |
| Goal/SubAgent查询 | 先新repository验证Chat存活及scope，再独立store；保留旧无parent_chat_id兼容 | `goals/router.py:54-59,121-125`；`routers/subagents.py:29-56`；`run_access.py:23-64` |
| generated-files/文件管理/附件下载 | 仍查Agent目录、media/static、路径安全服务；NAS迁移不能搬掉现有附件URL对应文件 | `routers/console.py:2635-2675,2697-3024` |
| context-references | 继续发现scope内Skills/MCP/文件；不等于Message历史 | `routers/console.py:3032-3044` |
| push/suggestions/qa-content | 原消费队列、有界TTL辅助缓存及scope匹配；读取可能消费缓存，不能伪装成历史SELECT | `routers/console.py:3048-3143`；`suggestions/store.py:292-395` |
| Cron定义/执行/auth、W+业务store、审批 | 独立领域不搬表，但所有查Chat/Session的路径接新adapter；不能留JsonChatRepository旁路 | `crons/managed_receipts.py`；`emit_wplus_sop_event.py:105-118` |

### 10.5 旧孤立永久分享的身份闭合

旧share只有tenant/creator/chat_id/snapshot_key，没有source/agent；原Chat已删除且
无可靠映射时，不能猜default workspace，也不能因此丢token。publication和retention
新增`scope_kind`，通常为`workspace`，正常会话仍要求完整真实scope。仅旧孤立share
导入允许`scope_kind=legacy_share`，tenant取旧记录原值，source/agent存空二进制
“未记录”而非真实默认身份，chat_id/session_id=NULL；迁移清单记录来源证据。

该命名空间仅供现有share token读取、备份、pin与GC，不参与普通Session/Chat查询
或写入，不能新建会话。scope guard与fence的唯一键/查询必须包含scope_kind，避免
空值与真实空scope碰撞。能够从可信旧映射恢复source/agent的share用workspace
scope；不从creator或不透明snapshot_key猜。token读取、reader pin和GC全部从publication
取得scope_kind及scope，不从当前请求或旧share.tenant反推workspace；legacy_share
仅导入可创建，强制chat/session为NULL且source/agent为空。scope级备份需显式纳入相应tenant的
legacy_share集合，不能只枚举活跃workspace而遗漏它。新分享必须使用真实workspace
scope。保留token及原snapshot_key，系统化新bundle locator通过独立回填安装。

### 10.6 适配交付门槛

保留所有既有API形状、身份和错误码，包括无分页数组、has_more/cursor推进、完整
meta、用户源msgid、旧Turn无状态、不可用context快照、永久token、目标用户Tracing
以及任务关联；不新增全文搜索等当前不存在的会话库能力。前端临时timestamp→realId
映射仍由sessionStorage管理，不建数据库别名表；逻辑Session深链的前端createdAt
选择与后端updatedAt选择各自保留，不强行统一。

切换前必须枚举全部`JsonChatRepository`/chats.json/Session直接读取者，替换工厂外
旁路；在数据库实现上运行本节对应契约，并补充：无user多匹配、equal updated时间、
旧无Turn状态/重复源ID、原Chat已删且无scope的share、跨目标scope approval overlay、
W+不依赖chats.json、管理台全量数组、channel过滤EXPLAIN。当前源码专项通过只能
说明已读懂旧契约；不等于新MySQL实现或目标库已验收。

### 10.7 本次核验记录

2026-10-30在上述HEAD的现行NAS实现运行：主查询后端8文件79 passed；周边查询
7文件37 passed、9 skipped（push路由测试显式标记Requires full app dependencies）；
前端11文件105 passed。另单独重跑managed_cron_result_recovery的2项通过，与周边
集合重叠，不重复计数。合计后端116 passed/9 skipped，前端105 passed。

后端主集合为test_chat_pagination、test_chat_answer_turn_api、
test_chat_compaction_history_api、test_chat_history_message_identity、
test_cold_turn_point_queries、chat_sharing/test_router、test_tracing_chats_api、
test_context_usage。周边为test_context_references、test_console_push_store、
test_suggestions_store、test_console_suggestions、test_cron_task_view、
test_managed_cron_result_recovery、test_emit_wplus_sop_event。

前端集合覆盖chat.contextUsage、chat.expertBinding、cronTaskSources、
sessionApi/index/historyAssembly/messageIdentity/taskSessions/initialSessionSelection、
shareSelection、useExpertBinding、useContextUsageController。

SQL重新以sqlglot MySQL方言解析：仍六张新表，无JSON字段类型；41条显式二级/
唯一索引的字段引用核对通过，最大声明键宽2112字节；相对文档链接与空白检查通过。
这些都是静态与旧契约验证；未连接目标MySQL执行DDL/EXPLAIN，也未实现/测试
MySQL repository或切换数据。新增字段/索引仍是评审草案。

## 11. 2026-10-30 SQL规范补充

按用户要求，两份SQL不声明物理外键，不使用CONSTRAINT或CHECK；主键、UNIQUE KEY
和普通KEY保留，继续让数据库承担身份唯一性、幂等竞争和范围查询。全部表及字段
有中文COMMENT。6张新表共151个字段；现有分享表3个新增字段及8个旧字段均补注释。
分享旧字段的注释DDL从information_schema读取实际类型、字符集、排序规则、NULL及
默认值，避免用统一定义覆盖生产token列；schema不符时停止，先人工评审。

引用完整性由统一repository的短事务负责：先验证scope及关联记录、publication状态、
范围和CAS，再安装引用；GC与新增引用共锁publication行，删除仍按独立生命周期。
后台巡检应检查孤立引用和scope不一致，只报告/隔离并阻止危险回收，不静默补零或
级联删除。移除物理关联后补显式publication(turn_id,id)和message(origin_chat_id,id)
索引，避免丢失此前由数据库隐式建立的关联索引；未增加表、未更改字段类型或原唯一键。
这些是DDL设计更新，不意味着已执行目标数据库变更。

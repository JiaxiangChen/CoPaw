# 新任务执行登记与 SWE 执行入口

状态：执行核心、本地接口、新版普通/批回调、冻结优先级、模型池轮转与统一容量反馈已有本地实现；恢复器及容量领取已接入应用启动。最终隔离核对、集中 SQL 及真实环境验证仍待完成，本文不是第二阶段全部完成的声明。

## 分工

Scheduler 在 `swe_cron_execution_runs` 登记已经收到的执行请求，保存完整身份、定义快照、逻辑执行期和冻结的 Workflow 版本，再调用 SWE。普通和批入口同一逻辑执行期使用同一个执行 ID；手动请求使用独立请求键。

SWE 只接受数据库已有的新链路执行记录，不接受调用方再传一份任务定义。SWE 在开始 Agent/文本/Workflow 前落受理、运行状态，完成后在一个事务中写执行登记、已有 `swe_cron_executions` 历史和任务运行状态。Scheduler 从同一个数据库观察结果，不需要 SWE 回调最终结果。

新入口不使用旧 CronManager.run_job，不读任务文件找定义，也不读写旧 `_states`。原执行器继续负责会话提交、产物、追踪和通道投递；Chat/会话文件本身不属于本次存储迁移范围。工作目录来自服务端实际工作区，不信任任务元数据里的路径。

## SWE 内部执行接口

`POST /api/internal/cron/managed/execute`

身份 Header 与新版管理接口一致，使用真实操作人和目标任务 tenant/source/Agent，不新增必配内部密钥。多 Agent 路径与 Header 不一致时拒绝。

```json
{
  "execution_id": "持久执行记录ID",
  "job_id": "任务定义ID",
  "dispatch_token": "Scheduler本次派发的共库租约标识"
}
```

额外的 spec、scope、调度状态等 Body 字段会被拒绝。授权及完整身份核对先于工作区初始化。

dispatch_token 由 Scheduler 从本次持久派发租约自动携带，不需要智拓提交，也不是环境配置或新增认证密钥。SWE 首次受理时在同一事务核对状态仍为 dispatching、占位持有者匹配且未过期，否则返回 409/cron_dispatch_lease_lost，不初始化工作区。受理先持久化，再准备并启动执行器；准备暂时不可用时仍保留 accepted 供同次请求恢复，不重建执行。

受理返回 202，例如：

```json
{
  "execution_id": "持久执行记录ID",
  "job_id": "任务定义ID",
  "status": "accepted",
  "accepted": true
}
```

这不是执行成功回执。重复请求可以返回已经运行或结束的同一记录，不重复启动。HTTP 超时不能作为“未受理”的依据；Scheduler 必须读共库事实。即使 HTTP 返回成功，没有数据库受理记录也不算受理成功。

## 并发和结果规则

- 任务定义行和执行行按一致顺序加锁；实际运行以执行记录和 owner 为准，不以实例内列表作唯一事实。
- 任务停用、自动暂停或删除后，尚未开始的工作跳过；已开始的执行继续。单纯切换三态不取消已入队工作。
- 每次执行使用入队快照；Workflow 不在执行时改用最新配置。冻结版本不可用时显式失败。
- 完成事务失败不报成功，重复完成不重复插历史或累加未读；合法旧快照仍累计未读，最近状态和预览按实际启动顺序归约，不用定义版本或实例墙钟排除有效结果。
- 手动成功历史标为已读，不累加自动任务未读；自动成功沿用未读阈值自动暂停配置，暂停事实落状态表，不由 SWE 修改定义表。
- 通知沿用现有历史队列字段、原排程/提前散列时间和周末配置。删除后的在途结果保留历史但不再产生完成通知。
- 删除资源前核对全部 `running` 执行，不能只检查聚合 `last_status`。正常延后清理由已装配的后台接续。
- SWE 可在执行超时加清理宽限期后，将遗失的运行登记收敛为 `error/cron_execution_lease_expired`，表示结果未能确认，不表示证明外部业务没有发生；旧 owner 随后不能覆盖结果。独立普通/手动任务不自动重跑；2026-10-22 起已登记新版批成员按用户要求沿用旧版失败/超时重试规则，接受可能重复外部动作的风险，详见 cron_batch_board_deployment.md。
- 派发确认失败重试同一个 execution_id，连续三次未确认后展示 `dispatch_failed`，后台按退避继续核对和投递同一个请求；每次核对仍未确认受理后，依次等待 5、10、20、30、60 秒，后续固定等待 60 秒，实际投递还受后台扫描及容量限制。已受理待执行的 5 秒间隔及运行中结果恢复的 30 秒间隔不变。这不是实际 Agent/Workflow 失败，不另建执行、不允许业务重跑。迟到原请求仍按相同 ID 去重。

## 结果恢复和后台边界

`POST /api/internal/cron/managed/recover` 使用相同的 Header，但 Body 仅含 execution_id/job_id，不带 dispatch_token。它仅恢复已生成结果的提交；优先读取已有会话产物中的待提交结果凭证，之后才判断是否超过运行期限。不得重新调用执行器来补结果。

Scheduler 恢复器只处理新协议、已经持久受理的工作：分发、三态、待派发执行、运行结果和正常延后清理。各工作域独立，长分发不阻塞派发及结果恢复；真正管理步骤失败仍人工续办。处理器已接入应用启动/退出，先只读检查所需表列并启用合并容量统计，再启动循环；缺少结构或既有连接配置时不启动新后台，不执行 DDL。后台已知不可用时，依赖它的新工作返回 503，而查询、停用等处置入口保留。

### 平台同步窗口的触发

平台绑定校验不等同于要求所有管理日志均已 completed：仍须匹配完整任务身份、新协议、状态表中已保存的平台 ID，以及持久化操作/同步计划。未知绑定、跨来源引用、无计划或失效的未完成引用拒绝受理。

- 创建、更新或删除进行中，合法绑定触发只保存 skipped 回执；即使随后发布完成，重放同一日志或逻辑计划期也不补执行。
- 三态部分切换失败不对全组禁触发：已生效批对象继续由新批入口受理，未切换的普通对象保留原普通入口；切普通时未发布的对象仍按实际批状态判断。模式发布前已保存的 skipped 回执不会在发布后变成执行。
- 分发已经持久保存批入口同步计划后，平台更新/启停响应尚未返回或丢失，不阻断已知绑定的有效触发；查询仍如实显示 sync_status=pending/failed，并在新一轮分发处理失败。实际批暂停、停用、删除及时间窗口规则照常生效。

异步超时处理兼容项目支持的 Python 3.10 独立 asyncio.TimeoutError；恢复轮询超时不结束循环，关闭超时先取消并收拢后台任务，SWE 执行超时仍登记 cron_execution_timeout。兼容测试模拟旧异常类型，不等同于已在实际 Python 3.10 部署运行。

## 手工建表 SQL

以下仅为人工执行材料，应用不会自动建此表；执行前与当前数据库结构核对。`swe_cron_executions` 不加字段，新增登记表通过 history_execution_id 引用原历史主键。新登记及状态时间使用 UTC；写入原历史时保持其现有北京时间约定。

```sql
CREATE TABLE IF NOT EXISTS swe_cron_execution_runs (
  execution_id VARCHAR(64) NOT NULL COMMENT '执行登记ID，同次执行保持不变',
  execution_key CHAR(64) NOT NULL COMMENT '完整任务身份及逻辑执行期去重摘要',
  queue_order BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '入队单调序号，优先级相同时稳定排序，不依赖墙钟',
  dispatch_order INT NOT NULL DEFAULT 0 COMMENT '受理时按既有优先级热度规则冻结的批内顺序',
  tenant_id VARCHAR(128) NOT NULL COMMENT '任务所属租户ID',
  source_id VARCHAR(128) NOT NULL COMMENT '任务所属来源ID',
  agent_id VARCHAR(128) NOT NULL COMMENT '任务所属Agent ID',
  job_id VARCHAR(64) NOT NULL COMMENT '定时任务定义ID',
  scope_id VARCHAR(512) NOT NULL COMMENT '完整租户来源作用域',
  actor_id VARCHAR(128) NOT NULL COMMENT '受理本次请求的真实操作人',
  definition_version BIGINT NOT NULL COMMENT '入队时冻结的定义版本',
  provider_id VARCHAR(128) NOT NULL COMMENT '入队冻结的模型服务商池标识，未指定时default',
  model_id VARCHAR(128) NOT NULL COMMENT '入队冻结的模型池标识，Workflow取已发布绑定版本',
  enqueued_state_version BIGINT NOT NULL COMMENT '入队时状态水位，用于停用后跳过旧队列',
  started_order BIGINT DEFAULT NULL COMMENT '在状态行锁内分配的实际启动顺序',
  kind VARCHAR(16) NOT NULL COMMENT '入口类型：ordinary普通、batch批、manual手动',
  trigger_key VARCHAR(128) NOT NULL COMMENT '平台逻辑执行期或手动请求键',
  scheduled_fire_at DATETIME(6) DEFAULT NULL COMMENT '逻辑计划时间，UTC',
  not_before_at DATETIME(6) NOT NULL COMMENT '冻结的最早派发时间，UTC，不能用逻辑执行期替代',
  spec_json LONGTEXT NOT NULL COMMENT '完整任务定义与资源绑定快照',
  dispatch_meta_json LONGTEXT NOT NULL COMMENT '冻结的执行参数，不保存认证凭据',
  status VARCHAR(32) NOT NULL COMMENT '排队派发状态及SWE受理运行终态',
  dispatch_attempt INT NOT NULL DEFAULT 0 COMMENT '同一执行ID的派发尝试次数',
  dispatch_owner VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Scheduler派发租约持有者',
  dispatch_lease_until DATETIME(6) DEFAULT NULL COMMENT '派发租约到期时间，UTC',
  run_owner VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'SWE实际运行持有者及结果围栏',
  run_lease_until DATETIME(6) DEFAULT NULL COMMENT '执行超时及清理宽限期截止时间，UTC',
  accepted_at DATETIME(6) DEFAULT NULL COMMENT 'SWE持久受理时间，UTC',
  started_at DATETIME(6) DEFAULT NULL COMMENT '实际开始执行时间，UTC',
  finished_at DATETIME(6) DEFAULT NULL COMMENT '执行结果持久提交时间，UTC',
  history_execution_id BIGINT DEFAULT NULL COMMENT '关联原swe_cron_executions历史主键',
  output_preview VARCHAR(512) NOT NULL DEFAULT '' COMMENT '执行结果简短预览',
  trace_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT '执行追踪ID',
  error_code VARCHAR(128) DEFAULT NULL COMMENT '可公开的错误码，不保存原始密钥错误文本',
  result_json LONGTEXT DEFAULT NULL COMMENT '已脱敏的运行结果元数据',
  settled_status VARCHAR(16) DEFAULT NULL COMMENT 'Scheduler整轮派发收敛结果，空表示尚未确认异步终态',
  settled_at DATETIME(6) DEFAULT NULL COMMENT 'Scheduler观察到整轮结束的时间，UTC，不替代SWE完成时间',
  settled_error_code VARCHAR(128) DEFAULT NULL COMMENT '整轮收敛错误码，不覆盖SWE主执行错误',
  version BIGINT NOT NULL DEFAULT 1 COMMENT '并发更新及租约围栏版本',
  created_at DATETIME(6) NOT NULL COMMENT '本次执行入队时间，UTC',
  updated_at DATETIME(6) NOT NULL COMMENT '最近登记更新时间，UTC',
  PRIMARY KEY (execution_id),
  UNIQUE KEY uk_managed_execution_key (execution_key),
  UNIQUE KEY uk_managed_queue_order (queue_order),
  KEY idx_managed_execution_job (tenant_id, source_id, agent_id, job_id, status, created_at),
  KEY idx_managed_execution_dispatch (status, dispatch_lease_until, not_before_at, updated_at),
  KEY idx_managed_execution_running (status, run_lease_until),
  KEY idx_managed_execution_pool (source_id, provider_id, model_id, status),
  KEY idx_managed_execution_history (history_execution_id),
  KEY idx_managed_execution_settlement (settled_status, status, queue_order),
  KEY idx_managed_execution_feedback (source_id, provider_id, model_id, settled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
COMMENT='Scheduler新任务逐次执行登记表';
```

批内顺序复用既有用户、分行、热度、到期时间及任务身份排序，受理时固定 dispatch_order；派发按 dispatch_order、queue_order 读取，不因配置更新或墙钟变化重排。queue_order 是数据库自增字段，代码不自行分配。热度只读原历史已有 async_status 字段，不新增或改写原历史结构。

执行查询新增三个可空字段：`settled_status`、`settled_at`、`settled_error_code`。`status` 仍是 SWE 主执行事实；主执行 success 且 settled_status 为空时，整轮仍待共库异步结果确认，模型占位和资源清理尚未释放。异步 error 只形成 settled_status=error，不改写 SWE status，也不要求 SWE 最终回传。

已经创建执行登记表的环境，需要先通过 SHOW COLUMNS/SHOW INDEX 核对，只对缺失列和索引执行以下人工更新；不能靠 CREATE IF NOT EXISTS 更新旧草案，也不要把未知异步执行回填成 success。原 swe_cron_executions 不新增字段。

```sql
-- 仅在相应列尚不存在时逐项执行；三个字段由 Scheduler 维护。
ALTER TABLE swe_cron_execution_runs
  ADD COLUMN settled_status VARCHAR(16) DEFAULT NULL COMMENT 'Scheduler整轮派发收敛结果，空表示异步终态尚未确认';
ALTER TABLE swe_cron_execution_runs
  ADD COLUMN settled_at DATETIME(6) DEFAULT NULL COMMENT 'Scheduler观察到整轮结束的时间，UTC';
ALTER TABLE swe_cron_execution_runs
  ADD COLUMN settled_error_code VARCHAR(128) DEFAULT NULL COMMENT '收敛错误码，不覆盖SWE主执行结果';
-- 仅在相应索引尚不存在时执行。
ALTER TABLE swe_cron_execution_runs
  ADD INDEX idx_managed_execution_settlement (settled_status, status, queue_order);
ALTER TABLE swe_cron_execution_runs
  ADD INDEX idx_managed_execution_feedback (source_id, provider_id, model_id, settled_at);
```

已有状态表需要队列失效水位。先检查列，只有缺失才执行 ALTER；应用不自动执行，人工停用/首次自动暂停推进该水位，恢复不清零，模式切换不推进。

```sql
SHOW COLUMNS FROM swe_cron_job_states LIKE 'queue_invalidation_version';
-- 仅在上面的列不存在时执行：
ALTER TABLE swe_cron_job_states
  ADD COLUMN queue_invalidation_version BIGINT NOT NULL DEFAULT 0
  COMMENT '未开始队列的失效水位，入队版本小于本值时跳过';
```

若曾执行旧版执行登记草案，CREATE IF NOT EXISTS 不会更新已有结构。仅对缺失列补充 enqueued_state_version、started_order、not_before_at、provider_id/model_id、dispatch_order、queue_order，按已有状态、入队时间和冻结模型快照评估回填；queue_order 需要唯一自增索引，不能以当前墙钟推断旧批次顺序，也不要盲目把已有队列水位填成 0。最终统一差异语句将在交付材料中汇总。

新版回调另有已受理触发回执表，不使用旧分发任务表，不扫描定义补造平台触发：

```sql
CREATE TABLE IF NOT EXISTS swe_cron_managed_trigger_receipts (
  receipt_id VARCHAR(64) NOT NULL COMMENT '触发回执ID，固定任务入口及逻辑执行期',
  callback_key CHAR(64) DEFAULT NULL COMMENT '平台稳定日志标识的幂等摘要，没有日志标识时为空',
  tenant_id VARCHAR(128) NOT NULL COMMENT '入口所属租户ID',
  source_id VARCHAR(128) NOT NULL COMMENT '入口所属来源ID',
  agent_id VARCHAR(128) NOT NULL COMMENT '入口所属Agent ID',
  job_id VARCHAR(64) NOT NULL COMMENT '入口任务ID，批入口为源任务',
  external_job_id VARCHAR(128) NOT NULL COMMENT '已验证的外部平台绑定ID',
  operation_id VARCHAR(128) NOT NULL COMMENT '绑定对应的管理操作或分发轮次ID',
  kind VARCHAR(16) NOT NULL COMMENT '入口类型：ordinary普通或batch批',
  trigger_id VARCHAR(128) DEFAULT NULL COMMENT '平台稳定执行或日志标识',
  logical_fire_at DATETIME(6) NOT NULL COMMENT '原排程逻辑执行期，UTC',
  physical_fire_at DATETIME(6) NOT NULL COMMENT '考虑批入口提前量的实际计划触发时间，UTC',
  batch_id VARCHAR(64) DEFAULT NULL COMMENT '本次固定批次ID，普通入口为空',
  status VARCHAR(16) NOT NULL COMMENT 'accepted已受理或skipped已跳过',
  response_json LONGTEXT NOT NULL COMMENT '固定受理回执，重放不重新推断执行期',
  members_json LONGTEXT NOT NULL COMMENT '冻结的成员身份、执行ID和优先级',
  created_at DATETIME(6) NOT NULL COMMENT '收到平台触发的处理时间，UTC',
  PRIMARY KEY (receipt_id),
  UNIQUE KEY uk_managed_trigger_callback (callback_key),
  KEY idx_managed_trigger_job (tenant_id, source_id, agent_id, job_id, logical_fire_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
COMMENT='Scheduler新版普通批入口的持久触发回执表';
```

原历史表中的 job_id、tenant_id、session_id 等字段长度沿用现有约束，不能因新增登记表支持更长身份就认为原历史表已自动扩容。真实表结构、字符集、时区和索引需要在预发布核对；当前测试数据库不能替代此项。

## 当前验证边界

新增测试使用真实 SQLite 关系查询和事务；SWE/平台、模型调用边界使用测试替身。另有实际 CronExecutor 文本/Agent 和 WorkflowCronExecutor 集成测试，验证已有会话提交契约及冻结版本。Agent 的 Runner 持久化契约在 Windows 依赖 fcntl，必须在 Linux/WSL 跑该项，不能用跳过结果当通过。

未连接真实业务 MySQL、调度平台或用户模型服务；新版普通/批回调已有本地测试，但后台已装配，但完整优先级/公平性和环境验收尚未完成，也未修改前端。Linux/WSL 已验证真实会话持久化契约；最终验证以交付时记录的最新测试输出为准。

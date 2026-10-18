# 第二阶段创建、更新、删除及 SWE 资源接口（开发记录）

本记录对应 U2 的已实现管理链路，不代表整个第二阶段已可上线。新普通/批执行入口、SWE 执行结果持久化、分发、三态和前端适配仍在后续单元中。

## 新接口

| 方法 | 路径 | 职责 |
| --- | --- | --- |
| POST | /api/scheduler/cron/jobs | 创建 Scheduler 管理的新任务 |
| PUT | /api/scheduler/cron/jobs/{job_id} | 修改内容，准备和平台同步完成后发布新版本 |
| DELETE | /api/scheduler/cron/jobs/{job_id} | 先逻辑删除，再停平台、清理资源 |
| GET | /api/scheduler/cron/jobs/{job_id}/operations/pending | 找回当前未完成操作；完成后返回 operation:null |
| GET | /api/scheduler/cron/jobs/{job_id}/operations/{operation_id} | 查询某次操作的真实进度 |
| POST | /api/scheduler/cron/jobs/{job_id}/operations/{operation_id}/retry | 人工续办原操作，跳过已完成步骤 |
| POST | /api/internal/cron/managed/prepare | SWE 从共库读取已授权操作，准备 Chat/会话/模型或 Workflow |
| POST | /api/internal/cron/managed/cleanup | SWE 只清理指定已删除任务的执行资源 |

上述接口新增，不修改旧 SWE 管理方法、原 Scheduler callback/execution 协议。新 SWE 准备接口不调用旧 create_or_replace_job、_ensure_task_binding，不读写任务 jobs.json 或旧 _states；Chat 本身仍沿用现有资源存储。

## Postman 入参

Header 与新查询接口一致，使用 X-User-Id、X-Tenant-Id、X-Source-Id、X-Agent-Id 及可选一致的 X-Scope-Id。沿用现有可信网关认证及角色，不新增必配内部 Token。调用方不能伪造 Header 或直接把服务端口暴露给不可信客户端。

分行和用户展示姓名沿用 X-Bbk-Id、X-User-Name（URL 编码）Header。普通用户不能创建到另一个人的工作区；管理者更新别人任务时保留原任务拥有者的分行/姓名，不改成操作者的展示身份。SWE 的 agent-scoped 路径也必须与 Header Agent 一致，不能通过路径切换到另一个工作区。

创建示例：

```json
{
  "request_id": "create-example-001",
  "dispatch_state": "ordinary",
  "spec": {
    "name": "查询验证任务",
    "enabled": false,
    "task_type": "agent",
    "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
    "request": {"input": "生成报告"}
  }
}
```

任务 ID 由 Scheduler 分配，不由调用方另分配。request_id 为一次操作的稳定标识，重复内容复用同一操作；同一标识换内容返回冲突。创建重提只查询原操作结果，不隐式重跑已失败阶段，需明确调用 retry。

更新请求包含 request_id、expected_definition_version、spec。内容更新保留任务自身启停，不以客户端默认值恢复任务；启停将在独立控制接口完成。源任务仍有未删除子任务时拒绝内容更新。

DELETE 使用 JSON Body，包含 request_id 和 expected_definition_version。它只删除明确指定的任务，不级联。逻辑删除后的外部失败不撤销删除；失败创建/更新可被删除替代，原操作变为 superseded，之后不能恢复。进行中的有效管理操作仍须先完成或等待，不强行打断；分发与三态并发约束在对应单元统一接入。

SWE prepare/cleanup 的 Body 只有 operation_id、job_id，不接受客户端另传权威 spec 或任意 Chat ID。SWE 读同一数据库操作记录、校验身份及任务归属后才初始化工作区。

## 响应及恢复

- 正常创建完成为 201，更新/删除/重试完成为 200；受理但尚未结束为 202；阶段失败为 503，并保留 operation 对象。
- 参数、身份或版本冲突分别用 400/422、404/409 表达，不返回原始凭据或外部异常全文。
- 操作对象包含 operation_id、job_id、action、status、stage、error_code、版本等。logical_delete_applied=true 表示本次删除标记已落库，不能因 503 就认为任务尚未删除。
- 界面刷新后可以查询 operations/pending 找回失败或待处理操作，不依赖浏览器原错误响应仍在内存。
- 更新新定义先保存在操作记录中，必要步骤成功后才发布；实际运行状态、未读等字段不被管理默认值覆盖。
- SWE 用完整任务身份计算固定 Chat ID，在实际资源仓库中创建/复用；操作重试和实例切换不会另分配 Chat。资源准备与逻辑删除用数据库事务锁协调。
- 注册成功取得并保存ID后再启停；启停失败只重试后续步骤，不新增。2026-10-17用户确认平台没有查询接口，无已保存ID时按注册失败处理，返回platform_registration_failed，原操作人工retry可以再次add。
- 明确拒绝、超时、协议错误等注册失败仍保留任务、Chat及原操作；不重做已完成资源准备。进程中断只留下registration_started而没有ID，也按未注册继续。已失败操作不因查询自动重试，同request_id重提不另建业务任务。
- 平台可能已受理但响应或ID持久保存丢失，因此再次add可能遗留重复/孤立定时器；没有ID无法自动停用该对象。删除只清理已知当前/历史ID，不再等待不存在的查询接口。该风险是用户确认的取舍，不保证平台严格单次注册。
- 删除时运行中资源延后清理；U3 将接入逐次执行登记的最终在途判定和正常收尾。目前尚不能用这部分管理代码宣称新执行生命周期已完成。

## 兼容投影

Scheduler 只为自己管理的新任务写 swe_cron_jobs。原 SWE/Monitor 的旧任务写入逻辑不动。原表容量较小，过长的 request_input 保留为合法的带 _truncated 标记的摘要 JSON，过长 meta 保留身份/绑定/状态等核心信息并标记 compat_meta_truncated；完整定义及业务输入保存在 v2，不从旧投影执行任务。

首次创建如果旧表 ID 已存在，在任何外部调用前拒绝；最终发布再次检查并发冲突，不能凭客户端可伪造的 meta 标记覆盖既有记录。失败创建的删除只清理本次新对象，不把同 ID 的既有旧表记录删掉；正常更新/删除只处理具有新链路创建凭据和归属的新任务投影。

## 外部连接

- SWE 地址沿用 Scheduler 已有 SCHEDULER_SWE_API_BASE_URL。
- 平台地址和凭据沿用已有 SWE_CRON_SCHEDULER_BASE_URL、JOB_GROUP、AUTHOR、ALARM_EMAIL、CLIENT_NO、CLIENT_KEY、CLIENT_REMARK 配置，由 Scheduler 直接调用平台，不委托 SWE 注册。
- 回调公共域名使用现有 SWE_SERVER_DOMAIN，要求网关能将 /api/scheduler 路径路由到 Scheduler。
- 配置缺失时新写接口返回明确不可用，不在请求里临时创建连接池、加载或输出凭据。现有旧接口行为不变。

当前还没有实现新普通/批执行回调，不能现在创建启用状态的真实定时器并当作完整联调成功。U2 只进行隔离开发验证；如需人工预览，应先核对手工表结构并使用停用任务或 batch_paused，完整上线待后续链路完成。

## 手工表结构（仅供人工执行，应用不执行 DDL）

当前管理链路使用原定的 swe_cron_registration_journal 保存原子管理操作、外部步骤、请求幂等及领取信息。下面是本次实现实际依赖的结构。如果此前已经建过同名表，先对照 SHOW CREATE TABLE 和下述字段，不能通过删除重建覆盖已有数据；CREATE IF NOT EXISTS 也不会自动补齐旧表字段。

新查询对身份列增加精确比较，目标定义表/状态表按既定 utf8mb4 字符集提供 utf8mb4_bin 比较能力；未在业务数据库上变更任何字符集或排序规则。真实 MySQL 的索引与执行计划仍需联调验证。

```sql
CREATE TABLE IF NOT EXISTS swe_cron_registration_journal (
    operation_id VARCHAR(64) NOT NULL COMMENT '管理操作唯一标识',
    active_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '未完成任务操作唯一键，结束后置空',
    tenant_id VARCHAR(128) NOT NULL COMMENT '逻辑租户标识',
    source_id VARCHAR(128) NOT NULL COMMENT '来源系统标识',
    agent_id VARCHAR(128) NOT NULL COMMENT '智能体标识',
    job_id VARCHAR(128) NOT NULL COMMENT '定时任务标识',
    actor_id VARCHAR(128) NOT NULL COMMENT '本次操作发起用户',
    action VARCHAR(32) NOT NULL COMMENT '操作类型：创建、更新、删除等',
    request_id VARCHAR(128) NOT NULL COMMENT '客户端同次请求幂等标识',
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '请求内容校验摘要',
    expected_version BIGINT NOT NULL COMMENT '操作开始时任务定义版本',
    payload_json LONGTEXT NOT NULL COMMENT '本次待处理定义和目标配置快照',
    progress_json LONGTEXT NOT NULL COMMENT '已完成步骤、准备结果、平台绑定及清理进度',
    status VARCHAR(32) NOT NULL COMMENT '待处理、进行中、失败、完成或被删除替代',
    stage VARCHAR(64) NOT NULL COMMENT '当前准备、平台、发布或清理步骤',
    error_code VARCHAR(128) DEFAULT NULL COMMENT '可公开的错误代码，不存原始凭据',
    lease_owner VARCHAR(64) NOT NULL DEFAULT '' COMMENT '当前领取者唯一标识',
    lease_until DATETIME(6) DEFAULT NULL COMMENT '领取到期时间，使用UTC',
    version BIGINT NOT NULL DEFAULT 1 COMMENT '操作进度并发控制版本',
    created_at DATETIME(6) NOT NULL COMMENT '操作创建时间，使用UTC',
    updated_at DATETIME(6) NOT NULL COMMENT '操作最近更新时间，使用UTC',
    PRIMARY KEY (operation_id),
    UNIQUE KEY uk_cron_management_active (active_key),
    KEY idx_cron_management_job (tenant_id, source_id, agent_id, job_id),
    KEY idx_cron_management_lease (status, lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='定时任务管理及外部调度平台注册操作日志表';
```

其余使用第一阶段 swe_cron_jobs_v2、swe_cron_job_states 及已有 swe_cron_jobs 列。本次没有修改 swe_cron_executions 表结构，没有向应用启动流程加入建表或 ALTER。

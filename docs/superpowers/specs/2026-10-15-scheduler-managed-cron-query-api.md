# Scheduler 新任务查询接口（实现记录）

范围：第二阶段 U1 只读查询。管理、分发、调度方式切换、执行及前端新页均已有本地实现；本页仅说明查询契约，不代表真实环境已验收或第二阶段整体可上线。[管理接口核心](2026-10-16-scheduler-cron-management-api.md)另行说明。

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | /api/scheduler/cron/jobs | 当前工作区新任务分页列表 |
| GET | /api/scheduler/cron/jobs/{job_id} | 新任务定义及运行/管理状态 |
| GET | /api/scheduler/cron/jobs/{job_id}/broadcast/children | 指定源任务的关联子任务分页列表 |

新查询路由已注册到 Scheduler。现有 /api/scheduler/cron/callback、/execution 和旧运行状态接口未替换。新增写路由另见 U2 文档；本页三个 GET 仍无写入、准备或修复副作用。

## Postman Header

- X-User-Id：真实调用用户，必填。
- X-Tenant-Id：本次查询的工作区 tenant；省略时沿用用户。
- X-Source-Id：source，必填。
- X-Agent-Id：目标 Agent；省略时 default，显式空值拒绝。
- X-Scope-Id：可省略。传入时必须匹配上述 tenant/source 的编码 scope、scope.v1 前缀形式，或现有前端的 tenant-source 展示形式；服务端不拆分展示字符串反推身份。
- X-User-Role：沿用现有网关的 manager/admin，非管理角色不获得额外权限。

部署前提与既有可信 Header 接口一致：网关先认证，并校验/覆写调用身份与角色。不能把 Scheduler 端口直接暴露为允许任意伪造身份的公共入口。本次不新增必配内部 Token。

## 参数与响应

列表/子任务列表支持 limit（默认 50、最大 200）、cursor、include_deleted（默认 false）。tenant_ids 以重复 Query 参数过滤目标 tenant，最多 1000 项；权限和父子关系条件始终生效，过滤不能扩大查询范围。

分页响应为 items 和 next_cursor；子任务列表另带 source_job_id。应继续读取 next_cursor，不能将第一页视为完整任务集合。游标绑定完整请求身份、权限级别、删除过滤、父任务及目标过滤，不是授权凭据，也不保证分页期间的全局快照。

普通列表保留原任务字段并增加 state、task、management_owner、definition_version 和管理进度字段。详情返回 spec、state、task 及上述管理字段。子任务列表为轻量视图，不批量返回每个接收方的完整正文。

`state.next_run_at` 和 `state.next_run_times` 在读取时按当前已发布定义的 Cron 与时区计算，后者提供未来三次带时区的 ISO 时间；有散列的子任务使用实际应用后的 Cron。它们仅为排程展示，不证明外部平台同步成功，不代表已有执行被受理，也不用于生成补发任务。读取不回写状态表，历史缓存值不会使展示长期停留在过去。暂停任务仍可有理论排程时间，界面应结合 enabled、task.is_paused 与 dispatch_state 判断是否显示；状态行缺失时返回 null/空数组。无法计算的损坏定义返回 503，不伪造成功空数据。

有权管理源任务的调用者可查询关联子任务；接收人只查自己的任务，不能枚举源任务的其他接收人。manager/admin 仍受请求中完整工作区、source、Agent 约束。include_deleted 用于授权查询删除及清理记录，不恢复任务，不改变运行状态。

只有 management_owner=scheduler 的定义可见。导入的 legacy 快照不自动接管，也不以旧表、文件或内存兜底。没有状态行时，返回 management_status=state_missing，而不是虚报 ready。定义 JSON 损坏或关键数据不可读时返回错误，不拼成空定义。

## 错误与只读保证

- 400：缺少或冲突的身份、非法分页游标/过滤。
- 404：新任务不存在、非 Scheduler 管理、无权访问，或默认查询排除的已删除任务。
- 422：limit 等 Query 参数不符合约束。
- 503：共享数据库未连接、所需表/列缺失、定义数据无法安全读取；不返回原始数据库凭据或异常文本。

查询只使用参数化 SELECT，不做 DDL，不准备工作区/Chat，不读取 jobs.json，不初始化旧 _states，不调用 SWE 或外部调度平台。读取前端展示数据时剔除授权头、Token、密码等结构化敏感字段和 Scheduler 内部控制元数据。

## 验证边界

本地用真实 SQLite 关系查询加 ASGI 请求验证，不用伪造 SQL 返回值；SQLite 仅用于测试，不是生产存储实现。生产仍使用现有 Scheduler MySQL 连接。

真实 MySQL 的事务/索引/执行计划、网关 Header 覆写及跨服务路由尚未做预发布实测。当前只读接口没有新增手工 DDL 要求，使用第一阶段定义/状态表已有列；缺列时明确返回 503。

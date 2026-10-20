# 智拓定时任务新接口对接文档

版本：V1.0 对接契约版
日期：2026年10月16日
适用方：智拓应用与 Scheduler、SWE 服务对接人员

本文覆盖创建、更新、删除、查询、分发、调度模式切换、父任务子任务查询，以及按 skill_id 查询 Workflow 绑定配置。智拓通过 HTTP 调用，不直接调用 SWE 内部方法，不直接写任务表或调度平台。

## 一 接口范围和当前可用状态

| 编号 | 能力 | 服务 | 方法和路径 | 当前本地代码状态 |
| --- | --- | --- | --- | --- |
| 1 | 创建定时任务 | Scheduler | POST /api/scheduler/cron/jobs | 管理核心已实现，待完整链路验收 |
| 2 | 更新定时任务 | Scheduler | PUT /api/scheduler/cron/jobs/{job_id} | 管理核心已实现，待完整链路验收 |
| 3 | 删除定时任务 | Scheduler | DELETE /api/scheduler/cron/jobs/{job_id} | 已实现，后台接续正常延后清理 |
| 4 | 查询定时任务 | Scheduler | GET /api/scheduler/cron/jobs/{job_id}；GET /api/scheduler/cron/jobs | 已实现本地查询 |
| 5 | 分发定时任务 | Scheduler | POST /api/scheduler/cron/jobs/{job_id}/broadcast | 接口、持久处理器及后台装配已实现，待部署联调 |
| 6 | 修改调度模式 | Scheduler | PUT /api/scheduler/cron/jobs/{job_id}/dispatch-state | 接口、逐对象处理器及后台装配已实现，待部署联调 |
| 7 | 查看分发子任务列表 | Scheduler | GET /api/scheduler/cron/jobs/{job_id}/broadcast/children | 已实现本地查询 |
| 8 | 按 skill_id 查询 Workflow 绑定 | SWE | GET /api/cron/workflow-bindings/{skill_id} | 存量接口，保留原实现 |
| 9 | 批量删除分发子任务（异步） | Scheduler | POST /api/scheduler/cron/jobs/{job_id}/broadcast/children/batch-delete | 202受理，后台处理，支持查询和恢复 |

第 5、6 项及其配套查询已注册到本地代码，后台处理器也已实现；后台已接入正常应用生命周期，但仍须全部必需表列、既有连接地址和平台配置就绪，提交受理不等于部署环境已经验收。“本地已实现”不代表预发布已部署。新版普通/批回调已开发，实际平台和数据库联调尚未完成，现在不要创建启用中的真实定时器做完整执行验收。

本文示例中的业务 ID、域名和用户均为占位示例，实际接入必须替换。SchedulerBaseUrl、SweBaseUrl 都指服务或网关根地址，不包含后面的 /api 路径。

## 二 通用调用约定

### 2.1 身份 Header

本文件的智拓及前端管理接口身份放在 Header，不在 Body 或 Query 中另传 scope_id、agent_id 来替代身份。平台专用 ordinary/batch callback 是例外：2026-10-19 已确认平台仅发送固定 Body，不提供 Header；其兼容及每请求独立执行语义见[部署与回调验证](../deployment/cron_phase2_deployment.md#第三轮实际执行和结果)。这不放开管理接口的 Header 身份要求。

| Header | 要求 | 说明 |
| --- | --- | --- |
| X-User-Id | 必填 | 真实操作用户，不是分发接收人列表 |
| X-Tenant-Id | 建议显式填写 | 当前任务所属工作区的 tenant；Scheduler 省略时使用 X-User-Id |
| X-Source-Id | 必填 | 使用现有真实 source，保持与技能和 Workflow 配置一致，不要为了智拓随意新造一个 source |
| X-Agent-Id | 建议显式填写 | 当前任务 Agent；省略时 default |
| X-Scope-Id | 可省略 | 服务端根据 tenant/source 计算；传入时必须一致，接入方无须自行编解码 |
| X-Bbk-Id | 按现有身份协议填写 | 分行或支行展示编码 |
| X-User-Name | 按现有身份协议填写 | 用户姓名，使用 URL 编码，例如张三为 %E5%BC%A0%E4%B8%89 |
| X-User-Role | 依既有真实授权 | 由现有可信身份链路提供；管理角色为 manager/admin，不能通过伪造此 Header 获取权限 |
| Authorization | 按现有网关认证 | 如已有登录认证则继续使用原凭据；不新增必配 SWE_INTERNAL_TOKEN |
| Content-Type | 有 JSON Body 时填写 | application/json |

常见创建调用：X-User-Id 与 X-Tenant-Id 相同。源管理者操作分发子任务时，X-User-Id 仍是原操作人，X-Tenant-Id、X-Source-Id、X-Agent-Id 切换为该子任务列表返回的真实身份。不要把操作用户伪装成接收人，也不要把父任务的 workspace 身份用于所有子任务。

普通用户不能通过修改 X-Tenant-Id 在别人工作区创建根任务。分发接收方子任务必须由分发接口基于源权限创建，不是客户端循环调用普通创建接口并伪造用户 Header。

### 2.2 三类 ID 不要混用

| 名称 | 含义 | 用法 |
| --- | --- | --- |
| job_id | 定时任务定义 ID | 查询、修改、删除、分发和模式切换路径 |
| operation_id | 创建、更新、删除、模式切换等管理操作 ID | 查询管理进度和人工重试；不是任务 ID |
| task_id | 一轮异步分发 ID | 查询分发进度，可保存到智拓的 broadcast_task_id |
| batch_id | 一次实际批执行 ID | 不属于本文的任务分发 ID，不拿来查询分发结果 |
| binding_id | SWE 的 Workflow 绑定配置 ID | 创建 Workflow 任务时传作 workflow_binding_id |

### 2.3 幂等和版本

所有新写入操作使用稳定的 request_id。建议每一次新的业务动作生成一个 UUID，并在智拓保存；因超时重提同一次动作时，复用原 request_id 和原 Body。不要为同一次超时自动换 request_id，否则可能创建另一项任务或另一轮分发。

同一 request_id 换内容会冲突。对已失败的创建、更新、删除，调用管理操作 retry 续办；分发失败则选择需处理接收方，用新 request_id 发起新一轮分发，不续办旧轮。

| 字段 | 来源 | 可用于哪个入参 |
| --- | --- | --- |
| definition_version | 任务详情 | 更新、删除、分发的 expected_definition_version |
| 调度状态查询中的 version | GET /dispatch-state | 模式切换的 expected_version |
| operation.version | 管理操作进度 | 仅观察操作进度，不用于更新任务定义 |
| state_version、management_version | 查询展示和并发诊断 | 不用于代替上述两个业务版本 |

更新或创建成功后，重新 GET 任务详情取得当前 definition_version。不要把创建响应中的 operation.expected_version 或 operation.version 当成任务的新定义版本。

## 三 公共任务定义 spec

| 字段 | 类型 | 要求和含义 |
| --- | --- | --- |
| name | string | 必填，非空，最多 255 字符 |
| task_type | string | agent、workflow、text；建议显式填写 |
| enabled | boolean | 创建时默认 true；仅控制该任务自身，不等于批调度入口状态 |
| schedule.cron | string | 必填，标准 5 字段，不含秒，例如 0 9 * * *；不要预先转成平台的 6 字段 |
| schedule.timezone | string | 建议显式填写，常用 Asia/Shanghai |
| plan_id | string 或 null | 智拓规划关联标识，最多 255 字符；不表示 Scheduler 接管规划数据 |
| skill_ids | string | 逗号分隔技能 ID；Workflow 使用第一个技能解析绑定 |
| workflow_binding_id | string 或 null | 仅 Workflow 使用，来自第 8 项的 binding_id；同时传技能和绑定时必须相符 |
| request | object | agent 必填；request.input 使用原 SWE 消息结构 |
| text | string | text 类型必填的非空文本；不作为 Workflow 的执行入口 |
| model_slot | object 或 null | agent 可选，仅 provider_id、model 两个引用字段，不传 API Key |
| dispatch | object | 可省略，默认 console 输出；用户和会话绑定由服务端补全 |
| runtime | object | 可省略，见下方默认值 |
| meta | object | 可存智拓业务关联；服务端身份、Chat 绑定、运行和调度控制字段由服务端维护 |

runtime 默认值为 max_concurrency=1、timeout_seconds=7200、misfire_grace_seconds=300；前两项至少为 1，misfire_grace_seconds 至少为 0。不要依靠省略字段保留更新前的自定义 runtime，PUT 必须传完整的业务定义。

agent 的 request.input 示例：

```json
[
  {
    "content": [{"text": "请按场景技能生成本期经营报告。", "type": "text"}],
    "role": "user",
    "type": "message"
  }
]
```

Workflow 通常传 skill_ids、可选 workflow_binding_id，不传 request、text 或 model_slot。模型、Workflow 和资源准备只做配置与绑定校验，不提前运行任务，也不保证未来调用外部服务一定成功。

## 四 创建定时任务

### 4.1 请求

```text
POST {SchedulerBaseUrl}/api/scheduler/cron/jobs
```

```json
{
  "request_id": "zhituo-create-scene-001",
  "dispatch_state": "batch_paused",
  "spec": {
    "name": "经营规划 场景一",
    "enabled": true,
    "task_type": "workflow",
    "plan_id": "plan-001",
    "skill_ids": "skill-report",
    "workflow_binding_id": "efb4fa2a-4e72-49b5-9a13-b89ce64cde11",
    "schedule": {
      "cron": "0 9 * * *",
      "timezone": "Asia/Shanghai"
    },
    "meta": {
      "wealth_plan_id": "plan-001",
      "wealth_scene_id": "skill-report",
      "wealth_item_id": "item-001"
    }
  }
}
```

顶层字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| request_id | string | 是 | 1～128 字符；不要包含路径分隔符、连续两个点、控制字符或首尾空白 |
| dispatch_state | string | 否 | ordinary、batch_enabled、batch_paused，省略默认为 ordinary |
| spec | object | 是 | 公共任务定义；不要自己分配 id |

智拓创建批任务必须显式传 dispatch_state。示例用 batch_paused 便于安全准备；正式业务需要开启时使用 batch_enabled，且应在完整新链路部署完成后使用。enabled=false 不能代替暂停整组批入口：它只停用源任务自身。

### 4.2 完成响应

创建主流程同步等待必要准备、平台同步和数据库发布，完成返回 201：

```json
{
  "operation": {
    "operation_id": "8e6bb28f-822b-5da6-beca-6cb501abc001",
    "job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
    "action": "create",
    "status": "completed",
    "stage": "completed",
    "error_code": null,
    "expected_version": 1,
    "version": 7,
    "logical_delete_applied": false
  }
}
```

version 的数值仅为示例，不保证每次具有相同步骤数。智拓保存 job_id 和 operation_id，并 GET 任务详情取得后续修改所需的 definition_version。

响应 202 表示已有操作仍在进行，不表示创建完成。503 也可能已经保存任务和资源；若响应含 operation，应保存其标识并查询/重试，不能直接当作“完全没创建”后再生成另一 request_id。

同次创建重提已完成结果也可能返回 201，不能仅凭 201 判断又新增了一项任务；以稳定的 operation_id、job_id 判断幂等复用。

## 五 更新定时任务

### 5.1 请求

```text
PUT {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}
```

```json
{
  "request_id": "zhituo-update-scene-001",
  "expected_definition_version": 1,
  "spec": {
    "name": "经营规划 场景一 新名称",
    "task_type": "workflow",
    "plan_id": "plan-001",
    "skill_ids": "skill-report",
    "workflow_binding_id": "efb4fa2a-4e72-49b5-9a13-b89ce64cde11",
    "schedule": {
      "cron": "30 9 * * *",
      "timezone": "Asia/Shanghai"
    },
    "runtime": {
      "max_concurrency": 1,
      "timeout_seconds": 7200,
      "misfire_grace_seconds": 300
    },
    "meta": {
      "wealth_plan_id": "plan-001",
      "wealth_scene_id": "skill-report",
      "wealth_item_id": "item-001"
    }
  }
}
```

这是完整定义替换，不是局部 PATCH。需要保留的业务字段应重新提交，但 task 身份、Chat、会话和实际运行结果不因此重建或重置。

expected_definition_version 必须来自最近一次 GET 详情。接口同步处理，完成返回 200，Body 为与创建相同的 operation 包装，action=update。新定义版本以随后 GET 详情为准。

### 5.2 业务限制

- 源任务仍有未删除子任务时，禁止修改源内容；停用但未删除的子任务也算存在。
- 分发尚未结束时也不修改源内容。子任务全部逻辑删除且分发结束后，才可再次修改源内容。
- PUT 不用于启停任务、切换批模式或重新分发；内容更新保留任务自身的启停状态。
- 更新期间新触发按已定规则暂缓且不补跑；已入队、运行中的执行使用原快照继续。
- 必要准备和平台同步完成后才发布新定义。失败时保留原生效定义与操作进度，通过附录的 retry 续办。
- 409 版本冲突后应重新查询并由业务决定是否重提，不要无条件用新版本覆盖别人修改。

## 六 删除定时任务

### 6.1 请求

```text
DELETE {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}
Content-Type: application/json
```

```json
{
  "request_id": "zhituo-delete-scene-001",
  "expected_definition_version": 2
}
```

DELETE 带 JSON Body；使用不支持 DELETE JSON 快捷方法的 SDK 时，改用其通用 request 方法，不要把版本丢掉。

### 6.2 响应和清理语义

正常完成为 200：

```json
{
  "operation": {
    "operation_id": "514db372-c83d-5a9f-9919-d7121cabc002",
    "job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
    "action": "delete",
    "status": "completed",
    "stage": "completed",
    "error_code": null,
    "expected_version": 2,
    "version": 5,
    "logical_delete_applied": true
  }
}
```

删除先落逻辑删除标记，再处理外部停用和资源清理。logical_delete_applied=true 即说明本次逻辑删除已生效，外部清理失败不能把任务恢复为正常任务。

清理失败可返回 503 和失败 operation，仍可能 logical_delete_applied=true。正常等待在途执行结束时可返回 202、stage=cleanup_waiting；不能将它解释成重新启用或要求取消已经开始的执行。

单任务删除只处理指定 job_id，不自动级联子任务。智拓删除规划/场景时，应按原业务范围查询子任务，可调用本节异步批量删除接口，等待进度结束后再处理父任务；也可逐个带其真实身份调用单任务删除。部分失败必须保留相应业务关联用于后续清理。分发正在进行时先等待分发结束，不强行删除源任务。

同一任务重复删除会复用既有删除操作，不重复清理资源。需要恢复未完成清理时，显式调用操作 retry。不存在、非新链路管理或无权访问的任务不按“已删除成功”处理。

### 6.3 批量删除分发子定时任务（异步，2026-10-19更新）

本接口先保存固定目标清单和原操作身份，返回 HTTP 202；Scheduler 后台组合单任务删除能力，前端查询专用删除进度，不轮询分发 tasks 接口。关闭客户端不取消已受理操作，重启后按共库租约恢复。本地实现不代表预发布联调完成。

#### 请求

```text
POST {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}/broadcast/children/batch-delete
Content-Type: application/json
```

路径 job_id 是父任务 ID。Header 使用父任务的工作区/Agent 和真实操作人身份；由 Scheduler 校验父子关系并定位子任务，调用方不需要在同一次批量请求中切换 Header。Body 中 tenant_id、agent_id 仅用于指定目标子任务，不替代真实操作人身份或授予跨用户权限；source_id 继承父任务，不在 Body 中另传。

```json
{
  "request_id": "zhituo-delete-children-001",
  "items": [
    {
      "job_id": "0e9add04-89ec-4834-b3a1-741b7babc005",
      "tenant_id": "sap1002",
      "agent_id": "default",
      "expected_definition_version": 1
    },
    {
      "job_id": "80a6548c-d4c0-49c7-86bf-6d855fabc006",
      "tenant_id": "sap1003",
      "agent_id": "default",
      "expected_definition_version": 2
    }
  ]
}
```

| 字段 | 类型 | 要求 |
| --- | --- | --- |
| request_id | string | 必填，本次批量删除的稳定请求 ID；1～128 字符，沿用单任务操作的安全规则 |
| items | array | 选中删除为1～1000项；空数组不表示删除全部。完整全量快照使用下述delete-all入口 |
| items[].job_id | string | 必填，来自子任务列表的 job_id，不使用 tenant_id 代替任务 ID |
| items[].tenant_id | string | 必填，该子任务实际所属 tenant |
| items[].agent_id | string | 必填，该子任务实际 Agent，来自查询结果 |
| items[].expected_definition_version | integer | 必填，该子任务查询结果中的 definition_version，至少为 1；不是父任务版本 |

调用方先完整分页查询，再按自己的业务范围选择待删除项。仅删除 items 明确指定且属于该父任务的对象，不隐式删除父任务、其他子任务或同一接收方后来创建的新任务。重复目标属于请求格式错误，不在一次请求中处理两遍。源任务仍有进行中的分发时返回 409，本次不开始删除；批量删除和同源新分发应串行协调，避免边删边新增。

#### 受理和进度查询

初次受理返回 HTTP 202，包括 operation_id、operation_status=pending、total_count和pending_count，初始results可为空。受理不等于已逻辑删除。每次进度响应中，operation_status 表示整批清单 pending/running/completed/failed；completed 只表示逐项处理结束，仍需检查失败和待清理计数。系统意外失败会停止，不自动重试业务失败。

```text
POST /api/scheduler/cron/jobs/{job_id}/broadcast/children/delete-all
GET  /api/scheduler/cron/jobs/{job_id}/broadcast/children/batch-delete/tasks/current
GET  /api/scheduler/cron/jobs/{job_id}/broadcast/children/batch-delete/tasks/{operation_id}
POST /api/scheduler/cron/jobs/{job_id}/broadcast/children/batch-delete/tasks/{operation_id}/retry
```

delete-all 的 Body 与选中删除相同，但items提交确认时完整读取的未删除子任务快照，可超过1000项，一次持久受理；不含筛选外遗漏或确认后新增对象，不删除父任务。它不会放宽父子关系、权限或版本校验。current 返回 `{"task": null}` 或 `{"task": {...}}`，指定ID查询直接返回进度对象；GET只读，不调用SWE或平台。人工retry无业务Body，返回202，将整批意外失败重新排队，不重试子任务清理业务失败。所有入口沿用父任务Header身份。

新版通用 `POST /{job_id}/broadcast/children/actions` 的 `action=delete` 同样复用异步受理，返回202及上述进度对象；其可选agent_id继续按原协议省略时沿用源Agent。`action=run` 保持原执行受理行为。旧SWE接口不变。

示例：两项均已逻辑删除，其中一项等待在途执行结束后清理资源。

```json
{
  "request_id": "zhituo-delete-children-001",
  "source_job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
  "operation_id": "batch-delete-operation-id",
  "operation_status": "completed",
  "status": "pending",
  "total_count": 2,
  "deleted_count": 2,
  "completed_count": 1,
  "pending_count": 1,
  "failed_count": 0,
  "results": [
    {
      "job_id": "0e9add04-89ec-4834-b3a1-741b7babc005",
      "tenant_id": "sap1002",
      "source_id": "source-system-a",
      "agent_id": "default",
      "status": "completed",
      "http_status": 200,
      "logical_delete_applied": true,
      "operation_id": "514db372-c83d-5a9f-9919-d7121cabc002",
      "stage": "completed",
      "error_code": null
    },
    {
      "job_id": "80a6548c-d4c0-49c7-86bf-6d855fabc006",
      "tenant_id": "sap1003",
      "source_id": "source-system-a",
      "agent_id": "default",
      "status": "pending",
      "http_status": 202,
      "logical_delete_applied": true,
      "operation_id": "b695f5ce-1135-5f60-ad59-e705160bc008",
      "stage": "cleanup_waiting",
      "error_code": null
    }
  ]
}
```

- total_count 是目标数；completed_count、pending_count、failed_count 互斥且相加等于 total_count。
- deleted_count 只统计已确认逻辑删除的项，与清理统计独立；清理失败或等待清理的项也可能已逻辑删除，不能再视为可执行任务。
- 每项 status 为 completed、pending 或 failed；失败项通过 http_status/error_code 表达版本冲突和外部清理失败等原因；不存在、无权或非关联对象统一按 404 处理，不泄露其他任务信息。未产生删除操作时 operation_id、stage 为 null，logical_delete_applied=false。
- 顶层 status 在整批未结束时与operation_status一致；逐项处理结束后，全部完成为completed、仅有待清理为pending、全部失败为failed、部分失败为partial_failed。失败与等待同时存在仍继续检查各项和计数；不能只按status判断后台是否仍在处理。

#### 删除、重试和超时边界

1. 每项独立校验父子关系、真实操作权限和子任务版本，再先逻辑删除、后停用平台及清理 SWE 资源。某项失败不回滚其他项，也不因一个子任务异常而扩大删除范围。
2. 后台不等待 Agent/Workflow 一直完成：已开始的执行继续；未开始的已删除任务不再派发；在途资源延后清理，该项返回 pending、stage=cleanup_waiting。
3. 平台或资源清理失败时，该项为 failed；若已逻辑删除，logical_delete_applied 仍为 true，保留 operation_id 供人工重试，不恢复任务。
4. 服务端保存本次目标清单及稳定子操作身份。同一 request_id 和原 Body 重提复用已处理结果，不重复删除成功项；同一 request_id 换目标或版本返回 409，不把它当成新一轮操作。
5. 清理失败项按返回的子任务身份和operation_id，使用现有单任务进度查询及retry接口人工续办；专用批量进度GET可观察其后续状态，不重做已成功的外部清理。
6. 受理响应丢失时保留原request_id和原Body重提，或先查current；不会生成新的整批任务。关闭页面后整个已受理快照仍由后台继续，不再有前端关闭后剩余片未提交的问题。
7. 父任务不存在/无权、请求格式非法、同源分发冲突等前置问题使用404、400/422、409；后台未就绪或受理存储不可用返回503，不伪造受理成功。已有删除不回滚。
8. 同源同一request_id重提查回原进度，即使其他实例正在处理也不另起一批；不同新请求与进行中的同源操作互斥。意外失败后可重提原Body或调用专用整批retry；通用operations retry不负责该整批清单，失败子任务仍使用自己的operation_id人工续办。


## 七 查询定时任务

### 7.1 查询详情

```text
GET {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}
```

可选 Query：include_deleted=true，供授权查询删除状态和清理进度；默认排除已删除任务。

200 示例：

```json
{
  "spec": {
    "id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
    "name": "经营规划 场景一",
    "enabled": true,
    "tenant_id": "sap1001",
    "source_id": "source-system-a",
    "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
    "task_type": "workflow",
    "skill_ids": "skill-report",
    "workflow_binding_id": "efb4fa2a-4e72-49b5-9a13-b89ce64cde11",
    "meta": {"wealth_plan_id": "plan-001"}
  },
  "state": {
    "next_run_at": "2026-10-17T09:00:00+08:00",
    "next_run_times": ["2026-10-17T09:00:00+08:00", "2026-10-18T09:00:00+08:00", "2026-10-19T09:00:00+08:00"],
    "last_run_at": null,
    "last_prefetch_at": null,
    "last_status": null,
    "last_error": null
  },
  "task": {
    "visible_in_my_tasks": true,
    "chat_id": "a050830e-bf55-559b-9b52-e771f1abc003",
    "session_id": "cron-task:7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
    "has_scheduled_result": false,
    "latest_scheduled_preview": "",
    "unread_execution_count": 0,
    "last_scheduled_run_at": null,
    "is_running": false,
    "is_paused": false,
    "pause_reason": null,
    "auto_paused_at": null
  },
  "management_owner": "scheduler",
  "agent_id": "default",
  "definition_version": 1,
  "deleted": false,
  "dispatch_state": "batch_paused",
  "management_status": "ready",
  "management_version": 8,
  "state_version": 1,
  "offset_minutes": 0,
  "offset_window_hours": 0,
  "enable_offset": false
}
```

示例省略了部分可选 spec 字段。next_run_at/next_run_times 按当前已发布 Cron 与时区实时计算，仅供理论排程展示，不证明平台已启用或已受理执行；暂停时也可能有展示时间，前端应结合 enabled、task.is_paused、dispatch_state 判断。缺失状态行时为 null/空数组，并明确返回 state_missing，不虚报 ready。management_status=ready 表示管理准备完成，不代表执行成功。

本接口只查 Scheduler 管理的新任务。第一阶段导入的 legacy 快照不自动接管，不在找不到时回退 SWE 旧任务接口。

### 7.2 查询列表

```text
GET {SchedulerBaseUrl}/api/scheduler/cron/jobs?limit=50
```

| Query | 类型 | 说明 |
| --- | --- | --- |
| limit | integer | 默认 50，1～200 |
| cursor | string | 上一页返回的 next_cursor，保持其他查询条件不变 |
| include_deleted | boolean | 默认 false，显式查删除/清理记录时使用 |

返回：

```json
{
  "items": [
    {
      "id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
      "name": "经营规划 场景一",
      "enabled": true,
      "task_type": "workflow",
      "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
      "management_owner": "scheduler",
      "definition_version": 1,
      "deleted": false,
      "dispatch_state": "batch_paused",
      "management_status": "ready",
      "state": {"last_status": null, "last_error": null, "next_run_times": []},
      "task": {"visible_in_my_tasks": true, "unread_execution_count": 0}
    }
  ],
  "next_cursor": null
}
```

列表 items 将 spec 字段平铺，不再套 spec；详情则有 spec 包装。列表示例为字段节选，完整任务仍可用详情查询。当前不提供 plan_id、场景 ID 等专用 Query，智拓应保存自己业务与 job_id 的关联，不依赖扫描全部任务找回关联。

## 八 分发定时任务

本节及配套分发查询已在本地注册并实现持久处理器；应用后台已装配，实际平台联调尚待完成，不能把 202 受理视为所有目标已完成。

### 8.1 提交一轮异步分发

```text
POST {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}/broadcast
```

路径 job_id 是源任务，Header 使用源任务的工作区/Agent 身份。

```json
{
  "request_id": "zhituo-broadcast-scene-001",
  "expected_definition_version": 1,
  "targets": [
    {
      "tenant_id": "sap1002",
      "tenant_name": "李四",
      "bbk_id": "001",
      "agent_id": "default"
    },
    {
      "tenant_id": "sap1003",
      "tenant_name": "王五",
      "bbk_id": "002",
      "agent_id": "default"
    }
  ],
  "enable_offset": false,
  "offset_window_hours": 0
}
```

| 字段 | 类型 | 要求 |
| --- | --- | --- |
| request_id | string | 必填，一轮分发的稳定请求 ID |
| expected_definition_version | integer | 必填，GET 源任务详情取得 |
| targets | array | 必填，0～1000 个显式接收方 |
| targets[].tenant_id | string | 必填，接收方逻辑 tenant |
| targets[].tenant_name、bbk_id | string | 展示和目标身份辅助信息，不作为授权凭据 |
| targets[].agent_id | string | 可选，省略沿用源 Agent |
| enable_offset | boolean | 是否散列错峰；智拓保持现状时传 false |
| offset_window_hours | integer | 0～24；智拓保持现状时传 0 |

接收方 source 继承源任务，不允许从 targets 指定另一 source 或任意 scope。任务完整定义由 Scheduler 读取，智拓不在分发 Body 中重传完整 spec。

此接口不接受 enable_batch_dispatch 或三态字段；它只分发/更新子任务，不隐含开关批调度。创建任务、分发任务、实际执行任务是不同动作。

### 8.2 受理响应

分发记录及处理对象持久化后返回 202，不等待所有接收方完成准备：

```json
{
  "task_id": "b73c3dde-b1f9-49f6-9f80-0f1165abc004",
  "source_job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
  "source_definition_version": 1,
  "status": "queued",
  "tenant_count": 2,
  "success_count": 0,
  "failed_count": 0,
  "pending_count": 2,
  "reused": false
}
```

智拓保存 task_id 后轮询指定分发，不能把 202 受理显示为“已全部分发成功”。

目标状态：queued 排队、running 处理中、succeeded 全部完成、partial_failed 部分失败、failed 整轮失败。客户端以整体 status 和同步结果判断，不只看 failed_count 是否为 0。

### 8.3 查询指定分发及最近一轮

```text
GET {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}/broadcast/tasks/{task_id}?limit=50
GET {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}/broadcast/tasks/current?limit=50
```

目标分页约定：limit 默认 50、最大 200，cursor 继续读取结果页；全局统计不因分页而只统计当前页。没有后续页时 next_cursor=null。

指定分发返回示例：

```json
{
  "task_id": "b73c3dde-b1f9-49f6-9f80-0f1165abc004",
  "source_job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
  "source_definition_version": 1,
  "status": "partial_failed",
  "tenant_count": 2,
  "success_count": 1,
  "failed_count": 1,
  "pending_count": 0,
  "sync_status": "completed",
  "error_code": null,
  "results": [
    {
      "tenant_id": "sap1002",
      "source_id": "source-system-a",
      "agent_id": "default",
      "job_id": "0e9add04-89ec-4834-b3a1-741b7babc005",
      "operation": "created",
      "status": "succeeded",
      "cron": "0 9 * * *",
      "timezone": "Asia/Shanghai",
      "offset_minutes": 0,
      "error_code": null,
      "warning": null
    },
    {
      "tenant_id": "sap1003",
      "source_id": "source-system-a",
      "agent_id": "default",
      "job_id": "80a6548c-d4c0-49c7-86bf-6d855fabc006",
      "operation": "created",
      "status": "failed",
      "cron": "0 9 * * *",
      "timezone": "Asia/Shanghai",
      "offset_minutes": 0,
      "error_code": "target_prepare_failed",
      "warning": null
    }
  ],
  "next_cursor": null
}
```

current 返回包装 {"task": 上述分发对象}；没有记录为：

```json
{"task": null}
```

current 指按创建顺序最近一轮，不仅查询正在执行的一轮；不要因旧轮晚完成而覆盖新轮。轮询应保存并查询明确 task_id，不能在处理中只依赖 current 猜测自己提交的是哪轮。

分发成功表示子任务创建/更新及必要同步完成，不表示 Agent 或 Workflow 已执行成功。必要的批入口时间同步失败也必须在 sync_status/error_code 暴露，不能因为接收方都成功就宣布整体成功。

### 8.4 重复分发和散列边界

- 同一请求重提复用 task_id，不重复创建；同一源任务一次只允许一轮分发进行。
- 新一轮中已有未删除子任务走更新，保留 ID、Chat、会话、启停和运行结果，内容按源定义覆盖；并发独立修改需要版本冲突保护。
- 已删除的旧子任务只有明确新一轮才可以创建新 ID；旧轮重试/迟到处理不能复活它。
- 未在本轮 targets 中的历史子任务不删除、不顺带重新初始化；模式切换另算整组范围。
- 失败对象通过新 request_id 发起新一轮处理，不提供旧轮失败项续办接口。
- targets=[] 不新建、不删除，仅对源任务下全部未删除子任务重算散列排程，包含停用对象但不恢复启用，不改正文/模型/技能。
- 0 人模式中的 tenant_count 是实际纳入调整的既有对象数，不是“新增了多少人”。历史多轮累计对象数可大于本轮显式 targets 的 1000 人输入限制。
- 散列沿用在窗口内均匀提前的算法，不是用户 ID 哈希；新一轮人数或顺序不同，偏移可能变化。
- Cron 无法安全平移时回退原排程、实际偏移记 0 并告警，不默默声称已散列。
- Agent 分发到目标环境时，指定模型不可用按既有规则回退目标默认模型并告警；必要 Workflow/技能配置缺失则该对象失败。普通单任务创建不因此获得“任何配置错误都自动回退”的语义。

## 九 修改定时任务调度模式

本节及配套状态查询已在本地注册，逐对象同步及人工重试已有测试；后台启动已装配，真实平台联调仍待完成。分发与此接口独立。

### 9.1 三态

| state | 含义 |
| --- | --- |
| batch_enabled | 批调度，允许批入口接收新的触发 |
| batch_paused | 仍是批调度，暂停新的批触发，不改成普通定时器 |
| ordinary | 普通调度，由各任务已有排程及自身启用状态触发 |

### 9.2 请求与响应

先 GET 当前调度状态取得 version，再设置：

```text
PUT {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}/dispatch-state
```

```json
{
  "request_id": "zhituo-dispatch-state-001",
  "state": "batch_enabled",
  "expected_version": 3
}
```

| 字段 | 要求 |
| --- | --- |
| request_id | 必填，稳定管理操作标识，超时重复提交时复用 |
| state | 必填，三态之一 |
| expected_version | 必填，调度状态 GET 返回的 version；不是 definition_version |

异步受理返回 202，目标响应：

```json
{
  "job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
  "operation_id": "ed81f87b-48ca-5e9c-bfd6-d6ab86abc007",
  "state": "batch_enabled",
  "version": 4,
  "effective_state": "mixed",
  "sync_status": "pending",
  "completed_count": 0,
  "pending_count": 3,
  "failed_count": 0,
  "error_code": null
}
```

state 是本次业务目标；effective_state 表示当前已生效事实，切换中可为 mixed；sync_status 区分 pending、running、completed、failed。version、计数均为示例，不依赖固定初始值。

### 9.3 查询状态和失败续办

```text
GET {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}/dispatch-state
```

返回与上面的状态对象相同的关键字段。客户端必须继续查询，直到 sync_status=completed 或 failed。失败保留 operation_id，使用附录的操作 retry 续办同次切换，不重新注册成功对象。

- 一次切换未完成时不能改成另一目标，返回 409；可以查询或重试当前切换。
- 外部同步失败只提示并支持重试，不因“同步失败”给整组增加禁止触发标记。
- 普通与批模式部分切换成功时允许短暂混合，已同步对象按新方式，未同步对象暂按原方式，继续收敛；按 2026-10-19 最新确认，平台每次新回调独立执行，不做跨入口周期去重。
- 明确的批暂停、单任务停用和删除仍有效。“外部失败不阻断”不表示可以忽略业务暂停。
- 已入队和运行中的执行继续，不因模式切换取消。
- 模式作用于源任务及全部有效关联子任务，不仅最近一次 targets。
- 接口不接受散列字段，不重新分发、不重新计算偏移，不重建 Chat/会话。
- 启停某个源任务自身不等于关闭全部接收方的批入口；整组模式与单任务 enabled 分开。

## 十 查看父任务下的分发子任务列表

### 10.1 请求

```text
GET {SchedulerBaseUrl}/api/scheduler/cron/jobs/{job_id}/broadcast/children?limit=50
```

Header 使用父任务身份。可选参数：

| Query | 类型 | 说明 |
| --- | --- | --- |
| tenant_ids | 重复参数 | 指定接收方过滤，例如 tenant_ids=sap1002&tenant_ids=sap1003 |
| include_deleted | boolean | 默认 false；复核删除和清理记录时 true |
| limit | integer | 默认 50，1～200 |
| cursor | string | 下一页游标，保持父任务、Header 和筛选不变 |

### 10.2 响应

```json
{
  "source_job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
  "items": [
    {
      "id": "0e9add04-89ec-4834-b3a1-741b7babc005",
      "job_id": "0e9add04-89ec-4834-b3a1-741b7babc005",
      "tenant_id": "sap1002",
      "source_id": "source-system-a",
      "scope_id": "由服务端返回的实际scope",
      "agent_id": "default",
      "name": "经营规划 场景一",
      "enabled": true,
      "task_type": "workflow",
      "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
      "definition_version": 1,
      "deleted": false,
      "management_owner": "scheduler",
      "management_status": "ready",
      "management_version": 8,
      "dispatch_state": "batch_paused",
      "offset_minutes": 0,
      "offset_window_hours": 0,
      "enable_offset": false,
      "state": {"last_status": null, "last_error": null, "next_run_times": []},
      "task": {"visible_in_my_tasks": false, "unread_execution_count": 0}
    }
  ],
  "next_cursor": null
}
```

这是轻量字段示例，完整子任务正文用其自己的身份调用详情接口。下一页必须继续查询，不要只删除/处理第一页就认为整个规划下所有子任务都已处理。

对子任务逐个更新/删除时，X-User-Id 保持真实操作人；工作区、source、Agent 使用该项返回值。查询父任务结果不表示客户端可以操作其他无关 tenant 的任意任务，服务端仍核对源子关系及权限。

## 十一 存量接口 按 skill_id 查询 Workflow 绑定

### 11.1 请求

```text
GET {SweBaseUrl}/api/cron/workflow-bindings/{skill_id}
```

无 Body。X-Source-Id 必须是技能当前真实 source，沿用现有登录和身份协议；该存量接口要求真实的 manager/admin 权限，权限不足返回 403。

### 11.2 响应

```json
{
  "binding_id": "efb4fa2a-4e72-49b5-9a13-b89ce64cde11",
  "version": 3,
  "skill_id": "skill-report",
  "url": "https://workflow.example.invalid/run",
  "method": "POST",
  "headers": {},
  "path_params": {},
  "query": {},
  "body": {},
  "success_rule": null,
  "result_fields": {"result": ["data", "result"]},
  "renderer_key": "direct",
  "provider_id": "provider-example",
  "model_id": "model-example",
  "timeout_seconds": 60
}
```

关键对应：

```text
响应 binding_id  →  创建/更新 spec.workflow_binding_id
请求 skill_id    →  创建/更新 spec.skill_ids 中的首个技能
```

这里查的是“按 source/skill 绑定的当前启用 Workflow 调用配置”，不是一个固定顶层 workflow_id。binding_id 不能改名当作外部平台的 workflow_id。如果外部平台工作流 ID 藏在 URL、path_params 或 body 中，位置取决于该配置，不存在统一取值规则。

智拓只需要取得绑定引用和业务需要的配置标识，不直接执行配置 URL，也不解析/传递执行凭据。未来任务执行时由 SWE 解析绑定及必要的运行配置；已入队执行保留其已冻结的版本。

此接口常见错误：400 缺少 source、403 权限不足、404 缺少或禁用绑定、503 配置存储不可用。不要把 403 或 503 当作“技能没有 Workflow”后静默改成另一种任务。

## 十二 管理进度和人工重试辅助接口

这些是创建/更新/删除必需的异常处理配套；不是智拓要调用的 SWE 内部准备接口。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/scheduler/cron/jobs/{job_id}/operations/pending | 返回 {"operation":对象}，没有未完成操作为 {"operation":null} |
| GET | /api/scheduler/cron/jobs/{job_id}/operations/{operation_id} | 返回裸操作对象，不再套 operation |
| POST | /api/scheduler/cron/jobs/{job_id}/operations/{operation_id}/retry | 无 Body；返回 {"operation":对象}，续办未完成步骤 |

重试只处理当前授权任务的原操作，成功步骤不重复执行。失败创建/更新可以被删除替代，被替代的旧操作为 superseded，不能通过重试把已删除任务恢复。

响应中的 error_code 用于展示或诊断，英文 detail 不作为成功判断。示例：

```json
{
  "operation": {
    "operation_id": "8e6bb28f-822b-5da6-beca-6cb501abc001",
    "job_id": "7ad85834-4ec4-5b9e-9a31-bd701ebc1234",
    "action": "create",
    "status": "failed",
    "stage": "platform",
    "error_code": "cron_management_step_failed",
    "expected_version": 1,
    "version": 5,
    "logical_delete_applied": false
  }
}
```

2026-10-17确认平台没有注册查询接口：未取得并保存平台ID时按注册失败处理，返回platform_registration_failed；调用原操作retry可以再次注册，任务ID和已准备资源不变。已保存平台ID则复用，只重试更新/启停等后续步骤。平台可能实际成功但响应丢失，故重试可能留下重复或孤立定时器，无法保证无重复；删除只关闭已知ID。SWE准备失败仍在修复配置后续办原操作，不另建任务。

超时没有拿到 job_id 时，使用原 request_id 和原 Body 重提创建找回原结果；不要直接换 request_id 新建。一般查询失败、数据库不可用不按“任务不存在”或“空列表成功”处理。

## 十三 智拓调用顺序

1. Workflow 场景先调用存量 SWE 绑定查询，取得 binding_id；Agent 场景准备原消息结构和可选模型引用。
2. 调 Scheduler 创建源任务，显式选择批状态，保存 job_id、operation_id。
3. GET 任务详情，保存当前 definition_version。
4. 调异步分发，保存 task_id，轮询指定分发及必要结果页；失败对象经新一轮处理。
5. 如需开启/暂停批入口或切回普通，单独 GET/PUT dispatch-state，轮询同步进度。
6. 删除规划/场景时按原业务范围完整查询子任务，带各自身份删除，再处理源任务；清理失败保留关联和操作标识。
7. 不调用 SWE 旧 /api/cron/jobs 来管理这批新任务，不调用 SWE 内部 prepare/cleanup，不直接调用调度平台或改共享表。

## 十四 错误处理和接入检查

| 情况 | 处理 |
| --- | --- |
| 200/201 且 operation.status=completed | 管理动作完成；不等于任务实际执行完成 |
| 202 | 已受理或仍处理中，继续查询，不能展示成全部成功 |
| 400/422 | 核对 Header/Body/版本字段类型，不自动换身份重试 |
| 403 | 存量绑定接口权限不足，使用真实授权，不伪造管理角色 |
| 404 | 核对任务完整身份、管理归属、删除过滤和业务 ID，不回退旧写接口探测 |
| 409 | 版本冲突、同次请求换内容、源内容冻结或切换/分发进行中；先查当前事实 |
| 503 且含 operation | 保存原标识，查询或人工重试；删除可能已经生效 |
| 503 且没有 operation | 服务/配置/数据库可能不可用；原稳定 request_id 保留，不能推断没有任何持久化 |

正式联调前确认：目标版本已部署、分发和模式接口已完成；网关正确路由 Scheduler 与 SWE；身份 Header 来源可信；共享表人工 DDL 已对齐；使用真实 source/技能/Workflow 绑定；新执行入口与回调配置已完成。本文不表示这些条件已经在当前环境满足。

请求端应允许响应增加字段，不把未知管理状态字符串当作 ready，也不要把管理 operation、分发 task 和执行 batch 的状态混为同一种状态。

## 附录 Postman 文件

随附 zhituo-scheduler-cron-api-v1.postman_collection.json；桌面对外文件名为 zhituo_scheduler_api.postman_collection.json。导入后先填写服务地址、真实 source、用户和 Agent，再查询 Workflow 绑定。后台启动已装配，仍须表结构及既有配置就绪；后台已知不可用时，新建、更新、启用、分发、模式切换及手动执行会明确返回 503/cron_managed_runtime_not_ready，不默默留下无人处理的新工作。查询、标读、停用及删除不加此就绪门禁。集合不会自动执行请求，示例创建从暂停批入口开始。

request_id 示例是便于阅读的固定值；实际业务需按动作生成并持久保存唯一值。同次操作重试保留原值，新一轮分发或新业务动作才换值。

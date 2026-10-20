# 新任务独立控制接口

当前实现：Scheduler 管理的新任务启停、标记已读、手动执行受理、优先级和选择子任务批量操作。旧 SWE 接口不变。新执行后台已有本地装配，但仍需最终数据库、网关与平台验收，不能把手动入队响应当成整条链路已经验收。

通用前缀为 `/api/scheduler/cron/jobs`，身份 Header、可信角色和源子关系授权沿用新版 CRUD；不回退旧写接口，不回填 jobs.json 或旧展示缓存。

## 1 单任务启停

`PUT /{job_id}/enabled`

```json
{
  "request_id": "一次启停动作的稳定请求ID",
  "enabled": false,
  "expected_management_version": 8
}
```

expected_management_version 取自最近一次任务详情的 management_version，不是 definition_version、state_version 或操作进度 version。enabled 必须是 JSON 布尔值，不接受字符串 false。

该操作只调整任务自身的启停，不改变源内容、不增加内容定义版本，不把源自身停用解释为暂停整个批入口。数据库启停事实先提交，普通模式下再同步已有普通定时器；批模式不停止整组批定时器。平台失败保留操作进度，可人工重试，不撤销已经落库的自身启停事实，不额外修改管理就绪状态。

停用后尚未开始的执行跳过，已经开始的执行继续。恢复会解除自身手动/自动未读暂停，并将本次已读边界内已有未读标读；之后完成的结果仍保持未读。它不会解除删除、内容更新和整组模式等独立限制。

返回统一的 `{"operation":对象}`：完成 200，仍处理中 202，同步失败 503。重复相同 request_id 和 Body 复用原操作，不重新清理之后产生的未读。

## 2 标记已读

`POST /{job_id}/read`

```json
{"request_id":"一次已读动作的稳定请求ID"}
```

只清理当前已读边界内的已有结果，不恢复启用、不清除暂停原因、不改正文。已读历史通过完整身份的执行登记关联原历史主键，不按裸 job_id 或 tenant_id 跨 source 清理。

同次请求重提不移动原边界；新的已读动作换 request_id。返回 200 和统一 operation 包装。报告预览应调用真实任务归属对应的标读接口，不能通过试写新旧接口探测归属。

## 3 手动执行和进度

`POST /{job_id}/run`

```json
{
  "request_id": "本次手动执行的稳定请求ID",
  "expected_definition_version": 1
}
```

先登记执行 ID、完整定义快照，再返回 202，例如：

```json
{
  "execution": {
    "execution_id": "同次执行ID",
    "job_id": "任务ID",
    "kind": "manual",
    "status": "queued",
    "definition_version": 1
  },
  "reused": false
}
```

queued 只是已受理排队，不是已开始或成功。停用、自动暂停或正在运行的任务拒绝新手动请求；已有受理但未开始的手动请求时返回原请求，reused=true，不追加执行。相同 request_id 的重提始终按原执行身份幂等；不得以相同请求 ID 改定义版本。

`GET /{job_id}/executions/{execution_id}` 查询同次执行记录。响应包含执行身份、快照版本、status、受理/开始/结束时间、安全 error_code、trace_id、output_preview、history_execution_id 和进度 version。它不是管理操作查询，execution_id 不能换用 operation_id。

实际 Agent/Workflow 失败不会自动重新跑该业务执行；新的人工执行需要新请求 ID。HTTP 触发响应丢失不新建执行，最终结果以 SWE 写入的共库记录为准。

## 4 批优先级

`GET /{job_id}/batch-dispatch/priority`

```json
{
  "priority": {"user_ids": [], "branch_ids": []},
  "management_version": 8
}
```

`PUT /{job_id}/batch-dispatch/priority`

```json
{
  "request_id": "一次优先级修改的稳定请求ID",
  "expected_management_version": 8,
  "user_ids": ["sap1001"],
  "branch_ids": ["110"]
}
```

只允许源任务配置，有源管理权限即可；接收方不能在自己的子任务上改整组策略。用户名单最多 500、一级分行最多 100，沿用现有有效标识、去重及一级分行校验。用户顺序优先、一级分行顺序其次，不匹配者默认排序；只排序已有对象，不增加接收方。

策略保存在源定义的服务端管理元数据中，仅后续新批次读取并冻结；不会改已入队执行的快照或顺序。后续内容 PUT 不丢失该独立策略。保存完成返回 200 和统一 operation 包装。

## 5 选择子任务批量操作

`POST /{job_id}/broadcast/children/actions`

Header 使用源任务身份，路径 job_id 为源任务。Body 只明确选择子任务，不自动扩大删除或执行范围：

```json
{
  "request_id": "本次选择操作的稳定请求ID",
  "action": "delete",
  "items": [
    {
      "tenant_id": "sap1002",
      "agent_id": "default",
      "job_id": "已查询到的子任务ID",
      "expected_definition_version": 1
    }
  ]
}
```

action 为 delete 或 run。每次 1～1000 项，重复完整身份拒绝；agent_id 省略时沿用源 Agent，source 始终继承源身份，不接受另传 source/scope。这里的接收方字段用于选择既有关联子任务，不是给任意任务指定身份的通用授权。

服务端先鉴权源，再逐项核对完整源子关系、版本和操作权限。真实 actor 保持不变，不把调用方伪装成接收人，也不人为提升为 manager。每项组合单任务能力，失败不回滚其他项；同次请求重提复用每项原操作或执行身份。

返回 200，包含 source_job_id、action、results、success_count、failed_count、pending_count。每项返回完整任务身份、status_code、status 和 operation 或 execution；错误返回安全 error_code。run 的 accepted 及其成功计数只代表受理成功，不代表执行成功，仍需查询 execution_id；删除的 pending 表示正常清理未结束。

## 6 管理操作查找和重试

沿用新版 CRUD 的 pending、指定 operation 查询和 retry 接口。启停同步失败重试只同步未完成的外部效果，不再次修改已读边界；被删除替代的失败启停不能通过重试恢复已删除任务。真正删除清理失败仍人工重试，正常等待在途结束的收尾由已装配后台完成。

自动暂停以 SWE 已提交的成功执行、首次暂停时间和失效水位作依据，内部产生 auto_pause 控制操作，不新增公开自动暂停接口或管理角色。Scheduler 停用任务定义并写旧表投影，再同步普通入口；批源仅停自身，不关闭整组批入口。平台同步失败可用原操作 retry，用户显式恢复或删除也可替代已失败旧动作；被替代的暂停重试返回 superseded，不能再次停用。运行中控制步骤仍按原互斥规则处理。

本单元不新增业务表；控制操作使用已有的新管理日志表，运行受理使用逐次执行登记表，优先级写源定义服务端元数据。真实 MySQL、外部平台和前端浏览器仍待完整联调。

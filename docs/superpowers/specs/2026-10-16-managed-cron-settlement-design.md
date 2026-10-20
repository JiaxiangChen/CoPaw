# 新执行异步收敛与共用容量反馈

本文件是既有第二阶段 U6 的实现补充，不新增执行入口，不改变外部平台的定时触发职责。

## 状态归属

SWE 继续写 `swe_cron_execution_runs.status/finished_at/history_execution_id` 及原执行历史。Scheduler 不覆盖这些实际执行结果，也不修改 Monitor 负责的 `swe_cron_executions.async_status`。

新运行登记表增加 Scheduler 专用的 `settled_status`、`settled_at`、`settled_error_code`，仅表达本次派发在容量及统计层面是否收敛。主执行 success 且历史异步状态仍未确认时保持未收敛；历史明确 success/error 后分别收敛为 success/error。主执行的明确失败、跳过或取消可直接收敛。重复观察不重复记账，不创建新的业务执行。

## 共库观察与保护

通过既有 `history_execution_id` 关联原历史，核对 job/tenant 以及历史 meta 中的管理归属、source、Agent 和 managed_execution_id，避免碰到其他任务或存量历史。缺失或不匹配的成功执行不虚报异步成功，保留占位并记录安全诊断。

等待异步收敛的成功执行仍占用共用模型池，并约束同任务的后续领取；删除清理同样等待，不能因主执行返回而提前移除仍在使用的资源。在途工作不因模式切换或源启停被取消。

## 动态反馈

已接入既有单一容量调整循环和策略算法。scope 清单去重、反馈合并新旧已登记工作，不开启第二套竞争同一个容量快照的调整器。新反馈来自共库已确认的收敛事实，HTTP 回执未知和未开始的 skipped 不伪装成业务失败；原 intents/events 的反馈算法不变。任一来源读取失败时不发布半份容量，取消或失败后收拢并行读取。

只读结构预检后才启用新统计；缺少新表字段的旧部署不查询新协议表。应用不执行 DDL，必要 ALTER 已补入 `2026-10-16-scheduler-cron-execution-api.md`，最终仍随 U8 集中核对，原 `swe_cron_executions` 不新增字段。

## 验证

覆盖主成功异步未结束仍占位、异步成功/失败后释放、重复收敛、关联身份错误、主失败、并发领取及重启恢复；验证删除等待、共用模型池不超领、只有新任务的 scope 也参与动态调整、反馈只合并一次且不回调 SWE、不改变旧表写入逻辑。

本地后端全范围 Windows 528 passed、4 skipped；Linux 532 passed、无跳过。101 个业务 Python 改动文件的 pre-commit 全项通过。并发收敛测试强制两个实例都先读到同一候选，再验证只提交一次；历史 tenant/job/归属/source/Agent/执行 ID 错配分别验证，不仅测试正常路径。均为本地数据库夹具及受控边界，不代表已完成真实 InnoDB、调度平台或网关验收。

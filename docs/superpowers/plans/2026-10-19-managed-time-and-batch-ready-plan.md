# 新版任务时间一致性与批成员就绪修复 Implementation Plan

> **For agentic workers:** 按用户约定主线程顺序执行executing-plans/TDD和自查，不启动子代理。基线已先提交e879d5e17，修复不自动提交、不推送，最后生成新的cron_trans行内文本包。

**Goal:** 新版列表/编辑时间一致；批入口提前触发后，内部成员按优先级和容量就绪，不按各自散列时间等待；看板使用用户要求的文案。

**Architecture:** 先用真实表单和列表/详情边界复现时间差，不臆定时区换算；批调度保留外部入口最早提前量和通知逻辑期，只将批成员not_before_at设为受理时间，普通调度散列不变。看板保留现有布局、接口字段及权限，删除解释句，展示“任务数”“子任务状态/时间”，不修改实际结果状态机。

**Tech Stack:** React/Ant Design、Vitest、Scheduler managed callbacks/order、pytest/SQLite、HTTP替身浏览器。

**Spec:** 用户本轮截图和要求；旧scheduler scheduling_service.create_batch_with_intents以context.now为整批due_at，不逐成员散列。对第一项只能在复现后确认原因；不以假设改业务排程。

## Constraints

- 新版通道修改，不动旧SWE接口或旧批调度行为。
- 不改表、不执行外部DDL或业务库写入；不取消在途/已入队工作，不批量改历史快照。
- 批入口提前时间、原通知时间、冻结顺位、热度优先、模型池容量保持原规则。
- UI沿用已有样式；不把业务分发子任务与执行中的异步子任务混为一谈，文案按上下文说明。
- 不使用子代理，最终如实报告主线程自查及真实环境未验证内容。

## Task 1: 时间不一致复现和最小修复

- [ ] 新增ManagedJobEditor.test.tsx：实际表单读取非默认10:00，保存不改为09:00；列表/详情一致及连续编辑两个时间；慢响应不覆盖当前任务。
- [ ] 核对job_view/list_item、detailToJob、parseCron、真实TimePicker；若差异源于表单初始化或展示，修复对应单点；用反向回归证明故障原因。
- [ ] 验证 `pnpm --dir console exec vitest run src/pages/Control/SchedulerCronJobs/ManagedJobEditor.test.tsx --maxWorkers=1 --testTimeout=20000`，源码变更先做GitNexus影响分析。

## Task 2: 批内成员不做散列等待

- [ ] 回归创建有0/非0提前量的父子批次，外部提前触发后所有not_before_at相同且已到期；原logical_fire_at、通知逻辑期不变，普通模式仍保留自身提前量。
- [ ] RED后修复batch_order.ordered_members的batch due_at为now，测试work_query当前可派发全部成员及原优先级顺位。
- [ ] 验证 `PYTHONPATH=src;scheduler/src .venv/Scripts/python.exe -m pytest tests/unit/scheduler/test_managed_batch_members.py tests/unit/scheduler/test_managed_cron_frozen_order.py tests/unit/scheduler/test_managed_cron_callbacks.py -q`。

## Task 3: 看板文案和交付

- [ ] index/BatchDetail及真实组件测试：顶部任务数，删除“主执行状态与异步收敛状态分别展示”整句，成员收敛改子任务状态/完成时间；不改status/settled_status协议。
- [ ] 运行新版管理/看板/旧辅助表单测试，Scheduler/SWE回归、TypeScript、定向lint/pre-commit与复杂度15。
- [ ] 浏览器替身核对编辑时间和看板截图；部署文档说明新入队批次生效、历史快照不重写。
- [ ] 使用package-internal-text-transfer生成新包，包含本次未提交修复，空目录/覆盖备份/哈希验证；旧包保留。提交基线与本次修复的打包边界在README明确。

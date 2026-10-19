# 新版执行失败详情和重试最终记录 Implementation Plan

> 后续要求覆盖：用户要求全部任务失败说明对齐旧版，非仅WorkflowCallError。失败摘要范围、200字符边界及去掉诊断后缀以2026-10-19-cron-error-compatibility-plan.md为准，以下原计划保留为决策背景。

> **For agentic workers:** 基线先提交为457d84a73。按用户AGENTS在主线程顺序执行TDD与自查，不启动子代理，不再自动提交或推送。

**Goal:** workflow历史保存可公开失败原因，新版业务重试维护旧历史is_final，新任务error状态hover显示同源当前失败详情。

**Architecture:** 保留cron_execution_failed等机器协议码，将workflow公开错误摘要存入现有result_json并映射历史error_message及状态last_error；原始响应、密钥及未知异常文本不公开。SWE新结果事务中插入is_final=1，并仅沿完整身份验证的previous_execution_id取消前一历史的最终标记。前端只覆盖新版状态列，复用原样式、添加Tooltip。

**Tech Stack:** SWE ManagedExecutionService/ExecutionWriter、共库swe_cron_executions、Scheduler既有BatchRetryService、React/Ant Design、pytest/SQLite、Vitest。

**Spec:** 用户三个问题。代码真实表名为swe_cron_executions；旧execution_sync_service._replace_dispatch_result在新历史替换时更新is_final=0，新版直接写共库路径缺失该逻辑。

## Global Constraints

- 不新增SQL/表字段，不执行外部DDL/业务库操作；使用已有error_message、result_json、last_error和is_final。
- 机器错误码及重试分类保持兼容，只公开WorkflowCallError已声明安全的信息、错误类别/HTTP状态。未知异常保留通用错误码，防止把请求体、令牌等写入表或Tooltip。
- 重试仅入队时旧历史仍是最终记录；新历史真正落库后同事务切换。运输重投不新增业务历史，不清其他任务/其他批次/手动运行的is_final。
- 同执行ID重复提交幂等，历史写入或is_final更新失败时整体回滚；验证source/Agent/tenant/job，不凭裸执行ID改旧结果。
- Tooltip仅新版error状态显示，running优先、success/idle不展示旧错误；缺失详情给明确提示；旧页面/旧接口不改。

## Task 1: 失败详情贯通

Files: src/swe/app/crons/managed_{failure,execution,history,results}.py；tests/unit/app/test_managed_cron_failure_detail.py。

- [ ] RED：WorkflowCallError(http_status=502)经实际执行写入，历史error_message和state.last_error应包含公开类别/HTTP状态，run.error_code保持cron_execution_failed；未知异常令牌不持久化；大文本截断及脱敏。
- [ ] 实现安全摘要及历史/状态映射，读取旧result_json无摘要仍回退机器码；状态取最新实际执行，不能由晚完成旧错误覆盖新成功。
- [ ] `PYTHONPATH=src;scheduler/src .venv/Scripts/python.exe -m pytest tests/unit/app/test_managed_cron_failure_detail.py tests/unit/app/test_managed_cron_execution.py -q`。

## Task 2: 新业务尝试切换最终历史

Files: managed_results.py；tests/unit/scheduler/test_managed_batch_retry.py；共享SQLite/MySQL测试历史schema补齐既有is_final列（仅测试表）。

- [ ] RED：真实批失败历史→Scheduler人工重试入队→SWE写新结果，前一is_final从1改0、新行1；重复重试/结果提交不重复切换；其他身份历史保持1。
- [ ] 新历史与前历史更新放在原结果事务，按完整身份校验前驱run及history_execution_id；无history前驱或非法身份不修改无关数据。
- [ ] 覆盖is_final更新异常事务回滚、二次重试链和运输重投；不改旧同步服务逻辑。

## Task 3: 新版error悬浮详情及验收

Files: console/src/pages/Control/SchedulerCronJobs/columns.tsx及columns.test.tsx；部署验证文档。

- [ ] RED：error hover出现state.last_error，running覆盖error、success残留错误不展示、空错误fallback。保留状态点/样式及旧列行为。
- [ ] 使用原状态列render结果加新版Tooltip，不发额外请求；校验定向Vitest、typecheck、eslint/格式、Pythonpre-commit复杂度15及扩大后端回归。
- [ ] 浏览器HTTP替身核对Tooltip，不作为预发布验收。仅部署SWE与Console，Scheduler旧重试契约不变；不修改旧历史存量错误文本。

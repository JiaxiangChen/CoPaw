# 新版批调度看板六项展示对齐

## 部署范围

部署 Monitor、Scheduler、Console。无需新增表/列、手工SQL或配置；本轮不修改SWE执行、调度/重试写入、旧版看板及共享Worker调节逻辑。先部署两个后端，再部署构建后的Console。此前SWE失败说明修复仍应随原变更部署，本轮不会恢复已丢失的历史异常。

## 展示和接口增量

- 错误：成员“结果 / 错误”及失败预览“原始失败原因”展示当前执行关联的历史error_message，支持悬浮查看；按history_execution_id、tenant_id、job_id校验历史归属。历史缺失时取result_json.error_detail，再回退公开错误码，不返回整个结果JSON。仅有cron_async_subtasks_failed时显示其已知含义“子任务执行失败”，不编造具体子任务原因；机器字段仍保留原码。
- 优先：顺位可hover/focus查看中文优先依据、用户/分行顺位、分行名称和热度；不再在单元格输出优先JSON。
- 模型：批次列表及详情标题下显示父任务本次执行冻结的provider/model，成员仍展示自身模型。没有父任务成员的批次显示“- / -”，不拿当前定义替代执行快照。
- Owner：按用户最新要求，移除新版详情头部的Owner项，其余父任务、计划、回调三项等宽展示。后端字段不变，不新增Owner采集，dispatch_owner派发令牌和SWE run_owner都不用于展示。
- 结果：显示成功、失败、跳过/取消数量；失败批次使用结果数量，其他批次使用终态进度。入口暂停显示“批调度入口已暂停”，已入队任务继续执行，重试不增加逻辑任务数。
- Worker：展开调整记录展示记录ID、Worker ID、Source、策略、最小/基线/最大、调整前后、等待/领取/运行、成功失败、失败率、平均耗时、记录时间及命中规则。保留共享模型池、完整游标历史、曲线和分页。

只增加响应字段，不改变既有字段：Monitor批次增加success_count、skipped_count、provider_id、model_id、dispatch_paused、lock_owner(null)，成员及Scheduler失败预览增加error_message。

## Postman与界面验证

使用专用测试Source及管理员Header：X-Source-Id、X-User-Id、X-User-Role=manager。沿用现有网关认证与其他Header，不另造用户身份。

1. GET /api/monitor/cron/managed-dispatch/batches，检查父任务模型、成功/跳过数；重试前后member_count不增大，成功不能包含跳过/取消。
2. GET /api/monitor/cron/managed-dispatch/batches/{batch_id}，核对members[].error_message与该execution关联的历史一致；主执行成功且子任务失败时不能显示成功终态。缺失/损坏摘要可回退码，但不得显示其他租户、任务或上一业务尝试的错误。
3. GET /api/scheduler/cron/managed-dispatch/batches/{batch_id}/failures，核对失败预览error_message与成员详情一致。此新增展示字段不进入retry候选请求，仍由原版本字段与request_id控制重试。
4. 打开/monitor/cron-batch-dispatch-new，检查列表和详情的模型、失败批次成功/失败数、跳过数，以及运行中批次的入口暂停标记；详情头部仅显示父任务、计划、回调，不显示Owner。
5. hover/focus顺位查看中文优先信息；横向滚动到“结果 / 错误”并hover长错误；失败页签核对同一说明。
6. 展开Worker调整记录核对结构化字段，再展开模型曲线。检查原看板/monitor/cron-batch-dispatch仍按原逻辑展示。

## 本地验证和限制

新旧看板回归9文件42项通过；最后标题布局调整后2文件7项通过。最终后端扩大回归625通过、1跳过（Windows缺Linux fcntl）、1个既有Starlette/httpx弃用警告，含子任务错误码中文回退的22项专项。TypeScript、ESLint、Prettier及生产构建通过，构建有既有chunk/动态导入警告。Python定向pre-commit全部通过，包含圈复杂度上限15、mypy、Black、Flake8、Pylint。

Chromium本地HTTP替身已核对优先Tooltip、错误Tooltip、模型、Owner缺值、结果数量、入口暂停及Worker结构化字段，截图output/playwright/cron-board-priority-worker-parity.png和cron-board-error-result-parity.png。不是行内联调或真实MySQL验证；页面有既有根布局/缺省接口日志，不宣称整站无错误。按用户AGENTS主线程自查，未运行独立代理评审。

## 部署后观察

- 测试批次至少覆盖一次主执行失败、一次子任务失败、一次业务重试及一个入口暂停但已有队列的批次；上线验证期间由部署人核对上述GET和同源历史。
- 健康信号：新接口200、错误说明与当前执行一致、任务数不随重试膨胀、Owner令牌不出现在响应、跨Source数据不可见，Worker曲线与历史仍完整。
- 异常信号：cron_batch_storage_unavailable、cron_batch_relation_incomplete、数据库未知列/SQL异常、查询明显变慢、跨身份错误或统计不一致。暂停使用新版看板并回滚本轮Monitor/Scheduler查询及Console发布，保留执行数据排查；不要删除批次、重跑任务或修改调度开关来掩盖展示问题。

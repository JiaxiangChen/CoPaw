---
status: completed
---
# 新批调度看板六项展示与旧版对齐

用户要求错误、优先、模型、Owner、批次结果和Worker调整详情保持旧版展示。仅改新版看板及必要的只读查询字段，保留新版统计、执行、子任务和重试语义，不改旧页面，不增加SQL/配置，不自动提交、打包、推送。

## 决定及边界

- 错误列显示当前执行关联历史error_message（租户/任务/历史ID校验），无记录时回退result_json.error_detail及公开错误码；成功成员不显示旧错误。失败预览同样补充错误说明。只读现有持久化摘要，不返回整个result_json、请求或异常链。
- 优先：使用旧版中文依据、用户/分行顺位、分行名称、热度Tooltip；保留新版冻结顺位及每成员模型信息。
- 模型：批次列表/详情头部显示父任务当前执行快照的provider/model，不用后来修改的定义替代；成员仍显示各自模型。
- Owner：新版没有旧版等价的批次lock_owner，沿用旧版缺值Owner -，绝不暴露作为dispatch_token的dispatch_owner或冒用SWE run_owner。无数据不虚构。
- 结果：展示成功/失败及跳过取消数、进度和暂停标识；暂停文案为“批调度入口已暂停”，不改变已入队继续执行的语义；任务数不重复计算重试。
- Worker：沿用旧版结构化调整详情，含Source、Worker ID、策略、容量、运行/失败指标、命中规则，保留完整游标历史和曲线，不改共享资源池行为。

## 实施单元

1. 查询字段（测试优先）：Monitor managed_board_sql.py/managed_board.py补充成功/跳过数、父任务模型、入口暂停以及当前历史错误；Scheduler batch_failures.py失败预览用独立只读批查询补充历史摘要，不改变用于人工重试的锁查询或候选字段。测试：test_managed_board_query.py及test_managed_batch_retry.py相邻专项。
2. 新版展示（测试优先）：managedBatchBoard.ts增量字段；ManagedBatchDispatch内组件增加旧样式提示和Tooltip；Worker结构化组件仅新版使用，旧版源码不改。测试：BatchDetail/FailurePanel/index/SharedWorkers。
3. 验证与交付：后端Scheduler新看板/重试和Monitor相关测试；新旧看板Vitest单进程回归、类型/格式/ESLint、Python定向pre-commit及生产构建；本地HTTP替身浏览器截图。不执行真实DDL或生产业务请求。

GitNexus新看板符号尚未入索引，impact为UNKNOWN；同名旧failure_preview/FailurePanel结果不能冒称覆盖新版。已核对实际调用方，未触及写入和调度路径。遵循用户AGENTS在主线程顺序实施/自查，不启动子代理；Superpowers工具不可用，以本地计划与TDD记录验证，不冒称独立评审。

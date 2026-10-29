# 有效执行记录统计实施计划

目标：执行详细设计中的全部查询变更，保留历史、API 和点击口径。按用户授权在当前会话顺序执行，不创建子代理，不提交或发布。

- [x] 加入有效执行统计的实际 SQL 行为测试，先确认旧实现失败。
- [x] 对目标方法运行 upstream impact，报告共享接口影响。
- [x] 修改 tracing 查询、用户成功 CASE 与 cron 页面查询。
- [x] 适配受影响 SQLite fixture，验证目标 SQL 覆盖、分页和导出。
- [x] 运行相关 pytest、差异检查并更新 playbook，记录数据库实测边界。

交付验证：相关回归组合 105 项通过；扩展历史回归的 7 项失败均使用 HEAD 原代码复现，详情见设计文档。本次不执行数据库 DDL，不提交或推送。

命令：`venv_swe/bin/python -m pytest tests/unit/monitor/test_cron_final_analytics.py -q`；扩展回归使用 monitor 查询测试、overview、BBK 和 Scheduler final-marker 测试。索引只形成评估结论，不执行建索引。

---
title: feat 定时任务一次提交全量快照迁移
type: feat
status: completed
date: 2026-10-15
---

# 一次提交全量快照迁移

## 边界与决定

用户不希望手工重复调用分页接口。本增量计划替代上一计划中“只能由客户端翻页”的交互决定，不改变迁移映射、保护条件或业务范围。

- 新增一次提交的异步任务入口，支持 preview/import/verify，后台调用现有 MigrationService 自动消费全部游标（包括核对的数据库阶段）。
- 业务数据仍只写定义与状态两表；运维进度和逐页安全诊断复用既有 `swe_async_tasks`、`swe_async_task_items`。这些既有表必须已部署，缺失明确返回 503，不自动建表、不增加列。
- 不承诺集群全部内存；采集执行实例已加载的状态，保存实例标识及缺失数量。
- 数据库保存累计进度、每页结果和心跳，其他实例可查询。实例退出不自动恢复后台 worker；查询能识别心跳过期，操作者重新提交同范围安全重跑，不要求逐页恢复。
- 提供请求停止能力，在当前批次结束后停止；已提交业务数据不回滚。保留原同步分页接口。
- 不提交、推送、运行生产迁移或 DDL。按 AGENTS 顺序在主线程执行；自检不冒充独立评审。

## U1：后台任务与持久进度（先测试）

新建 `src/swe/app/crons/migration_tasks.py`、`tests/unit/app/test_cron_migration_tasks.py`。
先写失败测试：跨多页汇总、空页继续、verify 双阶段、部分失败/缺失 OR 汇总、错误脱敏、取消、心跳过期、数据库进度写失败不能报告成功。
实现任务写入器、有限并发后台运行及分页结果读取；不得聚合所有明细在内存或单行 JSON 中。
进度中的 scanned_files 是各页扫描次数之和，不宣称不同文件数；target_count/done_count 是已扫描的检查项，不事先承诺全量百分比。

## U2：HTTP 能力（先测试）

追加 `migration_models.py`、`migration_api.py`；新建 `tests/unit/routers/test_cron_migration_tasks_api.py`。
新增 `POST /tasks`、`GET /tasks/{task_id}`（支持逐页结果查询）、`POST /tasks/{task_id}/stop`。
沿用全局认证与内部认证；正文禁止路径/外部游标，任务 ID 校验，缺表 503、不存在 404、并发保护 429。提交立即 202，不等待迁移完成。
HTTP 集成测试经过真实来源、迁移 Service/Repository、任务写入器，数据库使用显式 fake；至少验证一次请求完成多页导入和跨实例查询，文件/旧路径不变。

## U3：文档与验证

更新设计和 Postman 说明，新增异步验证记录，旧计划正文保留作为最初交互决定的记录。
运行新增测试并记录先失败后通过；重跑已有第一阶段测试、相关旧接口回归、格式/复杂度/类型检查及 git diff 检查。
真实 MySQL 未配置时显式跳过，不将 fake 当作锁或真实事务证据。

## 验证命令

项目 `.venv/Scripts/python.exe -m pytest tests/unit/app/test_cron_migration_tasks.py tests/unit/routers/test_cron_migration_tasks_api.py -q`。
随后使用 WSL 项目 `venv/bin/python` 执行第一阶段全部测试与相关旧接口回归。

## 完成条件

Postman 一次提交即可消费全部分页；进度、终态、所有分页诊断可查；中断或部分失败不伪装全成功；原文件、内存和执行入口不因本功能改变。

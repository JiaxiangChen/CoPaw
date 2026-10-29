# 定时任务追踪后端归属迁移 Implementation Plan

> 执行方式：按用户 AGENTS.md 在主线程顺序执行，不派发代理，不提交、不推送。

**Goal:** 将尚未发布的任务追踪后端从 Monitor 整体迁到 Scheduler，保留全部只读追踪能力及前端入口。

**Architecture:** Scheduler 直接读取三服务共用数据库，提供 lookup/overview/timeline 三个 GET；不经 Monitor 转发，不改变写入者。前端仍在运行监控菜单，路径 `/monitor/cron-task-tracking` 不变。

**Tech Stack:** 现有 FastAPI/Pydantic、异步数据库接口、React/Ant Design；无新增依赖。

**Spec:** 原详细设计 `docs/plans/2026-10-28-001-feat-cron-task-tracking-plan.md` 的表、节点、时间、身份、去重分页及局部降级规则不变，仅修订服务归属。

## Global Constraints

- API 前缀改为 `/api/scheduler/cron/task-tracking/jobs/{job_id}`；移除未发布的 Monitor 追踪路由，不维护两份实现。
- 仅 manager/admin；可信用户/来源/角色头、完整任务身份及新协议历史校验不变，不额外依赖调用方租户/Agent/scope 展示头。
- 不套用调度后台 ready 门禁；不新增表、配置、后台任务、跨服务依赖或写入。
- 保留原分页、时间范围、查询预算、错误脱敏和查询 ID；保留已有 staged/unstaged 的其他修改。

## Task 1：确定服务入口契约并观察失败

文件：`console/src/api/modules/cronTaskTracking.test.ts`、`tests/unit/monitor/test_cron_task_tracking_api.py`（之后随实现移动至 scheduler 测试目录）。

1. 先把预期 API 前缀改为 Scheduler，保留当前实现，运行前后端测试，确认因旧前缀而失败。
2. 补覆盖真实 Scheduler 注册、Monitor 旧入口 404、后台未就绪仍可读、额外展示 Header 不改变管理员所选任务身份。

验证：迁移前请求新路径得到 404；迁移后正常 200、权限错误保持 400/403，所有 SQL 仍只读。

## Task 2：整体移动后端与测试

文件：将 `monitor/src/monitor/app/services/cron/tracking_*.py`、模型和路由移至 `scheduler/src/scheduler/app/` 对应目录；八个 `test_cron_task_tracking_*.py` 移至 `tests/unit/scheduler/`。

1. 用受限于本 worktree 的精确文件移动保留既有实现，不改查询或节点生成语义。
2. 调整数据库工厂、三 Header 管理员校验、路由注册；取消对 Monitor 权限 helper 的依赖。
3. 更新测试引用及 `tests/manual/cron_task_tracking_app.py`，测试所有原有规则和新服务边界。

验证：Scheduler 不 import Monitor/SWE；Monitor 不再注册追踪接口；原有 90 个测试及新增服务边界测试通过。

## Task 3：调用方、文档与交付核验

文件：前端 API 模块、详细设计、部署验证指南、领域术语位置。

1. 只修改前端 API 前缀，不移动页面、菜单或改变交互样式。
2. 文档改为发布 Scheduler + Console、三个 Scheduler GET；保留 Monitor 作为部分状态原有写入者的说明。
3. 全局搜索旧服务引用，运行前端/API/权限/调度回归、类型检查、复杂度与格式检查。

验证命令：WSL 项目虚拟环境运行 `pytest tests/unit/scheduler/test_cron_task_tracking_*.py`；Console 运行相关 Vitest、`tsc -b --noEmit`；用本地隔离应用验证页面仍请求新后端。

预计交付：迁移后的工作区代码和更新文档。无需新建工作区、迁移数据、打包或推送。

## 完成核验

迁移后追踪测试 94 个通过，Scheduler 全部单元测试 725 个通过，前端及导航相关回归 25 个通过；类型、定向 lint、复杂度、生产构建及真实页面连接 Scheduler 测试应用通过。九个查询服务文件迁移前后字节相同。原 Monitor 代码目录和路由无本功能残留，页面/菜单保持原位置。提交、推送、行内数据库及 Chromium 108 验收均未执行。

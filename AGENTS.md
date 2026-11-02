# AGENTS.md

## 项目架构

### 架构总览

| 层级 | 目录 | 说明 |
|------|------|------|
| 核心后端 | `src/swe/` | Python 主体，包含 Agent、FastAPI 应用、配置、Provider、安全与租户能力 |
| 测试 | `tests/` | 单元、集成、启动与租户隔离测试 |
| Console | `console/` | 主控制台前端 |
| 部署 | `deploy/` | 容器构建、入口脚本、Supervisor 模板 |
| 工具脚本 | `scripts/` | 安装、打包、迁移、测试脚本 |
| 设计文档 | `docs/superpowers/specs/` | 近期设计稿与专项方案 |

核心目录视图：

```text
src/swe/
├── agents/         Agent 编排、提示词、技能、工具、内存
├── app/            FastAPI、通道、路由、工作区、运行器、定时任务
├── cli/            `swe` 命令行入口与子命令
├── config/         配置模型、环境配置、上下文与路径工具
├── tenant_models/  租户模型、上下文、管理器与辅助函数
├── providers/      云模型 Provider 与适配层
├── local_models/   本地模型管理与下载
├── security/       工具审批、技能扫描、路径边界
├── tracing/        调用链追踪、脱敏、落盘
├── token_usage/    Token 使用统计
├── envs/           环境变量持久化
├── database/       MySQL 连接配置
├── tunnel/         Cloudflare 隧道
└── utils/          通用工具
```

### 运行入口

| 入口 | 关键文件 | 说明 |
|------|----------|------|
| Python 包入口 | `src/swe/__main__.py`, `src/swe/__init__.py`, `src/swe/__version__.py` | 包级执行与版本信息 |
| CLI 入口 | `src/swe/cli/main.py` | `swe` 命令主入口，按子命令延迟加载 |
| HTTP 应用入口 | `src/swe/app/_app.py` | FastAPI 应用工厂与生命周期管理 |
| 应用级管理器 | `src/swe/app/multi_agent_manager.py` | 多 Agent / 多工作区总控 |
| 工作区装配 | `src/swe/app/workspace/*.py` | 服务管理器、租户初始化、租户池、工作区对象 |
| 请求执行 | `src/swe/app/runner/*.py` | Query 执行、会话、任务跟踪、控制命令、Repo 落盘 |

主链路：

```text
CLI / HTTP / Channel Request
  -> src/swe/cli/main.py 或 src/swe/app/_app.py
  -> src/swe/app/multi_agent_manager.py
  -> src/swe/app/workspace/workspace.py
  -> src/swe/app/runner/runner.py
  -> src/swe/agents/react_agent.py
  -> tools / skills / memory / providers / local_models
```

## 功能索引

功能域的实际子文件、关键路径和职责说明统一放在 `analysis/` 目录。

| 功能域 | 摘要 | 链接 |
|--------|------|------|
| Agent 编排与执行内核 | 覆盖 Agent、Prompt、Tool Guard 接入、技能、内存、内置工具 | [analysis/agent-and-orchestration.md](analysis/agent-and-orchestration.md) |
| 通道接入、API 与访问界面 | 覆盖 Channels、Routers、Middleware、审批入口与 Console | [analysis/channels-and-access.md](analysis/channels-and-access.md) |
| 配置体系与租户隔离 | 覆盖 `constant.py`、配置模型、请求级目录、租户模型与工作区初始化 | [analysis/config-and-tenant-isolation.md](analysis/config-and-tenant-isolation.md) |
| 模型、Provider 与本地运行时 | 覆盖云 Provider、本地模型、MCP、数据库连接与模型运行栈 | [analysis/model-provider-and-local-runtime.md](analysis/model-provider-and-local-runtime.md) |
| 安全、审批与治理边界 | 覆盖 Tool Guard、技能扫描、路径边界、认证与审批服务 | [analysis/security-and-governance.md](analysis/security-and-governance.md) |
| 观测能力与支撑系统 | 覆盖 Tracing、Token Usage、Cron、备份、Tunnel、Deploy、Scripts | [analysis/observability-and-supporting-systems.md](analysis/observability-and-supporting-systems.md) |

## 经验累积

经验类文档统一放在 `analysis/playbook/`，用于沉淀排查入口和重复问题处理方式。
如果出现冲突，请对文档同步进行修正。如果没有的，请对文档同步进行补充。

| 主题 | 摘要 | 链接 |
|------|------|------|
| Playbook 索引 | 汇总经验文档、适用场景和阅读入口 | [analysis/playbook/README.md](analysis/playbook/README.md) |
| 常见报错 | 收敛高频报错样式、典型来源和第一落点 | [analysis/playbook/common-errors.md](analysis/playbook/common-errors.md) |
| 定位路径 | 说明常见问题对应的代码入口、配置入口和命令入口 | [analysis/playbook/location-paths.md](analysis/playbook/location-paths.md) |
| 日志入口 | 汇总 `swe.log`、query error dump、Tracing 和 daemon logs 的查看方式 | [analysis/playbook/log-entrypoints.md](analysis/playbook/log-entrypoints.md) |
| 排查顺序 | 提供从复现到收敛范围的最小排查顺序 | [analysis/playbook/troubleshooting-order.md](analysis/playbook/troubleshooting-order.md) |

## 开发环境

### 部署环境

- OS: Linux 3.15 内核
- 部署方式: Kubernetes 容器多实例部署
- 外部依赖:
  - Redis 集群（可连接）
  - MySQL 数据库（可连接）

### 仓库结构

- 核心 Python 代码位于 `src/swe/`
- 主控制台前端位于 `console/`
- 测试位于 `tests/`
- 部署与安装资源位于 `deploy/` 和 `scripts/`
- 长文档设计稿位于 `docs/superpowers/specs/`

### 多用户并发支持

Swe 支持多用户并发，并通过请求级目录实现隔离：

```text
~/.swe/
├── alice/
│   ├── config.json
│   ├── active_skills/
│   ├── customized_skills/
│   ├── memory/
│   ├── models/
│   └── sessions/
├── bob/
│   └── ...
└── (default user)
    └── ...
```

关键函数位于 `src/swe/constant.py`：

- `set_request_user_id(user_id)`：设置当前请求用户上下文
- `get_request_working_dir()`：获取请求级工作目录
- `get_request_secret_dir()`：获取请求级密钥目录
- `get_active_skills_dir()`：获取请求级激活技能目录
- `get_memory_dir()`：获取请求级记忆目录
- `get_models_dir()`：获取请求级模型目录

通道请求会自动携带 `sender_id` 并映射到 `request.user_id`。CLI 单用户模式使用 `swe app --user-id <id>`。

### Provider 配置隔离

Provider 配置按租户独立存放：

```text
~/.swe.secret/
├── default/
│   └── providers/
│       ├── builtin/
│       ├── custom/
│       └── active_model.json
├── alice/
│   └── providers/
└── bob/
    └── providers/
```

- 每个租户拥有独立的 API Key、Base URL 和激活模型配置
- `ProviderManager.get_instance(tenant_id)` 返回租户级实例
- 新租户首次访问时可继承默认租户配置
- CLI 支持 `--tenant-id` 进行多租户管理

### Sonar 规范

- 控制函数圈复杂度，普通函数建议不超过 `15`
- 控制函数参数数量，普通函数建议不超过 `7`
- 对重复出现的错误文案、状态文案、字段描述文案，优先提取为模块级常量或小型 helper，避免散落的重复字面量

## 测试要求

### 基本要求

- Python 测试统一使用 `pytest`
- 优先将测试放在对应子系统附近，例如 `tests/unit/app/`、`tests/unit/providers/`、`tests/unit/workspace/`

### 运行方式

始终使用项目虚拟环境运行测试：

```bash
# 运行全部测试
venv/bin/python -m pytest

# 运行单个测试文件
venv/bin/python -m pytest tests/integrated/test_version.py

# 运行某个目录
venv/bin/python -m pytest tests/unit/tenant_models/ -v

# 跳过慢测试
venv/bin/python -m pytest -m "not slow"
```

### 交付校验

- 提交前建议执行 `pre-commit run --all-files` 与必要范围的 `pytest`

# coding guidelines

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

### 技能管理服务边界

技能管理功能由两个服务协作完成：

| 功能 | API 端点 | 服务 | 说明 |
|------|----------|------|------|
| 用户技能 CRUD | `/market/skills/*` | market | 用户技能创建、删除、启用/禁用、文件编辑 |
| 工作空间技能列表 | `/skills` | src/swe | Agent 运行时技能状态查询（只读） |
| 技能池管理 | `/skills/pool/*` | src/swe | 管理员管理共享技能池 |
| Hub 导入 | `/skills/hub/*` | src/swe | 从外部 Hub 导入技能 |
| Agent 重载回调 | `/api/internal/agents/{id}/reload` | src/swe | Market 服务修改技能后回调通知 |

**服务间通信：** Market 服务通过 HTTP 回调触发 SWE 服务的 Agent 重载，确保技能配置实时生效。

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

### 案例配置

案例配置用于在首页展示不同的业务场景案例，支持按用户进行案例列表隔离：

```text
~/.swe/
├── cases.json           # 案例定义（iframe_url、标题、步骤等）
└── user_cases.json      # 用户-案例映射（userId -> caseIds）
```

**案例定义结构 (`cases.json`)：**

```json
{
  "cases": [
    {
      "id": "case-deposit-maturity",
      "label": "他行存款到期潜力客户名单",
      "value": "帮我分析...",
      "sort_order": 0,
      "is_active": true,
      "detail": {
        "iframe_url": "https://...",
        "iframe_title": "详情面板标题",
        "steps": [
          { "title": "步骤1", "content": "步骤说明" }
        ]
      }
    }
  ]
}
```

**用户映射结构 (`user_cases.json`)：**

```json
{
  "user_cases": {
    "default": ["case-deposit-maturity", "case-fund-sales"],
    "alice": ["case-deposit-maturity"],
    "bob": []
  }
}
```

**userId 过滤逻辑：**

1. API 读取 `X-User-Id` 请求头
2. 若无则读取 `user_id` 查询参数
3. 若无则使用 `default` 映射
4. 返回映射数组中对应的案例列表

**管理页面：**

Console 提供 `/cases-management` 页面，包含两个标签页：
- 案例定义：创建/编辑/删除案例
- 用户分配：配置 userId 可见案例列表

### 代码风格

- Python 使用 4 空格缩进、`snake_case` 模块名、Black 79 列
- 目录与文件命名遵循现有模式，例如 `channel.py`、`registry.py`、`test_*.py`

### Sonar 规范

- 控制函数圈复杂度，普通函数建议不超过 `15`
- 控制函数参数数量，普通函数建议不超过 `7`
- 对重复出现的错误文案、状态文案、字段描述文案，优先提取为模块级常量或小型 helper，避免散落的重复字面量

### 代码注释规范

#### 基本要求

- **语言要求**：所有注释必须使用简体中文，包括 docstring、行内注释、TODO/FIXME 标记

#### 注释时机（以下情况必须添加注释）

- **模块级**：每个 Python 模块文件开头应有简短说明，描述模块职责和主要功能
- **类级**：类定义应有 docstring，说明类的用途、关键属性、使用示例（如适用）
- **函数/方法级**：公共函数和类方法必须有 docstring，说明功能、参数含义、返回值、可能的异常
- **行内注释**：
  - 复杂算法或非直观逻辑的实现步骤
  - 业务规则或业务逻辑的决策依据
  - 边界条件处理、异常捕获的原因
  - 性能优化相关的技术决策
  - 临时方案或已知限制（配合 TODO/FIXME）
  - 正则表达式、复杂公式等难以直接理解的代码片段

#### 注释内容要求

- 注释应解释 **WHY（为什么这样做）**，而非简单重复 **WHAT（代码做什么）**
- 注释应提供代码本身无法表达的信息：设计意图、约束原因、相关背景
- 避免无意义注释（如 `# 循环遍历列表` 对应 `for item in list`）

#### 注释维护

- **同步更新原则**：修改代码时，相关注释必须同步更新，确保注释与代码一致
- 删除代码时，相关注释一并删除，不要保留过时的注释
- 发现注释与代码不一致时，优先检查代码正确性，然后修正注释

#### 禁止事项

- 不要保留注释掉的代码块，使用版本控制管理历史代码
- 不要添加已废弃或不再生效的注释
- 不要在注释中包含敏感信息（密码、密钥、内部 IP 等）
- 不要用注释掩盖代码质量问题（应直接修复代码）
- **不要在注释中出现"中文注释"、"以下是注释"等无意义的标签式文字**，注释应直接表达实质内容
- 不要添加显而易见的注释（如 `# 定义变量 x` 对应 `x = 1`）
- 不要在注释中重复函数名或变量名（如 `# 调用 get_user_info 获取用户信息`）

#### 特殊标记

- **TODO**：标记待完成的功能或优化项，格式：`# TODO: 简要描述待完成内容`
- **FIXME**：标记已知问题或待修复的 bug，格式：`# FIXME: 简要描述问题及影响`
- **HACK**：标记临时方案或 workaround，格式：`# HACK: 简要描述临时方案及后续计划`
- 以上标记应在后续迭代中及时处理和清理

### Subagent 工作方式

- 默认直接在当前仓库中处理开发内容，不使用 worktree；如需隔离工作区，需由用户明确提出

## 测试要求

### 基本要求

- 所有测试位于 `tests/`
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

### 前端与交付校验

- Console 变更至少需要通过格式化、Lint 和构建检查
- 提交前建议执行 `pre-commit run --all-files` 与必要范围的 `pytest`

### Commit 与 PR

- 提交信息使用 Conventional Commits：`feat(scope): summary`、`fix(scope): summary`、`docs(scope): summary`

### 开发规范（按照难易程度选择开发范式）

- 对简单问题或者 bugfix，直接进行开发和修复
- 对于较复杂的问题，使用 brainstorm 和 superpowers 工具进行规划和开发
- 对于横跨多个模块的特性开发和问题处理，请先使用 openspec 工具进行深入分析和指定计划，再使用 TDD 的范式进行开发和实现
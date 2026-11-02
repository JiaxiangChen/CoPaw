## Context

状态：2026-10-30 用户已确认沿用原有外部平台/网关权限，开始实施。

当前链路：Monitor `/monitor/tracing/sessions` 从 `swe_tracing_traces` 聚合会话；弹窗通过 SWE `/tracing/chats?user_id=…` 将 session_id 映射为 chat_id；`ReadOnlySessionChat` 调用看板详情接口读取正文。该接口自行处理 agent.memory 和 task_messages，与 Chat 的 `_build_chat_history` 已有差异。

Chat 详情还处理 schema v3 分轮存储、轮次消息、失败消息、归档元信息和隐藏上下文清理；归档通过 `_archive_page` 读取。Chat 前端 `MessageList` 直接调用当前用户 `chatApi.getChatHistory`，不能将其原样放进运营弹窗，否则仍会请求错误的用户上下文。

Console 使用 React / Ant Design，遵循 `console/DESIGN.md` 与中央 Token，兼容 Chromium 108。该变更是历史浏览能力补齐，沿用现有弹窗视觉，不重新设计运营看板。

## Goals / Non-Goals

**Goals:**

- 保持看板接口的目标用户语义，在权限校验之后共享 Chat 的历史数据组装与归档分页。
- 同一 source、目标用户、Agent、chat_id 和固定持久化快照下，两条读取路径返回相同的消息、归档边界及分页内容。
- 看板支持查看压缩前历史，按页读取并保持阅读位置；界面只读，切换对象不会串数据。
- 错误与正常空内容明确区分；归档失败可重试，已显示内容不丢失。

**Non-Goals:**

- 不改变 Monitor 排行榜、会话列表、日期及资源筛选口径，不新增聊天存储或迁移数据库。
- 不允许发送、停止生成、审批或恢复执行，不接入实时流和自动轮询；运行中的记录显示最近持久化快照，因此不承诺与实时 Chat 同步到毫秒。
- 不跨 Agent 聚合，不修改当前用户 Chat 的访问语义，不全面重构大型 MessageList。

## Decisions

### 1. 分开访问入口，共享数据读取函数

保留现有映射、详情接口，新增归档接口。后端调用顺序为：操作人查看权限校验 → source/目标用户/Agent 工作区解析 → ChatSpec 归属校验 → 共用读取 helper。

| 接口（均带 `/api` 前缀） | 职责与返回 |
| --- | --- |
| `GET /tracing/chats?user_id=…` | 目标用户聊天映射；现有响应不变 |
| `GET /tracing/chats/{chat_id}?user_id=…` | `_build_chat_history(..., non_blocking=True)`，返回现有 `ChatHistory`，补齐 chat/archive 字段 |
| `GET /tracing/chats/{chat_id}/history?user_id=…&before=…&limit=…` | `_archive_page(workspace, chat_id, before, limit)`，返回 `ChatArchivePage` |

归档参数与 Chat 一致：before 是不透明游标，limit 为 1–50，默认 50；前端与当前 Chat 一样使用 limit=20。非法参数/游标返回 422，不存在或不属于目标上下文的聊天返回 404，读取故障不能伪装为空历史。权限拒绝响应按确认后的授权契约实现。

保留公共读取 helper 在 `runner/api.py` 的位置，先直接复用，不为该任务搬迁大量 helper。现有看板已从此模块导入函数，这能最小化回归范围。若必须改动共享符号，先执行其 GitNexus impact，并回归 Chat 的直接消费者。

不复用 Chat 路由处理器、不修改 request.state 来假扮目标用户、不通过服务内 HTTP 转发。

### 2. 权限定位与数据归属分开处理

用户已确认：沿用既有外部平台/网关权限控制，有权访问排行榜的操作人可查看相应用户的历史，不在 SWE 内新增运营角色/分行限制。新增归档路径沿用同一认证/路由入口；部署时须覆盖相同网关规则。

user_id 指定查看对象；映射、详情和归档保持相同目标工作区解析规则。详情与归档共用 ChatSpec 校验，检查目标 user 和可用 source/Agent 元信息，缺少历史元信息时保持兼容。保留当前用户 Chat 的权限语义，不修改 request.state 或调用当前用户鉴权函数来冒充目标用户。

### 3. 看板只读区域接入专用归档适配器

保留 `ReadOnlySessionChat` 的独立状态，不使用 Chat 的全局 session 状态或直接绑定 Chat 的 MessageList。调用 `tracingApi.getUserChatHistory`；详情转换按需复用 `convertMessagesForSession`，归档复用 `convertArchivedPage`、`mergeHistoryResponses` 和已有边界卡片。已有私有 helper 仅在确有必要时以小范围导出，不复制其实现。

本次推荐顶部可键盘操作的“加载更早历史”入口，每次加载一页，沿用 Chat 的边界语义、去重规则和错误反馈。暂不复制主 Chat 的自动补页、生成态刷新等复杂机制；内容一致性的定义是可逐页读取同一完整存档，不要求滚动触发策略完全相同。初次仅请求当前详情，有归档时才展示入口；即使当前消息为空但存在 archive.has_more，也可继续加载。

分页状态按 source / targetUserId / Agent / chatId 隔离：cursor、has_more、loading、error、generation 和已加载消息/边界。关闭弹窗、改变用户/会话或上下文时重置，旧响应不得写入新状态。请求串行，has_more=true 却缺少新游标视为失败，防止无限重复加载。

合并消息保持从旧到新的展示顺序，按稳定原始消息标识与边界标识去重；失败不推进游标，保留已有内容。前插历史前记录当前可见消息锚点，渲染后恢复阅读位置；快捷导航使用合并后的消息。只读区域不注册发送、审批等写操作，继续禁用 HTML 预览事件记录。

“暂无聊天内容”、未找到映射、加载失败分别提供准确反馈；不把网络或权限错误显示成无记录。不引入新字体或新设计 Token。

### 4. 用共同存档验证一致性

固定同一工作区快照，分别调用 Chat 和看板详情/归档接口，比较 messages、archive、status 及分页内容，覆盖普通会话、任务会话、v3 分轮存储、模型失败、隐藏上下文、无当前消息但有归档、跨页边界与重复消息。

权限覆盖：合法操作人读取目标用户、无权限操作人、user_id/chat_id 不匹配、跨 source/Agent、不合法游标，以及确认后的网关/平台边界。运行中测试验证读取最近提交快照且不会等待模型结束；不以持续变化的两个实时请求做字节相等断言。

前端覆盖加载/重试/到达起点、快速切换用户/会话、重复点击、旧请求返回、滚动锚点、只读交互、快捷导航和空快照加载归档。浏览器按 DESIGN Verification 检查桌面尺寸、嵌入模式、焦点与异常反馈，并验证 Chromium 108 和当前浏览器；缺失真实基线环境须报告限制。

## Risks / Trade-offs

- [外部平台/网关需覆盖新路径] → 沿用用户确认的现有访问边界，部署检查新归档路径与详情接口的规则一致。
- [共享 helper 导入会涉及 Chat] → 避免迁移和改变算法，保持现有响应契约，执行 Chat 历史回归。
- [界面使用 Bubble.List，未自动获得 Chat 的分页行为] → 显式接入看板归档适配和独立分页状态，测试真实 API 路径，避免误用当前用户接口。
- [归档已清理、映射缺失或共享存储不通] → 展示准确状态，本次不能恢复已不存在的历史；Kubernetes 多实例需确认相同目标工作区可访问同一份持久化存档。
- [运行中与 Chat 实时流不完全同步] → 以持久化快照内容一致为验收标准，实时订阅不在本次范围。

## Migration Plan

1. 确认权限规则并补全对应契约/测试；实施前对实际修改符号做影响分析。
2. 后端先补齐详情共用逻辑和看板归档 API，响应模型保持兼容。
3. 前端再接归档浏览及异常状态；不修改现有 Monitor 列表查询。
4. 执行项目 venv 的定向 pytest、前端定向测试/类型和构建检查、项目前端审查与浏览器验证；补充 analysis/playbook 对应定位和排查记录。
5. 同一发布完成前后端切换。回滚代码即可，不删除或重写任何存档，无数据迁移回滚。

## Confirmed Scope

- 权限：沿用既有外部平台/网关控制，不新增 SWE 角色/范围校验。
- 完整历史不受排行榜日期截断；沿用既有 Agent 定位；顶部按页加载；只读持久化快照。

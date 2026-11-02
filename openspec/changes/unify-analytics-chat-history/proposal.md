## Why

运营看板用户详情的聊天正文虽然读取 SWE 会话存档，却维护了独立的消息组装逻辑，未覆盖 Chat 的新版分轮存储、失败消息和压缩归档历史。需要在保留目标用户访问边界的同时共用 Chat 的读取逻辑，使两个入口对同一存档提供一致的历史内容。

## What Changes

- 改造现有 `/api/tracing/chats/{chat_id}?user_id=…`，定位目标工作区并校验后，复用 Chat 的 `_build_chat_history`，不经 HTTP 调用当前用户的 Chat 接口。
- 增加看板专用 `/api/tracing/chats/{chat_id}/history?user_id=…&before=…&limit=…`，复用 Chat 的归档分页读取逻辑和响应模型。
- 看板只读聊天区域支持归档边界、分页加载更早历史、去重合并、滚动位置保持、失败重试和切换会话后的过期请求丢弃。
- 三个看板读取入口（映射、详情、归档）沿用既有外部平台/网关权限控制；用户已确认，可访问活跃用户排行榜的操作人可以查看对应用户的全部可用历史。本次不新增 SWE 角色或分行授权。
- 保留 Monitor 的会话统计和列表筛选口径；日期筛选决定入选会话，不截断选中会话的完整历史。保持只读，不接入发送、审批操作或运行中流式续接。

## Capabilities

### New Capabilities

- `analytics-chat-history`: 目标用户聊天历史读取、与 Chat 同源组装、归档分页及看板只读浏览契约。

### Modified Capabilities

无。既有 Chat 行为不变，新增看板历史读取能力。

## Impact

- 后端：`src/swe/app/routers/tracing.py`，复用 `src/swe/app/runner/api.py` 的历史组装和归档分页 helper；尽量保持公共读取函数原位，避免无关模块迁移。
- 前端：`console/src/api/modules/tracing.ts`、用户详情弹窗的 `ReadOnlySessionChat.tsx` 及其局部样式；按需导出已有消息/归档转换 helper。
- 测试：`tests/unit/routers/test_tracing_chats_api.py`、Chat 历史/归档回归测试、看板组件与浏览器交互验证。
- 不需要数据库迁移，不复制历史数据，不全量加载所有归档。新增接口使用现有存档，依赖共享存储及目标工作区定位正确。
- 权限规则已于 2026-10-30 经用户确认，进入实施。

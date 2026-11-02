# 本地交付验证（2026-10-30）

## 改造范围

- 运营看板已有详情接口改为调用 Chat `_build_chat_history(non_blocking=True)`；新增看板专用归档接口，调用 `_archive_page`。
- 普通 Chat 后端 `src/swe/app/runner/api.py` 无 diff；前端 `sessionApi/index.ts` 仅给 `withArchiveBoundaries`、`convertMessagesForSession` 增加 export，算法无变化。
- 权限沿用用户确认的外部平台/网关规则，不新增 SWE 运营角色/分行授权。目标工作区解析沿用现有实现；详情/归档共用聊天归属检查。
- 前端保留只读弹窗，新增归档分页、游标校验、重试、去重合并和阅读位置保持；source/user/Agent/session/chat 改变后重建浏览状态。

## 自动验证

- 后端：项目 venv 定向 pytest **53 passed**。范围为 `test_tracing_chat_history_parity.py`、`test_tracing_chats_api.py`、`test_session_turn_ledger.py`、`test_model_call_error_history.py`、`test_conversation_archive.py`。
- 新测试先复现看板与 Chat 返回差异，再验证真实临时 Session 存档、v3 冷轮次、失败消息、隐藏上下文、连续归档页、错误游标、目标归属和运行中非阻塞读取。
- 前端：Vitest **156 passed / 10 files**。覆盖看板只读组件、tracing API 适配、Chat sessionApi、BubbleList 和 Chat MessageList 历史测试。
- 修改范围 ESLint、TypeScript typecheck 通过；Python 修改文件 pre-commit 全部适用检查通过（mypy、black、flake8、pylint 等）。未运行全仓库 lint/格式化，避免修改无关基线。
- `npm run build` 首次触及默认约 2GB Node heap 上限；以 `NODE_OPTIONS=--max-old-space-size=8192 npm run build` 完成构建。构建保留既有大 chunk/混合动态导入提示。
- OpenSpec strict validate 与 git diff --check 通过。
- pytest 退出时出现现有日志包装器关闭已关闭流的提示（`my_logging._Utf8StderrStream.close`），不影响测试 exit code；本次未修改日志系统。

## 浏览器与前端审查

- Playwright + 本机 Chrome **156.0.8078.74**，真实挂载 `UserDetailModal`（含左侧会话、统计、只读聊天）；通过模拟 Monitor/SWE API 提供固定脱敏数据，不连接真实用户数据。
- 地址带 `hideMenu=true`，验证 1280×720、1440×900、1920×1080；截图在 `/tmp/analytics-history-{1280,1440,1920}.png`，临时 QA 页面不入库。
- 验证真实消息渲染、连续两页归档、重复消息去重、到达起点、注入一次 503 后保留游标并键盘 Enter 重试。请求仅使用看板专用历史路由，未调用当前用户 `/api/chats`。
- 前插一页后当前消息位置偏移 **-0.25px**。无 pageerror；仅出现既有 forwardRef、Ant Design 弃用提示和刻意注入的 503。
- copaw-f2e-review：接口契约、只读卡片、Hooks 生命周期、可发现的加载/重试入口及主题局部性通过；共享转换函数只导出，未改 Chat 算法。加载、空、错误及 unavailable 反馈已区分。

## 实施准备与部署限制

- GitNexus：`get_user_chat`、`ReadOnlySessionChat`、`withArchiveBoundaries`、`convertMessagesForSession` 的预改分析均 LOW。两个共享转换 helper 各 3 个直接调用、1 个间接调用，影响集中在 SessionApi；HTTP/JSX 图未识别的关联由源码和回归测试补充确认。
- 现有部署代码和数据库结构不变，无会话/归档数据迁移；前后端一起发布，回滚代码即可，不删除任何存档。
- 待部署环境验证：真实 Chromium 108/360、Windows Chrome/Edge、平台/网关新增路径覆盖、Kubernetes 共享存储及真实历史。当前浏览器验证不能替代这些环境验收。

## Review 后的局部修复

- 已修复第一项：仅在看板初始快照转换后补 `history: true`，不修改公共转换/合并函数或原 Chat 逻辑。
- 新增 `ReadOnlySessionChat.archive.test.tsx`，使用真实 Chat 转换及合并函数，先复现两个回答，再验证合并为一个；调用公共转换函数本身仍无 history 标记。
- 本轮定向 Vitest 145 passed / 9 files（看板两组测试、Chat sessionApi 与 MessageList）；修改文件 ESLint 通过。
- 第二项历史计划详情已按后续用户授权修复，见下节。

## 历史计划只读展示修复

- 仅调整看板 `READONLY_CARDS`：回答旁复用原有 `PlanReviewSnapshot`，普通 RuntimeResponseCard 仍移除交互计划数据，独立 PlanInteraction 保持为空，避免同一计划重复展示。
- 公共 PlanInteractionCards、RuntimeResponseCard、Chat 接口均无修改。快照保留标题、摘要、执行步骤、风险、验证方式、决定状态和修改意见，没有执行/修改/退出操作按钮。
- 待确认、已提交计划的回归测试先复现内容缺失，再验证单份完整快照且无按钮。看板、归档合并、Chat 计划及 sessionApi 共 **139 passed / 8 files**；TypeScript typecheck、修改文件 ESLint 和 Prettier check 通过。
- Chrome 156 使用真实看板只读组件及模拟接口验证当前消息、归档、任务分组中的计划：三场景均只有一个计划 region、零操作按钮、无 pageerror；1280×720、1440×900、1920×1080 均无横向溢出。截图 `/tmp/analytics-plan-{current,archive,task}.png`；临时页面已清理。
- 仍未验证真实 Chromium 108/360 和生产网关/共享存储；本次不扩大部署范围。

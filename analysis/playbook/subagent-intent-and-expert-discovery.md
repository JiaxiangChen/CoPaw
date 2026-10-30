# SubAgent 意图与专家发现排查

## 工具没有出现

先区分启动权限与管理权限。当前真实用户文本命中“子代理”“子智能体”“subagent”“sub-agent”，或结构化 `enable_subagents=true`，才开放新启动。英文忽略大小写，历史、工具结果及模型生成的 Contract 不产生授权。关闭专家主执行开关时，显式 `selected_expert_id` 是独立且排他的单轮 SubAgent 入口，只过滤可启动目录，由 Main 决定是否启动；开启后的普通 Chat 使用服务端持久专家绑定。

## 专家主执行与绑定冲突

- Source 管理员开关：`feature_switches.expert_as_main_enabled`，默认关闭，与自主发现开关独立。只有现有结构化选择器建立绑定，正文提及专家不建立绑定。
- 绑定接口：`GET/PUT /chats/{id}/expert-binding`；PUT 显式提交 `expected_revision` 与 `expert_id`（null 解除）。JSON 仓储在跨进程 flock 内比较修订；通用 Chat 更新不能覆盖绑定。
- 发送前：`runner/expert_main_admission.py` 在发布 user turn 前校验修订、开关和专家。409 `expert_binding_changed` 同步最新身份；`expert_main_disabled` 解除绑定；`expert_unavailable` 保留绑定并阻止执行。前端保留草稿与附件供手动重发，不自动重试。
- 执行：`runner/expert_main.py` 捕获每条消息的最新有效本地定义和私有依赖；`expert_main_runtime.py` 管理内部执行身份、预算和审批快照。专家直接流式回复，不创建 SubAgent Run，不读取 Profile 长期记忆，不能继续委派。
- 审批：原进程的 pending lease 续接原身份与预算。进程重启或请求落到没有该 lease 的实例时安全拒绝，reason=`expert_execution_unavailable`，需重新发起任务；不能用最新配置重放旧工具。
- 开关关闭、Plan/Goal、`/new`/`/clear` 解除绑定；`/compact` 保留。失效专家按精确 ID 恢复，同名重建不接替。
- 回归入口：`test_expert_main_binding.py`、`test_console_chat_expert_main.py`、`test_expert_main_preparation.py`、`test_expert_main_runtime.py` 与 `test_expert_main_dependency_versions.py`。本地两个进程的 CAS 测试不代表 Kubernetes/NAS 已验收。

- 判定：`src/swe/app/subagents/intent.py`。
- 注册：`SWEAgent._register_background_subagent_tools`。
- Source 默认关闭开关：`feature_switches.subagent_expert_discovery_enabled`；Console 文案“允许自主选择我的专家”。开关不代替用户意图，也不控制 Skill-owned 定义。
- 当前租户/Profile 的专家还须有效且已启用。不可见定义应同时从工具描述和名称解析排除。

## 能管理旧运行但不能启动

这是无新意图轮次的预期行为。`run_access.py` 用逻辑 session 及记录中的 Chat ID 校验归属；缺少可靠 session 不开放管理。get/cancel 在 supervisor 副作用前验证，wait 从持久记录投影当前会话，不消费其他 Chat 的终态通知。预读 bool 仅控制工具展示，不替代每次访问校验。

普通异步 Query 在 `_prepare_query_agent_context` 的 worker 中扫描保留记录，再在事件循环安装工具。不要将文件扫描移回 Agent 构造。刷新必须清除预读缓存；Goal 的工具刷新也在 worker 执行。回归入口为 `test_event_loop_request_preparation.py` 与 `test_chat_runtime_access.py`。

## 昵称控制、等待与错误

- Main 使用 `start_subagent(expert_name, objective, background="")` 选择目录中的专家，使用返回的 `nickname` 调用 get/cancel；不再传 `name`、`instruction` 或内部 `run_id`。选中专家只缩小目录，不强制启动、等待或接管本轮回复。协议入口为 [subagent_background.py](../../src/swe/agents/tools/subagent_background.py)，回归为 [test_background_tools.py](../../tests/unit/subagents/test_background_tools.py)。
- 昵称在当前 Chat 的保留记录范围内唯一，同名依次分配 `研究员`、`研究员-2` 等，终态记录仍占用昵称。查不到昵称时先核对 tenant/Profile、逻辑 session、Chat ID 与保留记录；跨 Chat 或重复匹配均不解析。分配与跨进程预留入口为 [nicknames.py](../../src/swe/app/subagents/nicknames.py)、[supervisor.py](../../src/swe/app/subagents/supervisor.py)，归属解析为 [run_access.py](../../src/swe/app/subagents/run_access.py)。
- `wait_subagent(nicknames=[...], timeout_ms=...)` 等待指定组全部终态；其他运行完成不会提前结束。超时返回最新状态及 `timed_out: true`，不会取消运行。省略 nicknames 使用 Chat 级监控；空数组为 `invalid_request`，未知昵称在等待前返回 `not_found`。回归入口为 [test_chat_runtime_access.py](../../tests/unit/subagents/test_chat_runtime_access.py)。
- 模型侧失败只返回通用错误，不能依赖原始异常文本；终态失败记录的通用消息为 `SubAgent execution failed.`。排查原始原因请查内部运行日志与 worker stderr，勿将内部 ID 或异常重新拼进工具响应。存储及错误投影入口为 [run_store.py](../../src/swe/app/subagents/run_store.py)、[runtime.py](../../src/swe/app/subagents/runtime.py) 与工具适配器。

## 社区专家依赖与并发首次启动

每个 Chat/专家使用独立冻结视图，每次启动使用自己的 context 副本。不要将专家绑定 Skill/MCP 注册到父 Agent 通用目录。

同 Chat、同专家并发首次发布时，不能删除已发布目标。原子发布失败的一方须验证并复用赢家，只在 finally 清理自己的临时目录。回归：`test_discovered_expert_dependency_views.py::test_same_expert_concurrent_first_starts_reuse_published_view`。

## Goal 意图丢失或模式退出泄漏

初始授权在服务端 Chat meta 的 `goal_draft_subagent_delegation_enabled` 跨澄清保存，确认创建后消费；普通/Plan/明确专家入口及关闭 Goal 模式清除。客户端不能写此内部字段，回传 Plan meta 时须过滤。

正式 Goal 使用快照 JSON 字段 `subagent_delegation_enabled`，旧记录缺省 false。Steering 可提升为 true；同一 Goal 内不能被旧快照降级。MySQL save 在同一事务 SELECT FOR UPDATE 后 OR 合并，内存存储同语义。不要只给部分 service 写者补锁，否则 completion review 等旧快照仍能覆盖授权。后续轮刷新 Source 配置与目录，已启动运行保持原快照。

回归入口：`tests/unit/app/goals/test_delegation_intent.py`。SQL 事务单测不替代真实 Kubernetes 多实例 MySQL/NAS 验收。

## 新会话首条消息未携带手选专家

2026-10-28 诊断：两个 Source 开关均关闭时，普通 Chat 的手动专家选择仍应通过 `selected_expert_id` 提供对应 SubAgent 目录；无需正文关键词授权。2026-10-30 起由 Main 自主决定是否启动，不再强制执行。不要用开启自主发现作为补救。

原 Console 存在首次发送时序缺口：`Chat/index.tsx` 的 `beforeSubmit` 仅快照专家绑定修订，没有快照 legacy 手选专家 ID；`useChatController.submitTurn` 在请求前执行 `ensureSession`，新建会话改变 `activeSessionId`；页面依赖该 ID 的 effect 清空 `legacySelectedExpertId`，而 `useChatRequest` 使用最新 `apiOptionsRef` 中的 fetch。随后 `customFetch` 从已清空的状态构造请求，省略 `selected_expert_id`，后端按默认 Agent 执行。

已修复：`beforeSubmit` 将本轮手选专家 ID 冻结到提交参数；`customFetch` 优先使用该快照。会话创建和临时 ID 解析登记为同一选择范围的别名，避免误清空；发送后保留选择及输入框下方名称，后续消息继续提交该专家，切换其他会话/Profile、手动取消或进入 Plan/Goal 时清除。回归测试 `index.test.tsx` 的 `keeps a manual expert visible through new-chat creation, resolution, and repeated submissions` 覆盖首次发送、ID 解析、重复发送和切换会话。后端回归入口为 `test_runner_auth_token_passthrough.py`，验证所选专家身份传递与不再注入强制执行标志。定位实际会话仍应核对 `/console/chat` 请求字段及是否进入 Plan/Goal；此复现不代表所有未调用专家的会话均为同一原因。修复应在提交时冻结本轮选择并贯穿会话创建，保留切换其他会话时清空选择的行为。

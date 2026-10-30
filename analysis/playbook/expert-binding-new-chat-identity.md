# 新草稿选择专家后的会话身份排查

## 适用现象与结论边界

新聊天选择专家后进入历史内容，或专家绑定的 Chat 与下一条消息实际执行的 Chat 不一致。2026-10-29 本地分析确认了固定逻辑 ID 的产生条件与重复 ID 下的后端定位漂移；尚未取得用户现场网络请求，因此不把这些最小复现直接等同于现场完整根因。

## 两种新聊天状态

- 显式点击新建：`console/src/pages/Chat/sessionApi/index.ts` 的 `createSession` 使用 `createLocalSessionId` 生成时间戳加毫秒内序号，将其写入草稿 `id` 和 `sessionId`。
- 初始空白 `/chat`：`initialSessionSelection.ts` 在 URL 无 ID 时返回空选择；初始化不会主动分配草稿 ID。普通首次提交由 `useChatController.submitTurn` 调用 `ensureSession` 后才创建草稿。
- 提交前选择专家会提前落库：`Chat/index.tsx` 的 `useExpertBinding.ensureChat` 在草稿对象、URL ID 和 activeSessionId 均为空时回退到 `${getChannel()}:${getUserId()}`。实际执行该函数的最小探针分别得到 `console:user-1`、显式新建的时间戳、仅 activeSessionId 存在时的时间戳。

## 重复逻辑 ID 的后果

`POST /chats` 为每条记录生成新 UUID，但没有阻止多个 Chat 共用逻辑 `session_id`。`_get_or_create_console_chat` 的普通发送分支按 session_id、user_id、channel 调用 ChatManager，而不使用请求里的 chat_id 选择目标。JsonChatRepository 的逻辑索引在重复键中选择 updated_at 最新的 Chat。

最小复现使用临时 JsonChatRepository 和真实 `_get_or_create_console_chat`：先创建 old-history-chat、new-expert-chat，二者 session_id 均为 console:user-1；请求指定 new-expert-chat 时最初解析到新 Chat。随后更新旧 Chat 名称，相同请求解析到 old-history-chat。临时文件已随 TemporaryDirectory 清理。

底层会话文件按 session_id 和 user_id 定位；新版 v3 历史还按 Chat UUID 投影，不能断言重复逻辑 ID 必然展示全部旧消息。旧格式、无 chat_id 归属记录及后端重新定位仍需分别核对。

## 为什么存在独立专家绑定接口

ADR 0042 和 `2026-10-27-expert-as-main-execution-design.md` 要求绑定立即保存、后续消息生效、在途消息身份保持快照、多页面旧状态拒绝。当前 GET/PUT expert-binding 负责所有权与专家有效性验证、Source 开关协调、修订号比较、Plan/Goal 互斥及冲突视图；通用 meta 写入禁止客户端提交 expert_main_binding。

这些要求支持保留已有 Chat 的独立绑定更新接口，但不要求新 Chat 的创建和初始绑定必须分为两个请求。当前无 Chat 时执行 POST chats → GET binding → PUT binding，失败会留下无绑定的空 Chat；这是实现取舍，不是产品决策必然要求。

另一个静态可见的生命周期问题：无 Chat 且尚无 binding view 时，`useExpertBinding.change` 会先 ensureChat，再 GET binding 检查 enabled。即使 Source 实际关闭主执行模式，也可能先创建空 Chat，随后才返回 legacy；此分支不会执行 saved 分支的新旧身份映射和导航。该问题尚未做浏览器复现。

## 现场判别顺序与改进方向

1. 核对选择专家前 URL、activeSessionId、草稿 sessionId 和 POST chats 的 session_id；显式新建仍走固定值时继续查状态同步，不能直接归因于初始空白页。
2. 记录 POST 返回的 Chat UUID、PUT expert-binding 的目标 UUID、选择后 URL；区分新 UUID 展示旧内容与直接进入旧 UUID。
3. 对照随后 POST console/chat 的 chat_id、session_id 和响应 X-Swe-Chatid；若不一致，检查重复逻辑 ID 及逻辑索引选择。
4. 新草稿应先获得稳定独立的逻辑 ID；已有 Chat 的普通发送应按经授权且身份一致的 chat_id 精确定位。缺失 ID 的旧客户端兼容路径不能模糊覆盖明确目标。
5. 如优化创建流程，可为创建请求提供显式初始专家字段，由服务端验证后一次提交 Chat 与初始绑定；继续保留已有 Chat 的修订号更新。仅减少接口数量不能修复重复 session_id 或发送定位漂移。

## 本次验证

- 前端原函数探针：空白页固定 ID、显式草稿时间戳、无草稿对象但 active ID 存在时使用 active ID。
- 后端真实 JsonChatRepository/ChatManager/Console 解析探针：重复逻辑 ID 随旧 Chat 更新发生定位漂移。
- `venv/bin/python -m pytest tests/unit/app/test_expert_main_binding.py -q`：23 passed。
- 前端指定 index、initialSessionSelection、useExpertBinding 测试并筛选 creates and immediately binds / initial / binding：10 passed、59 skipped。未执行浏览器现场端到端复现。

## 2026-10-29 修复结果

用户授权继续后已实施：专家与 Plan 提前创建不再使用固定用户级逻辑 ID，改为复用草稿独立时间戳 ID；POST chats 支持 initial_expert_id，经验证后一次原子提交 Chat 和初始绑定，返回绑定视图；已有 Chat 继续使用独立 CAS 更新。主执行关闭时初始绑定请求不创建空记录，前端转为旧的单消息选择。

初始专家创建按逻辑草稿键幂等：响应丢失后的重试返回原记录，不覆盖另一页面的新绑定。发生绑定冲突时响应包含经授权的 Chat UUID，前端同步身份和修订再导航，避免把已有绑定修订写入未知身份的草稿。

普通请求提取阶段保留并校验 chat_id；明确 Chat 发送按其 UUID 和归属精确定位，逻辑身份不一致返回冲突，不改投其他 Chat。保留旧客户端把未解析逻辑 ID 同时放入 chat_id 的兼容路径；即使 chat_id 与 session_id 文本相同，明确 UUID 也不能在不存在时被解释为新草稿。

最终后端相关 166 项通过；前端相关 118 项通过，两个已确认基线失败的上下文占用按钮测试单独跳过。TypeScript 通过，业务变更 ESLint 无错误（保留页面三项既有 Hook 警告）。未改动历史数据，未做现场浏览器端到端验收。

## 创建响应丢失后专家不可用的重试

初始创建的幂等恢复必须先按逻辑草稿身份查找已提交的 Chat，再校验新建记录所需的初始专家。否则，首次创建成功但响应丢失、专家随后禁用或删除时，重试会在找到原 Chat 前返回不带 chat_id 的 expert_unavailable，导致草稿无法恢复。

恢复现有草稿先校验用户、Source、Profile 归属，再读取当前绑定及其可用性；同一专家不可用时仍返回原 Chat UUID、修订号和 available=false，禁止执行的职责继续留在发送准入。若绑定已被其他页面切换，返回带原 Chat UUID 与最新绑定的冲突，不重建或覆盖记录。未落库的新草稿仍需通过原有初始专家校验。原子创建中发现并发已创建记录时，也复用同一恢复流程。

回归覆盖：相同专家不可用、绑定已切换且专家不可用、跨 Source 和跨 Profile 重试必须在解析专家前拒绝。

恢复过程中若 Source 关闭并需要解除绑定，预读后被并发删除的 Chat 应返回 404，不能把仓储 KeyError 暴露为 500。

本次后端相关回归共 171 项通过，包含原子创建、会话发送与恢复、绑定并发和复杂度预算。

## 首次查询未命中后的并发提交

首次查询草稿不存在不代表初始专家校验失败时仍不存在：另一创建请求可能已在两者之间提交。初始绑定校验抛出 HTTP 业务错误时，再按相同 session_id、channel、user_id 查询一次；存在则复用授权恢复流程，返回当前绑定和 Chat 身份，不存在则原样抛出最初错误。该检查不放宽新草稿的校验，也不绕过恢复时的归属校验或重建已提交记录。

确定性回归在首次查询读到空结果后提交另一请求的草稿，再使专家不可用或关闭功能开关，覆盖两个时间窗口，不依赖 sleep 或随机调度。

本轮相关后端回归共 173 项通过。

## 选择专家后的界面重新加载（草稿别名）

草稿绑定成功后，前端记录 local session ID → Chat UUID 并替换 URL。即使二者代表同一会话，`ChatSessionInitializer` 在列表已经包含服务端 Chat 时仍会调用 `setCurrentSessionId`，触发会话加载和输入区重建。仅在目标不在列表时跳过数字草稿不能覆盖此路径。

普通可编辑会话中，若当前 ID 已明确映射到路由解析后的 Chat，保留当前会话状态；真正切换到其他 Chat 及 content-only 深链接仍按原流程加载。新增回归用例覆盖“本地草稿活跃 + 后端记录已在列表 + 路由换为 UUID”。不应取消专家绑定持久化或取消 URL 同步来掩盖重载。

消息宽度排查同时发现：响应卡片中的无宽度包装 div 是纵向 flex 气泡的直接子项，`align-items: flex-start` 使它按长表格的固有宽度展开，内部 Markdown 的 `max-width: 100%` 无法约束外层。应为气泡、内容列及直接卡片子项设置收缩/最大宽度边界，工具卡片采用 border-box 并在代码内容区保留滚动。不要只对页面设置 overflow hidden，否则右侧内容无法访问。

本地真实组件浏览器探针：390px 视口修复前 document.scrollWidth=6298，修复后为390；360、390、768、1280、1440、1920 六种宽度均无页面横向溢出，6290px 的表格与14356px 的代码在内部滚动。探针不连接生产会话；未验证实际 Chromium 108 或现场端到端流程。

### 完整响应卡片遗漏路径的补充修复

上一轮单气泡探针没有包含真实 `RuntimeResponseCard`，因此不能据此判定完整会话无溢出。真实路径为 Bubble → RuntimeResponseCard → `messageBlockStart`（align-items: flex-start）→ 带 data-history-anchor-id 的响应包装 div → Markdown/工具卡片。只约束 Bubble 的直接子项，不能约束 messageBlockStart 内部另一个 flex 子项；其仍按表格的固有宽度展开。

完整组件探针采用真实 RuntimeResponseCard、AgentScopeRuntimeWebUI、消息列表、全局 layout.css、swe 主题及240px侧边栏，加载模拟会话数据，不访问远端。保留上一轮修复时，1280px视口的列表 clientWidth=992、scrollWidth=5574；内部消息包装层宽5558px。补充约束消息块直接子项后，长表格在内部滚动。另将列表 width:100% 的 padding 和审批卡片 border 纳入 border-box，并去掉列表/输入区的300px最小宽度，避免窄容器额外溢出。

修复后320、360、390、768、1280、1440、1920px七种视口的列表 scrollWidth 均等于 clientWidth；表格与展开的审批代码分别保留5558px和14356px内部内容，scrollLeft=100可读到右侧内容。验收必须检查消息列表自身的 scrollWidth/clientWidth，不能只检查 document（页面可能将溢出裁切）。远端内网测试环境尚未访问，不能据本地结果声称已部署生效。

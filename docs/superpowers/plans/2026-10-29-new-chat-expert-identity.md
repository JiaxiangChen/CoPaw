# 新草稿专家身份修复计划

目标：每个新草稿使用独立且稳定的逻辑 ID；新 Chat 与初始专家绑定一次保存；已有 Chat 发送按明确 UUID 定位，保留旧客户端兼容与绑定修订校验。

本计划执行用户在完整方案后的“请继续”授权，在当前会话串行实施，并按代码审查技能进行只读复核；不自动提交工作区改动。

1. 在 `tests/unit/routers/test_console_chat_expert_main.py` 添加重复逻辑 ID、明确 Chat 不存在、所有权与逻辑身份不一致测试；先运行看到失败，再修改 Console 目标解析。明确 UUID 失败不得回落到其他 Chat；客户端尚无后端 UUID 时保持逻辑 ID 创建兼容。
2. 在 `tests/unit/app/test_expert_main_binding.py` 添加创建携带 initial_expert_id 的测试，验证绑定 revision=1、非法或关闭时无新记录、Plan/Goal 冲突、普通创建兼容。创建请求模型扩展 ChatSpec；服务端先验证再一次保存 Chat 与服务端生成的绑定，响应附绑定视图。
3. 更新 Console API 类型、`useExpertBinding` 和 Chat 创建适配：无 Chat 时直接调用带初始专家的创建流程并采用返回修订；已有 Chat 保留 GET/PUT。关闭主执行模式的创建冲突转为 legacy，且不创建空记录。
4. 将时间戳生成移到共用模块；Chat 专家与 Plan 提前落库都使用当前草稿的稳定独立 ID，无 ID 时按当前草稿 scope 分配，重试复用。补前端组件与 hook 回归，先失败再实现。
5. 执行相关 pytest、前端测试、TypeScript 与变更文件 lint/format，检查差异和用户既有改动，更新排查文档中的修复与验证结果。

## 完成记录

五项已完成。初始创建另增加了仓储原子幂等提交：响应丢失重试复用原 Chat；已有绑定改变时返回经授权的 Chat UUID 和最新修订，前端同步原 Chat 并保留冲突语义。普通发送保留请求提取阶段的 chat_id，再按明确身份定位；兼容尚无后端 UUID 的旧客户端逻辑 ID 别名。

后端相关 166 项 pytest 通过，包含复杂度预算。前端相关 118 项通过；另两个上下文占用按钮测试在修改前的 HEAD 页面基线也失败，本次回归明确跳过。TypeScript 检查通过；业务变更文件 ESLint 无错误，Chat 页面保留三项既有 Hook 警告。未执行现场浏览器端到端验证，未迁移已有重复逻辑 ID 数据。

# AgentTraceSDK 0.2.2 Implementation Plan

Goal: 修复 operation 契约，补模型与整轮 I/O。
Architecture: 保留 reasoning chat Span，以 task-scoped collector 连接模型包装器。

- [x] 先增加长输出、工具结构和 operation 回归断言并确认失败。
- [x] 锁定 SDK、更新 boundary/fallback/fake 和三个 operation。
- [x] 实现白名单投影、总预算、模型输入与中断快照。
- [x] 补整轮 I/O 与真实 SDK 独立验收入口。
- [x] 执行专项 pytest、black 和 diff 检查，更新文档。

验证结果：相关 pytest 358 passed；新增模块 flake8 与 git diff --check 通过。真实 SDK 验收脚本已提供，但当前镜像未装私有包，公共镜像无 0.2.2 制品，因此真实包验收仍是发布前阻塞项。

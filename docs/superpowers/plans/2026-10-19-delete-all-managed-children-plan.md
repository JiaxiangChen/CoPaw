# 删除全部新版分发子任务 Implementation Plan

> **For agentic workers:** 按用户 AGENTS 在主线程顺序执行 writing-plans、executing-plans、TDD 和自查，不启动子代理，不把自查称为独立评审。

**Goal:** 在新版子任务管理弹窗增加“删除全部子任务”，覆盖当前父任务全部未删除子任务，不受筛选或分页限制。

**Architecture:** 复用 childrenAll 与现有父任务授权 batch-delete 接口。确认前重新完整读取未删除子任务及版本，确认框显示父任务、数量和全量范围；固定确认时快照，不删除之后新增的子任务。超过接口 1000 项限制时顺序分片，各片保存稳定请求 ID 和原版本，失败续办不换 ID/清单、不重复提交已完成片；以已有逐项结果展示已知进度，尚未返回结果的项算待确认，不虚构“未删除”事实。

**Tech Stack:** React、现有 Ant Design/AgentScope Button 和 Modal、TypeScript、Vitest/Testing Library、现有 Scheduler API。

**Spec:** 用户要求“子任务管理页面加一个删除全部子任务的按钮”。沿用现有逻辑删除/在途继续语义，不删除父任务，不修改旧页、后端、表或配置。新按钮与现有删除选中按钮同排、同款 danger 样式；确认前不写数据，取消不产生删除请求。

## Global Constraints

- 范围是当前 context + parent job.id 的全部未删除子任务，不是当前页、当前筛选或其他父任务。
- 每片最多 1000，真实用户 Header 与父授权入口不变，保留服务端父子关系及版本检查。
- 请求结果未知时保留原计划，已有原批量删除未结束时不另启全量删除。
- 弹窗卸载后不再提交下一片，不把迟到响应写入其他身份或工作区；确认框一并销毁。
- 不提交、不推送、不更新行内包，保留所有已有本地修改。
- GitNexus impact 已执行但未识别 ManagedChildrenModal，风险 UNKNOWN；源码确认只由新版 SchedulerCronJobs/index.tsx 使用。不修改公共 API 签名或旧管理组件。

### Task 1: 测试先行

**Files:** Modify console/src/pages/Control/SchedulerCronJobs/ManagedChildrenModal.test.tsx；Create console/src/pages/Control/SchedulerCronJobs/childrenDeletion.test.ts。

- [ ] 页面测试：12 项跨两次 API 分页，筛选只显示 1 项时仍确认全部未删除项；取消不删除，确认调用父任务 batch-delete 且不包含父任务/已删除项。
- [ ] 页面测试：刷新全量清单失败不出现确认框、不发删除；空列表按钮禁用。
- [ ] 分片测试：1001 项为 1000+1，部分网络失败后重试保留请求 ID、清单和原版本，已完成片不重提；关闭后停止下一片，未返回项为待确认。
- [ ] 运行 `pnpm --dir console exec vitest run src/pages/Control/SchedulerCronJobs/ManagedChildrenModal.test.tsx src/pages/Control/SchedulerCronJobs/childrenDeletion.test.ts`，先看到缺按钮/缺功能的预期失败。

### Task 2: 最小实现

**Files:** Modify console/src/pages/Control/SchedulerCronJobs/ManagedChildrenModal.tsx；Create console/src/pages/Control/SchedulerCronJobs/childrenDeletion.ts。

- [ ] childrenDeletion 导出固定清单计划、顺序提交与已知结果汇总函数，单个请求调用原 schedulerCronApi.childrenDelete。
- [ ] 让原选择删除和新增全量删除共用计划与续办，普通执行选中逻辑不变；新增按钮危险样式及明确的确认范围/数量。
- [ ] 全量读取、确认和提交均检查当前弹窗存活；确认框随弹窗销毁；写入互斥防止重复点击开启两轮。
- [ ] 沿用原逐项表和统计展示，只有 completed 后可结束操作、重新选择范围。

### Task 3: 验证和交付

- [ ] 运行新版页面测试目录、typecheck、定向 Prettier/ESLint、git diff --check。
- [ ] 用本地 Vite 与浏览器拦截 API 的测试数据验证按钮位置、确认数量和取消；只对测试数据验证删除，不调用真实后台。
- [ ] 主线程逐行自查确认固定清单、1000 限制和失败恢复，报告验证与未覆盖的真实部署联调，不创建提交或包。

# 新版定时任务筛选按钮位置调整

目标：把“包含已删除任务（查看清理进度）”放到 PageHeader 右侧，位于刷新左侧，采用现有 Button 样式及 aria-pressed 选中状态；保持旧页、公共页头和 include_deleted 请求语义不变。

按用户约定主线程顺序执行 TDD 和自查，不启动子代理，不提交，不更新已有行内包。沿用 Ant Design/AgentScope 现有字体、颜色、按钮高度和间距，不新增动画或设计体系。

GitNexus impact 已执行，但索引未识别 SchedulerCronWorkspace，结果 UNKNOWN。源码引用检查确认此内部组件仅被本文件 SchedulerCronJobsPage 渲染；修改仅限本页 JSX 与对应测试，不修改公共组件。

## 执行

1. 修改 console/src/pages/Control/SchedulerCronJobs/index.test.tsx：断言筛选控件与刷新、创建位于同一页头中，能作为按钮用键盘聚焦，aria-pressed 默认 false，点击开启/关闭后请求分别使用 include_deleted=true/false。
2. 运行 `pnpm --dir console exec vitest run src/pages/Control/SchedulerCronJobs/index.test.tsx`，预期因旧控件是页头外的 Switch 而失败。
3. 修改 console/src/pages/Control/SchedulerCronJobs/index.tsx：移除 Switch 导入和页头下独立 Space，在 PageHeader.extra 的 Space 首位使用标准 Button，保留筛选文案；type 随开关变为 primary/default，aria-pressed 绑定 includeDeleted，使用函数式 setIncludeDeleted 切换。
4. 重跑页面及 useManagedCronJobs 测试，运行前端 typecheck、定向 prettier 和 eslint；检查 diff 未涉及后台或旧页。
5. 尝试启动本地 Vite，用本地浏览器与测试数据检查右侧按钮高度、间距和切换，保存截图。若浏览器不可用，明确说明未做视觉验证，不把 jsdom 当作截图证据。

验收：桌面宽度下三按钮同排、默认筛选按钮与刷新同款、选中状态可见、筛选行为保持；不需要后端部署或配置变更。

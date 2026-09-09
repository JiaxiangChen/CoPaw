# 分行维度 Excel 导出接口

定时任务详情的「分行维度」按钮保持现有样式。前端传递点击时的日期、分行和排序；后端查询数据并生成完整 Excel，前端只下载二进制响应，不发送表格数据、不生成工作簿。

## 请求

`GET /api/monitor/cron/export-branch-dimension`

| Query 参数 | 必填 | 格式与语义                                      |
| ---------- | ---- | ----------------------------------------------- |
| start_date | 是   | YYYY-MM-DD，含当天                              |
| end_date   | 是   | YYYY-MM-DD，含当天，不早于 start_date           |
| bbk_ids    | 否   | 逗号分隔分行 ID；省略表示当前身份可见的全部分行 |
| sort_by    | 否   | 下列指标字段白名单；必须与 sort_order 成对提供  |
| sort_order | 否   | asc 或 desc；均省略时使用分行维度查询默认顺序   |

示例：`?start_date=2026-09-01&end_date=2026-09-09&bbk_ids=200%2C300&sort_by=skillCount&sort_order=desc`

排序字段：skillCount、totalTasks、successCount、readTasks、involvedManagers、resultViewManagers、resultViewManagerRate、planManagers、planManagerRate、insightManagers、insightManagerRate、phoneManagers、phoneManagerRate、recommendedCustomers、viewedCustomers、viewedCustomerRate、insightCustomers、phoneCustomers、contactedCustomers、contactRate。

排序按数值比较，同值保留原顺序；比例与页面两位小数展示后的数值一致。序号按结果从 1 开始，导出全部符合条件的数据，不分页。后端重新查询可能包含页面加载后产生的数据更新，不保证与旧页面快照同一时点。

请求通过现有 `buildAuthHeaders()` 携带认证、用户、租户、来源、分行等上下文。根据用户在 claw监控任务中的确认，本次后端沿用现有 `X-Source-Id` 和 `bbk_ids` 查询过滤，不新增授权机制；查询过滤不等同于权限校验。

## 响应

成功：HTTP 200，原始 `.xlsx` 二进制，不能返回 JSON、base64 或下载 URL。

```http
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename*=UTF-8''<URL编码的文件名>
```

前端使用 `response.blob()` 下载，文件名为 `定时任务分行维度_YYYYMMDD_HHmmss.xlsx`。无数据返回含表头的合法工作簿。

错误：400/422 参数错误、401/403 无权限、500 服务端错误；响应为 JSON，例如 `{"detail":"无权导出该分行"}`。不要使用 HTTP 200 返回错误内容。

## 工作簿内容

- 22 列及两层表头对应 `src/pages/Analytics/CronJobOverview/index.tsx` 的 `RankingTable`，格式/计算口径对应 `src/api/modules/monitor.ts` 的 `mapCronJobOverviewPageData`。
- A/B 两列纵向合并两行：序号（空白表头）、分行名称；C-F 合并「任务信息」；G-O 合并「by客户经理」；P-V 合并「by客户」。
- 计数保留千分位，百分比两位小数，零分母显示 0.00%；名称依次回退分行 ID、`-`。文本不能解释为 Excel 公式。
- 保留现行口径：insightManagerRate、phoneManagerRate 的分母为 plan_managers；contactRate 为 contact_rate × 100%。

## 对接与验证

接口需求已发送至 claw监控项目，监控项目已完成路由挂载。前端适配器为 `monitorApi.exportBranchDimension`，页面入口为 `handleBranchExport`。部署包含新路由的 Monitor 服务后可联调下载。

运行 `pnpm exec vitest run src/pages/Analytics/CronJobOverview src/api/modules/monitor.branchExport.test.ts src/api/modules/monitor.test.ts` 验证前端契约。

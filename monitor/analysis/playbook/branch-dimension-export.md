# 分行维度 Excel 导出

## 接口契约

目标路径：`GET /api/monitor/cron/export-branch-dimension`。

- `start_date`、`end_date`：必填有效 `YYYY-MM-DD`，包含起止日，开始不能晚于结束。
- `bbk_ids`：可选、逗号分隔的分行 ID；仅为数据筛选，不能代表授权范围。
- `sort_by`、`sort_order`：同时省略或同时提供。方向 `asc|desc`；字段白名单见 `services/cron/branch_export.py` 的 `BRANCH_COLUMNS`。
- 不接收前端行数据，不分页；复用 `QueryService.get_branch_behavior`，保留其金葵花名单、统计技能、分行及日期口径。
- 成功返回原始 XLSX；MIME 为 `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`，文件名为 UTF-8 Content-Disposition attachment。
- 日期/排序/空分行参数错误返回 400，导出异常返回 500，错误体为中文字符串 `detail`。

## 展示与统计

22 列，两层表头与 Console RankingTable 一致，首列空白表头、数据为排序后序号。A/B 纵向合并，C-F 任务信息、G-O by客户经理、P-V by客户。计数使用数值单元格和 `#,##0` 格式，比例使用 `0.00%`，分行名强制文本类型。无数据仍输出合法表头工作簿。

排序是白名单字段映射后的内存数值排序，不拼 SQL。比例先转换为与页面 `toFixed(2)` 一致的百分点再排序；同值保持查询返回顺序；无排序时保持原始顺序。

- 结果查看率 = result_view_managers / involved_managers。
- 方案用户比例 = plan_managers / result_view_managers。
- 洞察、电访用户比例的分母均为 plan_managers，保持页面现有口径。
- 客户查看率 = viewed_customers / recommended_customers。
- 接触客户率 = contact_rate × 100%。
- 分母为零返回 0.00%。

## 接入状态

已挂载到 `api_router`。用户于 2026-09-09 明确本次“只需沿用现有查询过滤”：来源读取 `X-Source-Id`，缺省 `default`；分行读取 `bbk_ids`，省略沿用原查询的全部分行范围。本次未新增身份鉴权或分行授权机制，不将客户端角色或分行头认定为可信权限凭证。

## 验证入口

`venv/Scripts/python.exe -m pytest tests/test_branch_export.py tests/test_branch_export_api.py -q`

覆盖空工作簿、22 列与合并表头、字段与比例、稳定数值排序、百分比显示精度、全量行、公式文本安全，以及正式 API 路由挂载、二进制 HTTP 响应、筛选传递和错误响应。

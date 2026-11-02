# HTML 预览事件维度设计

## 目标

在保留原 HTML 按钮点击统计的基础上，让同一张事件明细表能够记录：

- 主方案弹窗查看；
- 二次弹窗中的子方案查看；
- 原有按钮点击；
- 二次弹窗内具体模块曝光。

每次行为仍新增一条 `swe_html_preview_click_events` 记录，不更新模板、
名单快照或之前的事件。

## 字段约定

| 字段 | 说明 |
|------|------|
| `event_type` | `button_click`、`preview_view`、`module_exposure` |
| `template_type` | 当前事件关联模板的类型：`main` 主模板、`sub` 子模板 |
| `template_id/result_id` | 当前事件实际关联的模板记录及其生成结果；不重复记录入口主模板 |
| `event_target_id/name` | 模块等模板内具体曝光对象的稳定标识与展示名称 |
| `trace_id` | 方案生成与浏览链路标识；同一链路的事件应传相同值 |

原有 `button_id/name/text/type` 只描述按钮，不复用为模板或模块标识。
原有 `file_url/name`、`list_key/name`、客户、任务、用户及机构字段继续承担
HTML 和业务上下文关联，但可视化查询不依赖解析 URL 获得模板 ID。

`template_id` 是对模板记录的逻辑引用，不建立数据库外键。即使模板记录
被删除，历史事件仍应保留；`file_name` 等字段同时作为事件发生时的名称快照。

为兼容现有表结构和查询，`clicked_at` 继续作为所有类型事件的发生时间；
上报方可显式传入，未传时由后端使用当前时间。非 `button_click` 事件不做
按钮分类，`button_type` 保持为空。

## 兼容策略

- `event_type` 为 `NOT NULL DEFAULT 'button_click'`，旧记录和旧客户端无需回填。
- `template_type`、`template_id` 和 `result_id` 均允许为空，旧按钮点击继续兼容；
  新的页面查看和模块曝光事件必须同时传这三个字段。按钮点击一旦传模板与结果，
  也必须同时传 `template_type`。
- 写接口只接受三个受控事件类型，避免看板出现拼写不同的同义事件。
- 原按钮汇总、名单汇总和客户汇总显式过滤 `event_type='button_click'`。
- `GET /api/html-preview/events` 支持 `event_type` 查询参数并返回新增字段；
  未传参数时默认查询 `button_click`，保持旧点击明细接口语义。
- 明细接口传 `event_type=all` 时查询全部事件类型；可按模板类型、模板、结果、
  模块和链路字段筛选，并通过 `limit/offset` 分批读取。

## 工作台无文件点击的兼容补充（2026-10-29）

点击事件的 `file_url` 允许省略或传空字符串，省略时默认保存 `""`，
不接受 `null`。沿用数据库 `NOT NULL` 列，无需迁移历史数据。
工作台洞察 / 电访不关联文件，不使用页面地址冒充文件 URL。

无文件地址且无 `list_key` 的点击仍参与事件明细、按钮汇总和客户统计，
但从 HTML 名单汇总及名单客户并集查询中排除，避免生成“未知名单”。
有真实文件 URL 或显式名单标识的事件保持原有名单分组规则。
名单快照接口继续要求非空 `file_url`；模板关联校验保持原规则。
发布时先部署兼容省略字段的后端，再发布不传该字段的工作台前端。

## 上报示例

模块曝光：

```json
{
  "file_url": "https://example.com/plan.html",
  "event_type": "module_exposure",
  "template_type": "sub",
  "template_id": 12,
  "result_id": "result-sub",
  "event_target_id": "module-customer-profile",
  "event_target_name": "客户核心信息",
  "trace_id": "trace-001"
}
```

主方案和子方案查看都使用 `preview_view`，分别通过 `template_type=main` 和
`template_type=sub` 区分，`template_id/result_id` 始终填写本条事件实际关联的
模板和结果。模块曝光按当前业务只由子模板上报，但后端不额外限制其
`template_type`。按钮点击继续兼容不传模板字段。

## 迁移与发布顺序

1. 先且仅执行一次 `scripts/sql/html_preview_event_dimensions_migration.sql`
   扩展表结构。
2. 再部署包含新字段写入逻辑的后端。
3. 最后逐步接入主方案、子方案和模块曝光上报方。

新增字段均有默认值或允许为空，旧版本前端与新版本后端可以并行运行。
现有表数据量较大时，应先在对应 MySQL 版本验证在线建索引能力，并选择低峰期
执行迁移。

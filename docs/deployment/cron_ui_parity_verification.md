# 新版定时任务管理页补齐与验证

日期：2026-10-17。工作区：D:/Projects/CoPaw，分支：v1.0.0。

## 本轮内容

- 子任务支持姓名或 UID 搜索、机构筛选，筛选基于全部游标页结果，不只当前表格页；两个条件取交集，变更时清空选中并回第一页。
- 展示姓名及 UID、机构、子任务 ID、时区、最近执行状态；不同 UID 同名时提示，同一 UID 多个 Agent 不重复算人。缺失资料仍展示 UID，其他缺失项显示短横线。
- 沿用原子任务弹窗样式、1280 宽度及每页 8 条，保留新版编辑、启停、进度与批量能力；原删除请求待续办时禁用筛选，避免改变其操作范围。
- 主列表立即执行复用旧版二次确认文案，取消不请求，确认才按原 Scheduler 协议提交。
- Scheduler 子任务轻量列表补充已有定义中的 tenant_name、bbk_id，只读追加字段，不加表、不修改写入、不逐个查询任务正文。

本轮不修改旧页面、旧 API、智拓或批调度看板，不处理此前暂缓的认证、时区、Cron 日期/星期及 SWE 清理问题。没有提交、推送、应用 DDL 或连接真实业务库/调度平台。

## 验证结果

1. 新版管理页目录 Vitest：9 个文件、26 项通过。覆盖跨后端分页搜索、机构组合筛选、搜索和机构变化清空隐藏选择、分页复位、同名去重、取消/确认及新版请求版本。
2. 后端 test_managed_cron_queries.py：34 项通过；使用 SQLite 与 ASGI，新增用例验证资料字段、缺失字段及不暴露正文。
3. TypeScript、定向 ESLint、生产构建通过。两份本轮 Python 文件的 pre-commit 检查全部通过，包含复杂度上限 15、mypy、black、flake8、pylint。未运行整个仓库的全量 lint。
4. 新旧管理页整体回归首次：16 个文件，95 项通过、2 项失败。旧子任务筛选用例为 5 秒超时，使用 20 秒上限重跑旧弹窗与旧列测试后，19 项通过、1 项失败。剩余失败是旧 columns.test.tsx 的 restores the cron text when batch dispatch metadata is removed；单独执行仍失败，旧列实现与 HEAD 一致，原代码 const batchDispatch = (record) 导致对象恒为真。本轮未改旧代码，不将回归报告为全绿。
5. 真实 Chromium 页面使用明确 HTTP 替身验证：12 个子任务分两次游标请求读完，跨页 UID 搜索、姓名与机构组合筛选正常；取消立即执行时 /run 请求为 0，确认后恰好为 1。不是 SWE/平台/业务数据库联调。

测试中仍有 jsdom 伪元素 getComputedStyle 提示；构建有大分包提示；浏览器有既有组件 forwardRef、弃用属性及 SSR 伪类告警。本轮没有扩展修改公共组件。

## 浏览器产物

- 执行脚本：output/playwright/cron-ui-parity-fixture.js。
- 完整子任务展示：output/playwright/cron-ui-children.png。
- 姓名与机构筛选：output/playwright/cron-ui-filtered.png。
- 横向滚动后的时区及最近状态：output/playwright/cron-ui-runtime-columns.png。
- 立即执行确认：output/playwright/cron-ui-run-confirm.png。

浏览器截图已目视核对。按用户约定未启动子代理，采用主线程逐项自查，不冒称独立评审。GitNexus 对 child_item 的影响评级为 LOW；新增前端符号未入索引为 UNKNOWN，已以调用方、单测和浏览器交互验证补充，未将 UNKNOWN 当作 LOW。

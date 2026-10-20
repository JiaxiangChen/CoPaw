# 新版定时任务编辑时间兼容

用户已确认：只修新版页面。旧版页面、共享 CronJobs helper/表单/解析器以及后端均不改。

## 验收

- 单时间每日任务从hours[0]读取，兼容hour；0点、非整点、每周和每小时正确回显和提交。
- 多小时、月/年/分钟间隔及其他现有基础控件不能完整表达的排程，使用现有自定义Cron输入，保留原表达式。
- 未改变排程、仅改名称等其他字段时保留原Cron文本；明确修改时间或自定义表达式时提交新值。
- 不增加请求、组件样式或依赖，不修改旧页面行为。已有未提交内容保留，不自动提交/打包/推送。

## 实施

1. RED：ManagedJobEditor真实表单现有10:00测试失败；新增scheduleForm.test.ts覆盖以上转换和保存边界，并补真实多小时/间隔编辑框原样保存测试。
2. 新建仅在SchedulerCronJobs中使用的scheduleForm.ts，复用既有非排程字段转换，覆盖新版排程字段并在排程语义未变时保留原Cron文本；ManagedJobEditor改用专属适配函数。
3. GREEN：定向Vitest、旧表单helper回归、typecheck、ESLint/Prettier、生产构建；本地HTTP替身浏览器核对10:00和多小时自定义框并截图。

GitNexus：ManagedJobEditor未入当前索引，impact结果UNKNOWN，不解释为低风险；实际rg调用方及目录隔离作为补充。遵循用户AGENTS主线程顺序TDD/自查，不运行子代理；无Superpowers工具，使用本计划记录代替，不冒称独立评审。

命令：pnpm --dir console exec vitest run src/pages/Control/SchedulerCronJobs/ManagedJobEditor.test.tsx src/pages/Control/SchedulerCronJobs/scheduleForm.test.ts；pnpm --dir console run typecheck；pnpm --dir console run build。

## 实施与验证结果

- RED：真实10点编辑用例及3项复杂排程编辑用例失败；新helper测试在文件未实现时导入失败。
- GREEN：专项2个文件23项通过；扩大新版任务目录、旧表单helper/表单组件及旧Cron工具回归16个文件94项通过（--maxWorkers=1）。首次并行跑有3个默认5秒超时，单进程未放宽超时重跑全部通过；保留既有jsdom伪元素、i18next及Card弃用提示。
- TypeScript、定向ESLint/Prettier及生产构建通过；构建有既有大chunk和动态/静态导入混用警告。git diff --check通过，旧CronJobs目录及共享工具diff为空。
- Chromium HTTP替身确认10:00回显并提交0 10 * * *，多小时使用自定义框并提交0 9,10,11 * * *；截图为output/playwright/cron-editor-new-single-time.png和cron-editor-new-multi-time.png。无真实SWE/平台/数据库联调，浏览器有缺失后端与公共组件日志，不宣称整站控制台无错误。已关闭本次浏览器和开发服务。
- 生产代码只改新版ManagedJobEditor.tsx并新增scheduleForm.ts；无样式/依赖/API/后端/SQL变化。适配保留原React请求并行与控件样式。按用户要求仅主线程自查，未执行独立代理评审，未提交/推送/打包。

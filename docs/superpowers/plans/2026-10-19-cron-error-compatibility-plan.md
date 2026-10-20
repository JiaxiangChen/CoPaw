# 新版执行失败信息兼容旧版

用户明确要求所有任务类型保留旧版失败说明，而非仅公开 WorkflowCallError。
按用户 AGENTS 在主线程顺序执行，不启动子代理；当前无 Superpowers 工具，采用本地计划、TDD 与自查，不冒称独立评审。

## 边界和验收

- 旧版 CronManager._handle_execution_error 使用 str(error)[:200] 写历史。新版统一保留同一异常说明，继续脱敏令牌、授权头和 Cookie，最多 200 字符；不展开异常链，不增加异常类型前缀或 workflow 诊断后缀。
- 所有 text/agent/workflow 任务经 ManagedExecutionService 的失败都适用，历史 error_message 与状态 last_error 同源。run.error_code、失败分类和重试协议不变。
- 取消保留旧版 Job was cancelled；无文本错误仍回退机器码。旧历史不回填，不改旧接口或数据库结构。
- 原工作区修改保留，不在本轮自动提交、打包或推送。

## 顺序

1. RED：扩展 tests/unit/app/test_managed_cron_failure_detail.py，覆盖三种任务类型、子任务失败/超时、workflow 缺少用户信息/授权过期、普通配置异常、200 字符、令牌脱敏、取消和空超时；运行后确认因详情丢失而失败。
2. 修改 managed_failure.py 统一摘要，managed_execution.py 让取消也保留说明；已存在的 history/state 映射不重复开发。GitNexus 新增 helper 无索引，现有 _failure_outcome 上游影响 LOW（1 个直接调用方、2 个影响符号、0 流程）；辅以 rg 核对实际调用。
3. GREEN：运行专项与 Scheduler/SWE 执行、引擎、HTTP 链回归，再运行定向 pre-commit 和 git diff --check。人工核对机器码、元数据、事务与旧接口未改变。更新部署验证说明。

测试命令（PowerShell，项目 .venv）：

```powershell
$env:PYTHONPATH='src;scheduler/src'
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe -m pytest tests/unit/app/test_managed_cron_failure_detail.py -q --tb=short
.venv/Scripts/python.exe -m pytest tests/unit/scheduler tests/unit/app/test_managed_cron_execution.py tests/unit/app/test_managed_cron_failure_detail.py tests/unit/app/test_managed_cron_engines.py tests/integrated/scheduler/test_managed_cron_http_chain.py -q --tb=short
```

## 本地验证记录

- RED：26失败、2通过，确认普通异常说明丢失、workflow诊断后缀不兼容、取消/超时说明丢失。随后修正text任务测试快照的必填正文，避免模型校验提前截断测试路径。
- GREEN：专项33项覆盖实际结果事务、三类任务和脱敏；后端扩大回归615通过、1跳过（Windows缺Linux fcntl）、1个既有Starlette/httpx弃用警告。
- 定向pre-commit全部通过，包含圈复杂度15、mypy、Black、Flake8、Pylint；首次Black调整格式后已重跑。
- 主线程自查：旧CronManager/Monitor同步链未改；无异常返回状态路径与旧版同样没有额外错误文案；机器码、重试分类和现有事务边界保持不变。仅有本地SQLite/HTTP替身验证，未操作真实业务库、外部workflow或预发布平台。未运行独立子代理评审。
- 部署本次兼容修复只需更新SWE；已有Console错误hover继续复用state.last_error。无新SQL、配置，不回填旧历史已丢失的信息；未提交、未推送、未打包。

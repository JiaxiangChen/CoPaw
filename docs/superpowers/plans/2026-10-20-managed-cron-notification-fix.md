# 新版任务完成通知修复

## 范围与依据

用户确认只恢复通知能力，不重置、不补发已有失败通知；现有重试配置不改。
`CONTEXT.md` 已以管理归属定义新旧任务，不按创建日期、ID 或有无导入快照判断。
新版历史 `meta.management_owner=scheduler`，携带 managed_execution_id/source_id/agent_id；旧版保持原 jobs.json 查找。

## 实施步骤

1. 新增通知回归：数据库有新版冻结执行、jobs.json 不存在仍应发送；Agent/workflow、非 default Agent、旧任务及错误隔离均覆盖。
   - 命令：`wsl -e sh -lc 'cd /mnt/d/Projects/CoPaw && venv/bin/python -m pytest tests/unit/app/test_managed_cron_notifications.py -q'`。
   - 先验证失败来自缺少新版数据库通知支持，而不是依赖/收集错误。
2. SWE 通知 worker 按历史归属分流，用现有共库只读查询绑定数字历史 ID 的冻结执行快照。
   - 完整核对 tenant/source/Agent/job、管理归属及未删除定义；缺数据或 DB 故障不回退 jobs.json。
   - 新版按真实 Agent 获取工作区，传入已核实 spec 复用发送方法；旧版保留原方法调用和 default Agent。
   - 启动时注入已有数据库连接，不增加配置、表或外部接口。
3. 回归及主线程自查。
   - 运行新增通知、原通知 worker、Monitor 领取/回写、已有 managed 执行结果与引擎用例。
   - 检查新旧路由隔离、冻结快照而非最新定义、同名任务/错误身份不误发、DB 故障不误标成功。
   - 检查复杂度不超过 15、格式、差异范围；不重置既有 failed 记录、不提交、不打包或部署。

## 预计文件

- `src/swe/app/crons/notification_worker.py`：归属分流及通知上下文。
- `src/swe/app/crons/managed_notifications.py`：新版冻结快照只读核对。
- `src/swe/app/crons/manager.py`：支持已核实 spec，默认仍从旧仓库获取。
- `src/swe/app/_app.py`：注入共用数据库连接。
- `tests/unit/app/test_managed_cron_notifications.py`：新增回归。
- `tests/unit/app/test_cron_notification_worker.py`：仅在日志断言用例内接入根日志捕获，生产日志不变。
- `analysis/playbook/common-errors.md`：补充报错定位与禁止回退规则。

## 验收与交付

新版无 jobs.json 仍通知成功；旧任务不访问新版数据库；新版数据异常不误发旧任务。
不改变通知时间、渠道、文案、结果写入或历史失败状态；不触发重新执行。
当前技能环境无 Superpowers，按用户 AGENTS 约定主线程串行执行 TDD 与自查，不冒充独立多轮评审。

## 本地验证结果

- 初始回归为红：通知 worker 不支持新版数据库读取，新增用例失败；接入后验证转绿。
- WSL 项目 venv：新增通知、原通知 worker、managed 引擎及失败详情、执行结果、应用生命周期、Monitor 通知服务共 90 项通过。
- Windows 项目 .venv：新增通知、原 worker、Monitor 通知服务、执行结果共 45 项通过。生命周期测试导入需要 Linux fcntl，已在上述 WSL 测试中验证，未伪造 Windows 兼容。
- Black（py312、79 列）、本地复杂度检查（变更函数不超过 15）和 git diff --check 通过。
- 新增回归包含真实 SQLite SQL 及真实通知组装、mock 通道发送；不等同于真实 MySQL 或行内招乎/W+ 实发联调。
- 主线程核对了新旧归属隔离、完整身份、历史 ID 绑定、冻结快照、删除门控及无历史重置；没有独立多代理评审。
- 业务改动仅 SWE，无表结构、配置、调度时间、通知文案或历史重试策略变化；未提交、推送、打包或部署。

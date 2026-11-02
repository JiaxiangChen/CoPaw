# 定时执行凭据数据库镜像运维

本阶段 cron_auth.json 仍是业务读取依据。文件成功保存/删除后尝试完整快照镜像；数据库失败不改变已成功文件操作。没有自动重试、定期对账、自动修复、读库切换或自动回退。

## 部署

由运维手工执行 scripts/sql/cron_auth_states.sql，脚本可重复运行；应用不执行 DDL。既有 swe_async_tasks、swe_async_task_items 必须已部署，所需列经零行 SELECT 检查后才接受任务。使用应用已有连接池。业务表含完整 user_info JSON 文本、Token 和 Cookie，按生产凭据要求限制权限并保护备份。单元测试使用 SQL double，不证明生产 MySQL 兼容性，部署验收另行完成。

## API 与认证

提交：POST /api/internal/cron/auth-migration/tasks，202 返回 task_id。查询：GET /api/internal/cron/auth-migration/tasks/{task_id}?after_page=0&limit=10。

保留全局登录中间件，没有匿名白名单。配置 SWE_INTERNAL_TOKEN 时沿用现有内部校验；登录 JWT 与内部凭据共存时发送以下头：

    Authorization: Bearer <login-jwt>
    X-Internal-Token: Bearer <SWE_INTERNAL_TOKEN>
    Content-Type: application/json

X-Internal-Token 优先于 Authorization。无需登录 JWT 的部署也可通过 Authorization 传内部 Bearer Token。

四种请求示例：

    {"operation":"preview","tenant_ids":["alice"],"source_ids":["RMASSIST"],"page_size":100}

    {"operation":"import","storage_scope_ids":["default","default_RMASSIST"],"page_size":100}

    {"operation":"verify","source_ids":["RMASSIST"],"page_size":100}

    {"operation":"prune","storage_scope_ids":["default_RMASSIST"],"page_size":100}

筛选维度取交集，数组非空、唯一、最多 200 项；page_size 为 1–200，默认 100。不提供筛选时 preview/import/verify 扫描本实例可见范围；prune 必须显式指定至少一个范围。非法请求 422，数据库或进度表不可用 503，本地容量满 429，任务不存在 404；响应均使用安全错误码，不回显输入。

## 行为

- preview：would_insert、would_update、unchanged、database_only，只读。
- import：每个当前有效文件完整 upsert，包括字段清空；可重复执行，可修复损坏 DB 快照，不删除 DB-only。
- verify：missing、different、unchanged、database_only；日期归一 UTC，user_info 按 JSON 值比较，仅返回差异字段名。
- prune：只扫描选定数据库范围；每次删除前重检当前文件仍缺失；存在文件则 kept。

根目录使用 config.utils.WORKING_DIR，直接访问 <root>/<exactScope>/.secret/cron_auth.json。不初始化 Workspace，不刷新凭据，不写源文件，不规范化物理目录。canonical、scope.v1. legacy、default、default_<source> 保留不同键且区分大小写。身份无法可靠解码时标记 unknown_metadata，仍可按 storage_scope_ids 精确操作。

只读最多 8 MiB。非对象 JSON、模型无效、读取错误、符号链接或不安全路径都报告 invalid_source，不按空状态导入，也不授权删除。根目录缺失/不可读使任务失败。根目录可访问但作用域或 .secret 目录缺失时可以认定文件缺失。一个可读取的空挂载点无法单靠文件系统检查和合法空目录区分，运维须在 prune 前确认挂载完整性。

响应、持久进度和迁移日志不包含 Token、Cookie、user_info、last_error 的值或 SQL 错误。局部无效数据报告后继续；数据库不可用导致整个任务失败。文件和数据库两个阶段可能重复报告同一无效源，counts 统计诊断出现次数而非去重作用域数。

## 进度和中断

独立 task_type=cron.auth.migration、service=swe。一次提交自动分批扫描文件及数据库。page:000000000001 等有界诊断页与累计 counts、pages_completed、scan_complete 在同一事务写入。GET 的 pages 是诊断页，按 next_after_page 继续，limit 为 1–20；GET 不执行扫描或接管工作。

状态：queued、running、succeeded、partial_failed、failed、interrupted。局部无效和差异需结合 counts 与诊断判断。每实例最多两个任务，在异步创建前预留容量；心跳 15 秒，单页超时 300 秒；queued/running 超过 120 秒无更新时 GET 只显示 interrupted，不持久化更改或自动恢复。

关闭时先取消并等待本实例任务再关闭数据库，尽力在有界等待内保存 interrupted。数据库同时不可用则等待最后心跳过期。中断后运维检查已完成页并手动重新提交；无自动重试、恢复或跨实例接管。

扫描按 UTF-8 字节键序分页，内存仅保留有界页面。文件扫描期间可变化，新增到已扫描键之前的目录可能本轮未见。没有锁或版本协议，不保证一致快照、严格顺序或原子清理；prune 重检和删除之间仍有并发窗口。切换前由运维安排最终核对、差异处理及必要写入静默窗口，本阶段不提供切换接口。

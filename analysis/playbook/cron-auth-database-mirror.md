# 定时执行凭据镜像排查

- 在线镜像入口 src/swe/app/crons/auth_mirror.py；严格完整快照仓库 auth_repository.py。数据库失败只影响镜像，业务继续读文件。
- CLI 刷新仅在 SWE_DB_HOST、SWE_DB_PORT、SWE_DB_USER、SWE_DB_ACCESS、SWE_DB_NAME 至少一项非空时尝试限时连接，支持显式 localhost、IPv4/IPv6 回环地址，也支持省略主机的本地配置；全未配置或空值时直接跳过，无连接告警或超时等待。连接失败或超时不阻止文件更新。
- 内部运维入口 auth_migration_api.py，任务 auth_migration_tasks.py，进度 auth_migration_progress.py，扫描 auth_migration_source.py，操作 auth_migration_service.py。
- 独立任务类型 cron.auth.migration，service=swe，复用既有异步进度表，不复用任务定义迁移的业务 Store。
- [部署与四种操作](../../docs/deploy/cron-auth-database-mirror.md)：先手工 SQL，确认实例挂载及权限，小范围 preview → import → verify，再显式范围 prune。
- invalid_source、不可读、符号链接不能解释为缺失；可读空挂载点需要运维判断。unknown_metadata 用实际 storage_scope_ids 筛选，不猜测逻辑身份。
- interrupted 为只读过期心跳标记，不能自动恢复。检查已完成页后重新提交；无锁扫描不保证一致快照，切换前运维负责最终核对和必要静默窗口。
- 测试 tests/unit/app/test_cron_auth_migration*.py 使用 SQL double，不等价生产 MySQL 验收。

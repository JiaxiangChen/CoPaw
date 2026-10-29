-- ============================================================
-- swe_tenant_init_source 表字段扩展
-- 新增 bbk_id（所属机构ID）和 tenant_name（用户名称）
-- 用于应用市场按机构维度分发技能时展开用户列表
-- ============================================================

ALTER TABLE swe_tenant_init_source
    ADD COLUMN bbk_id      VARCHAR(64)  DEFAULT NULL COMMENT '所属机构ID' AFTER source_id,
    ADD COLUMN tenant_name VARCHAR(256) DEFAULT NULL COMMENT '用户名称'   AFTER bbk_id;

-- 为 bbk_id 新增索引，支持按机构查询用户列表
-- 随后执行 migrate_tenant_init_source_source_bbk_index.sql，补充来源与机构联合索引。
ALTER TABLE swe_tenant_init_source
    ADD INDEX idx_bbk_id (bbk_id);

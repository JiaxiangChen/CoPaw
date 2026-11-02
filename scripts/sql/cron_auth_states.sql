-- Execute manually during deployment; the application never creates this table.
-- All DATETIME values are UTC. Credentials require restricted database access.
CREATE TABLE IF NOT EXISTS swe_cron_auth_states (
    storage_scope_id VARBINARY(1024) NOT NULL COMMENT 'Exact scope directory identity',
    tenant_id VARCHAR(255) DEFAULT NULL COMMENT 'Decoded tenant metadata, when known',
    source_id VARCHAR(255) DEFAULT NULL COMMENT 'Decoded source metadata, when known',
    user_info LONGTEXT NOT NULL COMMENT 'Complete JSON user_info snapshot',
    user_info_expires_at DATETIME(6) DEFAULT NULL,
    user_info_refreshed_at DATETIME(6) DEFAULT NULL,
    auth_token LONGTEXT DEFAULT NULL,
    auth_token_expires_at DATETIME(6) DEFAULT NULL,
    cookie_header LONGTEXT DEFAULT NULL,
    last_prefetch_at DATETIME(6) DEFAULT NULL,
    last_error LONGTEXT DEFAULT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (storage_scope_id),
    INDEX idx_cron_auth_tenant (tenant_id),
    INDEX idx_cron_auth_source (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='File-authoritative cron authorization snapshots';

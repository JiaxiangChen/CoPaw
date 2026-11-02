-- DESIGN DRAFT / NOT EXECUTED, updated 2026-10-30.
-- Spec: docs/superpowers/specs/2026-10-29-db-nas-conversation-storage-design.md
-- Six NEW tables. Existing swe_chat_shares uses the separate extension script.
-- Proposed target: MySQL 8.0.21+, InnoDB DYNAMIC, page size >= 16 KiB, strict mode.
-- Confirm actual MySQL/TDSQL compatibility; no automatic startup DDL.
-- Explicitly select an EMPTY review schema. Existing tables require inspected migrations.
-- DDL implicitly commits. There is deliberately no IF NOT EXISTS or DROP.
-- All times UTC DATETIME(6). Identities are exact UTF-8 bytes, never hashes.
-- TEXT fields contain application-validated serialized metadata, never SQL JSON types.
-- Bodies, tool arguments/results, summaries and recovery payloads remain on NAS.

CREATE TABLE swe_conversation_sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部自增主键',
    tenant_key VARBINARY(256) NOT NULL COMMENT '租户标识原值，按UTF-8字节精确匹配',
    source_key VARBINARY(256) NOT NULL COMMENT '来源标识原值，按UTF-8字节精确匹配',
    agent_key VARBINARY(256) NOT NULL COMMENT 'Agent标识原值，按UTF-8字节精确匹配',
    user_key VARBINARY(256) NOT NULL COMMENT '用户标识原值，按字节精确匹配',
    channel_key VARBINARY(64) NOT NULL COMMENT '渠道标识原值，按字节精确匹配',
    external_session_key VARBINARY(1024) NOT NULL COMMENT '外部逻辑Session标识，与完整scope及user和channel联合唯一',
    generation BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Session执行权代次，单调递增以拒绝过期writer',
    owner_key VARBINARY(128) NULL COMMENT '执行或发布owner的进程实例标识',
    lease_until DATETIME(6) NULL COMMENT '执行租约截止UTC时间；使用数据库时间校验，不代表可自动续跑',
    active_turn_id BIGINT UNSIGNED NULL COMMENT '当前占用Session的Turn内部ID，包含准入预留和待结算阶段',
    draft_chat_id BIGINT UNSIGNED NULL COMMENT '专家草稿重试固定的Chat内部ID；失效映射不可自动复活',
    state_revision BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Session当前状态CAS版本号',
    history_high BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '共享在线消息已分配的session_order上界，跨epoch不复用',
    context_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Session共享上下文重置代次，不等于Chat的checkpoint epoch',
    context_usage_text TEXT NULL COMMENT '应用校验的数字用量快照序列化文本；NULL表示无可用快照',
    context_usage_invalid TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '用量快照失效标记：0有效，1失效；与恢复状态同事务更新',
    recovery_publication_id BIGINT UNSIGNED NULL COMMENT '当前已提交恢复根所属publication内部ID',
    recovery_offset BIGINT UNSIGNED NULL COMMENT '恢复根在NAS bundle中的字节偏移',
    recovery_length BIGINT UNSIGNED NULL COMMENT '恢复根的字节长度',
    created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC，微秒精度',
    updated_at DATETIME(6) NOT NULL COMMENT '最近更新的UTC时间，微秒精度',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_session_identity
        (tenant_key, source_key, agent_key, user_key, channel_key, external_session_key),
    KEY ix_conv_session_lease (lease_until, active_turn_id),
    KEY ix_conv_session_lookup
        (tenant_key, source_key, agent_key, channel_key, external_session_key, user_key),
    KEY ix_conv_session_recovery (recovery_publication_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='逻辑会话身份、执行权威及共享在线状态，完整scope内唯一';

CREATE TABLE swe_conversation_chats (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部自增主键',
    session_id BIGINT UNSIGNED NOT NULL COMMENT '关联逻辑Session内部ID，由应用事务校验关联和作用域',
    external_chat_key VARBINARY(128) NOT NULL COMMENT '保留现有外部Chat UUID，不替换为数据库整数ID',
    registry_order BIGINT UNSIGNED NOT NULL COMMENT '原registry位置；新增记录递增分配，兼容相同更新时间的选择顺序',
    tenant_key VARBINARY(256) NOT NULL COMMENT '租户标识原值，按UTF-8字节精确匹配',
    source_key VARBINARY(256) NOT NULL COMMENT 'Chat所属来源标识，与关联Session同事务维护',
    agent_key VARBINARY(256) NOT NULL COMMENT 'Chat所属Agent标识，与关联Session同事务维护',
    user_key VARBINARY(256) NOT NULL COMMENT '用户标识原值，按字节精确匹配',
    channel_key VARBINARY(64) NOT NULL COMMENT '渠道标识原值，按字节精确匹配',
    title TEXT NOT NULL COMMENT '会话标题，保留完整显示文本',
    metadata_text MEDIUMTEXT NOT NULL COMMENT '白名单轻量元数据序列化文本，由应用校验，不存正文或指令',
    metadata_revision BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '标题及普通元数据CAS版本，与历史提交版本独立',
    expert_binding_revision BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '服务端专家绑定CAS版本，绑定和解绑均递增',
    expert_id VARBINARY(256) NULL COMMENT '绑定的专家定义精确ID；NULL表示当前未绑定',
    content_meta_publication_id BIGINT UNSIGNED NULL COMMENT '含场景指令等内容型meta的NAS publication内部ID',
    content_meta_offset BIGINT UNSIGNED NULL COMMENT '内容型meta在NAS bundle中的字节偏移',
    content_meta_length BIGINT UNSIGNED NULL COMMENT '内容型meta的字节长度',
    history_revision BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Chat历史和归档发布CAS版本，与标题元数据版本独立',
    history_high BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Chat独立归档的archive_order上界，不是共享在线历史快照',
    next_turn_order BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '该Chat下一次分配的Turn顺序号',
    epoch BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Chat的checkpoint epoch，与Session context_epoch区分',
    event_high BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '该Chat当前epoch中已提交连续事件序号上界',
    next_boundary_order BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '该Chat下一次分配的压缩边界顺序号',
    active_checkpoint_publication_id BIGINT UNSIGNED NULL COMMENT '当前已提交checkpoint所属publication内部ID',
    checkpoint_revision BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '当前checkpoint版本号，按Chat与epoch进行CAS',
    status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'idle' COMMENT '兼容Chat状态投影；执行准入以Session active Turn为权威',
    created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC，微秒精度',
    updated_at DATETIME(6) NOT NULL COMMENT '最近更新的UTC时间，微秒精度',
    deleted_at DATETIME(6) NULL COMMENT '逻辑删除UTC时间；NULL表示未删除，权限即时生效',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_chat_external (tenant_key, source_key, agent_key, external_chat_key),
    KEY ix_conv_chat_recent
        (tenant_key, source_key, agent_key, user_key, deleted_at, updated_at, external_chat_key),
    KEY ix_conv_chat_channel_recent
        (tenant_key, source_key, agent_key, user_key, channel_key, deleted_at, updated_at, external_chat_key),
    KEY ix_conv_chat_session (session_id, updated_at, registry_order),
    KEY ix_conv_chat_checkpoint (active_checkpoint_publication_id),
    KEY ix_conv_chat_content_meta (content_meta_publication_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='可显示Chat记录、专家绑定及独立归档和checkpoint元数据';

CREATE TABLE swe_conversation_turns (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部自增主键',
    chat_id BIGINT UNSIGNED NOT NULL COMMENT '关联Chat内部ID；历史归属保留，由应用校验可见性',
    session_id BIGINT UNSIGNED NOT NULL COMMENT '关联逻辑Session内部ID，由应用事务校验关联和作用域',
    question_msg_key VARBINARY(256) NOT NULL COMMENT '用户问题源Msg ID，在完整逻辑Session内唯一，用于幂等与查询',
    runtime_turn_key VARBINARY(128) NULL COMMENT '运行时turn-UUID，与用户问题Msg ID区分；旧数据可无此值',
    turn_order BIGINT UNSIGNED NOT NULL COMMENT 'Chat内问答顺序号，与Chat联合唯一',
    context_epoch BIGINT UNSIGNED NOT NULL COMMENT 'Session共享上下文重置代次，不等于Chat的checkpoint epoch',
    generation BIGINT UNSIGNED NOT NULL COMMENT 'Session执行权代次，单调递增以拒绝过期writer',
    state VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '内部执行状态：reserved/admitted/running/stopping/settling及终态',
    history_status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '现有API状态，如completed/cancelled/stopped/failed；旧结果未知时NULL',
    admission_publication_id BIGINT UNSIGNED NULL COMMENT '已持久化用户准入对应的publication内部ID',
    terminal_publication_id BIGINT UNSIGNED NULL COMMENT '已提交问答终态对应的publication内部ID',
    execution_identity_text TEXT NULL COMMENT '冻结的执行身份文本：kind、专家ID和名称及绑定版本，不含提示词',
    admitted_at DATETIME(6) NULL COMMENT '用户准入提交成功的UTC时间；未准入时NULL',
    terminal_at DATETIME(6) NULL COMMENT '问答终态提交成功的UTC时间；未结算时NULL',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_turn_question (session_id, question_msg_key),
    UNIQUE KEY uq_conv_turn_order (chat_id, turn_order),
    UNIQUE KEY uq_conv_turn_runtime (runtime_turn_key),
    KEY ix_conv_turn_session_state (session_id, state, id),
    KEY ix_conv_turn_admission (admission_publication_id),
    KEY ix_conv_turn_terminal (terminal_publication_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='问答准入与终态、执行身份及Session范围幂等查询';

CREATE TABLE swe_conversation_publications (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部自增主键',
    operation_key VARBINARY(64) NOT NULL COMMENT '首次预留INSERT前生成的随机操作身份；重试复用，不是内容摘要',
    scope_kind VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'workspace' COMMENT '作用域类型：workspace或仅旧孤立分享导入使用的legacy_share',
    tenant_key VARBINARY(256) NOT NULL COMMENT '租户标识原值，按UTF-8字节精确匹配',
    source_key VARBINARY(256) NOT NULL COMMENT '来源标识原值；旧孤立分享命名空间为空表示未记录',
    agent_key VARBINARY(256) NOT NULL COMMENT 'Agent标识原值；旧孤立分享命名空间为空表示未记录',
    session_id BIGINT UNSIGNED NULL COMMENT '关联逻辑Session内部ID；旧孤立分享导入允许NULL',
    chat_id BIGINT UNSIGNED NULL COMMENT '关联Chat内部ID；旧孤立永久分享允许NULL，不要求Chat存活',
    turn_id BIGINT UNSIGNED NULL COMMENT '关联问答Turn内部ID；旧数据无可靠关联时可为空',
    kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '操作类别；一次发布可同时包含归档、checkpoint、事件及恢复根',
    state VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '发布状态：prepared到committed或aborted，终态不可逆',
    generation BIGINT UNSIGNED NOT NULL COMMENT 'Session执行权代次，单调递增以拒绝过期writer',
    owner_key VARBINARY(128) NOT NULL COMMENT '执行或发布owner的进程实例标识',
    base_state_revision BIGINT UNSIGNED NULL COMMENT '提交校验所需的Session状态基线版本',
    base_history_revision BIGINT UNSIGNED NULL COMMENT '提交校验所需的Chat历史基线版本',
    epoch BIGINT UNSIGNED NULL COMMENT '提交所关联的Chat checkpoint epoch',
    relative_path VARBINARY(1024) NOT NULL COMMENT '由operation key生成的唯一NAS相对路径，每操作一个不可变bundle',
    format_key VARBINARY(64) NOT NULL COMMENT 'NAS bundle编码格式及版本标识，由应用解析',
    byte_length BIGINT UNSIGNED NULL COMMENT '完整bundle字节长度，持久化后固定；预留阶段可NULL',
    durable_at DATETIME(6) NULL COMMENT 'NAS文件及父目录完成持久化确认的UTC时间',
    result_metadata_text MEDIUMTEXT NULL COMMENT '响应身份或待提交计划的校验文本，不存消息正文或工具内容',
    boundary_key VARBINARY(128) NULL COMMENT '外部压缩边界ID，仅最终成功提交时安装',
    boundary_order BIGINT UNSIGNED NULL COMMENT 'Chat内压缩边界顺序，仅最终成功提交时安装',
    archive_first_order BIGINT UNSIGNED NULL COMMENT '本次归档包含的首个archive_order',
    archive_last_order BIGINT UNSIGNED NULL COMMENT '本次归档包含的最后一个archive_order',
    archive_source_count BIGINT UNSIGNED NULL COMMENT '本次归档源Msg数量，用于源消息分页计数',
    archive_display_count BIGINT UNSIGNED NULL COMMENT '显示安全消息数量，用于原有压缩边界展示',
    archive_first_source_msg_key VARBINARY(1024) NULL COMMENT '归档首条源消息ID，保留原边界接口字段',
    archive_last_source_msg_key VARBINARY(1024) NULL COMMENT '归档末条源消息ID，保留原边界接口字段',
    archive_first_timestamp DATETIME(6) NULL COMMENT '归档首条消息的UTC时间，缺失时NULL',
    archive_last_timestamp DATETIME(6) NULL COMMENT '归档末条消息的UTC时间，缺失时NULL',
    archive_offset BIGINT UNSIGNED NULL COMMENT '归档数据在bundle中的字节偏移',
    archive_length BIGINT UNSIGNED NULL COMMENT '归档数据的字节长度',
    checkpoint_revision BIGINT UNSIGNED NULL COMMENT '提交的checkpoint目标版本，prepared阶段保持NULL',
    checkpoint_base_revision BIGINT UNSIGNED NULL COMMENT 'checkpoint校验的前置版本号',
    checkpoint_event_watermark BIGINT UNSIGNED NULL COMMENT 'checkpoint已应用的事件序号水位',
    checkpoint_source_high BIGINT UNSIGNED NULL COMMENT 'checkpoint来源消息的稳定顺序前缀上界',
    checkpoint_offset BIGINT UNSIGNED NULL COMMENT 'checkpoint内容在bundle中的字节偏移',
    checkpoint_length BIGINT UNSIGNED NULL COMMENT 'checkpoint内容的字节长度',
    event_first BIGINT UNSIGNED NULL COMMENT '本批首个事件序号，提交时校验与已提交水位连续',
    event_last BIGINT UNSIGNED NULL COMMENT '本批末个事件序号，提交后推进Chat事件水位',
    event_offset BIGINT UNSIGNED NULL COMMENT '事件批数据在bundle中的字节偏移',
    event_length BIGINT UNSIGNED NULL COMMENT '事件批数据的字节长度',
    retained_history TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '历史保留标记：1需保留，0仍须全部引用检查通过才可回收',
    gc_state VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'live' COMMENT '回收状态：live/deleting/reclaimed；禁止向deleting对象新增引用',
    created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC，微秒精度',
    committed_at DATETIME(6) NULL COMMENT '数据库发布提交成功的UTC时间',
    aborted_at DATETIME(6) NULL COMMENT '经原操作对账后确认终止的UTC时间',
    reclaimed_at DATETIME(6) NULL COMMENT 'NAS bundle实际回收完成的UTC时间',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_publication_operation (operation_key),
    UNIQUE KEY uq_conv_publication_path (relative_path),
    UNIQUE KEY uq_conv_archive_boundary (chat_id, boundary_key),
    UNIQUE KEY uq_conv_archive_order (chat_id, boundary_order),
    UNIQUE KEY uq_conv_checkpoint_revision (chat_id, epoch, checkpoint_revision),
    KEY ix_conv_publication_pending (state, created_at, id),
    KEY ix_conv_publication_gc (gc_state, state, retained_history, id),
    KEY ix_conv_publication_events (chat_id, epoch, event_last, id),
    KEY ix_conv_publication_chat (chat_id, id),
    KEY ix_conv_publication_session (session_id, id),
    KEY ix_conv_publication_turn (turn_id, id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='NAS不可变内容发布操作、归档及checkpoint批次元数据';

CREATE TABLE swe_conversation_messages (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '保留消息occurrence内部ID；源Msg ID可重复，不能据其去重',
    session_id BIGINT UNSIGNED NOT NULL COMMENT '关联逻辑Session内部ID，由应用事务校验关联和作用域',
    origin_chat_id BIGINT UNSIGNED NULL COMMENT '准入anchor的来源Chat；NULL兼容旧无归属anchor',
    archive_chat_id BIGINT UNSIGNED NULL COMMENT '归档occurrence所属Chat；非归档行保持NULL',
    layer VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '消息层：memory/task/error/anchor/archive',
    session_order BIGINT UNSIGNED NULL COMMENT '非归档消息的Session稳定顺序；归档行NULL，跨epoch不复用',
    archive_order BIGINT UNSIGNED NULL COMMENT 'Chat归档稳定顺序；非归档行NULL，跨epoch不复用',
    turn_id BIGINT UNSIGNED NULL COMMENT '关联问答Turn内部ID；旧数据无可靠关联时可为空',
    source_msg_key VARBINARY(1024) NULL COMMENT '原始Msg ID，不唯一，保留现有source及fragment身份契约',
    source_occurrence_no BIGINT UNSIGNED NOT NULL COMMENT '原来源中该消息occurrence序号，用于区分重复源ID',
    role_key VARBINARY(32) NOT NULL COMMENT '原消息角色，如user/assistant/system/tool',
    kind_key VARBINARY(64) NOT NULL COMMENT '原消息类型，用于装配历史显示投影',
    task_run_key VARBINARY(256) NULL COMMENT '原任务运行标识，保持任务分组和step/final显示',
    publication_id BIGINT UNSIGNED NOT NULL COMMENT '消息正文所属publication内部ID，可不同于归档成员所属发布',
    byte_offset BIGINT UNSIGNED NOT NULL COMMENT '内容在不可变NAS bundle中的字节偏移',
    byte_length BIGINT UNSIGNED NOT NULL COMMENT '内容字节长度，读取前校验extent未越界',
    archive_publication_id BIGINT UNSIGNED NULL COMMENT '所属归档边界publication内部ID，不替代正文locator',
    context_epoch BIGINT UNSIGNED NOT NULL COMMENT 'Session共享上下文重置代次，不等于Chat的checkpoint epoch',
    in_context TINYINT UNSIGNED NOT NULL COMMENT '模型上下文成员标记，与历史是否可见独立',
    current_visible TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '当前在线视图可见标记，裁剪与恢复根同事务更新',
    message_timestamp DATETIME(6) NULL COMMENT '消息规范UTC时间；缺失时NULL，原始显示格式保留在NAS内容',
    deleted_at DATETIME(6) NULL COMMENT '逻辑删除UTC时间；NULL表示未删除，权限即时生效',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_message_session_order (session_id, session_order),
    UNIQUE KEY uq_conv_message_archive_order (archive_chat_id, archive_order),
    KEY ix_conv_message_current (session_id, current_visible, layer, deleted_at, session_order),
    KEY ix_conv_message_anchor (session_id, origin_chat_id, layer, session_order),
    KEY ix_conv_message_origin_chat (origin_chat_id, id),
    KEY ix_conv_message_turn (turn_id, session_order, id),
    KEY ix_conv_message_source (session_id, source_msg_key, layer),
    KEY ix_conv_message_context (session_id, context_epoch, in_context, session_order),
    KEY ix_conv_message_archive_page (archive_chat_id, deleted_at, archive_order),
    KEY ix_conv_message_archive_source (archive_chat_id, source_msg_key, deleted_at),
    KEY ix_conv_message_publication (publication_id),
    KEY ix_conv_message_archive_ref (archive_publication_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='在线及归档消息occurrence、稳定顺序和NAS正文范围索引';

CREATE TABLE swe_conversation_retentions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部自增主键',
    retention_key VARBINARY(128) NOT NULL COMMENT '保留集合标识，如reader、backup或依赖集合',
    member_key BIGINT UNSIGNED NOT NULL COMMENT '集合成员键：fence或guard为0，普通pin等于publication内部ID',
    kind VARCHAR(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '保留类型：reader/backup/dependency/backup_fence/gc_guard',
    scope_kind VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'workspace' COMMENT '作用域类型：workspace或仅旧孤立分享导入使用的legacy_share',
    tenant_key VARBINARY(256) NOT NULL COMMENT '租户标识原值，按UTF-8字节精确匹配',
    source_key VARBINARY(256) NOT NULL COMMENT '来源标识原值；旧孤立分享命名空间为空表示未记录',
    agent_key VARBINARY(256) NOT NULL COMMENT 'Agent标识原值；旧孤立分享命名空间为空表示未记录',
    publication_id BIGINT UNSIGNED NULL COMMENT '保留目标publication内部ID；仅scope fence或guard允许NULL',
    guard_marker TINYINT UNSIGNED NULL COMMENT 'scope GC guard固定为1；其他pin和fence为NULL，供唯一索引隔离',
    snapshot_ref VARBINARY(1024) NULL COMMENT '关联备份快照引用或运维定位信息，不含恢复正文',
    state VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '保留状态：active或released；guard不参与普通释放和reaper',
    created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC，微秒精度',
    released_at DATETIME(6) NULL COMMENT '显式释放保留的UTC时间，未释放为NULL',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_retention_member (retention_key, member_key),
    UNIQUE KEY uq_conv_retention_scope_guard (scope_kind, tenant_key, source_key, agent_key, guard_marker),
    KEY ix_conv_retention_object (publication_id, state),
    KEY ix_conv_retention_scope (scope_kind, tenant_key, source_key, agent_key, kind, state)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='读取、备份及依赖引用保留与scope级GC并发保护';

-- All relationships use application-managed IDs. The adapter validates all
-- scope/session/chat relationships, committed status, extents and CAS in one transaction.
-- sessions.active_turn_id/draft_chat_id/recovery_publication_id;
-- chats.active_checkpoint_publication_id/content_meta_publication_id; turns.*_publication_id.
-- No cascades or automatic age-based history deletion.

-- Each scope lazily creates exactly one retention kind=gc_guard row using
-- guard_marker=1, member_key=0 and a random opaque retention_key before any
-- publication/GC/backup in that scope. The scope guard UNIQUE key arbitrates races.
-- All ordinary pins/fences use guard_marker=NULL. No global GC mutex is required.
-- GC and backup fence installation lock this scope guard before publication rows.
-- Never hold guard locks during NAS IO, copying, candidate scans or pin enumeration.

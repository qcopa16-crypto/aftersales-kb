-- ============================================================================
-- 售后知识库 · MySQL 8 建表脚本（生产/联调环境）
-- 与 src/main/resources/schema-h2.sql 逐列对齐；权威表结构见 docs/services/database.md。
-- 枚举与状态机见 DESIGN.md §6 与 代码设计规范.md §3.6。
--
-- 执行方式：
--   mysql -h <host> -u root -p < schema-mysql.sql
-- 或由应用侧 JDBC 执行（本仓库交付时用 JDBC 初始化，见下方说明）。
--
-- 注意：本脚本在交付环境中已由 JDBC 真实执行并验证（目标 MySQL 主机:3306）。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS aftersales_kb
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_general_ci;

USE aftersales_kb;

-- ---------------------------------------------------------------------------
-- 1. 组织与权限
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    username      VARCHAR(64)  NOT NULL COMMENT '登录名',
    password_hash VARCHAR(128) NOT NULL COMMENT 'BCrypt',
    display_name  VARCHAR(64)           DEFAULT NULL,
    department_id BIGINT                DEFAULT NULL,
    status        TINYINT      NOT NULL DEFAULT 1 COMMENT '1 正常 0 停用',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_users_username (username)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '用户';

CREATE TABLE IF NOT EXISTS departments (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    parent_id  BIGINT      NOT NULL DEFAULT 0 COMMENT '父部门，0 为根',
    name       VARCHAR(64) NOT NULL,
    leader_id  BIGINT               DEFAULT NULL COMMENT '负责人 user_id',
    sort_order INT         NOT NULL DEFAULT 0,
    created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '部门（树）';

CREATE TABLE IF NOT EXISTS roles (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    role_name   VARCHAR(64)  NOT NULL,
    role_code   VARCHAR(64)  NOT NULL,
    description VARCHAR(255)          DEFAULT NULL,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_roles_code (role_code)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '角色';

CREATE TABLE IF NOT EXISTS user_roles (
    id      BIGINT NOT NULL AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_roles (user_id, role_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '用户-角色';

CREATE TABLE IF NOT EXISTS role_permissions (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    role_id         BIGINT      NOT NULL,
    permission_code VARCHAR(64) NOT NULL COMMENT '如 knowledge:import',
    permission_type VARCHAR(32) NOT NULL DEFAULT 'api' COMMENT 'menu / button / api',
    PRIMARY KEY (id),
    UNIQUE KEY uk_role_perm (role_id, permission_code)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '角色-操作权限';

-- ---------------------------------------------------------------------------
-- 2. 知识主数据
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS knowledge_units (
    id               BIGINT       NOT NULL AUTO_INCREMENT,
    unit_code        VARCHAR(64)  NOT NULL COMMENT '业务编码（对 AI 的 unit_id）',
    title            VARCHAR(255) NOT NULL,
    content          LONGTEXT              DEFAULT NULL COMMENT '全文或正文',
    summary          VARCHAR(512)          DEFAULT NULL,
    category         VARCHAR(64)           DEFAULT NULL COMMENT 'product/policy/sop/sales/finance',
    source_file_name VARCHAR(255)          DEFAULT NULL,
    file_type        VARCHAR(32)           DEFAULT NULL COMMENT 'pdf/docx/md/txt',
    file_size        BIGINT                DEFAULT NULL,
    storage_path     VARCHAR(512)          DEFAULT NULL COMMENT '落存储相对路径',
    content_hash     VARCHAR(128)          DEFAULT NULL COMMENT 'parse 原文哈希（审计）',
    chunks_hash      VARCHAR(128)          DEFAULT NULL COMMENT 'parse 切片哈希（embed 幂等）',
    use_vlm          TINYINT      NOT NULL DEFAULT 0
        COMMENT '1=导入时让 AI 对 PDF 图片做 VLM 图注',
    status           VARCHAR(16)  NOT NULL DEFAULT 'active'
        COMMENT '业务可用性：active/disabled/deleting/superseded',
    ingest_status    VARCHAR(16)  NOT NULL DEFAULT 'draft'
        COMMENT '流水线：draft/queued/parsing/parsed/embedding/active/failed',
    batch_id         VARCHAR(64)           DEFAULT NULL,
    creator_id       BIGINT                DEFAULT NULL,
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lifecycle_version BIGINT      NOT NULL DEFAULT 0
        COMMENT 'F-03 生命周期版本：状态/内容变更时 +1，用于 CAS 校验',
    PRIMARY KEY (id),
    UNIQUE KEY uk_knowledge_units_code (unit_code),
    KEY idx_units_title (title),
    KEY idx_units_category (category),
    KEY idx_units_status (status),
    KEY idx_units_ingest (ingest_status),
    KEY idx_units_batch (batch_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '知识单元';

-- R-03 业务编码序列：U- 编码由数据库原子计数器（行锁 + 段预留）分配，避免并发导入各自读到
-- 同一最大值而生成重复编码导致整批回滚；也不再每批全量扫描历史编码，分配成本为 O(1)。
CREATE TABLE IF NOT EXISTS knowledge_unit_code_seq (
    seq_name   VARCHAR(64) NOT NULL,
    next_value BIGINT      NOT NULL,
    PRIMARY KEY (seq_name)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '知识单元业务编码序列';

-- 初始值须高于内置种子编码（U-1001..U-1005）；既有生产库由迁移脚本按「现有最大 U- 数字 + 1」回填。
INSERT IGNORE INTO knowledge_unit_code_seq (seq_name, next_value) VALUES ('unit_code', 1006);

CREATE TABLE IF NOT EXISTS ingest_jobs (
    id            BIGINT      NOT NULL AUTO_INCREMENT,
    batch_id      VARCHAR(64) NOT NULL,
    unit_id       BIGINT      NOT NULL,
    stage         VARCHAR(32) NOT NULL DEFAULT 'queued'
        COMMENT 'queued/parse/chunk_qc/classify/quality_gate/embed/finalize/done/failed',
    progress      INT         NOT NULL DEFAULT 0 COMMENT '0-100',
    error_code    VARCHAR(64)          DEFAULT NULL,
    error_message VARCHAR(512)         DEFAULT NULL COMMENT '可读原因 + 建议动作',
    attempt       INT         NOT NULL DEFAULT 0 COMMENT '重试次数，<=3',
    created_by    BIGINT               DEFAULT NULL,
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- F-02 租约（lease）：worker 崩溃/重启后，过期租约由任意实例回收，避免作业永久卡住
    claimed_by      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '当前持有租约的 worker 标识',
    claimed_from    VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '领取前的可领取阶段（queued/embed），回收时回到该阶段',
    lease_until     DATETIME              DEFAULT NULL COMMENT '租约到期时间；过期即可被回收',
    heartbeat_at    DATETIME              DEFAULT NULL COMMENT '最近一次心跳/阶段推进时间',
    -- F-02 退避持久化：可重试失败的下次可见时间（多实例共享，不再依赖进程本地 notBefore）
    not_before      DATETIME              DEFAULT NULL COMMENT '退避窗口结束前不可被领取',
    PRIMARY KEY (id),
    KEY idx_jobs_batch (batch_id),
    KEY idx_jobs_unit (unit_id),
    KEY idx_jobs_stage (stage),
    KEY idx_jobs_lease (stage, lease_until)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '入库作业';

CREATE TABLE IF NOT EXISTS ingest_reviews (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    unit_id         BIGINT       NOT NULL,
    job_id          BIGINT                DEFAULT NULL,
    batch_id        VARCHAR(64)           DEFAULT NULL,
    category        VARCHAR(64)           DEFAULT NULL,
    decision        VARCHAR(24)  NOT NULL COMMENT 'auto_pass/human_required/human_confirmed/human_adjusted',
    decider         VARCHAR(16)  NOT NULL COMMENT 'ai/human',
    decider_user_id BIGINT                DEFAULT NULL,
    rules_verdict   VARCHAR(16)           DEFAULT NULL,
    llm_verdict     VARCHAR(16)           DEFAULT NULL,
    llm_confidence  DOUBLE                DEFAULT NULL,
    vlm_verdict     VARCHAR(16)           DEFAULT NULL,
    review_reasons  VARCHAR(512)          DEFAULT NULL,
    detail_json     TEXT                  DEFAULT NULL,
    ai_model        VARCHAR(64)           DEFAULT NULL,
    llm_ms          INT          NOT NULL DEFAULT 0,
    vlm_ms          INT          NOT NULL DEFAULT 0,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_reviews_unit (unit_id),
    KEY idx_reviews_decision_time (decision, created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '入库质检判定审计';

CREATE TABLE IF NOT EXISTS unit_permissions (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    unit_id     BIGINT      NOT NULL,
    target_type VARCHAR(16) NOT NULL COMMENT 'global/department/role/user',
    target_id   BIGINT      NOT NULL DEFAULT 0 COMMENT 'global 时为 0',
    created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_unit_perm (unit_id, target_type, target_id),
    KEY idx_unit_perm_unit (unit_id),
    -- 四维权限过滤：按 (target_type, target_id) 反查可见 unit，避免全表扫描
    KEY idx_unit_perm_target (target_type, target_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '知识单元数据权限（四维）';

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    unit_id      BIGINT       NOT NULL,
    chunk_index  INT          NOT NULL,
    content      TEXT COMMENT '切片正文（权威向量在向量库）',
    heading_path VARCHAR(512)          DEFAULT NULL
        COMMENT '标题路径 JSON 数组；embed 重算 chunks_hash 必需',
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_chunks_unit (unit_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '知识切片冗余';

-- ---------------------------------------------------------------------------
-- 3. 问答日志 / FAQ / 缺口
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS qa_access_logs (
    id                         BIGINT      NOT NULL AUTO_INCREMENT,
    session_id                 VARCHAR(64)          DEFAULT NULL,
    user_id                    BIGINT               DEFAULT NULL,
    question                   TEXT,
    answer                     LONGTEXT,
    recalled_unit_ids_json     JSON                 DEFAULT NULL,
    authorized_unit_ids_json   JSON                 DEFAULT NULL,
    unauthorized_unit_ids_json JSON                 DEFAULT NULL,
    prompt_tokens              INT         NOT NULL DEFAULT 0,
    completion_tokens          INT         NOT NULL DEFAULT 0,
    total_tokens               INT         NOT NULL DEFAULT 0,
    response_time_ms           INT         NOT NULL DEFAULT 0
        COMMENT '端到端耗时',
    retrieve_ms                INT         NOT NULL DEFAULT 0
        COMMENT '检索阶段耗时（含权限过滤前的 AI retrieve）',
    generate_ms                INT         NOT NULL DEFAULT 0
        COMMENT '生成阶段耗时（AI 流式 generate）',
    db_ms                      INT         NOT NULL DEFAULT 0
        COMMENT '数据库耗时（MySQL：四维权限过滤 + 知识单元加载）',
    cache_ms                   INT         NOT NULL DEFAULT 0
        COMMENT '缓存耗时（Redis：FAQ 命中查询）',
    chosen_unit_id             BIGINT               DEFAULT NULL
        COMMENT '澄清点选：本轮由用户从候选中选中的知识单元 id；未点选为 NULL',
    answer_source              VARCHAR(16) NOT NULL DEFAULT 'kb'
        COMMENT '答案来源：kb/faq/llm_general/clarify/refusal',
    faq_hit                    TINYINT     NOT NULL DEFAULT 0,
    created_at                 DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_logs_user_time (user_id, created_at),
    KEY idx_logs_time (created_at),
    -- 耗时分析最慢问题：ORDER BY response_time_ms DESC 走索引，避免 Using filesort
    KEY idx_qa_resp (response_time_ms)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '问答访问日志';

CREATE TABLE IF NOT EXISTS faqs (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    question        VARCHAR(512) NOT NULL,
    answer          LONGTEXT,
    category        VARCHAR(64)           DEFAULT NULL,
    related_unit_id BIGINT                DEFAULT NULL,
    source_type     VARCHAR(16)  NOT NULL DEFAULT 'manual' COMMENT 'manual/auto_mined',
    status          VARCHAR(16)  NOT NULL DEFAULT 'pending_review'
        COMMENT 'pending_review/published/rejected',
    hit_count       INT          NOT NULL DEFAULT 0,
    reviewer_id     BIGINT                DEFAULT NULL,
    reviewed_at     DATETIME              DEFAULT NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    question_norm   VARCHAR(512) NOT NULL DEFAULT ''
        COMMENT 'F-25 规范化问题键（与 Redis 缓存键一致）：TTL 过期后按该键 MySQL 回源并回填',
    PRIMARY KEY (id),
    KEY idx_faqs_status (status),
    KEY idx_faqs_norm (question_norm, status)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = 'FAQ';

CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id                    BIGINT       NOT NULL AUTO_INCREMENT,
    question_pattern      VARCHAR(512) NOT NULL COMMENT '归一化问题模式',
    sample_questions_json JSON                  DEFAULT NULL,
    ask_count             INT          NOT NULL DEFAULT 1,
    last_asked_at         DATETIME              DEFAULT NULL,
    status                VARCHAR(16)  NOT NULL DEFAULT 'unresolved'
        COMMENT 'unresolved/resolved/ignored',
    resolved_unit_id      BIGINT                DEFAULT NULL,
    created_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_gaps_pattern (question_pattern),
    KEY idx_gaps_status (status)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '知识缺口';

-- R-16：样例问题规范化到独立表（gap_id + question 唯一），避免应用层读改写 JSON 造成并发丢样例；
-- 唯一约束保证并发记录同一问题时幂等，容量上限由应用层 best-effort 限制（样例不丢、可能略超上限）。
CREATE TABLE IF NOT EXISTS knowledge_gap_samples (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    gap_id     BIGINT       NOT NULL COMMENT '关联 knowledge_gaps.id',
    question   VARCHAR(512) NOT NULL COMMENT '样例问题（截断到 512 字符）',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_gap_samples (gap_id, question),
    KEY idx_gap_samples_gap (gap_id, id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '知识缺口样例问题（gap_id+question 唯一）';

-- F-04 删除 outbox：向量删除失败/在途等待时持久化补偿任务（重试次数、下次重试、最近错误），
-- 由后台 worker 幂等重放 AI DELETE，确认后再清 MySQL 主数据。
CREATE TABLE IF NOT EXISTS knowledge_delete_tasks (
    id            BIGINT      NOT NULL AUTO_INCREMENT,
    unit_id       BIGINT      NOT NULL,
    unit_code     VARCHAR(64) NOT NULL COMMENT '删除时的业务编码（tombstone 编码来源）',
    status        VARCHAR(16) NOT NULL DEFAULT 'pending'
        COMMENT 'pending/waiting/succeeded/failed：waiting=等待在途入库作业租约结束',
    retry_count   INT         NOT NULL DEFAULT 0 COMMENT '补偿重试次数',
    next_retry_at DATETIME             DEFAULT NULL COMMENT '下次重试时间（退避）',
    last_error    VARCHAR(512)         DEFAULT NULL COMMENT '最近一次失败原因（管理端展示）',
    -- R-03 消费者原子抢占：worker_id + claim_token + lease_until；租约过期可被其他实例回收
    claimed_by    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '当前持有租约的消费者标识',
    claim_token   VARCHAR(64) NOT NULL DEFAULT '' COMMENT '本次抢占令牌（终态 CAS 用）',
    lease_until   DATETIME             DEFAULT NULL COMMENT '处理租约到期时间；过期可被回收',
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_delete_tasks_unit (unit_id),
    KEY idx_delete_tasks_due (status, next_retry_at, lease_until)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '知识删除补偿任务（outbox）';

-- F-03 可重放 tombstone：主数据删除后保留编码，供补偿/扫描重放 AI DELETE，清理迟到 embed 写的孤儿向量。
CREATE TABLE IF NOT EXISTS knowledge_unit_tombstones (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    unit_id     BIGINT      NOT NULL,
    unit_code   VARCHAR(64) NOT NULL,
    deleted_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_swept_at DATETIME           DEFAULT NULL COMMENT '最近一次墓碑重放扫描时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_tombstones_unit (unit_id),
    KEY idx_tombstones_code (unit_code)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '已删除知识单元墓碑（可重放补偿删除）';

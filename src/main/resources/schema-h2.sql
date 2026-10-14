-- H2 (MySQL 兼容模式) 建表脚本：本地开发 / 自动化测试专用。
-- 权威表结构见 docs/services/database.md；枚举与状态机见 DESIGN.md §6 与 代码设计规范.md §3.6。
-- 生产（MySQL）建表由迁移脚本负责，见 application-mysql.yaml。

CREATE TABLE IF NOT EXISTS users (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(128) NOT NULL,
    display_name  VARCHAR(64),
    department_id BIGINT,
    status        TINYINT      NOT NULL DEFAULT 1,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_users_username UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS departments (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    parent_id  BIGINT      NOT NULL DEFAULT 0,
    name       VARCHAR(64) NOT NULL,
    leader_id  BIGINT,
    sort_order INT         NOT NULL DEFAULT 0,
    created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS roles (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    role_name   VARCHAR(64)  NOT NULL,
    role_code   VARCHAR(64)  NOT NULL,
    description VARCHAR(255),
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_roles_code UNIQUE (role_code)
);

CREATE TABLE IF NOT EXISTS user_roles (
    id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    CONSTRAINT uk_user_roles UNIQUE (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS role_permissions (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    role_id         BIGINT      NOT NULL,
    permission_code VARCHAR(64) NOT NULL,
    permission_type VARCHAR(32) NOT NULL DEFAULT 'api',
    CONSTRAINT uk_role_perm UNIQUE (role_id, permission_code)
);

CREATE TABLE IF NOT EXISTS knowledge_units (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    unit_code        VARCHAR(64)  NOT NULL,
    title            VARCHAR(255) NOT NULL,
    content          TEXT,
    summary          VARCHAR(512),
    category         VARCHAR(64),
    source_file_name VARCHAR(255),
    file_type        VARCHAR(32),
    file_size        BIGINT,
    storage_path     VARCHAR(512),
    content_hash     VARCHAR(128),
    chunks_hash      VARCHAR(128),
    use_vlm          TINYINT      NOT NULL DEFAULT 0,
    status           VARCHAR(16)  NOT NULL DEFAULT 'active',
    ingest_status    VARCHAR(16)  NOT NULL DEFAULT 'draft',
    batch_id         VARCHAR(64),
    creator_id       BIGINT,
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- F-03 生命周期版本：状态/内容变更时 +1，用于 CAS 校验（删除进 deleting、最终发布不得覆盖 disabled/deleting）
    lifecycle_version BIGINT      NOT NULL DEFAULT 0,
    CONSTRAINT uk_knowledge_units_code UNIQUE (unit_code)
);

-- R-03 业务编码序列：U- 编码由数据库原子计数器（行锁 + 段预留）分配，避免并发导入各自读到
-- 同一最大值而生成重复编码导致整批回滚；也不再每批全量扫描历史编码，分配成本为 O(1)。
CREATE TABLE IF NOT EXISTS knowledge_unit_code_seq (
    seq_name   VARCHAR(64) NOT NULL,
    next_value BIGINT      NOT NULL,
    PRIMARY KEY (seq_name)
);

-- 初始值须高于内置种子编码（U-1001..U-1005）；既有生产库由迁移脚本按「现有最大 U- 数字 + 1」回填。
INSERT IGNORE INTO knowledge_unit_code_seq (seq_name, next_value) VALUES ('unit_code', 1006);

CREATE TABLE IF NOT EXISTS ingest_jobs (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    batch_id      VARCHAR(64) NOT NULL,
    unit_id       BIGINT      NOT NULL,
    stage         VARCHAR(32) NOT NULL DEFAULT 'queued',
    progress      INT         NOT NULL DEFAULT 0,
    error_code    VARCHAR(64),
    error_message VARCHAR(512),
    attempt       INT         NOT NULL DEFAULT 0,
    created_by    BIGINT,
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- F-02 租约（lease）：worker 崩溃/重启后，过期租约由任意实例回收，避免作业永久卡住
    claimed_by      VARCHAR(64) NOT NULL DEFAULT '',
    claimed_from    VARCHAR(32) NOT NULL DEFAULT '',
    lease_until     DATETIME,
    heartbeat_at    DATETIME,
    -- F-02 退避持久化：可重试失败的下次可见时间（多实例共享，不再依赖进程本地 notBefore）
    not_before      DATETIME
);

CREATE TABLE IF NOT EXISTS ingest_reviews (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    unit_id         BIGINT       NOT NULL,
    job_id          BIGINT,
    batch_id        VARCHAR(64),
    category        VARCHAR(64),
    decision        VARCHAR(24)  NOT NULL,
    decider         VARCHAR(16)  NOT NULL,
    decider_user_id BIGINT,
    rules_verdict   VARCHAR(16),
    llm_verdict     VARCHAR(16),
    llm_confidence  DOUBLE,
    vlm_verdict     VARCHAR(16),
    review_reasons  VARCHAR(512),
    detail_json     TEXT,
    ai_model        VARCHAR(64),
    llm_ms          INT          NOT NULL DEFAULT 0,
    vlm_ms          INT          NOT NULL DEFAULT 0,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS unit_permissions (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    unit_id     BIGINT      NOT NULL,
    target_type VARCHAR(16) NOT NULL,
    target_id   BIGINT      NOT NULL DEFAULT 0,
    created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_unit_perm UNIQUE (unit_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    unit_id     BIGINT NOT NULL,
    chunk_index INT    NOT NULL,
    content     TEXT,
    heading_path VARCHAR(512),
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS qa_access_logs (
    id                        BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id                VARCHAR(64),
    user_id                   BIGINT,
    question                  TEXT,
    answer                    TEXT,
    recalled_unit_ids_json    TEXT,
    authorized_unit_ids_json  TEXT,
    unauthorized_unit_ids_json TEXT,
    prompt_tokens             INT NOT NULL DEFAULT 0,
    completion_tokens         INT NOT NULL DEFAULT 0,
    total_tokens              INT NOT NULL DEFAULT 0,
    response_time_ms          INT NOT NULL DEFAULT 0,
    retrieve_ms               INT NOT NULL DEFAULT 0,
    generate_ms               INT NOT NULL DEFAULT 0,
    db_ms                     INT NOT NULL DEFAULT 0,
    cache_ms                  INT NOT NULL DEFAULT 0,
    chosen_unit_id            BIGINT,
    answer_source             VARCHAR(16) NOT NULL DEFAULT 'kb',
    faq_hit                   TINYINT NOT NULL DEFAULT 0,
    created_at                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS faqs (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    question        VARCHAR(512) NOT NULL,
    answer          TEXT,
    category        VARCHAR(64),
    related_unit_id BIGINT,
    source_type     VARCHAR(16) NOT NULL DEFAULT 'manual',
    status          VARCHAR(16) NOT NULL DEFAULT 'pending_review',
    hit_count       INT NOT NULL DEFAULT 0,
    reviewer_id     BIGINT,
    reviewed_at     DATETIME,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- F-25 规范化问题键（与 Redis 缓存键一致）：TTL 过期后按该键 MySQL 回源并回填
    question_norm   VARCHAR(512) NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_faqs_norm      ON faqs (question_norm, status);

CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id                     BIGINT AUTO_INCREMENT PRIMARY KEY,
    question_pattern       VARCHAR(512) NOT NULL,
    sample_questions_json  TEXT,
    ask_count              INT NOT NULL DEFAULT 1,
    last_asked_at          DATETIME,
    status                 VARCHAR(16) NOT NULL DEFAULT 'unresolved',
    resolved_unit_id       BIGINT,
    created_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- F-09：归一化问题模式唯一——并发记录同一问题只会有一行，配合数据库侧原子计数
CREATE UNIQUE INDEX IF NOT EXISTS uk_gaps_pattern ON knowledge_gaps(question_pattern);

-- R-16：样例问题规范化到独立表（gap_id + question 唯一），避免应用层读改写 JSON 造成并发丢样例；
-- 唯一约束保证并发记录同一问题时幂等，容量上限由应用层 best-effort 限制（样例不丢、可能略超上限）。
CREATE TABLE IF NOT EXISTS knowledge_gap_samples (
    id         BIGINT       AUTO_INCREMENT PRIMARY KEY,
    gap_id     BIGINT       NOT NULL,
    question   VARCHAR(512) NOT NULL,
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_gap_samples UNIQUE (gap_id, question)
);

CREATE INDEX IF NOT EXISTS idx_gap_samples_gap ON knowledge_gap_samples (gap_id, id);

-- F-04 删除 outbox：向量删除失败/在途等待时持久化补偿任务（重试次数、下次重试、最近错误），
-- 由后台 worker 幂等重放 AI DELETE，确认后再清 MySQL 主数据。
CREATE TABLE IF NOT EXISTS knowledge_delete_tasks (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    unit_id       BIGINT       NOT NULL,
    unit_code     VARCHAR(64)  NOT NULL,
    status        VARCHAR(16)  NOT NULL DEFAULT 'pending',
    retry_count   INT          NOT NULL DEFAULT 0,
    next_retry_at DATETIME,
    last_error    VARCHAR(512),
    -- R-03 消费者原子抢占：worker_id + claim_token + lease_until；租约过期可被其他实例回收
    claimed_by    VARCHAR(64)  NOT NULL DEFAULT '',
    claim_token   VARCHAR(64)  NOT NULL DEFAULT '',
    lease_until   DATETIME,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_delete_tasks_unit UNIQUE (unit_id)
);

CREATE INDEX IF NOT EXISTS idx_delete_tasks_due ON knowledge_delete_tasks (status, next_retry_at, lease_until);

-- F-03 可重放 tombstone：主数据删除后保留编码，供补偿/扫描重放 AI DELETE，清理迟到 embed 写的孤儿向量。
CREATE TABLE IF NOT EXISTS knowledge_unit_tombstones (
    id          BIGINT      AUTO_INCREMENT PRIMARY KEY,
    unit_id     BIGINT      NOT NULL,
    unit_code   VARCHAR(64) NOT NULL,
    deleted_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_swept_at DATETIME,
    CONSTRAINT uk_tombstones_unit UNIQUE (unit_id)
);

CREATE INDEX IF NOT EXISTS idx_tombstones_code ON knowledge_unit_tombstones (unit_code);

CREATE INDEX IF NOT EXISTS idx_units_title      ON knowledge_units (title);
CREATE INDEX IF NOT EXISTS idx_units_category   ON knowledge_units (category);
CREATE INDEX IF NOT EXISTS idx_units_status     ON knowledge_units (status);
CREATE INDEX IF NOT EXISTS idx_units_ingest     ON knowledge_units (ingest_status);
CREATE INDEX IF NOT EXISTS idx_units_batch      ON knowledge_units (batch_id);
CREATE INDEX IF NOT EXISTS idx_jobs_batch       ON ingest_jobs (batch_id);
CREATE INDEX IF NOT EXISTS idx_jobs_unit        ON ingest_jobs (unit_id);
CREATE INDEX IF NOT EXISTS idx_jobs_stage       ON ingest_jobs (stage);
CREATE INDEX IF NOT EXISTS idx_jobs_lease       ON ingest_jobs (stage, lease_until);
CREATE INDEX IF NOT EXISTS idx_logs_user_time   ON qa_access_logs (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_time        ON qa_access_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_reviews_unit     ON ingest_reviews (unit_id);
CREATE INDEX IF NOT EXISTS idx_reviews_decision_time ON ingest_reviews (decision, created_at);

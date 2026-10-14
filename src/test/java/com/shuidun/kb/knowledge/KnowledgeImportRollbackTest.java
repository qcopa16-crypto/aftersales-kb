package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

import com.shuidun.kb.config.QualityReviewProperties;
import com.shuidun.kb.job.IngestJobService;
import com.shuidun.kb.job.entity.IngestReview;
import com.shuidun.kb.job.mapper.IngestReviewMapper;
import com.shuidun.kb.knowledge.dto.BatchDetailVO;
import com.shuidun.kb.knowledge.dto.BatchFileVO;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.knowledge.service.KnowledgeImportService;
import javax.sql.DataSource;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.annotation.DirtiesContext;

/**
 * 回滚健壮性测试：批次详情不得依赖 {@code ingest_reviews} 表。
 *
 * <p>覆盖两条回滚路径：① 总开关 {@code app.ingest.quality.enabled=false}（关开关 = 回到改造前，
 * 完全不读审计）；② 表不存在/查询失败时静默降级，绝不让批次详情报错。
 *
 * <p><b>完全隔离</b>：使用独立的 H2 内存库（URL 与共享库 {@code aftersales_kb} 不同），
 * 从而形成独立 Spring 上下文；本类的 {@code DROP TABLE} 只作用于独立库，任何执行顺序都不会影响
 * 其它用例。{@code @DirtiesContext(AFTER_CLASS)} 在本类结束后丢弃该上下文，避免其上下文/连接驻留。
 * 另在 {@link #setUp()} 中<b>强校验</b>当前数据源确为独立库，一旦隔离失效立即报错而非误删共享表。
 *
 * <p>数据源 URL 可覆盖：{@code -Dtest.datasource.url=...}（或环境变量 {@code TEST_DATASOURCE_URL}）
 * 未设置时保持原 {@code jdbc:h2:mem:aftersales_kb_rollback_isolated} 默认值，离线自包含可跑。
 * mysql profile 下可用其指向真实库（driver/凭据由 application-mysql.yaml 提供）；覆盖时
 * {@link #setUp()} 的隔离强校验仍要求 URL 含专属库名 {@code aftersales_kb_rollback_isolated}
 * —— 指向真实库请单独建同名 schema，切勿指向共享库 {@code aftersales_kb}。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_rollback_isolated;MODE=MySQL;"
                + "DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeImportRollbackTest {

    /** 与 schema-h2.sql 中 ingest_reviews 逐列对齐，供用例内重建表。 */
    private static final String CREATE_REVIEWS =
            "CREATE TABLE IF NOT EXISTS ingest_reviews ("
                    + "id BIGINT AUTO_INCREMENT PRIMARY KEY, unit_id BIGINT NOT NULL, job_id BIGINT, "
                    + "batch_id VARCHAR(64), category VARCHAR(64), decision VARCHAR(24) NOT NULL, "
                    + "decider VARCHAR(16) NOT NULL, decider_user_id BIGINT, rules_verdict VARCHAR(16), "
                    + "llm_verdict VARCHAR(16), llm_confidence DOUBLE, vlm_verdict VARCHAR(16), "
                    + "review_reasons VARCHAR(512), detail_json TEXT, ai_model VARCHAR(64), "
                    + "llm_ms INT NOT NULL DEFAULT 0, vlm_ms INT NOT NULL DEFAULT 0, "
                    + "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)";

    @Autowired
    private KnowledgeImportService service;

    @Autowired
    private QualityReviewProperties props;

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private IngestJobService ingestJobService;

    @Autowired
    private IngestReviewMapper ingestReviewMapper;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private DataSource dataSource;

    /** 知识管理员口径的当前用户（持有 knowledge:perm，批次范围不受限，可读完整明细）。 */
    private final com.shuidun.kb.auth.CurrentUser adminUser = new com.shuidun.kb.auth.CurrentUser(
            1L, "admin", "系统管理员", 1L, java.util.List.of("admin"),
            java.util.Set.of("knowledge:perm"));

    private String batchId;
    private Long unitId;

    @BeforeEach
    void setUp() throws Exception {
        props.setEnabled(true);
        // 强校验隔离生效：数据源必须是本类专属库，否则拒绝执行 DROP（绝不误删共享库）
        try (var conn = dataSource.getConnection()) {
            String url = conn.getMetaData().getURL();
            if (url == null || !url.contains("aftersales_kb_rollback_isolated")) {
                throw new IllegalStateException("回滚测试未隔离到专属库，实际 URL=" + url + "，拒绝 DROP");
            }
        }
        // 每个用例前重建审计表：保证表存在且干净（本类用独立库，DROP 不影响其它测试）
        jdbcTemplate.execute("DROP TABLE IF EXISTS ingest_reviews");
        jdbcTemplate.execute(CREATE_REVIEWS);
        batchId = "B-ROLLBACK-" + System.nanoTime();
        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setUnitCode("U-RB-" + System.nanoTime());
        unit.setTitle("回滚健壮性测试");
        unit.setCategory("sop");
        unit.setUseVlm(0);
        unit.setStatus("active");
        unit.setIngestStatus("parsed");
        unit.setBatchId(batchId);
        unitMapper.insert(unit);
        unitId = unit.getId();
        ingestJobService.enqueue(batchId, unitId, null);
    }

    @AfterEach
    void tearDown() {
        props.setEnabled(true);
        jdbcTemplate.execute(CREATE_REVIEWS);
    }

    @Test
    void disabledQualityIgnoresExistingAuditRows() {
        // 表中已有审计行（含 AI 预审结论），但总开关关闭 → 批次详情不得读取/暴露
        IngestReview review = new IngestReview();
        review.setUnitId(unitId);
        review.setBatchId(batchId);
        review.setCategory("sop");
        review.setDecision("human_required");
        review.setDecider("ai");
        review.setDetailJson("{\"reason\":\"risk_category\",\"verdict\":\"pass\"}");
        review.setAiModel("glm-4.6v");
        ingestReviewMapper.insert(review);

        props.setEnabled(false);
        BatchDetailVO detail = service.batchDetail(adminUser, batchId);

        assertEquals(1, detail.getFiles().size());
        BatchFileVO file = detail.getFiles().get(0);
        assertNull(file.getReviewVerdict(), "关闭总开关后不得暴露审计结论");
        assertNull(file.getAiVerdict(), "关闭总开关后不得暴露 AI 预审结论");
        assertNull(file.getAiModel(), "关闭总开关后不得暴露 AI 模型");
    }

    @Test
    void disabledQualityWorksWhenAuditTableMissing() {
        jdbcTemplate.execute("DROP TABLE IF EXISTS ingest_reviews");
        props.setEnabled(false);

        BatchDetailVO detail = service.batchDetail(adminUser, batchId);

        assertEquals(1, detail.getFiles().size(), "表缺失时批次详情仍应正常返回");
        assertNull(detail.getFiles().get(0).getReviewVerdict());
        assertNull(detail.getFiles().get(0).getAiVerdict());
    }

    @Test
    void enabledQualityDegradesWhenAuditTableMissing() {
        jdbcTemplate.execute("DROP TABLE IF EXISTS ingest_reviews");
        props.setEnabled(true);

        BatchDetailVO detail = service.batchDetail(adminUser, batchId);

        assertEquals(1, detail.getFiles().size(), "审计读取失败应静默降级，绝不让批次详情报错");
        assertNull(detail.getFiles().get(0).getReviewVerdict());
        assertNull(detail.getFiles().get(0).getAiVerdict());
    }
}

package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.knowledge.dto.ImportResponseVO;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.knowledge.service.KnowledgeImportService;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.annotation.DirtiesContext;

/**
 * R-03 回归：U- 编码由数据库原子序列分配。
 *
 * <p>覆盖：① 并发提交多批次编码唯一且不整批回滚；② 编码生成不再依赖全表扫描；
 * ③ 唯一索引冲突时只重试冲突单元（换号），不整批回滚。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_code_seq_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}",
        "app.ingest.worker.enabled=false",
        "app.upload.max-concurrent-requests=64"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeUnitCodeSequenceTest {

    @Autowired
    private KnowledgeImportService service;

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    /** 并发提交多批次：所有成功批次编码唯一，且无批次因编码竞态整体失败。 */
    @Test
    void concurrentImportsProduceUniqueCodesWithoutBatchRollback() throws Exception {
        int threads = 4;
        int filesPerThread = 3;
        List<Long> unitIds = java.util.Collections.synchronizedList(new ArrayList<>());
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch start = new CountDownLatch(1);
        List<Future<?>> futures = new ArrayList<>();
        try {
            for (int t = 0; t < threads; t++) {
                final int thread = t;
                futures.add(pool.submit(() -> {
                    try {
                        start.await();
                        for (int i = 0; i < filesPerThread; i++) {
                            ImportResponseVO response = service.importFiles(
                                    List.of(file("c-" + thread + "-" + i + ".md")), null, false, 1L);
                            assertEquals(1, response.getAcceptedCount(), "并发导入不得因编码竞态整批回滚");
                            unitIds.add(response.getItems().get(0).getUnitId());
                        }
                    } catch (Exception ex) {
                        throw new IllegalStateException(ex);
                    }
                }));
            }
            start.countDown();
            for (Future<?> future : futures) {
                future.get(60, TimeUnit.SECONDS);
            }

            Set<String> codes = new HashSet<>();
            for (Long unitId : unitIds) {
                String code = jdbcTemplate.queryForObject(
                        "SELECT unit_code FROM knowledge_units WHERE id = ?", String.class, unitId);
                assertTrue(code != null && code.startsWith("U-"), "编码格式应为 U-<数字>: " + code);
                assertTrue(codes.add(code), "并发导入产生了重复编码: " + code);
            }
            assertEquals(threads * filesPerThread, codes.size());
        } finally {
            pool.shutdownNow();
        }
    }

    /** 编码分配走数据库序列：每批接受 N 个文件后计数器至少推进 N。 */
    @Test
    void codeAllocationAdvancesDatabaseSequence() {
        Long before = currentSeqValue();
        ImportResponseVO response = service.importFiles(List.of(file("seq.md")), null, false, 1L);
        assertEquals(1, response.getAcceptedCount());
        Long after = currentSeqValue();
        assertTrue(after > before, "编码分配应推进数据库序列，before=" + before + " after=" + after);
    }

    /** 编码生成不再依赖全表扫描：Mapper 不得再暴露 selectUnitCodes 之类的全量取码方法。 */
    @Test
    void codeGenerationDoesNotFullScanUnitCodes() {
        for (Method method : KnowledgeUnitMapper.class.getDeclaredMethods()) {
            assertFalse("selectUnitCodes".equals(method.getName()),
                    "编码生成不得再全表扫描所有 U- 编码（应改用 knowledge_unit_code_seq）");
        }
    }

    /** 唯一索引冲突时只重试冲突单元（换号），不整批回滚。 */
    @Test
    void duplicateCodeRetriesOnlyThatUnit() {
        long next = currentSeqValue();
        // 预占序列下一个编码，制造一次冲突
        KnowledgeUnit blocker = new KnowledgeUnit();
        blocker.setUnitCode("U-" + next);
        blocker.setTitle("占用编码 " + next);
        blocker.setCategory("sop");
        blocker.setUseVlm(0);
        blocker.setStatus("active");
        blocker.setIngestStatus("active");
        unitMapper.insert(blocker);

        ImportResponseVO response = service.importFiles(List.of(file("collide.md")), null, false, 1L);

        assertEquals(1, response.getAcceptedCount(), "冲突应只重试冲突单元，不应整批回滚");
        String assigned = jdbcTemplate.queryForObject(
                "SELECT unit_code FROM knowledge_units WHERE id = ?", String.class,
                response.getItems().get(0).getUnitId());
        assertNotEquals("U-" + next, assigned, "冲突后应换用下一个编码");
    }

    /**
     * R-03 纵深防御：序列指向墓碑编码时，分配必须跳过墓碑，绝不复用（否则新单元向量会被删除补偿误删）。
     */
    @Test
    void allocationSkipsTombstonedCode() {
        long tombstoneValue = 900001L;
        jdbcTemplate.update("UPDATE knowledge_unit_code_seq SET next_value = ? WHERE seq_name = 'unit_code'",
                tombstoneValue);
        jdbcTemplate.update("INSERT INTO knowledge_unit_tombstones(unit_id, unit_code, deleted_at, last_swept_at) "
                + "VALUES(?, ?, CURRENT_TIMESTAMP, NULL)", 900000001L, "U-" + tombstoneValue);

        ImportResponseVO response = service.importFiles(List.of(file("tombstone.md")), null, false, 1L);

        assertEquals(1, response.getAcceptedCount(), "命中墓碑应换号，不应整批回滚");
        String assigned = jdbcTemplate.queryForObject(
                "SELECT unit_code FROM knowledge_units WHERE id = ?", String.class,
                response.getItems().get(0).getUnitId());
        assertNotEquals("U-" + tombstoneValue, assigned, "不得复用墓碑编码");
        assertEquals("U-" + (tombstoneValue + 1), assigned, "应换到下一个非墓碑编码");
    }

    /**
     * R-03 纵深防御：连续多枚墓碑（超过旧的共用 3 次上限）仍能逐个跳过并成功分配，不整批失败。
     */
    @Test
    void allocationSkipsConsecutiveTombstonesWithinBound() {
        long base = 910001L;
        jdbcTemplate.update("UPDATE knowledge_unit_code_seq SET next_value = ? WHERE seq_name = 'unit_code'", base);
        for (int i = 0; i < 3; i++) {
            jdbcTemplate.update(
                    "INSERT INTO knowledge_unit_tombstones(unit_id, unit_code, deleted_at, last_swept_at) "
                            + "VALUES(?, ?, CURRENT_TIMESTAMP, NULL)", 910000000L + i, "U-" + (base + i));
        }

        ImportResponseVO response = service.importFiles(List.of(file("dense-tombstone.md")), null, false, 1L);

        assertEquals(1, response.getAcceptedCount(), "连续墓碑应逐个跳过且不整批失败");
        String assigned = jdbcTemplate.queryForObject(
                "SELECT unit_code FROM knowledge_units WHERE id = ?", String.class,
                response.getItems().get(0).getUnitId());
        assertEquals("U-" + (base + 3), assigned, "应跳过连续墓碑后分配下一个可用编码");
    }

    /** 无墓碑命中时分配不受影响（行为/性能不回归）。 */
    @Test
    void allocationUnaffectedWhenNoTombstoneForCandidate() {
        long before = currentSeqValue();
        ImportResponseVO response = service.importFiles(List.of(file("normal-alloc.md")), null, false, 1L);

        assertEquals(1, response.getAcceptedCount());
        String assigned = jdbcTemplate.queryForObject(
                "SELECT unit_code FROM knowledge_units WHERE id = ?", String.class,
                response.getItems().get(0).getUnitId());
        assertEquals("U-" + before, assigned, "无墓碑时应按序列正常分配，不受墓碑检查影响");
    }

    private Long currentSeqValue() {
        return jdbcTemplate.queryForObject(
                "SELECT next_value FROM knowledge_unit_code_seq WHERE seq_name = 'unit_code'", Long.class);
    }

    private MockMultipartFile file(String name) {
        return new MockMultipartFile("files", name, "text/markdown",
                ("# " + name + "\n\n正文").getBytes(StandardCharsets.UTF_8));
    }
}

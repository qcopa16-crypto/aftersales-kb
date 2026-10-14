package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.knowledge.dto.BatchDetailVO;
import com.shuidun.kb.knowledge.dto.ImportResponseVO;
import com.shuidun.kb.knowledge.service.KnowledgeImportService;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.annotation.DirtiesContext;

/**
 * R-15 回归：批次 ID 唯一化，且批次详情只能读到本批次自己的 unit。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_batchid_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}",
        "app.ingest.worker.enabled=false",
        "app.upload.max-concurrent-requests=64"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeBatchIdTest {

    @Autowired
    private KnowledgeImportService service;

    /** 顺序导入大量批次：批次 ID 互不重复。 */
    @Test
    void batchIdsAreUniqueAcrossImports() {
        Set<String> ids = new HashSet<>();
        for (int i = 0; i < 30; i++) {
            ImportResponseVO response = service.importFiles(List.of(file("seq-" + i + ".md")), null, false, 1L);
            assertTrue(ids.add(response.getBatchId()), "批次 ID 重复: " + response.getBatchId());
        }
    }

    /** 并发生成批次 ID：随机后缀无碰撞（直接压测私有生成逻辑）。 */
    @Test
    void concurrentBatchIdGenerationHasNoCollision() throws Exception {
        Method nextBatchId = KnowledgeImportService.class.getDeclaredMethod("nextBatchId");
        nextBatchId.setAccessible(true);

        int threads = 8;
        int perThread = 500;
        Set<String> ids = ConcurrentHashMap.newKeySet();
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch start = new CountDownLatch(1);
        List<Future<?>> futures = new ArrayList<>();
        try {
            for (int t = 0; t < threads; t++) {
                futures.add(pool.submit(() -> {
                    try {
                        start.await();
                        for (int i = 0; i < perThread; i++) {
                            ids.add((String) nextBatchId.invoke(service));
                        }
                    } catch (Exception ex) {
                        throw new IllegalStateException(ex);
                    }
                }));
            }
            start.countDown();
            for (Future<?> future : futures) {
                future.get(30, TimeUnit.SECONDS);
            }
        } finally {
            pool.shutdownNow();
        }
        assertEquals(threads * perThread, ids.size(), "并发生成的批次 ID 不得重复");
    }

    /** 批次详情只返回该批次自己的 unit。 */
    @Test
    void batchDetailOnlyReturnsOwnUnits() {
        ImportResponseVO first = service.importFiles(List.of(file("one.md")), null, false, 1L);
        ImportResponseVO second = service.importFiles(List.of(file("two.md")), null, false, 1L);

        CurrentUser admin = new CurrentUser(1L, "admin", "系统管理员", 1L,
                List.of("admin"), Set.of("knowledge:perm"));

        BatchDetailVO detail1 = service.batchDetail(admin, first.getBatchId());
        assertEquals(1, detail1.getFiles().size());
        assertEquals(first.getItems().get(0).getUnitId(), detail1.getFiles().get(0).getUnitId());

        BatchDetailVO detail2 = service.batchDetail(admin, second.getBatchId());
        assertEquals(1, detail2.getFiles().size());
        assertEquals(second.getItems().get(0).getUnitId(), detail2.getFiles().get(0).getUnitId());
    }

    private MockMultipartFile file(String name) {
        return new MockMultipartFile("files", name, "text/markdown",
                ("# " + name + "\n\n正文").getBytes(StandardCharsets.UTF_8));
    }
}

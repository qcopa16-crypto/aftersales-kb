package com.shuidun.kb.job;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.knowledge.FileStorageService;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.support.FakeAiClient;
import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;

/**
 * P7 入库 Worker「领取幂等 + 延迟重排」测试（H2 + 真实 Spring/MyBatis，手动驱动 poll）。
 *
 * <p>退避基数放大到 60s，用于验证可重试失败走「延迟重排」而非阻塞/立即重试。
 *
 * <p><b>独立 H2 库隔离</b>：本类开启了后台 {@code @Scheduled} Worker（{@code worker.enabled=true}），
 * 其启动时会立即 poll 一次；若与其它测试类共享 {@code aftersales_kb} 库，其它用例遗留的 queued 作业
 * 会被后台 poll 抢占，或因 {@code pick(QUEUED, 3)} 的条数上限挤掉本用例的作业，导致与执行顺序相关的偶发失败。
 * 故本类改用独立库 {@code aftersales_kb_worker_isolated}（{@code data-h2.sql} 不预置任何作业），
 * 使其不再受其它测试类遗留数据影响。
 *
 * <p>数据源 URL 可覆盖：{@code -Dtest.datasource.url=...}（或环境变量 {@code TEST_DATASOURCE_URL}）
 * 未设置时保持原 {@code jdbc:h2:mem:aftersales_kb_worker_isolated} 默认值，离线自包含可跑；
 * mysql profile 下可用其指向真实库（driver/凭据由 application-mysql.yaml 提供）。
 */
@SpringBootTest(properties = {
        "app.ingest.worker.enabled=true",
        "app.ingest.worker.interval-ms=3600000",
        "app.ingest.worker.retry-backoff-ms=60000",
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_worker_isolated;MODE=MySQL;"
                + "DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}"
})
class IngestWorkerIdempotencyTest {

    @TestConfiguration
    static class FakeAiConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }
    }

    @Autowired
    private IngestWorker ingestWorker;

    @Autowired
    private IngestJobService ingestJobService;

    @Autowired
    private IngestJobMapper ingestJobMapper;

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private FileStorageService fileStorageService;

    @Autowired
    private FakeAiClient fakeAiClient;

    @BeforeEach
    void resetFake() {
        fakeAiClient.failParse = false;
        fakeAiClient.parseFailTimes.set(0);
        fakeAiClient.suggestedCategory = "sop";
    }

    /** 同一作业重复领取：乐观锁 + 租约保证只有第一次成功，第二次影响行数为 0。 */
    @Test
    void duplicateClaimOfSameJobOnlyWinsOnce() {
        IngestJob job = ingestJobService.enqueue("B-CLAIM-" + System.nanoTime(), 999999L, 1L);
        LocalDateTime lease = LocalDateTime.now().plusMinutes(20);

        assertEquals(1, ingestJobMapper.claim(job.getId(), IngestJobService.STAGE_QUEUED,
                IngestJobService.STAGE_CLAIMED, "worker-test", lease), "首次领取应成功");
        assertEquals(0, ingestJobMapper.claim(job.getId(), IngestJobService.STAGE_QUEUED,
                IngestJobService.STAGE_CLAIMED, "worker-test", lease), "同一作业重复领取必须失败（阶段已变）");
    }

    /** 多线程并发轮询同一作业：只有一次真正执行 parse，且最终完成入库。 */
    @Test
    void concurrentPollExecutesSameJobOnlyOnce() throws Exception {
        KnowledgeUnit unit = newUnit("并发领取");
        String code = unit.getUnitCode();

        int threads = 3;
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            for (int i = 0; i < threads; i++) {
                pool.submit(() -> {
                    try {
                        start.await();
                        ingestWorker.poll();
                    } catch (InterruptedException ex) {
                        Thread.currentThread().interrupt();
                    } finally {
                        done.countDown();
                    }
                });
            }
            start.countDown();
            assertTrue(done.await(30, TimeUnit.SECONDS), "并发轮询应在超时内结束");
        } finally {
            pool.shutdownNow();
        }

        assertEquals(1, parseCount(code), "同一作业被并发轮询时只能执行一次 parse");
        assertEquals("active", unitMapper.selectById(unit.getId()).getIngestStatus(), "作业应正常完成入库");
    }

    /** 可重试失败走「延迟重排」：作业回到 queued 且在退避窗口内不会被立即重试。 */
    @Test
    void retryableFailureIsDelayedNotRetriedImmediately() {
        fakeAiClient.parseFailTimes.set(1);
        KnowledgeUnit unit = newUnit("延迟重排");
        String code = unit.getUnitCode();

        ingestWorker.poll();
        assertEquals(1, parseCount(code), "首次轮询应尝试 parse 一次");

        // 退避 60s 内再次轮询：作业已回到 queued 但仍在窗口内，不得立即重试（且不阻塞线程）
        ingestWorker.poll();
        assertEquals(1, parseCount(code), "退避窗口内不得立即重试");
        assertEquals("queued", ingestJobMapper.selectLatestByUnitId(unit.getId()).getStage(),
                "可重试失败后作业应回到 queued 等待下次可见");

        // 清理：避免把可领取作业残留给共享 H2 库中的其它用例
        ingestJobService.markFailed(ingestJobMapper.selectLatestByUnitId(unit.getId()),
                "TEST_CLEANUP", "测试清理");
    }

    private int parseCount(String code) {
        AtomicInteger counter = fakeAiClient.parseCallsByUnit.get(code);
        return counter == null ? 0 : counter.get();
    }

    private KnowledgeUnit newUnit(String title) {
        long nanos = System.nanoTime();
        byte[] content = ("# " + title + "\n\n正文内容").getBytes(StandardCharsets.UTF_8);
        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setUnitCode("U-TEST-" + nanos);
        unit.setTitle(title + nanos);
        unit.setCategory("sop");
        unit.setSourceFileName("t.md");
        unit.setFileType("md");
        // R-04：file_size 必须与实际存储长度一致，否则 fail-closed 拒绝解析
        unit.setFileSize((long) content.length);
        unit.setUseVlm(0);
        unit.setStatus("active");
        unit.setIngestStatus("queued");
        unit.setBatchId("B-WORKER-" + nanos);
        unit.setCreatorId(1L);
        unitMapper.insert(unit);
        String path = fileStorageService.save(unit.getBatchId(), unit.getId(), "t.md", content);
        unitMapper.updateStoragePath(unit.getId(), path);
        ingestJobService.enqueue(unit.getBatchId(), unit.getId(), 1L);
        return unitMapper.selectById(unit.getId());
    }
}

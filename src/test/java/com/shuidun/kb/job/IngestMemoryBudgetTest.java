package com.shuidun.kb.job;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.knowledge.FileStorageService;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.support.FakeAiClient;
import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;
import java.util.Arrays;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.test.annotation.DirtiesContext;

/**
 * R-02 / R-04 回归：入库解析前的内存准入（fail-closed）。
 *
 * <p>配置单文件硬上限 1024 字节、内存预算 2048 字节、放大系数 2 → 实际最大可接受单文件 1024 字节。
 *
 * <p><b>R-04 关键变更：</b>准入以「实际存储对象长度」为唯一依据，DB {@code file_size} 仅作一致性检查。
 * 因此：file_size 缺失/为 0、DB 大小与实际不符、实际大小无法确认，都必须 fail-closed 拒绝解析，
 * 绝不返回「无需预算」；超限文件在 claim 之前判失败且绝不调用 parse（不得把整文件读入堆）。
 */
@SpringBootTest(properties = {
        "app.ingest.worker.enabled=true",
        "app.ingest.worker.interval-ms=3600000",
        "app.ingest.worker.max-parse-file-bytes=1024",
        "app.ingest.worker.memory-budget-bytes=2048",
        "app.ingest.worker.parse-heap-factor=2",
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_mem_budget_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class IngestMemoryBudgetTest {

    /** 硬上限 / 实际可接受上限（字节）：min(1024, 2048/2) = 1024。 */
    private static final long CAP_BYTES = 1024L;

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
        fakeAiClient.parseCalls.set(0);
    }

    /** 超过单文件内存上限（实际文件确实超限、DB 一致）：claim 前判失败，绝不进入 parse。 */
    @Test
    void oversizedFileRejectedBeforeParse() {
        byte[] content = new byte[(int) (CAP_BYTES + 500)];
        Arrays.fill(content, (byte) 'a');
        KnowledgeUnit unit = newUnit("超大文件", content, (long) content.length);

        ingestWorker.poll();

        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());
        assertEquals("failed", job.getStage(), "超限作业应被判失败");
        assertEquals("FILE_TOO_LARGE_FOR_PARSE", job.getErrorCode());
        assertEquals("failed", unitMapper.selectById(unit.getId()).getIngestStatus());
        assertEquals(0, fakeAiClient.parseCalls.get(), "超限文件不得进入 parse（不得读入堆）");
    }

    /** 在上限内且 DB file_size 与实际一致：正常解析并完成入库。 */
    @Test
    void withinBudgetFileParsedNormally() {
        byte[] content = ("# 正常文件\n\n正文内容").getBytes(StandardCharsets.UTF_8);
        KnowledgeUnit unit = newUnit("正常文件", content, (long) content.length);

        ingestWorker.poll();

        assertTrue(fakeAiClient.parseCalls.get() >= 1, "预算内文件应正常进入 parse");
        assertEquals("active", unitMapper.selectById(unit.getId()).getIngestStatus(), "sop 应自动通过并完成入库");
    }

    /** R-04：DB file_size 缺失（null）→ fail-closed 拒绝解析，绝不进入 parse。 */
    @Test
    void missingFileSizeMetadataRejectedBeforeParse() {
        byte[] content = ("# 无元数据\n\n正文").getBytes(StandardCharsets.UTF_8);
        KnowledgeUnit unit = newUnit("无元数据", content, null);

        ingestWorker.poll();

        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());
        assertEquals("failed", job.getStage());
        assertEquals("FILE_SIZE_METADATA_MISSING", job.getErrorCode());
        assertEquals("failed", unitMapper.selectById(unit.getId()).getIngestStatus());
        assertEquals(0, fakeAiClient.parseCalls.get(), "元数据缺失不得进入 parse（fail-closed）");
    }

    /** R-04：DB file_size 为 0 → fail-closed 拒绝解析。 */
    @Test
    void zeroFileSizeMetadataRejectedBeforeParse() {
        byte[] content = ("# 零元数据\n\n正文").getBytes(StandardCharsets.UTF_8);
        KnowledgeUnit unit = newUnit("零元数据", content, 0L);

        ingestWorker.poll();

        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());
        assertEquals("failed", job.getStage());
        assertEquals("FILE_SIZE_METADATA_MISSING", job.getErrorCode());
        assertEquals(0, fakeAiClient.parseCalls.get(), "元数据为 0 不得进入 parse（fail-closed）");
    }

    /** R-04：DB file_size 与实际存储长度不一致 → 报警并阻止解析。 */
    @Test
    void fileSizeMismatchRejectedBeforeParse() {
        byte[] content = ("# 大小不符\n\n正文").getBytes(StandardCharsets.UTF_8);
        // 声明一个与真实长度不同的值（且未超限），必须因不一致被阻止
        KnowledgeUnit unit = newUnit("大小不符", content, (long) content.length + 1);

        ingestWorker.poll();

        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());
        assertEquals("failed", job.getStage());
        assertEquals("FILE_SIZE_MISMATCH", job.getErrorCode());
        assertEquals("failed", unitMapper.selectById(unit.getId()).getIngestStatus());
        assertEquals(0, fakeAiClient.parseCalls.get(), "大小不一致不得进入 parse");
    }

    /** R-04：存储路径缺失 → fail-closed 拒绝解析。 */
    @Test
    void missingStoragePathRejectedBeforeParse() {
        KnowledgeUnit unit = newUnitWithoutFile("缺路径", null);

        ingestWorker.poll();

        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());
        assertEquals("failed", job.getStage());
        assertEquals("STORAGE_PATH_MISSING", job.getErrorCode());
        assertEquals(0, fakeAiClient.parseCalls.get(), "无存储路径不得进入 parse（fail-closed）");
    }

    /** R-04：实际存储对象无法确认大小（文件缺失）→ fail-closed 拒绝解析。 */
    @Test
    void unverifiableStorageSizeRejectedBeforeParse() {
        KnowledgeUnit unit = newUnitWithoutFile("文件缺失", "no-such-dir/no-such-file.md");

        ingestWorker.poll();

        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());
        assertEquals("failed", job.getStage());
        assertEquals("FILE_SIZE_UNKNOWN", job.getErrorCode());
        assertEquals(0, fakeAiClient.parseCalls.get(), "无法确认大小不得进入 parse（fail-closed）");
    }

    /**
     * R-04 守卫写：作业已被其他节点 claim 时，准入拒绝不得跨 owner 覆盖（{@code stage='queued'} CAS 影响 0 行）。
     */
    @Test
    void admissionRejectDoesNotOverrideClaimedJob() {
        byte[] content = ("# 被抢占\n\n正文").getBytes(StandardCharsets.UTF_8);
        KnowledgeUnit unit = newUnit("被抢占", content, (long) content.length + 1);
        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());
        // 模拟另一节点已 claim 该作业（stage 变为 claimed）
        int claimed = ingestJobMapper.claim(job.getId(), IngestJobService.STAGE_QUEUED,
                IngestJobService.STAGE_CLAIMED, "node-b", LocalDateTime.now().plusMinutes(5));
        assertEquals(1, claimed, "预置：应成功 claim");

        boolean rejected = ingestJobService.markFailedIfQueued(job, "FILE_SIZE_MISMATCH", "不应覆盖");

        assertFalse(rejected, "已 claim 的作业不得被准入拒绝覆盖");
        IngestJob after = ingestJobMapper.selectLatestByUnitId(unit.getId());
        assertEquals(IngestJobService.STAGE_CLAIMED, after.getStage(), "作业应保持 claimed，不被置 failed");
        assertNull(after.getErrorCode(), "不得写入错误码");
        assertEquals("queued", unitMapper.selectById(unit.getId()).getIngestStatus(),
                "单元状态不得被跨 owner 拒绝改为 failed");
    }

    /** R-04 守卫写：作业仍为 queued 时，准入拒绝正常生效。 */
    @Test
    void admissionRejectAppliesToQueuedJob() {
        byte[] content = ("# 待拒绝\n\n正文").getBytes(StandardCharsets.UTF_8);
        KnowledgeUnit unit = newUnit("待拒绝", content, (long) content.length + 1);
        IngestJob job = ingestJobMapper.selectLatestByUnitId(unit.getId());

        boolean rejected = ingestJobService.markFailedIfQueued(job, "FILE_SIZE_MISMATCH", "拒绝");

        assertTrue(rejected, "queued 作业应被准入拒绝置失败");
        assertEquals("failed", ingestJobMapper.selectLatestByUnitId(unit.getId()).getStage());
    }

    /**
     * 创建 unit 并落盘内容；{@code declaredSize} 为写入 DB 的 file_size（可为 null，用于验证缺失场景）。
     */
    private KnowledgeUnit newUnit(String title, byte[] content, Long declaredSize) {
        long nanos = System.nanoTime();
        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setUnitCode("U-MEM-" + nanos);
        unit.setTitle(title + nanos);
        unit.setCategory("sop");
        unit.setSourceFileName("t.md");
        unit.setFileType("md");
        unit.setFileSize(declaredSize);
        unit.setUseVlm(0);
        unit.setStatus("active");
        unit.setIngestStatus("queued");
        unit.setBatchId("B-MEM-" + nanos);
        unit.setCreatorId(1L);
        unitMapper.insert(unit);
        String path = fileStorageService.save(unit.getBatchId(), unit.getId(), "t.md", content);
        unitMapper.updateStoragePath(unit.getId(), path);
        ingestJobService.enqueue(unit.getBatchId(), unit.getId(), 1L);
        return unitMapper.selectById(unit.getId());
    }

    /** 创建 unit 但 storage_path 置为给定值（可为 null 或指向不存在文件）。 */
    private KnowledgeUnit newUnitWithoutFile(String title, String storagePath) {
        long nanos = System.nanoTime();
        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setUnitCode("U-MEM-" + nanos);
        unit.setTitle(title + nanos);
        unit.setCategory("sop");
        unit.setSourceFileName("t.md");
        unit.setFileType("md");
        unit.setFileSize(100L);
        unit.setUseVlm(0);
        unit.setStatus("active");
        unit.setIngestStatus("queued");
        unit.setBatchId("B-MEM-" + nanos);
        unit.setCreatorId(1L);
        unitMapper.insert(unit);
        if (storagePath != null) {
            unitMapper.updateStoragePath(unit.getId(), storagePath);
        }
        ingestJobService.enqueue(unit.getBatchId(), unit.getId(), 1L);
        return unitMapper.selectById(unit.getId());
    }
}

package com.shuidun.kb.job;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.shuidun.kb.job.entity.IngestJob;
import java.time.LocalDateTime;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.annotation.DirtiesContext;

/**
 * R-01 回归：入库作业所有状态迁移都必须以 owner + 有效租约 CAS 为前提。
 *
 * <p>场景：worker A 的租约到期后，worker B 回收并领取同一 job；A 随后的
 * advance / markFailed / markDone / resetForRetry 必须全部失败（LeaseLost），且数据库保持 B 的状态。
 *
 * <p><b>独立 H2 库隔离</b>：避免与其它测试类共享库产生数据干扰。
 */
@SpringBootTest(properties = {
        "app.ingest.worker.enabled=false",
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_lease_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class IngestJobLeaseCasTest {

    @Autowired
    private IngestJobService jobService;

    @Autowired
    private IngestJobMapper jobMapper;

    /** A 租约过期 → B 回收并领取；A 的 advance / markFailed / markDone / resetForRetry 必须全部 LeaseLost。 */
    @Test
    void staleOwnerCannotWriteAfterNewOwnerClaims() {
        IngestJob job = jobService.enqueue("B-LEASE-" + System.nanoTime(), 900001L, 1L);
        // A 领取但租约已过期
        assertEquals(1, jobMapper.claim(job.getId(), IngestJobService.STAGE_QUEUED,
                IngestJobService.STAGE_CLAIMED, "worker-A", LocalDateTime.now().minusMinutes(1)));
        // 回收 → B 领取
        assertEquals(1, jobMapper.reclaimExpiredLease(job.getId(), LocalDateTime.now()));
        LocalDateTime leaseB = LocalDateTime.now().plusMinutes(20);
        assertEquals(1, jobMapper.claim(job.getId(), IngestJobService.STAGE_QUEUED,
                IngestJobService.STAGE_CLAIMED, "worker-B", leaseB));

        assertThrows(IngestJobService.LeaseLostException.class, () -> jobService.advance(
                job, IngestJobService.STAGE_PARSE, 10, "worker-A", LocalDateTime.now().plusMinutes(20)),
                "旧 owner 不得推进阶段");
        assertThrows(IngestJobService.LeaseLostException.class, () -> jobService.markFailed(
                job, "STALE", "旧 owner 写入", "worker-A"), "旧 owner 不得标记失败");
        assertThrows(IngestJobService.LeaseLostException.class, () -> jobService.markDone(job, "worker-A"),
                "旧 owner 不得标记完成");
        assertThrows(IngestJobService.LeaseLostException.class, () -> jobService.resetForRetry(
                job, IngestJobService.STAGE_QUEUED, null, "worker-A"), "旧 owner 不得重排重试");

        IngestJob after = jobMapper.selectLatestByUnitId(900001L);
        assertEquals(IngestJobService.STAGE_CLAIMED, after.getStage(), "库内必须保持 B 的阶段");
        assertEquals("worker-B", after.getClaimedBy(), "库内 owner 必须仍是 B");
        assertNull(after.getErrorCode(), "旧 worker 不得写入错误态");
    }

    /** 当前 owner 的推进与终态成功，且终态在同一语句清空租约。 */
    @Test
    void currentOwnerTransitionsSucceedAndTerminalClearsLease() {
        IngestJob job = jobService.enqueue("B-LEASE-OK-" + System.nanoTime(), 900002L, 1L);
        assertEquals(1, jobMapper.claim(job.getId(), IngestJobService.STAGE_QUEUED,
                IngestJobService.STAGE_CLAIMED, "worker-C", LocalDateTime.now().plusMinutes(20)));

        jobService.advance(job, IngestJobService.STAGE_PARSE, 10, "worker-C", LocalDateTime.now().plusMinutes(20));
        IngestJob mid = jobMapper.selectLatestByUnitId(900002L);
        assertEquals(IngestJobService.STAGE_PARSE, mid.getStage());
        assertEquals("worker-C", mid.getClaimedBy());

        jobService.markDone(job, "worker-C");
        IngestJob done = jobMapper.selectLatestByUnitId(900002L);
        assertEquals(IngestJobService.STAGE_DONE, done.getStage());
        assertEquals("", done.getClaimedBy(), "终态必须清空 owner");
        assertNull(done.getLeaseUntil(), "终态必须清空租约");
    }

    /** 租约已过期（即使尚未被回收）时 owner 也不得写入。 */
    @Test
    void ownerCasFailsWhenLeaseExpiredWithoutReclaim() {
        IngestJob job = jobService.enqueue("B-LEASE-EXP-" + System.nanoTime(), 900003L, 1L);
        assertEquals(1, jobMapper.claim(job.getId(), IngestJobService.STAGE_QUEUED,
                IngestJobService.STAGE_CLAIMED, "worker-A", LocalDateTime.now().minusSeconds(1)));
        assertThrows(IngestJobService.LeaseLostException.class, () -> jobService.advance(
                job, IngestJobService.STAGE_PARSE, 10, "worker-A", LocalDateTime.now().plusMinutes(20)));
    }
}

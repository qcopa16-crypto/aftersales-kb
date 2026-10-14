package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.knowledge.dto.DeleteItemResult;
import com.shuidun.kb.knowledge.entity.KnowledgeDeleteTask;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeDeleteTaskMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitTombstoneMapper;
import com.shuidun.kb.knowledge.service.KnowledgeDeleteService;
import com.shuidun.kb.support.FakeAiClient;
import java.time.LocalDateTime;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.annotation.DirtiesContext;

/**
 * R-03 回归：删除 outbox 的原子创建、reconcile 修复、消费者原子抢占、幂等 tombstone、
 * 失败任务人工重试入口。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_delete_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}",
        "app.ingest.worker.enabled=false",
        "app.knowledge.delete.max-retries=2",
        "app.knowledge.delete.retry-backoff-ms=0",
        // 关闭后台定时扫描的首次立即执行，避免与用例竞争
        "app.knowledge.delete.scan-interval-ms=3600000",
        "app.knowledge.delete.scan-initial-delay-ms=3600000",
        "app.knowledge.delete.sweep-interval-ms=3600000",
        "app.knowledge.delete.sweep-initial-delay-ms=3600000",
        "app.knowledge.delete.reconcile-interval-ms=3600000",
        "app.knowledge.delete.reconcile-initial-delay-ms=3600000"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeDeleteOutboxTest {

    @TestConfiguration
    static class FakeAiConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }
    }

    @Autowired
    private KnowledgeDeleteService deleteService;

    @Autowired
    private KnowledgeDeleteTaskMapper taskMapper;

    @Autowired
    private KnowledgeUnitTombstoneMapper tombstoneMapper;

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private FakeAiClient fakeAiClient;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    /** 与生产同一配置来源的重试上限，用于给显式驱动循环设定确定上界。 */
    @Value("${app.knowledge.delete.max-retries:20}")
    private int maxRetries;

    @BeforeEach
    void resetFake() {
        fakeAiClient.deleteFailTimes.set(0);
    }

    /** 正常路径：请求删除 → outbox task 创建 → 完成；tombstone 落库。 */
    @Test
    void deleteCreatesTaskAndCompletes() {
        KnowledgeUnit unit = newUnit("outbox-happy");
        DeleteItemResult result = deleteService.requestDelete(unit.getId());

        assertTrue(result.isSuccess(), result.getErrorMessage());
        assertNull(unitMapper.selectById(unit.getId()), "主数据应已删除");
        KnowledgeDeleteTask task = taskMapper.selectByUnitId(unit.getId());
        assertNotNull(task, "必须持久化删除任务");
        assertEquals(KnowledgeDeleteTask.STATUS_SUCCEEDED, task.getStatus());
        assertEquals(1, tombstoneCount(unit.getId()), "tombstone 应写入");
    }

    /** reconcile：deleting 但无 task 的单元应被补建任务，随后可自动完成。 */
    @Test
    void reconcileRepairsOrphanDeletingUnit() {
        KnowledgeUnit unit = newUnit("orphan-deleting");
        unitMapper.updateStatus(unit.getId(), "deleting");
        assertNull(taskMapper.selectByUnitId(unit.getId()), "前置条件：无任务");

        deleteService.reconcileDeletingWithoutTask();
        assertNotNull(taskMapper.selectByUnitId(unit.getId()), "reconcile 应补建删除任务");

        deleteService.scanDueTasks();
        assertNull(unitMapper.selectById(unit.getId()), "补建后应能自动完成删除");
        assertEquals(KnowledgeDeleteTask.STATUS_SUCCEEDED,
                taskMapper.selectByUnitId(unit.getId()).getStatus());
    }

    /** 消费者原子抢占：同一任务两个消费者并发只能一个成功。 */
    @Test
    void onlyOneConsumerCanClaimTask() {
        KnowledgeUnit unit = newUnit("claim-race");
        KnowledgeDeleteTask task = newTask(unit);
        LocalDateTime now = LocalDateTime.now();
        LocalDateTime lease = now.plusMinutes(5);

        assertEquals(1, taskMapper.claim(task.getId(), "consumer-A", "tokA", lease, now));
        assertEquals(0, taskMapper.claim(task.getId(), "consumer-B", "tokB", lease, now),
                "已被 A 持有有效租约，B 不得抢占");
        assertEquals("consumer-A", taskMapper.selectByUnitId(unit.getId()).getClaimedBy());
    }

    /** 幂等：tombstone 已存在（模拟 tombstone 写入后进程中断）时，重试仍能完成。 */
    @Test
    void tombstoneInsertIsIdempotent() {
        KnowledgeUnit unit = newUnit("tombstone-idem");
        tombstoneMapper.insert(unit.getId(), unit.getUnitCode());

        DeleteItemResult result = deleteService.requestDelete(unit.getId());
        assertTrue(result.isSuccess(), "唯一冲突必须被忽略，任务应能完成");
        assertNull(unitMapper.selectById(unit.getId()));
        assertEquals(1, tombstoneCount(unit.getId()));
    }

    /** 失败任务经管理端重试入口恢复并完成。 */
    @Test
    void failedTaskCanBeRetried() {
        KnowledgeUnit unit = newUnit("retry-failed");
        fakeAiClient.deleteFailTimes.set(1000);

        DeleteItemResult first = deleteService.requestDelete(unit.getId());
        assertFalse(first.isSuccess(), "首次删除应因向量删除失败而未成功");

        // 显式驱动补偿扫描：每轮先把该行置为「可立即领取」（status=pending、退避已过期、无租约/owner/token），
        // 再触发扫描。这样不依赖 wall-clock 退避窗口与 claimLeaseMs 租约窗口——避免任务被一次未过期的
        // 处理租约遮蔽而让 selectDue 空转（这是本用例此前偶发 pending 的根因）。
        // 循环上界取配置的重试上限 + 余量，保证「达到上限落 failed」在有限轮内必然发生。
        int maxAttempts = Math.max(maxRetries, 1) + 3;
        for (int i = 0; i < maxAttempts; i++) {
            jdbcTemplate.update(
                    "UPDATE knowledge_delete_tasks SET status = 'pending', next_retry_at = ?, "
                            + "lease_until = NULL, claimed_by = '', claim_token = '' WHERE unit_id = ?",
                    LocalDateTime.now().minusMinutes(1), unit.getId());
            deleteService.scanDueTasks();
            if (KnowledgeDeleteTask.STATUS_FAILED.equals(taskMapper.selectByUnitId(unit.getId()).getStatus())) {
                break;
            }
        }
        KnowledgeDeleteTask failed = taskMapper.selectByUnitId(unit.getId());
        assertEquals(KnowledgeDeleteTask.STATUS_FAILED, failed.getStatus(),
                "超过重试上限应落 failed（retryCount=" + failed.getRetryCount() + "）");
        assertEquals("deleting", unitMapper.selectById(unit.getId()).getStatus(), "失败期间保持 deleting");

        fakeAiClient.deleteFailTimes.set(0);
        DeleteItemResult retried = deleteService.retryDeleteTask(unit.getId(), 1L);
        assertTrue(retried.isSuccess(), "人工重试后应完成");
        assertNull(unitMapper.selectById(unit.getId()));
    }

    /**
     * tombstone 重放失败必须保留待重试：失败轮不得写 last_swept_at，下一轮仍应被 selectSweepCandidates
     * 命中；上游恢复后重放成功才标记已扫描。
     */
    @Test
    void tombstoneSweepFailureIsRetriedNextRound() {
        // 清空既有墓碑，保证本用例的墓碑是唯一扫描候选（batchSize 有限，避免被更早 id 遮蔽）
        jdbcTemplate.update("DELETE FROM knowledge_unit_tombstones");
        KnowledgeUnit unit = newUnit("tombstone-sweep-retry");
        tombstoneMapper.insert(unit.getId(), unit.getUnitCode());
        assertNull(lastSweptAt(unit.getId()), "前置条件：墓碑未被扫描");
        assertTrue(isSweepCandidate(unit.getId()), "前置条件：未扫描墓碑应被命中");

        // 第一轮：向量删除失败 → 不得 markSwept，墓碑保留待重试
        fakeAiClient.deleteFailTimes.set(1000);
        deleteService.sweepTombstones();
        assertNull(lastSweptAt(unit.getId()), "重放失败不得写入 last_swept_at");
        assertTrue(isSweepCandidate(unit.getId()), "重放失败的墓碑下一轮仍应被扫描到");

        // 第二轮：上游恢复 → 重放成功并标记已扫描
        fakeAiClient.deleteFailTimes.set(0);
        deleteService.sweepTombstones();
        assertNotNull(lastSweptAt(unit.getId()), "重放成功后应写入 last_swept_at");
        assertFalse(isSweepCandidate(unit.getId()), "已成功重放的墓碑不应再被扫描到");
    }

    private LocalDateTime lastSweptAt(Long unitId) {
        return jdbcTemplate.queryForObject(
                "SELECT last_swept_at FROM knowledge_unit_tombstones WHERE unit_id = ?",
                LocalDateTime.class, unitId);
    }

    /** 以生产同一截止口径（now - sweep-interval，用例中 3600000ms）判断墓碑是否仍为扫描候选。 */
    private boolean isSweepCandidate(Long unitId) {
        LocalDateTime cutoff = LocalDateTime.now().minusHours(1);
        return tombstoneMapper.selectSweepCandidates(cutoff, 100).stream()
                .anyMatch(t -> unitId.equals(t.getUnitId()));
    }

    private int tombstoneCount(Long unitId) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM knowledge_unit_tombstones WHERE unit_id = ?", Integer.class, unitId);
        return count == null ? 0 : count;
    }

    private KnowledgeDeleteTask newTask(KnowledgeUnit unit) {
        KnowledgeDeleteTask task = new KnowledgeDeleteTask();
        task.setUnitId(unit.getId());
        task.setUnitCode(unit.getUnitCode());
        task.setStatus(KnowledgeDeleteTask.STATUS_PENDING);
        task.setRetryCount(0);
        taskMapper.insert(task);
        return task;
    }

    private KnowledgeUnit newUnit(String title) {
        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setUnitCode("U-DEL-" + System.nanoTime());
        unit.setTitle(title);
        unit.setCategory("sop");
        unit.setStatus("active");
        unit.setIngestStatus("active");
        unit.setBatchId("B-DEL-" + System.nanoTime());
        unit.setUseVlm(0);
        unitMapper.insert(unit);
        return unitMapper.selectById(unit.getId());
    }
}

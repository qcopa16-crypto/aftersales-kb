package com.shuidun.kb.knowledge.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.zaxxer.hikari.HikariDataSource;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.datasource.DriverManagerDataSource;

/**
 * R-03 连接池预算判据单测（纯函数，不启 Spring）。
 *
 * <p>背景：序列预留运行在独立 REQUIRES_NEW 事务中，峰值每个并发导入占用 2 个连接；
 * 且池为共享资源（worker/健康检查/管理 API 同池），故要求池上限 ≥ 2×并发 + 余量（余量=2）。
 * 此处覆盖判据边界：<b>恰好 2×并发也判不足</b>（零余量不安全），以及并发下限兜底为 1。
 */
class UploadConnectionPoolBudgetTest {

    /** 池上限严格小于 2×并发 → 判定不足。 */
    @Test
    void poolBelowTwiceConcurrencyIsInsufficient() {
        assertTrue(KnowledgeImportService.isConnectionPoolBudgetInsufficient(7, 4));
        assertTrue(KnowledgeImportService.isConnectionPoolBudgetInsufficient(1, 4));
    }

    /** 池上限恰为 2×并发（零余量）→ 仍判不足（共享池，零余量不安全）。 */
    @Test
    void poolExactlyTwiceConcurrencyIsInsufficient() {
        assertTrue(KnowledgeImportService.isConnectionPoolBudgetInsufficient(8, 4));
    }

    /** 池上限为 2×并发 + 1（余量仅 1）→ 仍判不足。 */
    @Test
    void poolWithSingleSlotHeadroomIsInsufficient() {
        assertTrue(KnowledgeImportService.isConnectionPoolBudgetInsufficient(9, 4));
    }

    /** 池上限为 2×并发 + 余量 2（默认 pool=10 / 并发 4）→ 满足。 */
    @Test
    void poolWithFullHeadroomIsSufficient() {
        assertFalse(KnowledgeImportService.isConnectionPoolBudgetInsufficient(10, 4));
        assertFalse(KnowledgeImportService.isConnectionPoolBudgetInsufficient(12, 4));
    }

    /** 并发下限兜底为 1：并发 0 时等价于「至少需 2×1 + 余量 2 = 4 个连接」。 */
    @Test
    void zeroConcurrencyTreatedAsOne() {
        assertTrue(KnowledgeImportService.isConnectionPoolBudgetInsufficient(3, 0));
        assertTrue(KnowledgeImportService.isConnectionPoolBudgetInsufficient(2, 0));
        assertFalse(KnowledgeImportService.isConnectionPoolBudgetInsufficient(4, 0));
    }

    /** R-06：池容量充足时生效并发 = 配置并发（不误收敛）。 */
    @Test
    void effectiveConcurrencyKeepsConfiguredWhenPoolSufficient() {
        assertEquals(4, KnowledgeImportService.effectiveUploadConcurrency(10, 4));
        assertEquals(4, KnowledgeImportService.effectiveUploadConcurrency(64, 4));
        assertEquals(2, KnowledgeImportService.effectiveUploadConcurrency(10, 2));
    }

    /** R-06：池不足时生效并发收敛为 floor((pool - 余量)/2)。 */
    @Test
    void effectiveConcurrencyConvergesWhenPoolInsufficient() {
        // pool=8, configured=8 → floor((8-2)/2)=3
        assertEquals(3, KnowledgeImportService.effectiveUploadConcurrency(8, 8));
        // pool=10, configured=64 → floor((10-2)/2)=4
        assertEquals(4, KnowledgeImportService.effectiveUploadConcurrency(10, 64));
        // pool=5, configured=4 → floor((5-2)/2)=1
        assertEquals(1, KnowledgeImportService.effectiveUploadConcurrency(5, 4));
    }

    /** R-03：最小可支撑单许可的池上限（4）→ 生效并发恰为 1（不是 0，也不是危险 fallback）。 */
    @Test
    void effectiveConcurrencyIsOneAtMinimumSupportedPool() {
        assertEquals(1, KnowledgeImportService.effectiveUploadConcurrency(4, 4));
        assertEquals(1, KnowledgeImportService.effectiveUploadConcurrency(4, 64));
    }

    /**
     * R-03：池极小（< 2 + 余量）时生效并发为 0（<b>不再以 1 作为安全下限</b>）。
     *
     * <p>该情形由启动期 fail-closed 拦截，绝不进入运行态；此处仅固定纯函数语义。
     */
    @Test
    void effectiveConcurrencyIsZeroForUnsafePool() {
        assertEquals(0, KnowledgeImportService.effectiveUploadConcurrency(3, 4));
        assertEquals(0, KnowledgeImportService.effectiveUploadConcurrency(2, 4));
        assertEquals(0, KnowledgeImportService.effectiveUploadConcurrency(1, 1));
        assertEquals(0, KnowledgeImportService.effectiveUploadConcurrency(0, 4));
    }

    /** R-03：池太小判据（poolMax &lt; 2 + 余量 = 4）→ fail-closed。 */
    @Test
    void poolTooSmallForAnyPermitIsDetected() {
        assertTrue(KnowledgeImportService.isConnectionPoolTooSmallForAnyPermit(0));
        assertTrue(KnowledgeImportService.isConnectionPoolTooSmallForAnyPermit(1));
        assertTrue(KnowledgeImportService.isConnectionPoolTooSmallForAnyPermit(2));
        assertTrue(KnowledgeImportService.isConnectionPoolTooSmallForAnyPermit(3));
        assertFalse(KnowledgeImportService.isConnectionPoolTooSmallForAnyPermit(4));
        assertFalse(KnowledgeImportService.isConnectionPoolTooSmallForAnyPermit(10));
    }

    /** R-06：优先读取 Hikari 运行时的实际池上限（而非仅属性值）。 */
    @Test
    void effectivePoolMaxSizePrefersHikariActualValue() throws Exception {
        try (HikariDataSource hikari = new HikariDataSource()) {
            hikari.setMaximumPoolSize(3);
            assertEquals(3, KnowledgeImportService.resolveEffectivePoolMaxSize(hikari, 99),
                    "应优先取 Hikari 实际池上限 3，而非回退属性值 99");
        }
    }

    /** R-06：非 Hikari 数据源回退属性值。 */
    @Test
    void effectivePoolMaxSizeFallsBackForNonHikari() {
        DataSource nonHikari = new DriverManagerDataSource();
        assertEquals(7, KnowledgeImportService.resolveEffectivePoolMaxSize(nonHikari, 7));
    }

    /** R-06：dataSource 为 null 时回退属性值。 */
    @Test
    void effectivePoolMaxSizeFallsBackForNull() {
        assertEquals(5, KnowledgeImportService.resolveEffectivePoolMaxSize(null, 5));
    }
}

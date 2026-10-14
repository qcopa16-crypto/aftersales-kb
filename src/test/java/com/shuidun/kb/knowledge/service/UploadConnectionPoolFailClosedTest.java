package com.shuidun.kb.knowledge.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.zaxxer.hikari.HikariDataSource;
import java.lang.reflect.Field;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * R-03 连接池预算启动门禁回归（纯单元测试，不启 Spring）。
 *
 * <p>背景：序列预留走 REQUIRES_NEW，峰值每个导入占 2 个连接，另需固定余量 2，故池上限必须
 * {@code >= 4}。池太小时 {@code initUploadPermits()}（{@code @PostConstruct}）必须<b>启动失败</b>，
 * 不再以 1 作为危险的安全下限；池充足时按容量自动收敛，并通过
 * {@link KnowledgeImportService#uploadConcurrencySnapshot()} 公开 configured/effective/poolMax/pending。
 */
class UploadConnectionPoolFailClosedTest {

    /** R-03：池上限 2（< 4）→ 连一个导入许可都无法安全支撑 → 启动失败。 */
    @Test
    void tinyPoolFailsStartup() throws Exception {
        try (HikariDataSource hikari = new HikariDataSource()) {
            hikari.setMaximumPoolSize(2);
            KnowledgeImportService service = newService(hikari, 4, 2);
            assertThrows(IllegalStateException.class, service::initUploadPermits,
                    "池上限 2 < 4 必须启动失败（fail-closed），不得以 1 作为安全下限");
        }
    }

    /** R-03：池上限 3（< 4）→ 启动失败。 */
    @Test
    void poolOfThreeFailsStartup() throws Exception {
        try (HikariDataSource hikari = new HikariDataSource()) {
            hikari.setMaximumPoolSize(3);
            KnowledgeImportService service = newService(hikari, 4, 3);
            assertThrows(IllegalStateException.class, service::initUploadPermits, "池上限 3 < 4 必须启动失败");
        }
    }

    /** R-03：池上限 4（恰可支撑 1 个许可）→ 正常启动，生效并发收敛为 1。 */
    @Test
    void minimumPoolStartsWithOnePermit() throws Exception {
        try (HikariDataSource hikari = new HikariDataSource()) {
            hikari.setMaximumPoolSize(4);
            KnowledgeImportService service = newService(hikari, 4, 4);
            service.initUploadPermits();
            assertEquals(1, service.effectiveUploadConcurrency(), "pool=4 恰可支撑 1 个许可");
        }
    }

    /** R-03/R-06：池充足但配置并发过高 → 自动收敛，并公开 configured/effective/poolMax/pending。 */
    @Test
    void sufficientPoolConvergesAndExposesSnapshot() throws Exception {
        try (HikariDataSource hikari = new HikariDataSource()) {
            hikari.setMaximumPoolSize(10);
            KnowledgeImportService service = newService(hikari, 64, 10);
            service.initUploadPermits();
            assertEquals(4, service.effectiveUploadConcurrency(),
                    "pool=10、configured=64 应收敛为 floor((10-2)/2)=4");
            Map<String, Object> snapshot = service.uploadConcurrencySnapshot();
            assertEquals(64, snapshot.get("configured"));
            assertEquals(4, snapshot.get("effective"));
            assertEquals(10, snapshot.get("poolMax"));
            assertEquals(0L, snapshot.get("pending"));
        }
    }

    /**
     * 构造一个仅依赖 dataSource 的 {@link KnowledgeImportService} 实例（其余依赖传 null，本用例不触达）。
     * 通过反射注入配置项，等价于 Spring 的属性绑定。
     */
    private KnowledgeImportService newService(HikariDataSource dataSource, int concurrency, int poolProp)
            throws Exception {
        KnowledgeImportService service = new KnowledgeImportService(
                null, null, null, null, null, null, null, null, null, dataSource);
        setField(service, "maxConcurrentRequests", concurrency);
        setField(service, "connectionPoolMaxSize", poolProp);
        return service;
    }

    private static void setField(Object target, String name, Object value) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }
}

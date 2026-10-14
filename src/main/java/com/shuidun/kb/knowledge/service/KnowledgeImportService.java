package com.shuidun.kb.knowledge.service;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.auth.SecurityUtils;
import com.shuidun.kb.config.QualityReviewProperties;
import com.shuidun.kb.job.IngestJobService;
import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.job.entity.IngestReview;
import com.shuidun.kb.job.mapper.IngestReviewMapper;
import com.shuidun.kb.knowledge.FileStorageService;
import com.shuidun.kb.knowledge.KnowledgeEnums;
import com.shuidun.kb.knowledge.dto.BatchDetailVO;
import com.shuidun.kb.knowledge.dto.BatchFileVO;
import com.shuidun.kb.knowledge.dto.BatchSummaryVO;
import com.shuidun.kb.knowledge.dto.ImportItemVO;
import com.shuidun.kb.knowledge.dto.ImportResponseVO;
import com.shuidun.kb.knowledge.dto.QualityGateRequest;
import com.shuidun.kb.knowledge.dto.RetryRequest;
import com.shuidun.kb.knowledge.entity.KnowledgeChunk;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeChunkMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.permission.DataPermissionEngine;
import com.shuidun.kb.permission.UnitPermissionService;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.annotation.PostConstruct;
import java.io.IOException;
import java.io.InputStream;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Semaphore;
import javax.sql.DataSource;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.PessimisticLockingFailureException;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.multipart.MultipartFile;

/**
 * 知识导入与入库作业服务（DESIGN.md §6.2 / docs/api/api.md §3）。
 *
 * <p>上传接口只做：落存储 → 建 unit(draft/queued) → 建 job(queued) → 返回批次摘要；
 * 严禁在 HTTP 请求内同步 parse+embed。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class KnowledgeImportService {

    private static final int MAX_PAGE_SIZE = 100;
    private static final DateTimeFormatter DATE_FORMAT = DateTimeFormatter.ofPattern("yyyyMMdd");
    private static final int MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024;

    /** R-03 编码序列名。 */
    private static final String UNIT_CODE_SEQ_NAME = "unit_code";

    /** R-03 编码序列初始值：须高于内置种子编码 U-1001..U-1005；既有库由迁移按现有最大值回填。 */
    private static final long UNIT_CODE_SEQ_INITIAL = 1006L;

    /** R-03 单文件编码冲突（唯一索引）的最大重试次数：并发协调协议，保持小而明确。 */
    private static final int MAX_CODE_ASSIGN_ATTEMPTS = 3;

    /**
     * R-03 墓碑编码的最大连续跳过次数：与唯一索引冲突重试分开计数，避免「墓碑密集」异常场景下
     * 与冲突重试共用 3 次上限而过早耗尽整批失败。仍为有界（超过则 fail-safe 整批回滚，绝不复用）。
     */
    private static final int MAX_TOMBSTONE_SKIPS = 8;

    /**
     * R-03 序列预留的并发失败（死锁/锁等待超时）有界重试次数。
     *
     * <p>预留运行在独立 REQUIRES_NEW 事务中，故死锁只回滚该短事务，重试安全（不污染外层导入事务）。
     */
    private static final int MAX_SEQ_RESERVE_RETRIES = 3;

    /**
     * R-03 连接池余量：序列预留要求池上限在 2×并发之外再留出余量。
     *
     * <p>连接池是共享资源（IngestWorker、健康检查、管理 API、删除 outbox 消费者同池取连接），
     * 故峰值「恰好 2×并发」即零余量：任一其它取连接方都可能挤掉预留所需的连接 →
     * Hikari connection-timeout 后抛 CannotGetJdbcConnectionException → 导入 500。
     * 默认 2，与 application.yaml / application-mysql.yaml 文档「2×并发 + 余量」及默认 pool=10 / 并发=4 对齐。
     */
    private static final int CONNECTION_POOL_HEADROOM = 2;

    /**
     * R-03 支撑「一个」导入许可所需的最小连接池上限 = 2（外层导入事务 + REQUIRES_NEW 序列预留）
     * + {@link #CONNECTION_POOL_HEADROOM}（为其它 DB 操作保留的固定余量）。
     *
     * <p>池上限低于该值时，连一个导入都无法安全支撑：启动期直接 fail-closed，<b>不再以 1 作为安全下限</b>。
     */
    private static final int MIN_POOL_FOR_SINGLE_PERMIT = 2 + CONNECTION_POOL_HEADROOM;

    /** 无授权单元时传给 IN 列表的哨兵值：匹配不到任何真实 unit（id 自增为正），等效「仅本人创建」。 */
    private static final List<Long> NO_UNIT_SENTINEL = List.of(-1L);

    private final KnowledgeUnitMapper unitMapper;
    private final KnowledgeChunkMapper chunkMapper;
    private final IngestJobService ingestJobService;
    private final UnitPermissionService unitPermissionService;
    private final FileStorageService fileStorageService;
    private final IngestReviewMapper ingestReviewMapper;
    private final QualityReviewProperties qualityReviewProperties;
    private final DataPermissionEngine dataPermissionEngine;
    private final PlatformTransactionManager transactionManager;

    /** R-06 连接池指标观测：可选注入，非 Hikari 实现时静默跳过。 */
    private final DataSource dataSource;

    /**
     * R-03 Micrometer 指标注册表（可选注入）：注册上传并发 / 连接池 gauge
     * （configured / effective / poolMax / pending）。缺失（如纯单测、未启用 actuator）时静默跳过。
     */
    @Autowired(required = false)
    private MeterRegistry meterRegistry;

    /** R-07 单次请求所有文件的总大小上限（字节）：与 spring.servlet.multipart.max-request-size 对齐。 */
    @Value("${app.upload.max-request-bytes:536870912}")
    private long maxRequestBytes;

    /** R-07 并发上传请求上限：限制同时占用的临时磁盘与处理资源。 */
    @Value("${app.upload.max-concurrent-requests:4}")
    private int maxConcurrentRequests;

    /**
     * R-03 连接池预算校验用：JDBC（Hikari）连接池上限。
     *
     * <p>序列预留运行在独立 REQUIRES_NEW 事务中，故每个并发导入在峰值会同时占用 <b>2 个连接</b>
     * （外层导入事务 + 内层预留事务）。启动期据此校验池预算（见 {@link #initUploadPermits()}）。
     * 键缺失时按 Hikari 默认 10 兜底（如 h2 profile 未显式配置池）。
     */
    @Value("${spring.datasource.hikari.maximum-pool-size:10}")
    private int connectionPoolMaxSize;

    /** R-07 并发上传许可。 */
    private Semaphore uploadPermits;

    /** R-06 实际生效的上传并发许可数（按连接池容量自动收敛后的值）。 */
    private int effectiveUploadConcurrency;

    /**
     * R-03 序列预留事务模板：REQUIRES_NEW，使预留独立于外层导入事务。
     *
     * <p>好处：① 行锁只覆盖极短的序列操作，不再在外层事务（含文件写盘）期间长期持有；
     * ② 偶发死锁/锁等待超时只回滚该短事务，可在调用侧安全有界重试，不会污染外层导入事务。
     */
    private TransactionTemplate unitCodeReservationTemplate;

    /**
     * R-03/R-06 启动期初始化上传并发许可与序列预留事务模板。
     *
     * <p><b>R-06 常规收敛：</b>以数据库连接池容量为上限<b>自动收敛</b>{@code uploadPermits}
     * （见 {@link #effectiveUploadConcurrency}），把并发限制在「池上限 - 固定余量」可支撑的范围内，
     * 从而不会因外层导入事务与 REQUIRES_NEW 预留事务各占 1 个连接而耗尽池、抛
     * {@code CannotGetJdbcConnectionException} 导致导入 500。既有部署/测试可能配置较高并发（如 64），
     * 收敛到安全值既保证可用又消除危险配置；如需保留原始并发，请同步提高
     * {@code spring.datasource.hikari.maximum-pool-size}。
     *
     * <p><b>R-03 fail-closed：</b>当池上限连「一个导入许可」都无法安全支撑
     * （{@code poolMax < 2 + CONNECTION_POOL_HEADROOM}）时，<b>启动直接失败</b>，不再以 1 作为安全下限
     * 让危险配置上线（详见 {@link #isConnectionPoolTooSmallForAnyPermit(int)}）。
     */
    @PostConstruct
    void initUploadPermits() {
        this.unitCodeReservationTemplate = new TransactionTemplate(transactionManager);
        this.unitCodeReservationTemplate.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        int configured = Math.max(maxConcurrentRequests, 1);
        int poolMax = resolveEffectivePoolMaxSize(dataSource, connectionPoolMaxSize);
        // R-03 fail-closed：池太小，连一个导入许可都无法安全支撑 → 拒绝启动（不再 1 下限 fallback）。
        if (isConnectionPoolTooSmallForAnyPermit(poolMax)) {
            throw new IllegalStateException("R-03 fail-closed：连接池上限 " + poolMax
                    + " 太小，无法安全支撑任何上传导入。每个导入峰值需 2 个连接（外层导入事务 + "
                    + "REQUIRES_NEW 序列预留），另需为 API/worker/健康检查保留 " + CONNECTION_POOL_HEADROOM
                    + " 个余量，故要求 spring.datasource.hikari.maximum-pool-size >= "
                    + MIN_POOL_FOR_SINGLE_PERMIT + "（当前 " + poolMax + "）。请调高连接池，或禁用上传导入；"
                    + "不接受以 1 个许可的危险 fallback。");
        }
        int effective = effectiveUploadConcurrency(poolMax, maxConcurrentRequests);
        this.effectiveUploadConcurrency = effective;
        this.uploadPermits = new Semaphore(effective);
        if (effective < configured) {
            log.warn("R-06 连接池预算不足：已自动收敛上传并发 uploadPermits {} -> {}（池上限 {}，"
                            + "要求 ≥ 2×并发 + 余量{}）。序列预留走 REQUIRES_NEW，峰值每次并发导入占用 2 个连接；"
                            + "为 API/worker/健康检查保留固定余量。如需更高并发，请提高 "
                            + "spring.datasource.hikari.maximum-pool-size（当前判据：pool < 2×并发 + 余量）。",
                    configured, effective, poolMax, CONNECTION_POOL_HEADROOM);
        }
        log.info("R-03/R-06 上传并发许可初始化 configured={} effective={} poolMax={} pending={}"
                        + "（实际池上限优先；要求池上限 ≥ {}）",
                configured, effective, poolMax, pendingConnections(), MIN_POOL_FOR_SINGLE_PERMIT);
        registerPoolMetrics();
    }

    /**
     * R-06：解析**实际生效**的连接池上限——优先读取 Hikari 运行时的 {@code getMaximumPoolSize()}，
     * 非 Hikari 数据源或读取失败时回退到属性 {@code spring.datasource.hikari.maximum-pool-size}。
     *
     * <p>用实际池上限而非仅属性值，可避免「属性名不一致/非 Hikari 池」导致收敛依据偏差。
     *
     * @param dataSource 数据源（可为 null）
     * @param fallback   回退的池上限（属性值）
     * @return 实际池上限（≥1）
     */
    static int resolveEffectivePoolMaxSize(DataSource dataSource, int fallback) {
        if (dataSource instanceof HikariDataSource hikari) {
            try {
                return Math.max(hikari.getMaximumPoolSize(), 1);
            } catch (RuntimeException ex) {
                // 读取失败：回退属性值
                return Math.max(fallback, 1);
            }
        }
        return Math.max(fallback, 1);
    }

    /**
     * R-06 连接池预算判据（纯函数，便于单测）：池上限是否不足以支撑 2×并发 + 余量。
     *
     * <p>阈值取「2 × 并发 + {@link #CONNECTION_POOL_HEADROOM}」：<b>恰好 2×并发也视为不足</b>——
     * 池是共享资源，零余量时任一其它取连接方都可能挤掉预留所需的连接。
     *
     * @param poolMaxSize 连接池上限
     * @param concurrency 并发导入上限
     * @return true=池不足以支撑峰值（需 ≥ 2×并发 + 余量）
     */
    static boolean isConnectionPoolBudgetInsufficient(int poolMaxSize, int concurrency) {
        return poolMaxSize < 2 * Math.max(concurrency, 1) + CONNECTION_POOL_HEADROOM;
    }

    /**
     * R-03 连接池预算判据（纯函数，便于单测）：池上限是否小到连「一个导入许可」都无法安全支撑。
     *
     * <p>阈值 {@code 2 + CONNECTION_POOL_HEADROOM}：一个导入峰值占 2 个连接（外层 + REQUIRES_NEW），
     * 另需固定余量。低于该值即 fail-closed（启动失败），<b>不得</b>再以 1 作为安全下限。
     *
     * @param poolMaxSize 连接池上限
     * @return true=池太小，无法安全支撑任何导入许可
     */
    static boolean isConnectionPoolTooSmallForAnyPermit(int poolMaxSize) {
        return poolMaxSize < MIN_POOL_FOR_SINGLE_PERMIT;
    }

    /**
     * R-06 按连接池容量计算**安全的最大上传并发**（纯函数，便于单测）。
     *
     * <p>每个并发导入峰值占用 2 个连接（外层 + REQUIRES_NEW 预留），并为 API/worker/健康检查保留
     * {@link #CONNECTION_POOL_HEADROOM} 个固定余量，故安全并发 = floor((池上限 - 余量) / 2)，
     * 再与配置并发取较小值。
     *
     * <p><b>R-03：不再以 1 作为安全下限</b>——池上限低于 {@link #MIN_POOL_FOR_SINGLE_PERMIT} 时返回
     * 0（表示无可用许可）。该情形由 {@link #initUploadPermits()} 在启动期 fail-closed 拦截，绝不
     * 让「危险的单许可 fallback」进入运行态。
     *
     * @param poolMaxSize          连接池上限
     * @param configuredConcurrency 配置的并发上限
     * @return 实际生效的并发许可数（≥0；0 表示池太小、无安全许可）
     */
    static int effectiveUploadConcurrency(int poolMaxSize, int configuredConcurrency) {
        int configured = Math.max(configuredConcurrency, 1);
        int safeByPool = (Math.max(poolMaxSize, 0) - CONNECTION_POOL_HEADROOM) / 2;
        int safe = Math.max(safeByPool, 0);
        return Math.min(configured, safe);
    }

    /**
     * R-06 启动期连接池预算校验结果（true=池不足以支撑 2×并发 + 余量）。
     *
     * <p>由已绑定的 {@link #connectionPoolMaxSize} / {@link #maxConcurrentRequests} 现算，
     * 供启动告警与测试观察（配置是否足以支撑「原始」并发；实际生效并发见 {@link #effectiveUploadConcurrency}）。
     */
    boolean connectionPoolBudgetInsufficient() {
        return isConnectionPoolBudgetInsufficient(connectionPoolMaxSize, maxConcurrentRequests);
    }

    /** R-06 实际生效的上传并发许可数（供启动校验与测试观察）。 */
    int effectiveUploadConcurrency() {
        return effectiveUploadConcurrency;
    }

    /**
     * R-03：上传并发 / 连接池运行指标快照（供健康检查、日志与排查公开
     * {@code configured / effective / poolMax / pending}）。
     *
     * <p>字段含义：configured=配置的并发上限；effective=按池容量收敛后实际生效的许可数；
     * poolMax=实际生效的连接池上限；pending=当前等待连接的线程数（非 Hikari 时恒为 0）。
     *
     * @return 指标快照（键顺序稳定）
     */
    public Map<String, Object> uploadConcurrencySnapshot() {
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("configured", maxConcurrentRequests);
        snapshot.put("effective", effectiveUploadConcurrency);
        snapshot.put("poolMax", resolveEffectivePoolMaxSize(dataSource, connectionPoolMaxSize));
        snapshot.put("pending", pendingConnections());
        return snapshot;
    }

    /** R-03：当前等待连接池的线程数（非 Hikari / MXBean 不可用时返回 0）。 */
    private long pendingConnections() {
        if (dataSource instanceof HikariDataSource hikari) {
            HikariPoolMXBean pool = hikari.getHikariPoolMXBean();
            if (pool != null) {
                try {
                    return pool.getThreadsAwaitingConnection();
                } catch (RuntimeException ex) {
                    return 0L;
                }
            }
        }
        return 0L;
    }

    /**
     * R-03：注册上传并发 / 连接池 Micrometer gauge（configured / effective / poolMax / pending）。
     *
     * <p>无 {@link MeterRegistry}（纯单测、未启用 actuator）时静默跳过；注册失败只告警，不影响启动。
     */
    private void registerPoolMetrics() {
        if (meterRegistry == null) {
            return;
        }
        try {
            Gauge.builder("app.upload.concurrency.configured", this, s -> s.maxConcurrentRequests)
                    .description("配置的上传并发上限").register(meterRegistry);
            Gauge.builder("app.upload.concurrency.effective", this, s -> s.effectiveUploadConcurrency)
                    .description("按连接池容量自动收敛后实际生效的上传并发许可数").register(meterRegistry);
            Gauge.builder("app.upload.pool.max", this,
                            s -> s.resolveEffectivePoolMaxSize(s.dataSource, s.connectionPoolMaxSize))
                    .description("JDBC 连接池上限").register(meterRegistry);
            Gauge.builder("app.upload.pool.pending", this, s -> s.pendingConnections())
                    .description("等待连接的线程数").register(meterRegistry);
        } catch (RuntimeException ex) {
            log.warn("注册上传并发/连接池 Micrometer 指标失败: {}", ex.getMessage());
        }
    }

    /**
     * R-06 Hikari 连接池指标观测：周期打印 active / idle / total / pending(等待连接线程数) 与连接超时。
     *
     * <p>用于验证高并发导入时池是否接近饱和（active 逼近上限、pending &gt; 0 表示已有请求在等连接）。
     * 非 Hikari 数据源或未启用指标日志时静默跳过。间隔由
     * {@code app.upload.pool-metrics-interval-ms}（默认 600000ms）控制。
     */
    @Scheduled(fixedDelayString = "${app.upload.pool-metrics-interval-ms:600000}",
            initialDelayString = "${app.upload.pool-metrics-interval-ms:600000}")
    public void logConnectionPoolMetrics() {
        if (!(dataSource instanceof HikariDataSource hikari)) {
            return;
        }
        HikariPoolMXBean pool = hikari.getHikariPoolMXBean();
        if (pool == null) {
            return;
        }
        long active = pool.getActiveConnections();
        long idle = pool.getIdleConnections();
        long total = pool.getTotalConnections();
        long pending = pool.getThreadsAwaitingConnection();
        long timeoutMs = hikari.getConnectionTimeout();
        int poolMax = resolveEffectivePoolMaxSize(dataSource, connectionPoolMaxSize);
        if (pending > 0 || (total > 0 && active >= total)) {
            log.warn("Hikari 连接池接近饱和 pool={} active={} idle={} total={} pending={} connectionTimeoutMs={} "
                            + "configured={} effective={} poolMax={}",
                    hikari.getPoolName(), active, idle, total, pending, timeoutMs,
                    maxConcurrentRequests, effectiveUploadConcurrency, poolMax);
        } else {
            log.info("Hikari 连接池指标 pool={} active={} idle={} total={} pending={} connectionTimeoutMs={} "
                            + "configured={} effective={} poolMax={}",
                    hikari.getPoolName(), active, idle, total, pending, timeoutMs,
                    maxConcurrentRequests, effectiveUploadConcurrency, poolMax);
        }
    }

    /**
     * 异步导入：逐文件接收并建立作业，立即返回批次摘要。
     *
     * <p>R-07：请求级总大小与并发上限保护；文件流式落盘（不整文件读入堆内存），
     * 事务回滚时补偿删除已落盘文件。
     *
     * @param files     上传文件
     * @param category  可选分类
     * @param useVlm    是否让 AI 对 PDF 图片做 VLM 图注
     * @param creatorId 创建人
     * @return 批次摘要
     */
    @Transactional
    public ImportResponseVO importFiles(List<MultipartFile> files, String category, boolean useVlm, Long creatorId) {
        if (files == null || files.isEmpty()) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "files 不能为空");
        }
        if (category != null && !category.isBlank() && !KnowledgeEnums.CATEGORIES.contains(category)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "非法 category: " + category);
        }
        long totalBytes = 0;
        for (MultipartFile file : files) {
            totalBytes += Math.max(file.getSize(), 0);
        }
        if (totalBytes > maxRequestBytes) {
            throw new BusinessException(ErrorCode.UPLOAD_TOO_LARGE,
                    "本次上传总大小超过上限（" + maxRequestBytes + " 字节）");
        }
        if (!uploadPermits.tryAcquire()) {
            throw new BusinessException(ErrorCode.SERVICE_BUSY, "上传并发过多，请稍后重试");
        }
        try {
            return doImportFiles(files, category, useVlm, creatorId);
        } finally {
            uploadPermits.release();
        }
    }

    /**
     * 执行导入（在 {@link #importFiles} 的事务与并发许可内）。
     *
     * @param files     上传文件
     * @param category  可选分类
     * @param useVlm    是否 VLM
     * @param creatorId 创建人
     * @return 批次摘要
     */
    private ImportResponseVO doImportFiles(List<MultipartFile> files, String category, boolean useVlm,
                                           Long creatorId) {
        String batchId = nextBatchId();
        // R-03：从数据库原子序列预留一段连续编码，替代「读全表取最大值 + 唯一索引冲突整批回滚」
        UnitCodeAllocator codeAllocator = new UnitCodeAllocator(files.size());
        List<ImportItemVO> items = new ArrayList<>(files.size());
        List<String> savedPaths = new ArrayList<>(files.size());
        registerRollbackCleanup(savedPaths);
        int accepted = 0;
        int rejected = 0;
        for (MultipartFile file : files) {
            ImportItemVO item = new ImportItemVO();
            String originalName = file.getOriginalFilename();
            item.setTitle(originalName);
            String fileType = resolveFileType(originalName);
            if (fileType == null) {
                item.setRejectReason("不支持的文件类型（仅 pdf/docx/md/txt）");
                item.setIngestStatus("rejected");
                rejected++;
                items.add(item);
                continue;
            }
            if (file.isEmpty() || file.getSize() > MAX_FILE_SIZE_BYTES) {
                item.setRejectReason("文件为空或超过大小上限");
                item.setIngestStatus("rejected");
                rejected++;
                items.add(item);
                continue;
            }
            KnowledgeUnit unit = new KnowledgeUnit();
            // 先落临时编码，插入后回填 U-<数字>
            unit.setUnitCode("TMP-" + System.nanoTime());
            unit.setTitle(originalName);
            unit.setCategory(category == null || category.isBlank() ? "product" : category);
            unit.setSourceFileName(originalName);
            unit.setFileType(fileType);
            unit.setFileSize(file.getSize());
            unit.setUseVlm(useVlm ? 1 : 0);
            unit.setStatus("active");
            unit.setIngestStatus("queued");
            unit.setBatchId(batchId);
            unit.setCreatorId(creatorId);
            unitMapper.insert(unit);
            assignUniqueUnitCode(unit, codeAllocator);
            // R-01：文件落盘与 unit/job 创建是有补偿的状态机——任一 I/O 失败都删除已插入的 unit，
            // 绝不留下无 storage_path、无 ingest job、无法自行入库的孤立记录。
            String storagePath;
            try {
                // R-07：落盘前做磁盘容量保护；流式写临时文件后原子改名
                fileStorageService.ensureCapacity(file.getSize());
                storagePath = storeFile(file, batchId, unit.getId(), originalName, savedPaths);
            } catch (IOException | BusinessException ex) {
                compensateRejectedUnit(unit, item, ex);
                rejected++;
                items.add(item);
                continue;
            }
            unitMapper.updateStoragePath(unit.getId(), storagePath);
            ingestJobService.enqueue(batchId, unit.getId(), creatorId);

            item.setUnitId(unit.getId());
            item.setTitle(unit.getTitle());
            item.setFileType(fileType);
            item.setIngestStatus("queued");
            accepted++;
            items.add(item);
        }
        log.info("import accepted batchId={} accepted={} rejected={}", batchId, accepted, rejected);
        return new ImportResponseVO(batchId, accepted, rejected, items);
    }

    /**
     * R-01：读取上传流并落盘；文件一旦落盘立即登记补偿删除，不依赖 try-with-resources 成功退出。
     *
     * <p>登记动作在 {@code saveStream} 成功返回后<b>紧接在 try 块内</b>执行，先于任何 close：
     * 即便随后 close 抛出任何异常（IOException 或 RuntimeException），登记都不会被跳过，
     * 事务回滚时仍能清理该文件。close 异常被降级为告警（文件已原子落盘，不影响结果，也不掩盖落盘异常）。
     *
     * @param file         上传文件
     * @param batchId      批次 id
     * @param unitId       unit id
     * @param originalName 原始文件名
     * @param savedPaths   本次已落盘路径登记表（供事务回滚补偿删除）
     * @return 相对存储路径
     * @throws IOException 打开上传流失败
     */
    private String storeFile(MultipartFile file, String batchId, Long unitId, String originalName,
                             List<String> savedPaths) throws IOException {
        InputStream in = file.getInputStream();
        String storagePath;
        try {
            storagePath = fileStorageService.saveStream(batchId, unitId, originalName, in);
            // 关键：文件一旦落盘立即登记（在 close 之前）——即使 close 抛任何异常，事务回滚也能清理该文件
            savedPaths.add(storagePath);
        } finally {
            closeUploadStream(in, unitId);
        }
        return storagePath;
    }

    /**
     * 关闭上传读流；失败只告警（文件已完整落盘，close 失败不影响结果，也不掩盖落盘异常）。
     *
     * <p>捕获 {@link Exception}（含 IOException 与 RuntimeException）：确保 close 的任何异常都不会
     * 让已落盘文件的补偿登记被跳过，也不会掩盖 {@code saveStream} 的原始异常。
     */
    private void closeUploadStream(InputStream in, Long unitId) {
        try {
            in.close();
        } catch (Exception closeEx) {
            log.warn("close upload stream failed unitId={}: {}", unitId, closeEx.getMessage());
        }
    }

    /**
     * R-01：文件 I/O 失败时补偿删除已插入的 unit，并记录可审计的拒绝结果。
     *
     * <p>若补偿删除本身失败，异常上抛触发整批事务回滚（此时事务同步补偿会清理已落盘文件），
     * 保证不会同时残留孤立 unit 与孤立文件。
     *
     * @param unit 已插入的 unit
     * @param item 返回项
     * @param ex   原始失败
     */
    private void compensateRejectedUnit(KnowledgeUnit unit, ImportItemVO item, Exception ex) {
        unitMapper.deleteById(unit.getId());
        item.setRejectReason("读取/保存上传文件失败，该文件已回滚");
        item.setIngestStatus("rejected");
        log.warn("rejected file after io failure batchId={} unitId={} file={} err={}",
                unit.getBatchId(), unit.getId(), unit.getTitle(), ex.getMessage());
    }

    /**
     * R-03：为单个 unit 分配唯一 U- 编码；唯一索引冲突或命中墓碑编码时只重试该 unit（换号），不整批回滚。
     *
     * <p>纵深防御：AI 侧向量以 unit_code 为键，未清扫墓碑会被删除补偿持续重放
     * {@code deleteVectors(unit_code)}；若把该编码分配给新单元，其向量会被迟到重放误删（DB active 但
     * 检索静默缺失）。故候选编码一旦命中 {@code knowledge_unit_tombstones} 即换下一个号。
     *
     * <p>两类重试分别计数、各自有界：唯一索引冲突 ≤ {@link #MAX_CODE_ASSIGN_ATTEMPTS}（并发协调协议），
     * 墓碑跳过 ≤ {@link #MAX_TOMBSTONE_SKIPS}（防御异常密集墓碑）。任一超限即抛业务异常整批回滚，
     * 绝不冒险复用编码。换号只推进序列（只 bump、不重复插入已用编码），因此不会产生重复分配；
     * 被跳过的号码成为永久空号（编码不透明、空号无害）。
     *
     * @param unit      已插入的 unit
     * @param allocator 批次内编码分配器
     */
    private void assignUniqueUnitCode(KnowledgeUnit unit, UnitCodeAllocator allocator) {
        int duplicateRetries = 0;
        int tombstoneSkips = 0;
        while (duplicateRetries < MAX_CODE_ASSIGN_ATTEMPTS && tombstoneSkips < MAX_TOMBSTONE_SKIPS) {
            String code = "U-" + allocator.next();
            // R-03 纵深防御：跳过墓碑编码（未清扫的最危险；已清扫的一并避开更安全）
            if (isTombstonedCode(code)) {
                tombstoneSkips++;
                log.warn("unit code reserved by tombstone, skip unitId={} code={} skipped={}",
                        unit.getId(), code, tombstoneSkips);
                continue;
            }
            try {
                unitMapper.updateUnitCode(unit.getId(), code);
                return;
            } catch (DuplicateKeyException ex) {
                duplicateRetries++;
                log.warn("unit code collision unitId={} code={} retry={}", unit.getId(), code, duplicateRetries);
            }
        }
        throw new BusinessException(ErrorCode.SYSTEM_ERROR,
                "知识编码分配冲突，请重试导入（如持续失败，请确认 knowledge_unit_code_seq 已按现有最大编码与墓碑回填）");
    }

    /**
     * R-03 纵深防御：判断候选编码是否已存在于墓碑表。
     *
     * <p>表缺失或查询失败时容错为「无墓碑」（不阻断导入）：正常部署该表必然存在，
     * 仅在极端测试/降级环境缺失时退化。
     *
     * @param code 候选业务编码
     * @return true 表示命中墓碑，需换号
     */
    private boolean isTombstonedCode(String code) {
        try {
            return unitMapper.countTombstoneByCode(code) > 0;
        } catch (RuntimeException ex) {
            log.warn("tombstone lookup failed code={}: {}", code, ex.getMessage());
            return false;
        }
    }

    /**
     * R-03 编码分配器：从数据库序列预留连续编码段，段内顺序取号；段耗尽再预留。
     *
     * <p>事务内通过 {@code SELECT ... FOR UPDATE} 锁定计数行并成段推进，使并发导入不会读到同一
     * 最大值；分配成本 O(1)，不随 knowledge_units 行数增长。
     */
    private final class UnitCodeAllocator {

        private long cursor;
        private long end;

        UnitCodeAllocator(int size) {
            reserve(Math.max(size, 1));
        }

        long next() {
            if (cursor >= end) {
                reserve(1);
            }
            return cursor++;
        }

        private void reserve(int count) {
            long start = reserveUnitCodeBlock(count);
            this.cursor = start;
            this.end = start + count;
        }
    }

    /**
     * R-03：从数据库序列原子预留 {@code count} 个连续编码，返回起始值。
     *
     * <p>预留运行在独立的 REQUIRES_NEW 事务中（{@link #unitCodeReservationTemplate}），对偶发的
     * 死锁/锁等待超时做有界重试。这样即便发生并发失败，也只回滚该短事务，不会污染外层导入事务，
     * 重试因而安全。
     *
     * @param count 预留数量（&gt;=1）
     * @return 第一个可用编码数字
     */
    private long reserveUnitCodeBlock(int count) {
        PessimisticLockingFailureException lastFailure = null;
        for (int attempt = 0; attempt < MAX_SEQ_RESERVE_RETRIES; attempt++) {
            try {
                Long start = unitCodeReservationTemplate.execute(status -> reserveInNewTransaction(count));
                if (start == null) {
                    throw new BusinessException(ErrorCode.SYSTEM_ERROR, "知识编码序列不可用，请检查 knowledge_unit_code_seq");
                }
                return start;
            } catch (PessimisticLockingFailureException ex) {
                // 死锁（DeadlockLoserDataAccessException）或锁等待超时（CannotAcquireLockException）：
                // 只回滚了本 REQUIRES_NEW 短事务，退避后有界重试
                lastFailure = ex;
                log.warn("unit code sequence contention, retry attempt={} err={}", attempt + 1, ex.getMessage());
                sleepQuietly(20L << attempt);
            }
        }
        throw new BusinessException(ErrorCode.SERVICE_BUSY, "编码序列繁忙，请稍后重试导入");
    }

    /**
     * R-03：在独立事务内完成一次序列预留（存在性检查 → 行锁读取 → 成段推进）。
     *
     * <p><b>热路径不对已存在行执行 {@code INSERT IGNORE}</b>：InnoDB 对已存在行做重复键检查会加共享锁，
     * 随后 {@code SELECT ... FOR UPDATE} 升级排他锁会导致锁升级死锁。这里先做非锁定存在性检查，
     * 行已存在则直接 {@code FOR UPDATE}；仅行缺失时才 bootstrap（V10/schema 已保证行存在）。
     *
     * @param count 预留数量
     * @return 起始编码数字
     */
    private Long reserveInNewTransaction(int count) {
        if (unitMapper.countUnitCodeSequence(UNIT_CODE_SEQ_NAME) == 0) {
            // 极罕见 bootstrap 分支：行缺失时才写（该写可能短暂加锁，但已由外层重试兜底）
            unitMapper.initUnitCodeSequence(UNIT_CODE_SEQ_NAME, UNIT_CODE_SEQ_INITIAL);
        }
        Long next = unitMapper.lockUnitCodeSequence(UNIT_CODE_SEQ_NAME);
        if (next == null) {
            unitMapper.initUnitCodeSequence(UNIT_CODE_SEQ_NAME, UNIT_CODE_SEQ_INITIAL);
            next = unitMapper.lockUnitCodeSequence(UNIT_CODE_SEQ_NAME);
        }
        if (next == null) {
            return null;
        }
        unitMapper.bumpUnitCodeSequence(UNIT_CODE_SEQ_NAME, count);
        return next;
    }

    /** 退避等待；被中断则恢复中断标志并立即返回。 */
    private void sleepQuietly(long millis) {
        if (millis <= 0) {
            return;
        }
        try {
            Thread.sleep(millis);
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
        }
    }

    /**
     * R-05/R-07：注册事务完成回调，回滚（含提交失败）时补偿删除本次已落盘文件，避免孤儿文件。
     *
     * @param savedPaths 本次已落盘的相对路径
     */
    private void registerRollbackCleanup(List<String> savedPaths) {
        if (!TransactionSynchronizationManager.isSynchronizationActive()) {
            return;
        }
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCompletion(int status) {
                if (status != TransactionSynchronization.STATUS_COMMITTED) {
                    for (String path : savedPaths) {
                        fileStorageService.deleteQuietly(path);
                    }
                }
            }
        });
    }

    /**
     * 批次列表（分页，按当前用户数据范围过滤）。
     *
     * <p>数据权限（F-01）：知识管理员（{@code knowledge:perm}）可见全部批次；
     * 其余用户只能看到「本人创建」或其单元在 {@link DataPermissionEngine} 授权集合内的批次。
     *
     * @param currentUser 当前用户
     * @param page        页码
     * @param size        每页条数
     * @return 分页结果
     */
    public PageResult<BatchSummaryVO> batches(CurrentUser currentUser, int page, int size) {
        int normalizedPage = Math.max(page, 1);
        int normalizedSize = Math.min(Math.max(size, 1), MAX_PAGE_SIZE);
        long offset = (long) (normalizedPage - 1) * normalizedSize;
        if (offset > Integer.MAX_VALUE) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "page 超出范围");
        }
        List<Long> allowedUnitIds = resolveBatchScope(currentUser);
        long total = unitMapper.countBatches(currentUser.getId(), allowedUnitIds);
        List<BatchSummaryVO> records = unitMapper.selectBatchSummaries(
                currentUser.getId(), allowedUnitIds, (int) offset, normalizedSize);
        return PageResult.of(total, normalizedPage, normalizedSize, records);
    }

    /**
     * 批次详情：逐文件阶段、进度、错误与重试次数（按当前用户数据范围过滤）。
     *
     * <p>数据权限（F-01）：
     * <ul>
     *   <li>知识管理员（{@code knowledge:perm}）可见全部文件与完整明细；</li>
     *   <li>其余用户只能看到「本人创建」或其单元被授权的文件，越权批次返回空 files
     *       （与「批次不存在」不可区分，避免探测）；</li>
     *   <li>非知识管理员不返回切片正文（samples）、错误正文与质检审查细节。</li>
     * </ul>
     *
     * @param currentUser 当前用户
     * @param batchId     批次 id
     * @return 批次详情
     */
    public BatchDetailVO batchDetail(CurrentUser currentUser, String batchId) {
        List<IngestJob> jobs = ingestJobService.byBatch(batchId);
        BatchDetailVO detail = new BatchDetailVO();
        detail.setBatchId(batchId);
        if (jobs.isEmpty()) {
            detail.setFiles(List.of());
            return detail;
        }
        boolean knowledgeAdmin = isKnowledgeAdmin(currentUser);
        List<Long> scopeUnitIds = knowledgeAdmin ? null : resolveBatchScope(currentUser);
        // 收集 unitId：空集合必须短路，避免 IN () 语法错误
        List<Long> unitIds = new ArrayList<>(jobs.size());
        for (IngestJob job : jobs) {
            if (job.getUnitId() != null) {
                unitIds.add(job.getUnitId());
            }
        }
        if (unitIds.isEmpty()) {
            detail.setFiles(List.of());
            return detail;
        }
        // 一次 IN 批量取 unit，替代逐 job 的 selectById
        Map<Long, KnowledgeUnit> unitsById = new HashMap<>();
        for (KnowledgeUnit unit : unitMapper.selectByIds(unitIds)) {
            unitsById.put(unit.getId(), unit);
        }
        // 一次 IN 批量取切片并按 unitId 分组，替代逐 unit 的 selectByUnitId
        Map<Long, List<KnowledgeChunk>> chunksByUnit = new LinkedHashMap<>();
        for (KnowledgeChunk chunk : chunkMapper.selectByUnitIds(unitIds)) {
            chunksByUnit.computeIfAbsent(chunk.getUnitId(), key -> new ArrayList<>()).add(chunk);
        }
        // 一次 IN 批量取每个 unit 最新一条质检审计。
        // 回滚健壮性：① 总开关关闭时完全不查 ingest_reviews（关开关 = 回到改造前，不依赖新表）；
        //            ② 读取整体 try/catch（表不存在/查询失败静默降级），绝不让批次详情报错。
        Map<Long, IngestReview> reviewsByUnit = new HashMap<>();
        if (knowledgeAdmin && qualityReviewProperties.isEnabled()) {
            try {
                for (IngestReview review : ingestReviewMapper.selectLatestByUnitIds(unitIds)) {
                    reviewsByUnit.put(review.getUnitId(), review);
                }
            } catch (RuntimeException ex) {
                log.warn("read ingest_reviews failed batchId={}: {}", batchId, ex.getMessage());
            }
        }

        List<BatchFileVO> files = new ArrayList<>(jobs.size());
        for (IngestJob job : jobs) {
            KnowledgeUnit unit = unitsById.get(job.getUnitId());
            if (unit == null) {
                continue;
            }
            // R-15：批次详情只返回属于本批次的单元，杜绝历史碰撞批次混入
            if (unit.getBatchId() == null || !unit.getBatchId().equals(batchId)) {
                continue;
            }
            if (!isUnitInBatchScope(unit, currentUser, scopeUnitIds)) {
                continue;
            }
            BatchFileVO vo = new BatchFileVO();
            vo.setUnitId(unit.getId());
            vo.setUnitCode(unit.getUnitCode());
            vo.setTitle(unit.getTitle());
            vo.setCategory(unit.getCategory());
            vo.setStatus(unit.getStatus());
            vo.setIngestStatus(unit.getIngestStatus());
            vo.setStage(job.getStage());
            vo.setProgress(job.getProgress());
            vo.setErrorCode(job.getErrorCode());
            // 非知识管理员不返回错误正文（仅保留机器可读错误码）
            vo.setErrorMessage(knowledgeAdmin ? job.getErrorMessage() : null);
            vo.setAttempt(job.getAttempt());
            List<KnowledgeChunk> chunks = chunksByUnit.getOrDefault(unit.getId(), List.of());
            vo.setChunkCount(chunks.size());
            // 非知识管理员不返回切片正文
            vo.setSamples(knowledgeAdmin ? sampleChunks(chunks, unit.getId()) : List.of());
            if (knowledgeAdmin) {
                applyReview(vo, reviewsByUnit.get(unit.getId()));
            }
            files.add(vo);
        }
        detail.setFiles(files);
        return detail;
    }

    /**
     * 计算批次数据范围。
     *
     * @param currentUser 当前用户
     * @return null 表示不限制（知识管理员）；否则为可访问 unit id 列表（无授权单元时为哨兵列表）
     */
    private List<Long> resolveBatchScope(CurrentUser currentUser) {
        if (isKnowledgeAdmin(currentUser)) {
            return null;
        }
        List<Long> accessible = dataPermissionEngine.accessibleUnitIds(currentUser);
        return accessible.isEmpty() ? NO_UNIT_SENTINEL : accessible;
    }

    /** 是否知识管理员（含系统管理员口径：持有 knowledge:perm 权限码）。 */
    private boolean isKnowledgeAdmin(CurrentUser currentUser) {
        return currentUser != null && currentUser.hasPermission("knowledge:perm");
    }

    /** 批次文件是否在当前用户数据范围内：本人创建或其单元已授权。 */
    private boolean isUnitInBatchScope(KnowledgeUnit unit, CurrentUser currentUser, List<Long> scopeUnitIds) {
        if (scopeUnitIds == null) {
            return true;
        }
        return unit.getCreatorId() != null && unit.getCreatorId().equals(currentUser.getId())
                || scopeUnitIds.contains(unit.getId());
    }

    /**
     * 抽取最多 3 条切片正文供质检抽屉展示（DESIGN.md §6.3）。
     *
     * <p>切片列表须按 {@code chunk_index} 升序（与 {@code selectByUnitId} 一致），
     * 配合以 unitId 为种子的确定性洗牌，保证与逐条查询时抽样结果完全一致。
     *
     * @param chunks unit 的切片（按 chunk_index 升序）
     * @param unitId unit id（抽样随机种子）
     * @return 抽样切片
     */
    private List<String> sampleChunks(List<KnowledgeChunk> chunks, Long unitId) {
        if (chunks.isEmpty()) {
            return List.of();
        }
        int sampleSize = Math.min(3, chunks.size());
        List<KnowledgeChunk> shuffled = new ArrayList<>(chunks);
        Collections.shuffle(shuffled, new java.util.Random(unitId));
        List<String> samples = new ArrayList<>(sampleSize);
        for (int i = 0; i < sampleSize; i++) {
            samples.add(shuffled.get(i).getContent());
        }
        return samples;
    }

    /**
     * 质检确认（product 手册强制）。确认或调整后进入 embed。
     *
     * @param unitId  unit id
     * @param request 质检请求
     */
    @Transactional
    public void qualityGate(Long unitId, QualityGateRequest request) {
        KnowledgeUnit unit = unitMapper.selectById(unitId);
        if (unit == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "知识单元不存在");
        }
        IngestJob job = ingestJobService.latestByUnit(unitId);
        if (job == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "无入库作业");
        }
        if (!IngestJobService.STAGE_QUALITY_GATE.equals(job.getStage())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "当前不在质检阶段: " + job.getStage());
        }
        String action = request.getAction() == null ? "confirm" : request.getAction();
        if (!"confirm".equals(action) && !"adjust".equals(action)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "action 仅支持 confirm / adjust");
        }
        if ("adjust".equals(action)) {
            if (request.getCategory() != null && !request.getCategory().isBlank()) {
                if (!KnowledgeEnums.CATEGORIES.contains(request.getCategory())) {
                    throw new BusinessException(ErrorCode.PARAM_INVALID, "非法 category: " + request.getCategory());
                }
                // R-02：仅更新分类字段，不用整行实体覆盖业务状态
                unitMapper.updateCategory(unitId, request.getCategory());
                unit.setCategory(request.getCategory());
            }
            if (request.getPermissions() != null) {
                unitPermissionService.configure(unitId, buildPermissionRequest(request));
            }
        }
        // 人工兜底写审计（取不到当前用户则写 null，不抛错）
        writeHumanReview(unit, job, action);
        // 放行至 embed，交由 Worker 继续
        ingestJobService.advance(job, IngestJobService.STAGE_EMBED, 60);
        log.info("quality gate passed unitId={} action={}", unitId, action);
    }

    /**
     * 人工确认/调整时写一行审计（decision=human_confirmed / human_adjusted）。
     *
     * @param unit   单元
     * @param job    作业
     * @param action confirm / adjust
     */
    private void writeHumanReview(KnowledgeUnit unit, IngestJob job, String action) {
        try {
            IngestReview review = new IngestReview();
            review.setUnitId(unit.getId());
            review.setJobId(job.getId());
            review.setBatchId(unit.getBatchId());
            review.setCategory(unit.getCategory());
            review.setDecision("adjust".equals(action) ? "human_adjusted" : "human_confirmed");
            review.setDecider("human");
            review.setDeciderUserId(currentUserIdOrNull());
            review.setDetailJson(JsonUtils.toJson(Map.of(
                    "action", "adjust".equals(action) ? "adjust" : "confirm",
                    "reason", review.getDecision())));
            ingestReviewMapper.insert(review);
        } catch (RuntimeException ex) {
            log.warn("write human review audit failed unitId={}: {}", unit.getId(), ex.getMessage());
        }
    }

    /** 当前登录用户 id；未登录返回 null（不抛错）。 */
    private Long currentUserIdOrNull() {
        CurrentUser user = SecurityUtils.getCurrentUser();
        return user == null ? null : user.getId();
    }

    /** 把最新一条质检审计映射到批次文件 VO。 */
    private void applyReview(BatchFileVO vo, IngestReview review) {
        if (review == null) {
            return;
        }
        vo.setReviewVerdict(review.getDecision());
        vo.setReviewConfidence(review.getLlmConfidence());
        vo.setReviewSource(review.getDecider());
        if (review.getReviewReasons() == null || review.getReviewReasons().isBlank()) {
            vo.setReviewReasons(List.of());
        } else {
            vo.setReviewReasons(List.of(review.getReviewReasons().split(",")));
        }
        vo.setVlmFindings(JsonUtils.fieldStringList(review.getDetailJson(), "vlmFindings"));
        // AI 预审意见：从 detail_json 顶层解析（不嵌套），模型名直接取列
        vo.setAiVerdict(JsonUtils.fieldString(review.getDetailJson(), "verdict"));
        vo.setAiReasons(JsonUtils.fieldStringList(review.getDetailJson(), "reasons"));
        vo.setAiModel(review.getAiModel());
    }

    /**
     * 从失败阶段重试（未指定 fromStage 时从 parse 重跑整条流水线）。
     *
     * @param unitId  unit id
     * @param request 重试请求
     */
    @Transactional
    public void retry(Long unitId, RetryRequest request) {
        KnowledgeUnit unit = unitMapper.selectById(unitId);
        if (unit == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "知识单元不存在");
        }
        IngestJob job = ingestJobService.latestByUnit(unitId);
        if (job == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "无入库作业");
        }
        if (!IngestJobService.STAGE_FAILED.equals(job.getStage())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "仅失败作业可重试，当前阶段: " + job.getStage());
        }
        if (job.getAttempt() != null && job.getAttempt() >= IngestJobService.MAX_ATTEMPT) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "已达最大重试次数（3），请更换文件或联系管理员");
        }
        String fromStage = (request == null || request.getFromStage() == null || request.getFromStage().isBlank())
                ? IngestJobService.STAGE_PARSE : request.getFromStage();
        // Worker 轮询的是 queued / embed 两个阶段：parse 起的重试回到 queued，embed 起的重试回到 embed
        String workerStage = IngestJobService.STAGE_EMBED.equals(fromStage)
                ? IngestJobService.STAGE_EMBED : IngestJobService.STAGE_QUEUED;
        unitMapper.updateIngestStatus(unitId, "queued");
        ingestJobService.resetForRetry(job, workerStage);
        log.info("retry ingest unitId={} fromStage={} workerStage={}", unitId, fromStage, workerStage);
    }

    private com.shuidun.kb.permission.dto.UnitPermissionRequest buildPermissionRequest(QualityGateRequest request) {
        com.shuidun.kb.permission.dto.UnitPermissionRequest permissionRequest =
                new com.shuidun.kb.permission.dto.UnitPermissionRequest();
        permissionRequest.setMode("replace");
        permissionRequest.setItems(request.getPermissions());
        return permissionRequest;
    }

    private String nextBatchId() {
        String date = LocalDate.now().format(DATE_FORMAT);
        // R-15：随机 16 位十六进制后缀，避免旧实现 System.nanoTime() % 10000 的短周期碰撞与批次合并
        String suffix = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        return "B-" + date + "-" + suffix;
    }

    private String resolveFileType(String filename) {
        if (filename == null) {
            return null;
        }
        int dot = filename.lastIndexOf('.');
        if (dot < 0) {
            return null;
        }
        String ext = filename.substring(dot + 1).toLowerCase(Locale.ROOT);
        return KnowledgeEnums.FILE_TYPES.contains(ext) ? ext : null;
    }
}

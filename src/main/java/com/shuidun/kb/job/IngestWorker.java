package com.shuidun.kb.job;

import com.shuidun.kb.ai.AiClient;
import com.shuidun.kb.ai.AiException;
import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.ai.dto.ParseResult;
import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.knowledge.FileStorageService;
import com.shuidun.kb.knowledge.KnowledgeEnums;
import com.shuidun.kb.knowledge.entity.KnowledgeChunk;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeChunkMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.permission.UnitPermissionService;
import jakarta.annotation.PostConstruct;
import java.net.InetAddress;
import java.time.LocalDateTime;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;
import java.util.function.Supplier;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 入库 Worker（DESIGN.md §6.2）：单线程串行消费 ingest_jobs。
 *
 * <p>阶段：parse → chunk_qc → classify → embed → finalize；
 * product 手册在 quality_gate 停下等待人工确认。失败可按 stage 重试。
 *
 * <p>F-02 租约：领取时登记 {@code claimed_by / lease_until}，每次阶段推进即心跳续租；
 * 每轮 poll 先回收过期租约作业（worker 崩溃/重启后可恢复），未预期异常统一落失败态，
 * 可重试失败的退避时间持久化到 {@code not_before}（多实例共享）。
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.ingest.worker.enabled", havingValue = "true", matchIfMissing = true)
public class IngestWorker {

    /** 默认切片长度。 */
    private static final int DEFAULT_CHUNK_SIZE = 512;

    /** 空切片比例阈值。 */
    private static final double EMPTY_RATIO_THRESHOLD = 0.5;

    /** 平均切片字符下限：仅用于拦截明显乱码/空文档，软性质量信号由 review_reasons 承担。 */
    private static final double MIN_AVG_CHUNK_CHARS = 10.0;

    /** 可重试的 AI 错误码。 */
    private static final Set<String> RETRYABLE_CODES =
            Set.of("MINERU_TIMEOUT", "PARSE_TIMEOUT", "SOURCE_FETCH_FAILED", "MINERU_SUBMIT_FAILED",
                    "MINERU_POLL_FAILED", "EMBED_FAILED");

    private final IngestJobService jobService;
    private final KnowledgeUnitMapper unitMapper;
    private final KnowledgeChunkMapper chunkMapper;
    private final AiClient aiClient;
    private final FileStorageService fileStorageService;
    private final UnitPermissionService unitPermissionService;
    private final QualityReviewService qualityReviewService;

    /**
     * 可重试失败的退避基数（毫秒）。第 n 次退避 = base &lt;&lt; (n-1)。
     *
     * <p>退避通过「延迟重排」实现：作业重新回到可领取阶段并持久化下次可见时间
     * （{@code not_before} 列），而不是在 Worker 线程里 {@code Thread.sleep} 阻塞。
     * 测试可设为 0 以立即重试。
     */
    @Value("${app.ingest.worker.retry-backoff-ms:1000}")
    private long retryBackoffMs;

    /**
     * 租约时长（毫秒）。必须大于单次 AI 调用最坏耗时（parse 最坏 15 分钟），
     * 阶段推进时心跳续租，因此只需覆盖单个阶段而非整条流水线。
     *
     * <p>租约过期后作业可被任意实例回收，避免 worker 崩溃后永久卡住（F-02）。
     */
    @Value("${app.ingest.worker.lease-ms:1200000}")
    private long leaseMs;

    /** 可选的显式 worker 标识（多实例部署时可注入稳定 ID）；缺省用 主机名+随机后缀。 */
    @Value("${app.ingest.worker.id:}")
    private String configuredWorkerId;

    /**
     * R-02 单文件解析硬上限（字节）：超过即拒绝入库并给出可操作提示，绝不先读入堆再判断。
     *
     * <p>与上传侧 {@code spring.servlet.multipart.max-file-size} / AI 侧 max_source_bytes 对齐（默认 200 MiB）。
     * 这是「单文件实际可接受大小」的硬约束；总内存预算另见 {@link #memoryBudgetBytes}。
     */
    @Value("${app.ingest.worker.max-parse-file-bytes:209715200}")
    private long maxParseFileBytes;

    /**
     * R-02 解析阶段进程内堆内存总预算（字节）：预留量 = 文件字节 × {@link #parseHeapFactor}
     * （Java 侧 byte[] + AI 请求缓冲）。预算不足时本轮不领取该作业（排队等待），而不是把文件读入堆。
     *
     * <p>由此可推导本节点可并存的解析数上限 ≈ {@code memoryBudgetBytes / (maxParseFileBytes × factor)}。
     * 单文件「实际最大可接受大小」= {@code min(maxParseFileBytes, memoryBudgetBytes / factor)}。
     */
    @Value("${app.ingest.worker.memory-budget-bytes:536870912}")
    private long memoryBudgetBytes;

    /**
     * R-02 单文件堆放大系数：完整文件在 Java 侧至少同时存在于 byte[] 与 AI 请求缓冲中，默认 2。
     */
    @Value("${app.ingest.worker.parse-heap-factor:2}")
    private int parseHeapFactor;

    /** R-02 当前已预留的解析内存（字节）。 */
    private final AtomicLong reservedParseBytes = new AtomicLong();

    /** 本 Worker 标识：领取/心跳/日志统一使用，用于识别 stale 作业归属。 */
    private String workerId;

    /**
     * jobId → 下一次可处理时间（毫秒）。兼容保留的进程内快速判断；
     * 权威来源为数据库 {@code not_before} 列（见 {@link #resetForRetry}）。
     */
    private final Map<Long, Long> notBefore = new ConcurrentHashMap<>();

    @PostConstruct
    void initWorkerId() {
        this.workerId = (configuredWorkerId == null || configuredWorkerId.isBlank())
                ? defaultWorkerId() : configuredWorkerId.trim();
        log.info("ingest worker started workerId={} leaseMs={}", this.workerId, this.leaseMs);
    }

    private static String defaultWorkerId() {
        String host;
        try {
            host = InetAddress.getLocalHost().getHostName();
        } catch (java.net.UnknownHostException ex) {
            host = "unknown-host";
        }
        return host + "-" + UUID.randomUUID().toString().substring(0, 8);
    }

    /**
     * 轮询 queued / embed 阶段的作业并串行处理。
     *
     * <p>每轮先回收过期租约作业（F-02 stale recovery）：worker 崩溃后被其占用的在途作业
     * 在租约到期后回到可领取阶段，由任意实例重新执行。每个作业再经乐观锁原子领取
     * （{@link IngestJobService#claim}）：并发下只有一个 Worker 能领到，避免同一作业被重复执行；
     * 仍在退避窗口内的作业由 SQL 层 {@code not_before} 过滤，不会进入候选。
     */
    @Scheduled(fixedDelayString = "${app.ingest.worker.interval-ms:2000}")
    public void poll() {
        // R-05：多实例下未确认共享卷跨节点可见时，fail-fast 暂停作业分配，
        // 避免领取到本节点读不到的源文件（而不是仅告警后继续跑）。
        if (!fileStorageService.isClusterReady()) {
            log.warn("shared volume not verified across nodes; skip job assignment workerId={}", workerId);
            return;
        }
        reclaimStaleJobs();
        for (IngestJob job : jobService.pick(IngestJobService.STAGE_QUEUED, 3)) {
            // R-02：解析前内存准入（单文件硬上限 + 进程内堆预算预留），通过后才领取
            long reserved = tryAdmitForParse(job);
            if (reserved < 0) {
                continue;
            }
            try {
                runClaimed(job, IngestJobService.STAGE_QUEUED, this::processFromParse);
            } finally {
                releaseParseBytes(reserved);
            }
        }
        for (IngestJob job : jobService.pick(IngestJobService.STAGE_EMBED, 3)) {
            runClaimed(job, IngestJobService.STAGE_EMBED, this::embedAndFinalize);
        }
    }

    /**
     * R-02/R-04：解析前内存准入——单文件硬上限校验 + 进程内解析内存预算预留。
     *
     * <p>必须先于 {@code claim}：超过单文件上限的作业直接判失败并给出可操作提示；预算不足的作业
     * 本轮不领取（保持 queued 排队等待），两者都不会把整文件读入堆后再判断。
     *
     * <p><b>R-04 fail-closed：</b>准入以<b>实际存储对象长度</b>（{@link FileStorageService#storedSize}，
     * 本地文件用 {@code Files.size}）为唯一依据，DB {@code file_size} 仅作一致性检查：
     * <ul>
     *   <li>存储路径缺失 / 实际长度无法确认（文件缺失、IO 失败）/ 实际长度 ≤ 0 → <b>拒绝解析</b>
     *       （绝不返回「无需预算」）；</li>
     *   <li>DB {@code file_size} 缺失或 ≤ 0 → <b>拒绝解析并要求修复元数据</b>；</li>
     *   <li>DB {@code file_size} 与实际长度不一致 → <b>报警并阻止解析</b>；</li>
     *   <li>一致时以实际长度计算预留，保留内存预算作为并发保护。</li>
     * </ul>
     *
     * @param job 候选作业
     * @return &gt;0 已预留字节数（调用方须在 finally 释放）；0 无需预留（仅「单元不存在」交给
     *         {@code processFromParse} 落 UNIT_NOT_FOUND）；-1 未准入（已拒绝或延后）
     */
    private long tryAdmitForParse(IngestJob job) {
        KnowledgeUnit unit = unitMapper.selectById(job.getUnitId());
        if (unit == null) {
            // 单元不存在：交给 processFromParse 落 UNIT_NOT_FOUND，不在此处判断
            return 0;
        }
        String storagePath = unit.getStoragePath();
        if (storagePath == null || storagePath.isBlank()) {
            return rejectAdmission(job, unit, "STORAGE_PATH_MISSING",
                    "知识单元缺少存储路径，无法确认文件实际大小，已拒绝解析（fail-closed）；请修复元数据后重试。");
        }
        long actualSize;
        try {
            actualSize = fileStorageService.storedSize(storagePath);
        } catch (RuntimeException ex) {
            return rejectAdmission(job, unit, "FILE_SIZE_UNKNOWN",
                    "无法确认存储对象实际大小（" + ex.getMessage() + "），已拒绝解析（fail-closed）；"
                            + "请确认文件存在且可读后重试。");
        }
        if (actualSize <= 0) {
            return rejectAdmission(job, unit, "FILE_SIZE_UNKNOWN",
                    "存储对象实际大小不可用（" + actualSize + " 字节），已拒绝解析（fail-closed）；请修复文件后重试。");
        }
        Long declaredSize = unit.getFileSize();
        if (declaredSize == null || declaredSize <= 0) {
            return rejectAdmission(job, unit, "FILE_SIZE_METADATA_MISSING",
                    "知识单元 file_size 元数据缺失或非法（" + declaredSize + "），已拒绝解析（fail-closed）；"
                            + "请修复元数据（应等于实际存储长度 " + actualSize + " 字节）后重试。");
        }
        if (declaredSize != actualSize) {
            String message = "知识单元 file_size 元数据（" + declaredSize + " 字节）与实际存储长度（" + actualSize
                    + " 字节）不一致，已拒绝解析并要求修复元数据。";
            log.error("reject parse on file_size mismatch unitId={} declared={} actual={} jobId={}",
                    unit.getId(), declaredSize, actualSize, job.getId());
            return rejectAdmission(job, unit, "FILE_SIZE_MISMATCH", message);
        }
        // 以实际存储长度作为准入依据（DB file_size 已确认一致）
        long factor = Math.max(parseHeapFactor, 1);
        long cost = actualSize > Long.MAX_VALUE / factor ? Long.MAX_VALUE : actualSize * factor;
        long cap = Math.min(maxParseFileBytes, memoryBudgetBytes / factor);
        if (cap > 0 && actualSize > cap) {
            String message = "文件 " + actualSize + " 字节超过解析内存上限 " + cap + " 字节，请拆分文档后重试，"
                    + "或调大 app.ingest.worker.max-parse-file-bytes / memory-budget-bytes。";
            log.error("reject oversized parse unitId={} actualSize={} cap={} jobId={}",
                    unit.getId(), actualSize, cap, job.getId());
            return rejectAdmission(job, unit, "FILE_TOO_LARGE_FOR_PARSE", message);
        }
        if (!reserveParseBytes(cost)) {
            // 预算不足：不领取，保持 queued，下轮预算释放后再处理
            log.warn("parse memory budget exhausted; defer jobId={} cost={} budget={}",
                    job.getId(), cost, memoryBudgetBytes);
            return -1;
        }
        return cost;
    }

    /**
     * R-04/R-05：准入拒绝的**守卫写**——仅当作业仍为 {@code queued}（尚未被 claim）时才落失败态。
     *
     * <p>准入发生在 {@code claim} 之前。若并发下另一节点已 claim 该作业并开始 parse，本节点的拒绝
     * 不得跨 owner 覆盖对方作业：{@link IngestJobService#markFailedIfQueued} 以 {@code stage='queued'}
     * 为 CAS，写成功才同步把单元置 failed；写失败（已被 claim/终态）只记录并交由 owner 处理。
     *
     * @param job     作业
     * @param unit    单元
     * @param code    错误码
     * @param message 可操作提示
     * @return 恒为 -1（表示未准入）
     */
    private long rejectAdmission(IngestJob job, KnowledgeUnit unit, String code, String message) {
        boolean failed = jobService.markFailedIfQueued(job, code, message);
        if (failed) {
            unitMapper.updateIngestStatus(unit.getId(), "failed");
            log.error("reject parse (fail-closed) unitId={} jobId={} code={} path={}",
                    unit.getId(), job.getId(), code, unit.getStoragePath());
        } else {
            log.warn("skip admission reject (job already claimed/terminal) unitId={} jobId={} code={}",
                    unit.getId(), job.getId(), code);
        }
        return -1;
    }

    /** R-02：尝试预留解析内存；超预算返回 false（无副作用）。 */
    private boolean reserveParseBytes(long bytes) {
        long budget = Math.max(memoryBudgetBytes, 1);
        while (true) {
            long current = reservedParseBytes.get();
            if (current + bytes > budget) {
                return false;
            }
            if (reservedParseBytes.compareAndSet(current, current + bytes)) {
                return true;
            }
        }
    }

    /** R-02：释放解析内存预留。 */
    private void releaseParseBytes(long bytes) {
        if (bytes > 0) {
            reservedParseBytes.addAndGet(-bytes);
        }
    }

    /** F-02：回收租约过期的在途作业，使崩溃 worker 的任务可被重新执行。 */
    private void reclaimStaleJobs() {
        try {
            int reclaimed = jobService.reclaimExpiredLeases();
            if (reclaimed > 0) {
                log.warn("reclaimed {} stale ingest job(s) after lease expiry workerId={}", reclaimed, workerId);
            }
        } catch (RuntimeException ex) {
            log.warn("stale lease reclaim failed workerId={}: {}", workerId, ex.getMessage());
        }
    }

    /**
     * 领取并执行作业；租约丢失则放弃，未预期异常统一落失败态（F-02）。
     *
     * @param job           候选作业
     * @param expectedStage 期望的当前阶段
     * @param action        处理动作
     */
    private void runClaimed(IngestJob job, String expectedStage, Consumer<IngestJob> action) {
        try {
            if (claimIfDue(job, expectedStage)) {
                action.accept(job);
            }
        } catch (IngestJobService.LeaseLostException ex) {
            // 作业已被回收/终态：本地任何写入都可能与新的执行方冲突，直接放弃
            log.warn("abandon job after lease lost jobId={} workerId={}", job.getId(), workerId);
        } catch (RetryScheduled ex) {
            // 可重试失败已延迟重排，静待下次轮询重新领取
        } catch (RuntimeException ex) {
            failUnexpected(job, ex);
        }
    }

    /**
     * 未预期异常统一落失败态：作业 markFailed、单元 ingest_status=failed，
     * 绝不让作业停留在处理中阶段无人负责（F-02）。
     */
    private void failUnexpected(IngestJob job, RuntimeException ex) {
        log.error("ingest job unexpected failure jobId={} unitId={} workerId={}",
                job.getId(), job.getUnitId(), workerId, ex);
        try {
            jobService.markFailed(job, "WORKER_ERROR",
                    "Worker 未预期错误: " + ex.getClass().getSimpleName() + ": " + ex.getMessage(), workerId);
            KnowledgeUnit unit = unitMapper.selectById(job.getUnitId());
            if (unit != null) {
                unitMapper.updateIngestStatus(unit.getId(), "failed");
            }
        } catch (RuntimeException markEx) {
            log.error("failed to persist unexpected failure state jobId={}", job.getId(), markEx);
        }
    }

    /**
     * 领取作业：以乐观锁原子领取并登记租约。
     *
     * @param job           候选作业
     * @param expectedStage 期望的当前阶段
     * @return true 表示本次领取成功，可继续处理
     */
    private boolean claimIfDue(IngestJob job, String expectedStage) {
        return jobService.claim(job, expectedStage, workerId, leaseUntil());
    }

    /** 当前租约到期时间。 */
    private LocalDateTime leaseUntil() {
        return LocalDateTime.now().plus(leaseMs, ChronoUnit.MILLIS);
    }

    private void processFromParse(IngestJob job) {
        KnowledgeUnit unit = unitMapper.selectById(job.getUnitId());
        if (unit == null) {
            jobService.markFailed(job, "UNIT_NOT_FOUND", "知识单元不存在", workerId);
            return;
        }
        if (isTerminating(unit)) {
            // 单元正在删除/已下线：不得继续入库，避免"复活"已删除单元
            log.warn("skip ingest for terminating unit unitId={} status={}", unit.getId(), unit.getStatus());
            jobService.markFailed(job, "UNIT_TERMINATED", "单元处于删除/下线状态，已终止入库", workerId);
            return;
        }
        try {
            // 1) parse
            jobService.advance(job, IngestJobService.STAGE_PARSE, 10, workerId, leaseUntil());
            unitMapper.updateIngestStatus(unit.getId(), "parsing");
            unit.setIngestStatus("parsing");
            ParseResult result = withRetry(job, IngestJobService.STAGE_QUEUED,
                    () -> aiClient.parse(fileStorageService.read(unit.getStoragePath()),
                            unit.getSourceFileName(), unit.getFileType(), unit.getUnitCode(),
                            DEFAULT_CHUNK_SIZE, unit.getUseVlm() != null && unit.getUseVlm() == 1,
                            "ig-" + job.getId()));
            persistChunks(unit.getId(), result);
            unitMapper.updateHashes(unit.getId(), result.getContentHash(), result.getChunksHash());
            unit.setContentHash(result.getContentHash());
            unit.setChunksHash(result.getChunksHash());
            if (result.getMarkdown() != null) {
                unit.setContent(result.getMarkdown());
            }

            // 2) chunk_qc
            jobService.advance(job, IngestJobService.STAGE_CHUNK_QC, 30, workerId, leaseUntil());
            checkChunkQuality(result);

            // 3) classify（采纳 AI 建议分类，非法值则保留原分类）
            jobService.advance(job, IngestJobService.STAGE_CLASSIFY, 40, workerId, leaseUntil());
            applySuggestedCategory(unit, result);
            // R-02：仅更新分类字段，绝不用整行实体覆盖业务状态（status/ingest_status）
            unitMapper.updateCategory(unit.getId(), unit.getCategory());

            // 4) quality gate：AI 自动判定（fail-closed）；通过则自动 embed，否则停人工门
            jobService.advance(job, IngestJobService.STAGE_AUTO_REVIEW, 55, workerId, leaseUntil());
            QualityReviewService.Outcome outcome = qualityReviewService.review(unit, result, job);
            if (outcome.decision() == QualityReviewService.Decision.AUTO_PASS) {
                embedAndFinalize(job);
                return;
            }
            jobService.advance(job, IngestJobService.STAGE_QUALITY_GATE, 50, workerId, leaseUntil());
            unitMapper.updateIngestStatus(unit.getId(), "parsed");
            log.info("await manual quality gate unitId={} category={} reason={}",
                    unit.getId(), unit.getCategory(), outcome.reason());
            return;
        } catch (RetryScheduled ex) {
            // 可重试失败已延迟重排，静待下次轮询重新领取，不再阻塞当前 Worker 线程
        } catch (AiException ex) {
            handleAiFailure(unit, job, ex);
        } catch (BusinessException ex) {
            jobService.markFailed(job, "INGEST_FAILED", ex.getMessage(), workerId);
            unitMapper.updateIngestStatus(unit.getId(), "failed");
        }
    }

    private void embedAndFinalize(IngestJob job) {
        KnowledgeUnit unit = unitMapper.selectById(job.getUnitId());
        if (unit == null) {
            jobService.markFailed(job, "UNIT_NOT_FOUND", "知识单元不存在", workerId);
            return;
        }
        if (isTerminating(unit)) {
            log.warn("skip embed for terminating unit unitId={} status={}", unit.getId(), unit.getStatus());
            jobService.markFailed(job, "UNIT_TERMINATED", "单元处于删除/下线状态，已终止入库", workerId);
            return;
        }
        try {
            // 5) embed
            jobService.advance(job, IngestJobService.STAGE_EMBED, 70, workerId, leaseUntil());
            unitMapper.updateIngestStatus(unit.getId(), "embedding");
            List<KnowledgeChunk> stored = chunkMapper.selectByUnitId(unit.getId());
            if (stored.isEmpty()) {
                throw new BusinessException(ErrorCode.AI_PROCESS_FAILED, "无切片可向量化，请重新解析");
            }
            List<AiChunk> chunks = new ArrayList<>(stored.size());
            for (KnowledgeChunk chunk : stored) {
                AiChunk aiChunk = new AiChunk();
                aiChunk.setChunkIndex(chunk.getChunkIndex());
                aiChunk.setContent(chunk.getContent());
                // heading_path 必须还原：AI 侧 chunks_hash 由 index+content+heading_path 计算
                aiChunk.setHeadingPath(JsonUtils.toStringList(chunk.getHeadingPathJson()));
                chunks.add(aiChunk);
            }
            withRetry(job, IngestJobService.STAGE_EMBED, () -> {
                aiClient.embed(unit.getUnitCode(), chunks, unit.getContentHash(), unit.getChunksHash(),
                        "ig-" + job.getId());
                return null;
            });

            // 6) finalize：权限在 embed 成功后、active 前写入（DESIGN.md §6.4）
            jobService.advance(job, IngestJobService.STAGE_FINALIZE, 90, workerId, leaseUntil());
            if (unitPermissionService.listByUnit(unit.getId()).isEmpty()) {
                unitPermissionService.applyDefaultByCategory(unit.getId(), unit.getCategory());
            }
            // 最终发布 CAS（F-24/F-03）：仅当单元仍为 active 且仍由本作业驱动（ingest_status=embedding）
            // 才发布为可检索。入库期间被禁用（disabled）/删除（deleting）/被更新重新入队（queued）
            // 时一律不得覆盖其当前状态，宁可作业失败也不"复活"单元。
            int published = unitMapper.publishIfActive(unit.getId());
            if (published == 0) {
                jobService.markFailed(job, "UNIT_STATE_CHANGED",
                        "单元状态在入库期间发生变更（禁用/删除/重新入队），已终止发布", workerId);
                log.warn("publish skipped: unit state changed during ingest unitId={} unitCode={}",
                        unit.getId(), unit.getUnitCode());
                return;
            }
            jobService.markDone(job, workerId);
            log.info("ingest done unitId={} unitCode={} chunks={}", unit.getId(), unit.getUnitCode(), chunks.size());
        } catch (RetryScheduled ex) {
            // 可重试失败已延迟重排，静待下次轮询重新领取
        } catch (AiException ex) {
            handleAiFailure(unit, job, ex);
        } catch (BusinessException ex) {
            jobService.markFailed(job, "INGEST_FAILED", ex.getMessage(), workerId);
            unitMapper.updateIngestStatus(unit.getId(), "failed");
        }
    }

    private void persistChunks(Long unitId, ParseResult result) {
        chunkMapper.deleteByUnitId(unitId);
        if (result.getChunks() == null || result.getChunks().isEmpty()) {
            return;
        }
        List<KnowledgeChunk> chunks = new ArrayList<>(result.getChunks().size());
        for (AiChunk aiChunk : result.getChunks()) {
            KnowledgeChunk chunk = new KnowledgeChunk();
            chunk.setUnitId(unitId);
            chunk.setChunkIndex(aiChunk.getChunkIndex());
            chunk.setContent(aiChunk.getContent());
            chunk.setHeadingPathJson(JsonUtils.toJson(aiChunk.getHeadingPath()));
            chunks.add(chunk);
        }
        chunkMapper.insertBatch(chunks);
    }

    private void checkChunkQuality(ParseResult result) {
        if (result.getChunks() == null || result.getChunks().isEmpty()) {
            throw new BusinessException(ErrorCode.AI_PROCESS_FAILED, "解析结果无切片，请检查文档内容");
        }
        var quality = result.getQuality();
        if (quality == null) {
            return;
        }
        if (quality.getEmptyChunkRatio() != null && quality.getEmptyChunkRatio() > EMPTY_RATIO_THRESHOLD) {
            throw new BusinessException(ErrorCode.AI_PROCESS_FAILED,
                    "空切片比例过高（" + quality.getEmptyChunkRatio() + "），建议更换清晰版本文档");
        }
        if (quality.getAvgChunkChars() != null && quality.getAvgChunkChars() < MIN_AVG_CHUNK_CHARS) {
            throw new BusinessException(ErrorCode.AI_PROCESS_FAILED,
                    "平均切片过短（" + quality.getAvgChunkChars() + "），可能为扫描件或乱码");
        }
    }

    private void applySuggestedCategory(KnowledgeUnit unit, ParseResult result) {
        String suggested = result.getSuggestedCategory();
        if (suggested != null && KnowledgeEnums.CATEGORIES.contains(suggested)) {
            unit.setCategory(suggested);
        }
    }

    private boolean isTerminating(KnowledgeUnit unit) {
        return "deleting".equals(unit.getStatus()) || "superseded".equals(unit.getStatus());
    }

    /**
     * 执行动作；遇可重试的 AI 失败时**延迟重排**而非阻塞重试。
     *
     * <p>与原内联 {@code Thread.sleep} 的语义差异：原实现占住 Worker 线程等待退避；
     * 现实现把作业重置回可领取阶段并持久化下次可见时间（{@code not_before}），然后抛出
     * {@link RetryScheduled} 中止本次处理，交由后续 {@link #poll()} 在退避到期后重新领取。
     * 尝试次数上限仍为 {@link IngestJobService#MAX_ATTEMPT}（含首次），超限则抛出原异常走失败。
     *
     * @param job        作业
     * @param retryStage 重排回的可领取阶段（queued / embed）
     * @param action     实际操作
     * @param <T>        返回类型
     * @return 动作结果
     */
    private <T> T withRetry(IngestJob job, String retryStage, Supplier<T> action) {
        try {
            return action.get();
        } catch (AiException ex) {
            int nextAttempt = (job.getAttempt() == null ? 0 : job.getAttempt()) + 1;
            if (isRetryable(ex) && nextAttempt < IngestJobService.MAX_ATTEMPT) {
                long backoffMs = retryBackoffMs << (nextAttempt - 1);
                LocalDateTime notBefore = backoffMs > 0
                        ? LocalDateTime.now().plus(backoffMs, ChronoUnit.MILLIS) : null;
                jobService.resetForRetry(job, retryStage, notBefore, workerId);
                if (notBefore != null) {
                    // 进程内快速判断 + 数据库权威来源（多实例共享）
                    this.notBefore.put(job.getId(), System.currentTimeMillis() + backoffMs);
                }
                log.warn("retryable ai failure jobId={} code={} attempt={} backoffMs={} -> 延迟重排",
                        job.getId(), ex.getAiErrorCode(), job.getAttempt(), backoffMs);
                throw RetryScheduled.INSTANCE;
            }
            throw ex;
        }
    }

    private boolean isRetryable(AiException ex) {
        if (ex.getErrorCode() == ErrorCode.AI_UNAVAILABLE) {
            return true;
        }
        return ex.getAiErrorCode() != null && RETRYABLE_CODES.contains(ex.getAiErrorCode());
    }

    private void handleAiFailure(KnowledgeUnit unit, IngestJob job, AiException ex) {
        String code = ex.getAiErrorCode() == null ? "AI_FAILED" : ex.getAiErrorCode();
        String message = buildActionableMessage(code, ex.getMessage());
        jobService.markFailed(job, code, message, workerId);
        unitMapper.updateIngestStatus(unit.getId(), "failed");
        log.error("ingest failed unitId={} code={} message={}", unit.getId(), code, message);
    }

    private String buildActionableMessage(String code, String detail) {
        String suggestion = switch (code) {
            case "MINERU_TIMEOUT", "PARSE_TIMEOUT" -> "解析超时，可稍后重试或拆分文档后重新导入。";
            case "SOURCE_FETCH_FAILED" -> "源文件读取失败，请确认文件完整后重试。";
            case "PARSE_FAILED" -> "解析失败，常见原因为 PDF 加密或格式损坏；请提供未加密文件后重试。";
            case "EMBED_FAILED", "EMBED_TOO_MANY_CHUNKS" -> "向量化失败，可重试或减少文档规模。";
            case "AI_UNAVAILABLE" -> "AI 服务不可用，请稍后重试。";
            default -> "请稍后重试，或联系管理员查看服务日志。";
        };
        return suggestion + "（" + code + "）";
    }

    /**
     * 内部控制流信号：可重试失败已被延迟重排，需中止当前处理等待下次轮询。
     *
     * <p>非错误（关闭栈追踪），仅用于跳出当前阶段，不写日志、不置失败。
     */
    private static final class RetryScheduled extends RuntimeException {

        private static final long serialVersionUID = 1L;

        static final RetryScheduled INSTANCE = new RetryScheduled();

        private RetryScheduled() {
            super(null, null, false, false);
        }
    }
}

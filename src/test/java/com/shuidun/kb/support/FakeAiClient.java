package com.shuidun.kb.support;

import com.shuidun.kb.ai.AiClient;
import com.shuidun.kb.ai.AiGenerateMode;
import com.shuidun.kb.ai.GenerateStreamHandler;
import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.ai.dto.EmbedResult;
import com.shuidun.kb.ai.dto.GenerateContext;
import com.shuidun.kb.ai.dto.ParseQuality;
import com.shuidun.kb.ai.dto.ParseResult;
import com.shuidun.kb.ai.dto.RetrieveResult;
import com.shuidun.kb.ai.dto.RetrievedChunk;
import com.shuidun.kb.ai.dto.ReviewRequest;
import com.shuidun.kb.ai.dto.ReviewResult;
import com.shuidun.kb.ai.dto.SessionTurn;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 测试用内存版 AI 客户端（无外网依赖），用于真实验证 Java 侧编排与状态机。
 */
public class FakeAiClient implements AiClient {

    /** 内存向量库：unitCode → 切片。 */
    public final Map<String, List<AiChunk>> vectorStore = new ConcurrentHashMap<>();

    /** parse 返回的建议分类，用例可覆盖。 */
    public volatile String suggestedCategory = "sop";

    /** 覆盖 parse 返回的 review_reasons；为 null 时按 suggestedCategory 推导（product → product_manual_heuristic）。 */
    public volatile List<String> reviewReasonsOverride = null;

    /** 质检 review 结论桩。 */
    public volatile String reviewVerdict = "pass";
    public volatile double reviewConfidence = 0.95;
    public volatile String rulesTier = "pass";
    public volatile String llmTier = "pass";
    public volatile String vlmTier = "pass";

    /** 模拟 review 失败（抛 AiException）。 */
    public volatile boolean failReview = false;

    /** review 调用次数。 */
    public final AtomicInteger reviewCalls = new AtomicInteger();

    /** parse 是否模拟失败（加密 PDF 等不可重试错误）。 */
    public volatile boolean failParse = false;

    /** parse 是否模拟未预期运行时异常（F-02：worker 未预期异常必须落失败态）。 */
    public volatile boolean parseThrowsUnexpected = false;

    /** parse 是否模拟可重试失败（前 N 次）。 */
    public final AtomicInteger parseFailTimes = new AtomicInteger(0);

    /** embed 阻塞闸门（F-03/F-24：模拟 embed 在途期间发生删除/禁用）。 */
    public volatile CountDownLatch embedBlockLatch;

    /** embed 阻塞期间是否放行后继续（true 正常写向量；false 直接返回不写）。 */
    public volatile boolean embedBlockThenSkip = false;

    /** deleteVectors 模拟失败次数（F-04：首次 503、第二次成功的补偿回归）。 */
    public final AtomicInteger deleteFailTimes = new AtomicInteger(0);

    /** deleteVectors 调用次数（tombstone 重放/补偿断言）。 */
    public final AtomicInteger deleteVectorsCalls = new AtomicInteger();

    /** generate 模拟 SSE error 终态（F-23：error 后不得再记成功/发 done）。 */
    public volatile boolean generateError = false;

    /** generate 模拟流截断（F-23：只有 delta、无 done/error，不得记成功）。 */
    public volatile boolean generateTruncate = false;

    /** 记录调用次数。 */
    public final AtomicInteger parseCalls = new AtomicInteger();
    /** 检索调用次数：用于断言「澄清点选」等路径确实跳过了向量检索。 */
    public final AtomicInteger retrieveCalls = new AtomicInteger();

    /** 按 unitCode 统计 parse 调用次数：用于断言同一作业不会被重复领取执行。 */
    public final Map<String, AtomicInteger> parseCallsByUnit = new ConcurrentHashMap<>();

    /** 强制追加返回的 unit 编码（用于模拟 AI 越权召回，验证 Java 侧兜底过滤）。 */
    public volatile List<String> forcedRetrieveUnitCodes = List.of();

    /** 最近一次 generate 收到的 contexts（用于断言未授权内容未进入 Prompt）。 */
    public final List<GenerateContext> lastGenerateContexts =
            java.util.Collections.synchronizedList(new ArrayList<>());

    /** 最近一次 generate 的模式（用于断言闲聊兜底确实走 GENERAL）。 */
    public volatile AiGenerateMode lastGenerateMode;

    /** 闲聊兜底（GENERAL）返回的可区分文案。 */
    public static final String GENERAL_ANSWER = "这是通用知识回答。";

    /** 最近一次 retrieve/generate 收到的会话参数（用于断言接线）。 */
    public volatile String lastSessionId;
    public volatile String lastOwnerId;
    public volatile String lastTurnId;
    public volatile List<String> lastAclUnitCodes = List.of();
    public volatile boolean lastWriteHistory;

    /** 收到的 appendSessionTurn 调用（用于断言 FAQ/澄清/拒答轮次也落会话记忆）。 */
    public final List<SessionTurn> appendCalls =
            java.util.Collections.synchronizedList(new ArrayList<>());

    /** 模拟 append 失败（验证 Java 侧吞异常、不破坏 SSE）。 */
    public volatile boolean failAppend = false;

    @Override
    public ParseResult parse(byte[] content, String filename, String fileType, String unitId,
                             int chunkSize, boolean useVlm, String ingestId) {
        parseCalls.incrementAndGet();
        parseCallsByUnit.computeIfAbsent(unitId, key -> new AtomicInteger()).incrementAndGet();
        if (parseThrowsUnexpected) {
            throw new IllegalStateException("simulated unexpected worker crash");
        }
        if (failParse) {
            throw new com.shuidun.kb.ai.AiException(com.shuidun.kb.common.ErrorCode.AI_PROCESS_FAILED,
                    "PARSE_FAILED", "PDF 加密，无法读取");
        }
        if (parseFailTimes.get() > 0) {
            parseFailTimes.decrementAndGet();
            throw new com.shuidun.kb.ai.AiException(com.shuidun.kb.common.ErrorCode.AI_PROCESS_FAILED,
                    "MINERU_TIMEOUT", "MinerU 轮询超时");
        }
        ParseResult result = new ParseResult();
        result.setUnitId(unitId);
        result.setMarkdown("# 测试文档\n\n第一段正文内容。\n\n第二段正文内容。");
        List<AiChunk> chunks = new ArrayList<>();
        chunks.add(chunk(0, "第一段正文内容，用于验证切片与向量化链路。", List.of("安装")));
        chunks.add(chunk(1, "第二段正文内容，用于验证切片与向量化链路。", List.of("安装", "电源")));
        result.setChunks(chunks);
        ParseQuality quality = new ParseQuality();
        quality.setChunkCount(chunks.size());
        quality.setEmptyChunkRatio(0.0);
        quality.setAvgChunkChars(24.0);
        quality.setHeadingCount(1);
        quality.setSuggestReview("product".equals(suggestedCategory));
        List<String> reasons = reviewReasonsOverride != null
                ? reviewReasonsOverride
                : ("product".equals(suggestedCategory) ? List.of("product_manual_heuristic") : List.of());
        quality.setReviewReasons(reasons);
        result.setQuality(quality);
        result.setContentHash("content-hash-" + unitId);
        // 与真实 AI 一致：chunks_hash 由 chunk_index + content + heading_path 计算
        result.setChunksHash(chunksFingerprint(chunks));
        result.setSuggestedCategory(suggestedCategory);
        result.setPageCount(1);
        result.setFileType(fileType);
        return result;
    }

    @Override
    public ReviewResult review(ReviewRequest request) {
        reviewCalls.incrementAndGet();
        if (failReview) {
            throw new com.shuidun.kb.ai.AiException(com.shuidun.kb.common.ErrorCode.AI_UNAVAILABLE,
                    "REVIEW_TIMEOUT", "review 超时");
        }
        ReviewResult result = new ReviewResult();
        result.setVerdict(reviewVerdict);
        result.setConfidence(reviewConfidence);
        result.setReasons(new ArrayList<>(List.of(
                "rules:" + rulesTier, "llm:" + llmTier, "vlm:" + vlmTier)));
        ReviewResult.Tiers tiers = new ReviewResult.Tiers();
        tiers.setRules(rulesTier);
        tiers.setLlm(llmTier);
        tiers.setVlm(vlmTier);
        result.setTiers(tiers);
        result.setModel("fake-model");
        ReviewResult.StageTimings timings = new ReviewResult.StageTimings();
        timings.setLlmMs(1.0);
        timings.setVlmMs(2.0);
        result.setStageTimings(timings);
        result.setIngestId(request.getIngestId());
        return result;
    }

    @Override
    public EmbedResult embed(String unitId, List<AiChunk> chunks, String contentHash,
                             String chunksHash, String ingestId) {
        CountDownLatch block = embedBlockLatch;
        if (block != null) {
            try {
                block.await(30, TimeUnit.SECONDS);
            } catch (InterruptedException ex) {
                Thread.currentThread().interrupt();
            }
        }
        if (embedBlockThenSkip) {
            EmbedResult skipped = new EmbedResult();
            skipped.setUnitId(unitId);
            skipped.setEmbeddedCount(0);
            skipped.setDim(1024);
            skipped.setPartial(false);
            skipped.setIngestId(ingestId);
            return skipped;
        }
        // 与真实 AI 一致：提供 chunks_hash 时必须校验，否则 409 CHUNKS_HASH_MISMATCH。
        // 这样一旦 Java 侧在"从库中重建切片"时丢了 heading_path，测试会立刻失败。
        if (chunksHash != null && !chunksHash.isBlank()) {
            String computed = chunksFingerprint(chunks);
            if (!computed.equals(chunksHash)) {
                throw new com.shuidun.kb.ai.AiException(com.shuidun.kb.common.ErrorCode.AI_PROCESS_FAILED,
                        "CHUNKS_HASH_MISMATCH", "request chunks_hash does not match provided chunks");
            }
        }
        vectorStore.put(unitId, new ArrayList<>(chunks));
        EmbedResult result = new EmbedResult();
        result.setUnitId(unitId);
        result.setEmbeddedCount(chunks.size());
        result.setDim(1024);
        result.setPartial(false);
        result.setIngestId(ingestId);
        return result;
    }

    /**
     * 复刻 AI 侧 ai/ingest/fingerprint.py 的算法：sha256(每片: idx \x1f content \x1f join(hp,"\x1e") \x1e)。
     *
     * @param chunks 切片
     * @return 十六进制指纹
     */
    private static String chunksFingerprint(List<AiChunk> chunks) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            for (AiChunk c : chunks) {
                int idx = c.getChunkIndex() == null ? 0 : c.getChunkIndex();
                String content = c.getContent() == null ? "" : c.getContent();
                List<String> hp = c.getHeadingPath() == null ? List.of() : c.getHeadingPath();
                md.update(String.valueOf(idx).getBytes(java.nio.charset.StandardCharsets.UTF_8));
                md.update((byte) 0x1f);
                md.update(content.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                md.update((byte) 0x1f);
                md.update(String.join("\u001e", hp).getBytes(java.nio.charset.StandardCharsets.UTF_8));
                md.update((byte) 0x1e);
            }
            StringBuilder hex = new StringBuilder();
            for (byte b : md.digest()) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (java.security.NoSuchAlgorithmException ex) {
            throw new IllegalStateException(ex);
        }
    }

    @Override
    public RetrieveResult retrieve(String question, int topK, List<String> aclUnitIds, List<String> denyUnitIds,
                                   String sessionId, String ownerId, boolean enableSessionRewrite) {
        retrieveCalls.incrementAndGet();
        lastSessionId = sessionId;
        lastOwnerId = ownerId;
        lastAclUnitCodes = aclUnitIds == null ? List.of() : List.copyOf(aclUnitIds);
        List<RetrievedChunk> chunks = new ArrayList<>();
        java.util.LinkedHashSet<String> codes = new java.util.LinkedHashSet<>(aclUnitIds);
        codes.addAll(forcedRetrieveUnitCodes);
        int index = 0;
        for (String unitCode : codes) {
            if (!vectorStore.containsKey(unitCode)) {
                continue;
            }
            for (AiChunk chunk : vectorStore.get(unitCode)) {
                RetrievedChunk hit = new RetrievedChunk();
                hit.setUnitId(unitCode);
                hit.setChunkId(unitCode + "-" + chunk.getChunkIndex());
                hit.setScore(0.9 - index * 0.1);
                hit.setContent(chunk.getContent());
                chunks.add(hit);
                index++;
                if (chunks.size() >= topK) {
                    break;
                }
            }
            if (chunks.size() >= topK) {
                break;
            }
        }
        RetrieveResult result = new RetrieveResult();
        result.setChunks(chunks);
        result.setAclApplied(true);
        result.setHybrid(true);
        result.setDegradedToDense(false);
        return result;
    }

    @Override
    public void generate(String question, List<GenerateContext> contexts, String sessionId, AiGenerateMode mode,
                         String ownerId, String turnId, List<String> aclUnitIds, Long chosenUnitId,
                         boolean writeHistory, GenerateStreamHandler handler) {
        lastGenerateMode = mode;
        lastSessionId = sessionId;
        lastOwnerId = ownerId;
        lastTurnId = turnId;
        lastAclUnitCodes = aclUnitIds == null ? List.of() : List.copyOf(aclUnitIds);
        lastWriteHistory = writeHistory;
        lastGenerateContexts.clear();
        lastGenerateContexts.addAll(contexts);
        if (generateError) {
            // F-23：AI SSE error 终态；handler 之后不得再有 done
            handler.onDelta("部分输出");
            handler.onError("GENERATE_FAILED", "模型服务异常");
            return;
        }
        if (generateTruncate) {
            // F-23：流被截断（EOF），无 done 也无 error
            handler.onDelta("半截答案");
            return;
        }
        if (mode == AiGenerateMode.GENERAL) {
            // 闲聊兜底：文案与 kb 路径可区分，便于测试断言"确实走了通用知识"
            handler.onDelta(GENERAL_ANSWER);
            handler.onDone(30, 8, 300L, false);
            return;
        }
        handler.onDelta("根据资料，");
        handler.onDelta("保修期为一年。");
        handler.onDone(120, 20, 500L, false);
    }

    @Override
    public void appendSessionTurn(SessionTurn turn) {
        if (failAppend) {
            // 模拟 AI 侧 append 失败：Java 侧必须吞掉异常，绝不破坏 SSE
            throw new IllegalStateException("simulated append failure");
        }
        appendCalls.add(turn);
    }

    @Override
    public int deleteVectors(String unitId) {
        deleteVectorsCalls.incrementAndGet();
        if (deleteFailTimes.get() > 0) {
            deleteFailTimes.decrementAndGet();
            throw new com.shuidun.kb.ai.AiException(com.shuidun.kb.common.ErrorCode.AI_UNAVAILABLE,
                    "DELETE_TIMEOUT", "delete 上游 503");
        }
        List<AiChunk> removed = vectorStore.remove(unitId);
        return removed == null ? 0 : removed.size();
    }

    @Override
    public String vlmDescribe(byte[] content, String filename, String contentType) {
        return "测试图注";
    }

    private AiChunk chunk(int index, String content) {
        return chunk(index, content, List.of());
    }

    private AiChunk chunk(int index, String content, List<String> headingPath) {
        AiChunk chunk = new AiChunk();
        chunk.setChunkIndex(index);
        chunk.setContent(content);
        chunk.setHeadingPath(new ArrayList<>(headingPath));
        return chunk;
    }
}

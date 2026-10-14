package com.shuidun.kb.job;

import com.shuidun.kb.ai.AiClient;
import com.shuidun.kb.ai.AiException;
import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.ai.dto.ParseImage;
import com.shuidun.kb.ai.dto.ParseResult;
import com.shuidun.kb.ai.dto.ReviewRequest;
import com.shuidun.kb.ai.dto.ReviewResult;
import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.config.QualityReviewProperties;
import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.job.entity.IngestReview;
import com.shuidun.kb.job.mapper.IngestReviewMapper;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

/**
 * 入库质检判定编排（阶段 2）：硬失败门 → 风险分级 → 规则档 → LLM/VLM 档，全程 fail-closed。
 *
 * <p>硬失败门（空切片比例 / 平均切片长度）已由 {@code IngestWorker.checkChunkQuality} 完成，此处不重复。
 *
 * <p><b>不拖垮 Worker</b>：AI 档失败直接转人工，不做阻塞式重试；熔断打开时直接转人工。
 * 只有 {@code contentWarn || imageHigh} 的文档才调用 AI（绝大多数走规则档零成本）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class QualityReviewService {

    /** 决策结果。 */
    public enum Decision {
        /** 自动通过，继续 embed。 */
        AUTO_PASS,
        /** 转人工质检门。 */
        HUMAN_REQUIRED
    }

    /** 决策 + 审计理由。 */
    public record Outcome(Decision decision, String reason) {
    }

    private static final String DECIDER_AI = "ai";
    private static final String DECISION_AUTO_PASS = "auto_pass";
    private static final String DECISION_HUMAN_REQUIRED = "human_required";

    private final QualityReviewProperties props;
    private final ReviewCircuitBreaker circuitBreaker;
    private final AiClient aiClient;
    private final IngestReviewMapper ingestReviewMapper;

    /**
     * 对单元做质检判定。
     *
     * @param unit   知识单元（内存态，已带 contentHash / category）
     * @param result parse 结果（内存态，含 quality / chunks / markdown / images）
     * @param job    入库作业
     * @return 决策与理由
     */
    public Outcome review(KnowledgeUnit unit, ParseResult result, IngestJob job) {
        // 总开关关闭：完全回到历史行为（仅 product 停门），不调用 AI、不写审计
        if (!props.isEnabled()) {
            return legacyDecision(unit.getCategory());
        }
        // ② 风险分级（先取该文档的 parse 复核原因，供审计回填）
        List<String> reviewReasons = result.getQuality() == null ? null : result.getQuality().getReviewReasons();
        if (!QualityReviewPolicy.isAutoCategory(unit.getCategory(), props)) {
            // manual 类别：默认先跑 AI 预审（结论仅作预审意见），决策恒为人工
            if (props.isPreReviewManualCategories()) {
                return preReviewManualCategory(unit, result, job, reviewReasons);
            }
            audit(unit, job, DECISION_HUMAN_REQUIRED, "risk_category", "skipped", null, null, reviewReasons);
            return new Outcome(Decision.HUMAN_REQUIRED, "risk_category");
        }
        boolean imageHeavy = QualityReviewPolicy.isImageHeavy(reviewReasons);
        // ③ 规则档干净：零 AI 成本直接通过（规则档确定性判定为满置信）
        if (!QualityReviewPolicy.needsAiTier(reviewReasons)) {
            audit(unit, job, DECISION_AUTO_PASS, "rules_clean", "pass", 1.0, null, reviewReasons);
            return new Outcome(Decision.AUTO_PASS, "rules_clean");
        }
        // ④ LLM/VLM 档：档位开关的 fail-closed（规则档已告警 → rules_verdict=review）
        boolean contentWarn = reviewReasons != null
                && (reviewReasons.contains(QualityReviewPolicy.REASON_EMPTY_CHUNKS)
                || reviewReasons.contains(QualityReviewPolicy.REASON_LOW_HEADING_COVERAGE));
        if (contentWarn && !props.isLlmEnabled()) {
            audit(unit, job, DECISION_HUMAN_REQUIRED, "llm_disabled", "review", null, null, reviewReasons);
            return new Outcome(Decision.HUMAN_REQUIRED, "llm_disabled");
        }
        if (imageHeavy && props.isVlmRequired() && !props.isVlmEnabled()) {
            audit(unit, job, DECISION_HUMAN_REQUIRED, "vlm_unavailable", "review", null, null, reviewReasons);
            return new Outcome(Decision.HUMAN_REQUIRED, "vlm_unavailable");
        }
        // 熔断打开：直接 fail-closed，不再调用 AI
        if (circuitBreaker.isOpen()) {
            audit(unit, job, DECISION_HUMAN_REQUIRED, "circuit_open", "review", null, null, reviewReasons);
            return new Outcome(Decision.HUMAN_REQUIRED, "circuit_open");
        }
        ReviewRequest request = buildRequest(unit, result, job);
        ReviewResult resp;
        try {
            resp = aiClient.review(request);
            circuitBreaker.recordSuccess();
        } catch (AiException ex) {
            circuitBreaker.recordFailure();
            String code = ex.getAiErrorCode() == null ? "AI_FAILED" : ex.getAiErrorCode();
            String reason = "ai_error:" + code;
            log.warn("ai review failed unitId={} code={} -> fail-closed 转人工", unit.getId(), code);
            audit(unit, job, DECISION_HUMAN_REQUIRED, reason, "review", null, null, reviewReasons);
            return new Outcome(Decision.HUMAN_REQUIRED, reason);
        }
        Outcome outcome = QualityReviewPolicy.decide(resp, imageHeavy, props);
        audit(unit, job, outcome.decision() == Decision.AUTO_PASS ? DECISION_AUTO_PASS : DECISION_HUMAN_REQUIRED,
                outcome.reason(), null, null, resp, reviewReasons);
        return outcome;
    }

    /**
     * manual 类别「AI 预审 + 人工确认」：走与 auto 类别相同的 AI 调用链
     * （熔断检查 → {@code aiClient.review} → 失败记 {@code ai_error:<code>}），
     * 但<b>最终决策恒为 {@link Decision#HUMAN_REQUIRED}、reason 恒为 {@code risk_category}</b>。
     *
     * <p>AI 失败/超时/熔断不改变决策（人工本就是决策者），<b>不做 fail-closed</b>，
     * 只如实记录 AI 是否可用与结论。AI 成功时把原始结论落审计（detail_json 顶层），
     * 供批次详情暴露 {@code aiVerdict/aiReasons/aiModel} 作为预审意见。
     *
     * @param unit          单元
     * @param result        parse 结果
     * @param job           入库作业
     * @param reviewReasons 该文档的 parse 复核原因
     * @return 恒为 HUMAN_REQUIRED / risk_category
     */
    private Outcome preReviewManualCategory(KnowledgeUnit unit, ParseResult result, IngestJob job,
                                            List<String> reviewReasons) {
        // 熔断打开：不调用 AI，如实记录不可用，决策不受影响
        if (circuitBreaker.isOpen()) {
            auditManualPreReview(unit, job, null, "circuit_open", reviewReasons);
            return new Outcome(Decision.HUMAN_REQUIRED, "risk_category");
        }
        ReviewRequest request = buildRequest(unit, result, job);
        ReviewResult resp;
        try {
            resp = aiClient.review(request);
            circuitBreaker.recordSuccess();
        } catch (AiException ex) {
            circuitBreaker.recordFailure();
            String code = ex.getAiErrorCode() == null ? "AI_FAILED" : ex.getAiErrorCode();
            log.warn("manual pre-review ai failed unitId={} code={} -> 仍人工决策", unit.getId(), code);
            auditManualPreReview(unit, job, null, code, reviewReasons);
            return new Outcome(Decision.HUMAN_REQUIRED, "risk_category");
        }
        auditManualPreReview(unit, job, resp, null, reviewReasons);
        return new Outcome(Decision.HUMAN_REQUIRED, "risk_category");
    }

    /** 关闭总开关时的历史行为：仅 product 停门，其余自动放行。 */
    private Outcome legacyDecision(String category) {
        if ("product".equals(category)) {
            return new Outcome(Decision.HUMAN_REQUIRED, "legacy_product_gate");
        }
        return new Outcome(Decision.AUTO_PASS, "legacy_auto");
    }

    /** 从内存中的 parse 结果构造 review 请求，不额外查库。 */
    private ReviewRequest buildRequest(KnowledgeUnit unit, ParseResult result, IngestJob job) {
        ReviewRequest request = new ReviewRequest();
        request.setUnitId(unit.getUnitCode());
        request.setCategory(unit.getCategory());
        request.setMarkdown(result.getMarkdown());
        request.setQuality(result.getQuality());
        request.setContentHash(unit.getContentHash());
        request.setIngestId(job == null ? null : "ig-" + job.getId());
        request.setEnableLlm(props.isLlmEnabled());
        request.setEnableVlm(props.isVlmEnabled());
        if (result.getChunks() != null) {
            List<ReviewRequest.Chunk> chunks = new ArrayList<>(result.getChunks().size());
            for (AiChunk chunk : result.getChunks()) {
                ReviewRequest.Chunk c = new ReviewRequest.Chunk();
                c.setChunkIndex(chunk.getChunkIndex());
                c.setContent(chunk.getContent());
                c.setHeadingPath(chunk.getHeadingPath() == null ? new ArrayList<>() : chunk.getHeadingPath());
                chunks.add(c);
            }
            request.setChunks(chunks);
        }
        if (result.getImages() != null) {
            List<ReviewRequest.Image> images = new ArrayList<>(result.getImages().size());
            for (ParseImage image : result.getImages()) {
                ReviewRequest.Image img = new ReviewRequest.Image();
                img.setRelativePath(image.getRelativePath());
                images.add(img);
            }
            request.setImages(images);
        }
        return request;
    }

    /**
     * 写一行质检审计。审计失败绝不阻断入库主链路。
     *
     * <p>无论走哪条路径都填齐：{@code rules_verdict}、{@code review_reasons}、{@code detail_json}（含 reason）；
     * AI 真实跑通时再回填 {@code llm/vlm_verdict}、{@code llm_confidence}、{@code ai_model}、档位耗时。
     *
     * @param unit          单元
     * @param job           作业（可为 null）
     * @param decision      auto_pass / human_required
     * @param reason        决策理由（写入 detail_json）
     * @param rulesVerdict  规则档结论（resp 为 null 时使用；resp 非空则由 resp.tiers.rules 覆盖）
     * @param confidence    置信度（resp 为 null 时使用，如 rules_clean 的 1.0；resp 非空则由 resp.confidence 覆盖）
     * @param resp          AI 响应（规则档/异常/熔断时为 null）
     * @param reviewReasons 该文档的 parse 复核原因（回填 review_reasons 列）
     */
    private void audit(KnowledgeUnit unit, IngestJob job, String decision, String reason,
                       String rulesVerdict, Double confidence, ReviewResult resp, List<String> reviewReasons) {
        writeReview(unit, job, decision, reason, rulesVerdict, confidence, resp, reviewReasons, false, null);
    }

    /**
     * manual 类别「AI 预审」审计：决策恒为 {@code human_required}、reason 恒为 {@code risk_category}，
     * 仅记录 AI 预审结论（resp 非空）或不可用原因（aiError 非空）。
     *
     * <p>detail_json 顶层：AI 可用时写 {@code aiPreReview=true} + AI 原始结论（与 AI 已调用行形状一致）；
     * AI 不可用时写 {@code aiError}。<b>不新增数据库列。</b>
     *
     * @param unit          单元
     * @param job           作业（可为 null）
     * @param resp          AI 响应（AI 不可用时为 null）
     * @param aiError       AI 不可用错误码（AI 可用时为 null）
     * @param reviewReasons 该文档的 parse 复核原因
     */
    private void auditManualPreReview(KnowledgeUnit unit, IngestJob job, ReviewResult resp, String aiError,
                                      List<String> reviewReasons) {
        writeReview(unit, job, DECISION_HUMAN_REQUIRED, "risk_category", null, null, resp, reviewReasons,
                true, aiError);
    }

    private void writeReview(KnowledgeUnit unit, IngestJob job, String decision, String reason,
                             String rulesVerdict, Double confidence, ReviewResult resp, List<String> reviewReasons,
                             boolean aiPreReview, String aiError) {
        try {
            IngestReview review = new IngestReview();
            review.setUnitId(unit.getId());
            review.setJobId(job == null ? null : job.getId());
            review.setBatchId(unit.getBatchId());
            review.setCategory(unit.getCategory());
            review.setDecision(decision);
            review.setDecider(DECIDER_AI);
            review.setRulesVerdict(rulesVerdict);
            review.setLlmConfidence(confidence);
            if (resp != null) {
                if (resp.getTiers() != null) {
                    if (resp.getTiers().getRules() != null) {
                        review.setRulesVerdict(resp.getTiers().getRules());
                    }
                    review.setLlmVerdict(resp.getTiers().getLlm());
                    review.setVlmVerdict(resp.getTiers().getVlm());
                }
                if (resp.getConfidence() != null) {
                    review.setLlmConfidence(resp.getConfidence());
                }
                review.setAiModel(resp.getModel());
                if (resp.getStageTimings() != null) {
                    review.setLlmMs(toMillis(resp.getStageTimings().getLlmMs()));
                    review.setVlmMs(toMillis(resp.getStageTimings().getVlmMs()));
                }
            }
            review.setReviewReasons(joinReasons(reviewReasons));
            review.setDetailJson(detailJson(reason, resp, aiPreReview, aiError));
            ingestReviewMapper.insert(review);
        } catch (RuntimeException ex) {
            log.warn("write ingest review audit failed unitId={} reason={}: {}", unit.getId(), reason,
                    ex.getMessage());
        }
    }

    /** review_reasons 列：逗号拼接该文档的 parse 复核原因；为空则 null（语义：无 content/image 告警）。 */
    private String joinReasons(List<String> reviewReasons) {
        if (reviewReasons == null || reviewReasons.isEmpty()) {
            return null;
        }
        return String.join(",", reviewReasons);
    }

    /**
     * detail_json：AI 结论 + 决策理由（含 VLM findings 可读串），供阶段 4 聚合与批次详情读取。
     *
     * <p>manual 预审行额外在顶层写 {@code aiPreReview=true}（AI 可用时）；AI 不可用时写 {@code aiError}。
     * 批次详情只读顶层 {@code verdict / reasons} 字段，故 AI 原始结论必须平铺在顶层。
     *
     * @param reason      决策理由
     * @param resp        AI 响应（规则档/异常/熔断时为 null）
     * @param aiPreReview 是否为 manual 预审行
     * @param aiError     AI 不可用错误码（AI 可用时为 null）
     * @return JSON 字符串
     */
    private String detailJson(String reason, ReviewResult resp, boolean aiPreReview, String aiError) {
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("reason", reason);
        if (resp != null) {
            if (aiPreReview) {
                detail.put("aiPreReview", true);
            }
            detail.put("verdict", resp.getVerdict());
            detail.put("confidence", resp.getConfidence());
            detail.put("reasons", resp.getReasons());
            detail.put("model", resp.getModel());
            if (resp.getTiers() != null) {
                detail.put("tiers", resp.getTiers());
            }
            detail.put("vlmFindings", vlmFindingStrings(resp));
        } else if (aiError != null) {
            detail.put("aiError", aiError);
        }
        return JsonUtils.toJson(detail);
    }

    /** 把 {relative_path, usable, reason} 压成可读字符串。 */
    private List<String> vlmFindingStrings(ReviewResult resp) {
        List<String> out = new ArrayList<>();
        if (resp.getVlmFindings() == null) {
            return out;
        }
        for (ReviewResult.VlmFinding finding : resp.getVlmFindings()) {
            String path = finding.getRelativePath() == null ? "" : finding.getRelativePath();
            boolean usable = Boolean.TRUE.equals(finding.getUsable());
            String reason = finding.getReason() == null ? "" : finding.getReason();
            out.add(path + (usable ? " 可用" : " 不可用") + (reason.isBlank() ? "" : ": " + reason));
        }
        return out;
    }

    private int toMillis(Double value) {
        return value == null ? 0 : (int) Math.round(value);
    }
}

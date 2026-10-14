package com.shuidun.kb.job;

import com.shuidun.kb.ai.dto.ReviewResult;
import com.shuidun.kb.config.QualityReviewProperties;
import java.util.List;

/**
 * 质检判定策略（纯函数，无 Spring 依赖，便于单测）。
 *
 * <p>负责：风险分级（{@link #isAutoCategory}）、规则档触发闸（{@link #needsAiTier}）、
 * 图片型判定（{@link #isImageHeavy}）与 AI 响应→决策的映射（{@link #decide}）。
 *
 * <p><b>fail-closed 贯穿</b>：任何档位非 {@code pass}（且非规则档允许的 {@code skipped}）
 * 一律转人工；图片型手册在 {@code vlm-required=true} 时 VLM 必须 {@code pass}。
 */
public final class QualityReviewPolicy {

    /** 触发 AI 档的原因：空切片。 */
    public static final String REASON_EMPTY_CHUNKS = "empty_chunks";

    /** 触发 AI 档的原因：标题覆盖率低。 */
    public static final String REASON_LOW_HEADING_COVERAGE = "low_heading_coverage";

    /** 触发 AI 档的原因：图片数偏高（图片型手册）。 */
    public static final String REASON_IMAGE_COUNT_HIGH = "image_count_high";

    private QualityReviewPolicy() {
    }

    /**
     * 是否允许 AI 自动通过：命中 auto-categories 且不在 manual-categories。
     *
     * @param category 分类（可为 null）
     * @param props    质检配置
     * @return true 表示该分类可由 AI 自动判定
     */
    public static boolean isAutoCategory(String category, QualityReviewProperties props) {
        if (category == null || category.isBlank()) {
            return false;
        }
        if (props.getManualCategories() != null && props.getManualCategories().contains(category)) {
            return false;
        }
        return props.getAutoCategories() != null && props.getAutoCategories().contains(category);
    }

    /**
     * 规则档是否需要进入 AI 档。
     *
     * @param reviewReasons parse 复核原因（可为 null）
     * @return true 表示命中 content/image 告警，需要 LLM/VLM 档
     */
    public static boolean needsAiTier(List<String> reviewReasons) {
        if (reviewReasons == null || reviewReasons.isEmpty()) {
            return false;
        }
        return reviewReasons.contains(REASON_EMPTY_CHUNKS)
                || reviewReasons.contains(REASON_LOW_HEADING_COVERAGE)
                || reviewReasons.contains(REASON_IMAGE_COUNT_HIGH);
    }

    /**
     * 是否为图片型手册（需 VLM 抽检）。
     *
     * @param reviewReasons parse 复核原因
     * @return 含 image_count_high 返回 true
     */
    public static boolean isImageHeavy(List<String> reviewReasons) {
        return reviewReasons != null && reviewReasons.contains(REASON_IMAGE_COUNT_HIGH);
    }

    /**
     * 把 AI 响应映射为决策（fail-closed）。
     *
     * <p>规则顺序：① 非 pass 的 LLM 档（且非 skipped）→ 人工；② LLM 真实跑通（pass）时置信度
     * 缺失、NaN、越界或低于阈值 → 人工（fail-closed）；③ 图片型且 VLM 非 pass → 人工；
     * ④ 综合 verdict 非 pass → 人工；否则自动通过。
     *
     * @param resp       AI 响应（可为 null）
     * @param imageHeavy 是否图片型
     * @param props      质检配置
     * @return 决策与理由
     */
    public static QualityReviewService.Outcome decide(ReviewResult resp, boolean imageHeavy,
                                                      QualityReviewProperties props) {
        if (resp == null) {
            return human("ai_error");
        }
        ReviewResult.Tiers tiers = resp.getTiers();
        String llm = tiers == null ? null : tiers.getLlm();
        String vlm = tiers == null ? null : tiers.getVlm();
        // ① LLM 档：非 pass 且非 skipped（或缺失）→ 人工
        if (llm == null || (!"pass".equals(llm) && !"skipped".equals(llm))) {
            return human("llm_review");
        }
        // ② LLM 真实跑通（pass）时校验置信度阈值。
        // fail-closed：置信度缺失视为「无法证明满足阈值」，NaN/越界/低于阈值一律转人工，
        // 不允许 AI 响应不完整时绕过置信度门槛（F-30）。
        if ("pass".equals(llm)) {
            Double confidence = resp.getConfidence();
            if (confidence == null) {
                return human("confidence_missing");
            }
            if (!Double.isFinite(confidence) || confidence < 0.0 || confidence > 1.0) {
                return human("confidence_invalid");
            }
            if (confidence < props.getLlmConfidenceThreshold()) {
                return human("low_confidence");
            }
        }
        // ③ 图片型手册：VLM 必须 pass（vlm-required 的 fail-closed）
        if (imageHeavy && props.isVlmRequired() && !"pass".equals(vlm)) {
            return human(vlmUnusableReason(vlm));
        }
        // ④ 综合结论
        if (!"pass".equals(resp.getVerdict())) {
            return human("verdict_review");
        }
        return auto("ai_pass");
    }

    private static String vlmUnusableReason(String vlm) {
        if (vlm == null || "unavailable".equals(vlm) || "skipped".equals(vlm)) {
            return "vlm_unavailable";
        }
        return "vlm_unusable";
    }

    private static QualityReviewService.Outcome human(String reason) {
        return new QualityReviewService.Outcome(QualityReviewService.Decision.HUMAN_REQUIRED, reason);
    }

    private static QualityReviewService.Outcome auto(String reason) {
        return new QualityReviewService.Outcome(QualityReviewService.Decision.AUTO_PASS, reason);
    }
}

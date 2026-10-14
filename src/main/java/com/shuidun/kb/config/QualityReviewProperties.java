package com.shuidun.kb.config;

import java.util.ArrayList;
import java.util.List;
import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 入库质检门「AI 自动判定」配置（app.ingest.quality.*）。
 *
 * <p>全部字段带默认值：配置缺失不得导致启动失败。{@link #enabled} 为总开关，
 * 关闭即回到历史行为（仅 product 停门），可秒级回滚。
 */
@Data
@ConfigurationProperties(prefix = "app.ingest.quality")
public class QualityReviewProperties {

    /** 总开关；false = 历史行为（仅 product 人工）。 */
    private boolean enabled = true;

    /** 允许 AI 自动通过的类别。 */
    private List<String> autoCategories = new ArrayList<>(List.of("product", "sop"));

    /** 永远人工的类别（金额/价格/对外承诺）。 */
    private List<String> manualCategories = new ArrayList<>(List.of("finance", "sales", "policy"));

    /**
     * manual 类别是否先做「AI 预审 + 人工确认」。
     *
     * <p>true（默认）：仍跑完整 AI 审核链，结论仅作预审意见（落审计 + 批次详情暴露），
     * 最终决策恒为 {@code HUMAN_REQUIRED}（停在质检门）；AI 失败/超时/熔断不改变决策。
     * false：维持历史行为（不调 AI，直接转人工）。
     */
    private boolean preReviewManualCategories = true;

    /** LLM 档开关。 */
    private boolean llmEnabled = true;

    /** VLM 抽检档开关。 */
    private boolean vlmEnabled = true;

    /** image_count_high 时 VLM 不可用是否 fail-closed 转人工。 */
    private boolean vlmRequired = true;

    /** 自动通过所需最低置信度（LLM 真实跑通时校验）。 */
    private double llmConfidenceThreshold = 0.8;

    /** 熔断阈值：连续失败达到该次数即打开熔断。 */
    private int reviewCircuitFailureThreshold = 3;

    /** 熔断冷却时长（毫秒）。 */
    private long reviewCircuitCooldownMs = 60000;
}

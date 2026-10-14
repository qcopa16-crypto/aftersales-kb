package com.shuidun.kb.faq;

import com.shuidun.kb.faq.entity.Faq;
import com.shuidun.kb.faq.mapper.FaqMapper;
import java.time.Duration;
import java.util.Locale;
import java.util.Optional;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

/**
 * FAQ 答案缓存（DESIGN.md §6.2 / kb-design-spec.md §5.2）。
 *
 * <p>F-25 cache-aside：仅缓存 status=published 的 FAQ；Redis TTL（24h）到期后不再永久 miss，
 * 而是按规范化键回源 MySQL {@code faqs} 表，命中后回填缓存。Redis 不可用时同样走 MySQL 回源，
 * FAQ 问答不因缓存层故障而静默退化。
 *
 * <p>R-04 纵深防御：norm 键未命中时再按原始问题精确匹配一次，兜住 Java/SQL 规范化差异
 * （Unicode 特例、规则变更、历史数据）导致的永久静默 miss。
 */
@Slf4j
@Service
public class FaqCacheService {

    private static final String KEY_PREFIX = "faq:answer:";

    /** FAQ 缓存 TTL：防止 Redis 键无界增长（安全审计 P6）。24h 后自动过期，回源 MySQL 后重新回填。 */
    private static final Duration TTL = Duration.ofHours(24);

    private final FaqAnswerStore answerStore;
    private final FaqMapper faqMapper;

    public FaqCacheService(FaqAnswerStore answerStore, FaqMapper faqMapper) {
        this.answerStore = answerStore;
        this.faqMapper = faqMapper;
    }

    /**
     * 查询 FAQ 答案：先查缓存，未命中按规范化键回源 MySQL 并回填（F-25）。
     *
     * @param question 用户问题
     * @return 命中则返回答案
     */
    public Optional<String> findAnswer(String question) {
        if (question == null || question.isBlank()) {
            return Optional.empty();
        }
        String key = key(question);
        Optional<String> cached = answerStore.get(key);
        if (cached.isPresent()) {
            return cached;
        }
        Optional<String> fromDb = loadPublished(question);
        if (fromDb.isPresent()) {
            // 命中回填：延长 TTL，避免每次提问都打库
            answerStore.put(key, fromDb.get(), TTL);
        }
        return fromDb;
    }

    /**
     * 写入 FAQ 缓存（审核通过后调用）。
     *
     * <p>安全审计 P6：必须带 TTL，避免 Redis 键无界增长（审核通过的 FAQ 长期累积）。
     *
     * @param question 问题
     * @param answer   答案
     */
    public void put(String question, String answer) {
        if (question == null || question.isBlank() || answer == null) {
            return;
        }
        answerStore.put(key(question), answer, TTL);
    }

    /**
     * 删除 FAQ 缓存（驳回 / 下线 / 问题变更时调用）。
     *
     * @param question 问题
     */
    public void evict(String question) {
        if (question == null || question.isBlank()) {
            return;
        }
        answerStore.evict(key(question));
    }

    /**
     * 按规范化键查询 published FAQ（TTL 过期后的 MySQL 回源，F-25）。
     *
     * <p><b>纵深防御（R-04 复核）</b>：Java 的 {@link #normalize} 与 SQL 侧规范化在 Unicode
     * 特例上可能不等价（如 {@code U+0130 İ}、希腊词尾 sigma），迁移回填的 {@code question_norm}
     * 与运行时键不一致会导致按 norm 查询**永久静默 miss**。因此 norm 未命中时再按**原始问题
     * 精确匹配**查一次 published FAQ；命中同样回填缓存。这样任何规范化实现差异（Unicode 特例、
     * 规则变更、历史数据）最多只造成一次额外查询，而不会永久 miss。
     *
     * @param question 用户问题
     * @return published 且匹配的答案
     */
    private Optional<String> loadPublished(String question) {
        try {
            Faq faq = faqMapper.selectPublishedByNorm(normalize(question));
            if (faq == null) {
                // 兜底：norm 键未命中 → 按原始问题精确匹配（规范化分歧下的最后一道防线）
                faq = faqMapper.selectPublishedByQuestion(question);
            }
            if (faq == null || faq.getAnswer() == null || faq.getAnswer().isBlank()) {
                return Optional.empty();
            }
            return Optional.of(faq.getAnswer());
        } catch (RuntimeException ex) {
            // 回源失败不阻断问答：按未命中处理，走正常检索/生成链路
            log.warn("faq mysql fallback failed: {}", ex.getMessage());
            return Optional.empty();
        }
    }

    /**
     * 问题规范化（缓存键 / 数据库回源键共用，F-25）。
     *
     * @param question 原始问题
     * @return 规范化键
     */
    public static String normalize(String question) {
        return question.toLowerCase(Locale.ROOT)
                .replaceAll("[\\s\\p{Punct}，。！？；：、“”‘’（）《》【】]", "");
    }

    private String key(String question) {
        return KEY_PREFIX + normalize(question);
    }
}

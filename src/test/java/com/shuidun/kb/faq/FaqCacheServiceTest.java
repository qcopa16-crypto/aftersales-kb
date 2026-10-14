package com.shuidun.kb.faq;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.shuidun.kb.faq.entity.Faq;
import com.shuidun.kb.faq.mapper.FaqMapper;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;

/**
 * R-04 纵深防御回归：Java 规范化与 SQL 回填的 {@code question_norm} 在 Unicode 特例上可能不等价，
 * norm 键查询会永久静默 miss。验证 {@code findAnswer} 在 norm 未命中时按原始问题精确匹配兜底并回填缓存，
 * 且 norm 正常命中路径不增加额外查询（性能不回归）。
 */
class FaqCacheServiceTest {

    /** 内存版缓存，记录写入以验证回填。 */
    private static final class InMemoryStore implements FaqAnswerStore {

        private final Map<String, String> values = new HashMap<>();

        @Override
        public Optional<String> get(String key) {
            return Optional.ofNullable(values.get(key));
        }

        @Override
        public void put(String key, String answer, Duration ttl) {
            values.put(key, answer);
        }

        @Override
        public void evict(String key) {
            values.remove(key);
        }
    }

    private static Faq published(String question, String questionNorm, String answer) {
        Faq faq = new Faq();
        faq.setQuestion(question);
        faq.setQuestionNorm(questionNorm);
        faq.setAnswer(answer);
        faq.setStatus("published");
        return faq;
    }

    @Test
    void normMissFallsBackToExactQuestionAndBackfillsCache() {
        FaqMapper mapper = mock(FaqMapper.class);
        InMemoryStore store = new InMemoryStore();
        FaqCacheService service = new FaqCacheService(store, mapper);

        // U+0130 'İ'：Java 会展开为 i + U+0307，SQL 侧可能不同 → 迁移回填的 norm 键与运行时键不一致
        String question = "保修期İ多久";
        String answer = "整机保修一年。";
        when(mapper.selectPublishedByNorm(anyString())).thenReturn(null);
        when(mapper.selectPublishedByQuestion(question)).thenReturn(published(question, "migrated-wrong-norm", answer));

        Optional<String> result = service.findAnswer(question);

        assertEquals(Optional.of(answer), result, "norm 未命中时应按原始问题精确匹配兜底，不得静默 miss");
        assertEquals(answer, store.get("faq:answer:" + FaqCacheService.normalize(question)).orElse(null),
                "兜底命中后应回填缓存（键仍用 Java normalize 结果）");
        verify(mapper, times(1)).selectPublishedByNorm(anyString());
        verify(mapper, times(1)).selectPublishedByQuestion(question);
    }

    @Test
    void normHitDoesNotIssueExtraQuery() {
        FaqMapper mapper = mock(FaqMapper.class);
        InMemoryStore store = new InMemoryStore();
        FaqCacheService service = new FaqCacheService(store, mapper);

        String question = "保修期是多久";
        String answer = "整机保修一年。";
        when(mapper.selectPublishedByNorm(FaqCacheService.normalize(question)))
                .thenReturn(published(question, FaqCacheService.normalize(question), answer));

        Optional<String> result = service.findAnswer(question);

        assertEquals(Optional.of(answer), result);
        // norm 命中路径不得多打一次库（性能不回归）
        verify(mapper, never()).selectPublishedByQuestion(anyString());
        verify(mapper, times(1)).selectPublishedByNorm(anyString());
    }

    @Test
    void cachedHitSkipsDatabaseEntirely() {
        FaqMapper mapper = mock(FaqMapper.class);
        InMemoryStore store = new InMemoryStore();
        FaqCacheService service = new FaqCacheService(store, mapper);

        String question = "缓存直出";
        store.put("faq:answer:" + FaqCacheService.normalize(question), "缓存答案", Duration.ofHours(1));

        assertEquals(Optional.of("缓存答案"), service.findAnswer(question));
        verify(mapper, never()).selectPublishedByNorm(anyString());
        verify(mapper, never()).selectPublishedByQuestion(anyString());
    }
}

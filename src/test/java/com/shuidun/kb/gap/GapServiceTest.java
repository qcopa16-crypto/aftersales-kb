package com.shuidun.kb.gap;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.gap.entity.KnowledgeGap;
import com.shuidun.kb.gap.mapper.KnowledgeGapMapper;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

/**
 * R-16 回归：缺口样例问题规范化到 gap_id + question 唯一表。
 *
 * <p>旧的读改写 JSON 在并发记录同一 pattern 的不同问题时互相覆盖丢样例；本用例验证
 * 同一 pattern 下多个（空白变体）问题都能保留、ask_count 正确增长、重复问题幂等去重。
 */
@SpringBootTest
class GapServiceTest {

    @Autowired
    private GapService gapService;

    @Autowired
    private KnowledgeGapMapper gapMapper;

    @Test
    void samplesAreRecordedPerQuestionAndDeduplicated() {
        String base = "样例去重-" + System.nanoTime();
        String q1 = base + "  问题";
        String q2 = base + " 问题";

        gapService.recordUnanswered(q1);
        gapService.recordUnanswered(q2);
        gapService.recordUnanswered(q2); // 重复同一原始问题：唯一约束幂等

        KnowledgeGap gap = gapMapper.selectByPattern(normalize(base + " 问题"));
        assertEquals(3, gap.getAskCount(), "同一 pattern 的提问次数应累计为 3");
        List<String> samples = gapMapper.selectSamples(gap.getId(), 10);
        assertEquals(2, samples.size(), "两个不同样例都应保留，重复问题去重：" + samples);
    }

    @Test
    void concurrentDistinctSamplesOnSamePatternAreNotLost() throws Exception {
        String base = "并发样例-" + System.nanoTime();
        int threads = 5;
        List<String> questions = new ArrayList<>();
        for (int i = 0; i < threads; i++) {
            // 空白变体：normalize 后是同一 pattern，但原始问题互不相同
            questions.add(base + " ".repeat(i + 1) + "问题");
        }

        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        List<Throwable> errors = new CopyOnWriteArrayList<>();
        for (String question : questions) {
            Thread worker = new Thread(() -> {
                try {
                    start.await(5, TimeUnit.SECONDS);
                    gapService.recordUnanswered(question);
                } catch (Throwable ex) {
                    errors.add(ex);
                } finally {
                    done.countDown();
                }
            });
            worker.start();
        }
        start.countDown();
        assertTrue(done.await(30, TimeUnit.SECONDS), "并发记录应在超时前完成");

        assertEquals(0, errors.size(), "并发记录不应抛异常：" + errors);
        KnowledgeGap gap = gapMapper.selectByPattern(normalize(base + " 问题"));
        assertEquals(threads, gap.getAskCount(), "并发提问次数应全部累计");
        List<String> samples = gapMapper.selectSamples(gap.getId(), 10);
        assertEquals(threads, samples.size(), "并发不同样例不得丢失：" + samples);
    }

    private String normalize(String question) {
        return question.trim().replaceAll("\\s+", " ");
    }
}

package com.shuidun.kb.dashboard.service;

import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.dashboard.LatencySample;
import com.shuidun.kb.dashboard.entity.QaAccessLog;
import com.shuidun.kb.dashboard.mapper.QaAccessLogMapper;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

/**
 * 问答访问日志服务（DESIGN.md §6.1 步骤 7）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class QaAccessLogService {

    private final QaAccessLogMapper qaAccessLogMapper;

    /**
     * 记录一次问答访问。
     *
     * @param sessionId          会话 id
     * @param userId             用户 id
     * @param question           问题
     * @param answer             答案
     * @param chosenUnitId       澄清点选：本轮由用户选中的知识单元 id；未点选传 null
     * @param answerSource       答案来源：kb / faq / llm_general / clarify / refusal
     * @param recalledUnitIds    召回 unit
     * @param authorizedUnitIds  授权 unit
     * @param unauthorizedUnitIds 未授权 unit
     * @param promptTokens       输入 token
     * @param completionTokens   输出 token
     * @param latency            耗时采样（端到端 / 检索 / 生成 / 数据库 / 缓存）
     * @param faqHit             是否命中 FAQ
     */
    public void record(String sessionId, Long userId, String question, String answer, Long chosenUnitId,
                       String answerSource,
                       List<Long> recalledUnitIds, List<Long> authorizedUnitIds, List<Long> unauthorizedUnitIds,
                       int promptTokens, int completionTokens, LatencySample latency, boolean faqHit) {
        QaAccessLog entity = new QaAccessLog();
        entity.setSessionId(sessionId);
        entity.setUserId(userId);
        entity.setQuestion(question);
        entity.setAnswer(answer);
        entity.setChosenUnitId(chosenUnitId);
        entity.setAnswerSource(answerSource == null ? "kb" : answerSource);
        entity.setRecalledUnitIdsJson(JsonUtils.toJson(recalledUnitIds));
        entity.setAuthorizedUnitIdsJson(JsonUtils.toJson(authorizedUnitIds));
        entity.setUnauthorizedUnitIdsJson(JsonUtils.toJson(unauthorizedUnitIds));
        entity.setPromptTokens(promptTokens);
        entity.setCompletionTokens(completionTokens);
        entity.setTotalTokens(promptTokens + completionTokens);
        entity.setResponseTimeMs(latency.responseMs());
        entity.setRetrieveMs(latency.retrieveMs());
        entity.setGenerateMs(latency.generateMs());
        entity.setDbMs(latency.dbMs());
        entity.setCacheMs(latency.cacheMs());
        entity.setFaqHit(faqHit ? 1 : 0);
        qaAccessLogMapper.insert(entity);
        log.debug("qa access logged userId={} faqHit={} chosenUnitId={} source={} tokens={} latency={}",
                userId, faqHit, chosenUnitId, entity.getAnswerSource(), promptTokens + completionTokens, latency);
    }
}

package com.shuidun.kb.gap;

import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.gap.dto.GapUpdateRequest;
import com.shuidun.kb.gap.dto.KnowledgeGapVO;
import com.shuidun.kb.gap.entity.KnowledgeGap;
import com.shuidun.kb.gap.mapper.KnowledgeGapMapper;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.permission.DataPermissionEngine;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * 知识缺口服务（kb-design-spec.md §5.3）。
 *
 * <p>聚合无召回/低置信问题；无权限召回不算"已命中知识"，同样计入缺口。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class GapService {

    private static final int MAX_PAGE_SIZE = 100;
    private static final int MAX_SAMPLE_QUESTIONS = 5;

    /** 样例问题入库最大字符数（与 knowledge_gap_samples.question 列长度一致）。 */
    private static final int SAMPLE_MAX_CHARS = 512;

    private static final Set<String> ALLOWED_STATUS = Set.of("unresolved", "resolved", "ignored");
    private static final ObjectMapper JSON = JsonMapper.builder().build();

    private final KnowledgeGapMapper gapMapper;
    private final KnowledgeUnitMapper unitMapper;
    private final DataPermissionEngine dataPermissionEngine;

    /**
     * 分页查询缺口。
     *
     * @param page   页码
     * @param size   每页条数
     * @param status 状态过滤，可空
     * @return 分页结果
     */
    public PageResult<KnowledgeGapVO> list(int page, int size, String status) {
        int normalizedPage = Math.max(page, 1);
        int normalizedSize = Math.min(Math.max(size, 1), MAX_PAGE_SIZE);
        String normalizedStatus = (status == null || status.isBlank()) ? null : status.trim();
        if (normalizedStatus != null && !ALLOWED_STATUS.contains(normalizedStatus)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "非法 status: " + normalizedStatus);
        }
        long offset = (long) (normalizedPage - 1) * normalizedSize;
        if (offset > Integer.MAX_VALUE) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "page 超出范围");
        }
        long total = gapMapper.countPage(normalizedStatus);
        List<KnowledgeGap> gaps = gapMapper.selectPage(normalizedStatus,
                (int) offset, normalizedSize);
        List<KnowledgeGapVO> records = new ArrayList<>(gaps.size());
        for (KnowledgeGap gap : gaps) {
            records.add(toVO(gap));
        }
        return PageResult.of(total, normalizedPage, normalizedSize, records);
    }

    /**
     * 记录一次未命中问题（聚合到同一归一化模式）。
     *
     * <p>R-16：样例问题规范化写入 {@code knowledge_gap_samples}（gap_id + question 唯一），
     * 不再由应用层读取、改写、整体覆盖 {@code sample_questions_json}——并发记录不同问题时
     * 不会再互相覆盖丢样例。计数仍由数据库侧原子自增。
     *
     * @param question 用户问题
     */
    @Transactional
    public void recordUnanswered(String question) {
        if (question == null || question.isBlank()) {
            return;
        }
        String pattern = normalize(question);
        KnowledgeGap existing = gapMapper.selectByPattern(pattern);
        if (existing == null) {
            KnowledgeGap gap = new KnowledgeGap();
            gap.setQuestionPattern(pattern);
            gap.setAskCount(1);
            gap.setStatus("unresolved");
            try {
                gapMapper.insert(gap);
                recordSample(gap.getId(), question);
                log.info("knowledge gap created gapId={}", gap.getId());
                return;
            } catch (DuplicateKeyException ex) {
                // F-09：并发下唯一索引（uk_gaps_pattern）冲突 → 转为累加，保证"并发 N 次只留一行且计数准确"
                existing = gapMapper.selectByPattern(pattern);
                if (existing == null) {
                    throw ex;
                }
            }
        }
        recordSample(existing.getId(), question);
        // F-09：计数在数据库侧原子自增，不再写入应用读到的绝对值（避免并发覆盖丢计数）
        gapMapper.incrementCount(existing.getId());
    }

    /**
     * 记录样例问题（R-16）：规范化写入 gap_id + question 唯一表，容量上限 best-effort。
     *
     * <p>(gap_id, question) 唯一约束保证并发记录同一问题时幂等；容量判断与插入之间存在竞态，
     * 极端并发下可能略超 {@link #MAX_SAMPLE_QUESTIONS}，但**不会丢样例**（旧实现的读改写 JSON
     * 在并发下会互相覆盖丢样例）。重复插入由唯一约束兜底并忽略。
     */
    private void recordSample(Long gapId, String question) {
        if (gapId == null) {
            return;
        }
        try {
            if (gapMapper.countSamples(gapId) >= MAX_SAMPLE_QUESTIONS) {
                return;
            }
            gapMapper.insertSample(gapId, truncateSample(question));
        } catch (DuplicateKeyException ex) {
            // 同一 gap 同一问题重复记录：唯一约束兜底，忽略
            log.debug("gap sample duplicate ignored gapId={}", gapId);
        }
    }

    private String truncateSample(String question) {
        String text = question.strip();
        return text.length() <= SAMPLE_MAX_CHARS ? text : text.substring(0, SAMPLE_MAX_CHARS);
    }

    /**
     * 更新缺口状态（F-08：resolved 时校验并持久化关联的解决单元）。
     *
     * @param currentUser 当前调用者（用于关联单元的授权校验）
     * @param id          主键
     * @param request     更新请求
     */
    @Transactional
    public void updateStatus(CurrentUser currentUser, Long id, GapUpdateRequest request) {
        KnowledgeGap gap = gapMapper.selectById(id);
        if (gap == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "知识缺口不存在");
        }
        String status = request.getStatus();
        if (!ALLOWED_STATUS.contains(status)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "status 仅支持 unresolved / resolved / ignored");
        }
        Long resolvedUnitId = request.getResolvedUnitId();
        if (resolvedUnitId != null) {
            if (!"resolved".equals(status)) {
                throw new BusinessException(ErrorCode.PARAM_INVALID, "仅 resolved 状态可关联解决单元");
            }
            KnowledgeUnit unit = unitMapper.selectById(resolvedUnitId);
            if (unit == null) {
                throw new BusinessException(ErrorCode.PARAM_INVALID, "关联的知识单元不存在");
            }
            if (!"active".equals(unit.getStatus()) || !"active".equals(unit.getIngestStatus())) {
                throw new BusinessException(ErrorCode.PARAM_INVALID, "关联的知识单元不可用（需已发布且可检索）");
            }
            if (currentUser != null) {
                boolean authorized = dataPermissionEngine.filter(currentUser, List.of(resolvedUnitId))
                        .authorized().contains(resolvedUnitId);
                if (!authorized) {
                    throw new BusinessException(ErrorCode.FORBIDDEN, "无权关联该知识单元");
                }
            }
        }
        // 兼容旧的只传 status 请求：未显式给出关联单元时保持原值不变
        Long effectiveResolvedUnitId = ("resolved".equals(status) && resolvedUnitId != null)
                ? resolvedUnitId
                : gap.getResolvedUnitId();
        gapMapper.updateStatusAndResolved(id, status, effectiveResolvedUnitId);
        log.info("knowledge gap status updated gapId={} status={} resolvedUnitId={}",
                id, status, effectiveResolvedUnitId);
    }

    private String normalize(String question) {
        return question.trim().replaceAll("\\s+", " ");
    }

    private List<String> parseSamples(String json) {
        if (json == null || json.isBlank()) {
            return new ArrayList<>();
        }
        try {
            List<String> samples = JSON.readValue(json, new TypeReference<List<String>>() {
            });
            return new ArrayList<>(samples);
        } catch (RuntimeException ex) {
            return new ArrayList<>();
        }
    }

    private KnowledgeGapVO toVO(KnowledgeGap gap) {
        KnowledgeGapVO vo = new KnowledgeGapVO();
        vo.setId(gap.getId());
        vo.setQuestionPattern(gap.getQuestionPattern());
        vo.setSampleQuestions(loadSamples(gap));
        vo.setAskCount(gap.getAskCount());
        vo.setLastAskedAt(gap.getLastAskedAt());
        vo.setStatus(gap.getStatus());
        vo.setResolvedUnitId(gap.getResolvedUnitId());
        return vo;
    }

    /**
     * 读取样例问题（R-16）：优先规范化表；为空时回退旧的 sample_questions_json，
     * 兼容 R-16 上线前已存在、尚未迁移的历史数据。
     */
    private List<String> loadSamples(KnowledgeGap gap) {
        if (gap.getId() != null) {
            List<String> samples = gapMapper.selectSamples(gap.getId(), MAX_SAMPLE_QUESTIONS);
            if (!samples.isEmpty()) {
                return samples;
            }
        }
        return parseSamples(gap.getSampleQuestionsJson());
    }
}

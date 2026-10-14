package com.shuidun.kb.faq;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.faq.dto.FaqReviewRequest;
import com.shuidun.kb.faq.dto.FaqUpdateRequest;
import com.shuidun.kb.faq.dto.FaqVO;
import com.shuidun.kb.faq.entity.Faq;
import com.shuidun.kb.faq.mapper.FaqMapper;
import java.util.ArrayList;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * FAQ 服务（kb-design-spec.md §5.2）。
 *
 * <p>仅 approve / reject 两个审核动作；通过后写入 Redis 缓存供问答链路命中，rejected 不进缓存。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class FaqService {

    private static final int MAX_PAGE_SIZE = 100;
    private static final String STATUS_PENDING = "pending_review";
    private static final String STATUS_PUBLISHED = "published";
    private static final String STATUS_REJECTED = "rejected";

    private final FaqMapper faqMapper;
    private final FaqCacheService faqCacheService;

    /**
     * 分页查询 FAQ。
     *
     * @param page   页码
     * @param size   每页条数
     * @param status 状态过滤，可空
     * @return 分页结果
     */
    public PageResult<FaqVO> list(int page, int size, String status) {
        int normalizedPage = Math.max(page, 1);
        int normalizedSize = Math.min(Math.max(size, 1), MAX_PAGE_SIZE);
        String normalizedStatus = (status == null || status.isBlank()) ? null : status.trim();
        long offset = (long) (normalizedPage - 1) * normalizedSize;
        if (offset > Integer.MAX_VALUE) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "page 超出范围");
        }
        long total = faqMapper.countPage(normalizedStatus);
        List<Faq> faqs = faqMapper.selectPage(normalizedStatus, (int) offset, normalizedSize);
        List<FaqVO> records = new ArrayList<>(faqs.size());
        for (Faq faq : faqs) {
            records.add(toVO(faq));
        }
        return PageResult.of(total, normalizedPage, normalizedSize, records);
    }

    /**
     * 待审核推荐列表。
     *
     * @param page 页码
     * @param size 每页条数
     * @return 分页结果
     */
    public PageResult<FaqVO> recommendations(int page, int size) {
        return list(page, size, STATUS_PENDING);
    }

    /**
     * 审核 FAQ。
     *
     * @param id         主键
     * @param request    审核请求
     * @param reviewerId 审核人
     */
    @Transactional
    public void review(Long id, FaqReviewRequest request, Long reviewerId) {
        Faq faq = requireFaq(id);
        if (!STATUS_PENDING.equals(faq.getStatus())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "仅待审核 FAQ 可审核，当前状态: " + faq.getStatus());
        }
        String action = request.getAction();
        if (!"approve".equals(action) && !"reject".equals(action)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "action 仅支持 approve / reject");
        }
        if ("approve".equals(action)) {
            if (request.getEditedAnswer() != null && !request.getEditedAnswer().isBlank()) {
                faq.setAnswer(request.getEditedAnswer());
            }
            faq.setStatus(STATUS_PUBLISHED);
            faq.setReviewerId(reviewerId);
            faq.setQuestionNorm(FaqCacheService.normalize(faq.getQuestion()));
            faqMapper.updateReview(faq);
            // 通过后必须写缓存，供 ChatOrchestrator 步骤 2 命中
            faqCacheService.put(faq.getQuestion(), faq.getAnswer());
        } else {
            faq.setStatus(STATUS_REJECTED);
            faq.setReviewerId(reviewerId);
            faqMapper.updateReview(faq);
            // rejected 不进缓存
            faqCacheService.evict(faq.getQuestion());
        }
        log.info("faq reviewed faqId={} action={} reviewerId={}", id, action, reviewerId);
    }

    /**
     * 编辑已发布 FAQ，并同步刷新缓存。
     *
     * @param id      主键
     * @param request 编辑请求
     */
    @Transactional
    public void update(Long id, FaqUpdateRequest request) {
        Faq faq = requireFaq(id);
        if (!STATUS_PUBLISHED.equals(faq.getStatus())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "仅已发布 FAQ 可编辑");
        }
        String previousQuestion = faq.getQuestion();
        if (request.getQuestion() != null && !request.getQuestion().isBlank()) {
            faq.setQuestion(request.getQuestion());
        }
        if (request.getAnswer() != null) {
            faq.setAnswer(request.getAnswer());
        }
        if (request.getCategory() != null) {
            faq.setCategory(request.getCategory());
        }
        faq.setQuestionNorm(FaqCacheService.normalize(faq.getQuestion()));
        faqMapper.update(faq);
        if (!previousQuestion.equals(faq.getQuestion())) {
            faqCacheService.evict(previousQuestion);
        }
        faqCacheService.put(faq.getQuestion(), faq.getAnswer());
        log.info("faq updated faqId={}", id);
    }

    /**
     * 下线已发布 FAQ，并清除缓存。
     *
     * @param id 主键
     */
    @Transactional
    public void offline(Long id) {
        Faq faq = requireFaq(id);
        faq.setStatus(STATUS_REJECTED);
        faqMapper.update(faq);
        faqCacheService.evict(faq.getQuestion());
        log.info("faq offline faqId={}", id);
    }

    private Faq requireFaq(Long id) {
        Faq faq = faqMapper.selectById(id);
        if (faq == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "FAQ 不存在");
        }
        return faq;
    }

    private FaqVO toVO(Faq faq) {
        FaqVO vo = new FaqVO();
        vo.setId(faq.getId());
        vo.setQuestion(faq.getQuestion());
        vo.setAnswer(faq.getAnswer());
        vo.setCategory(faq.getCategory());
        vo.setRelatedUnitId(faq.getRelatedUnitId());
        vo.setSourceType(faq.getSourceType());
        vo.setStatus(faq.getStatus());
        vo.setHitCount(faq.getHitCount());
        vo.setReviewedAt(faq.getReviewedAt());
        vo.setCreatedAt(faq.getCreatedAt());
        vo.setUpdatedAt(faq.getUpdatedAt());
        return vo;
    }
}

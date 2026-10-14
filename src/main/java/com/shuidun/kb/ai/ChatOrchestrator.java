package com.shuidun.kb.ai;

import com.shuidun.kb.ai.dto.ChatRequest;
import com.shuidun.kb.ai.dto.ClarifyEvent;
import com.shuidun.kb.ai.dto.ClarifyOptionVO;
import com.shuidun.kb.ai.dto.DeltaEvent;
import com.shuidun.kb.ai.dto.DoneEvent;
import com.shuidun.kb.ai.dto.ErrorEvent;
import com.shuidun.kb.ai.dto.GenerateContext;
import com.shuidun.kb.ai.dto.NoticeEvent;
import com.shuidun.kb.ai.dto.RetrieveResult;
import com.shuidun.kb.ai.dto.RetrievedChunk;
import com.shuidun.kb.ai.dto.SessionTurn;
import com.shuidun.kb.ai.dto.SourceItemVO;
import com.shuidun.kb.ai.dto.SourcesEvent;
import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.config.AiProperties;
import com.shuidun.kb.dashboard.LatencySample;
import com.shuidun.kb.dashboard.service.QaAccessLogService;
import com.shuidun.kb.faq.FaqCacheService;
import com.shuidun.kb.gap.GapService;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.permission.DataPermissionEngine;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.RejectedExecutionException;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * 问答编排器（代码设计规范 §3.7 / kb-design-spec.md §6.1）。
 *
 * <p>固定顺序：JWT → FAQ 缓存 → retrieve（Java 下发 acl_filter）→ 四维权限过滤 →
 * generate（仅 authorized contexts）→ SSE 透传 → 写 qa_access_logs。禁止跳步。
 *
 * <p>两条补充路径：
 * <ul>
 *   <li><b>澄清点选</b>：请求带 {@code chosenUnitId} 时跳过检索，直接用该单元正文作答
 *       （必须校验该单元在当前用户的可访问集合内，否则是越权读取）；</li>
 *   <li><b>澄清候选</b>：纯无召回时，从可访问单元里按关键词挑出候选推给前端，让用户点选。</li>
 * </ul>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ChatOrchestrator {

    /** 检索 Top-K。 */
    private static final int TOP_K = 8;

    /** SSE 超时（毫秒）。 */
    private static final long SSE_TIMEOUT_MS = 180_000L;

    /**
     * 问题已经问清楚、但知识库确实没命中时的说明（落库为答案）。
     *
     * <p>措辞刻意区别于追问：这里是"没找到"，参考列表只是可选入口，
     * 不是"必须先选一条才能得到回答"。
     */
    private static final String NO_MATCH_HINT =
            "知识库里没有找到直接匹配这个问题的资料。下面几份可能相关，点开我可以基于它作答；也可以换个说法再问。";

    /** 点选的知识不可访问或不存在：统一话术，不区分二者，避免探测。 */
    private static final String CHOSEN_UNAVAILABLE =
            "抱歉，所选知识不存在或您暂无访问权限，无法基于其作答。";

    /** 点选的知识没有正文。 */
    private static final String CHOSEN_EMPTY =
            "抱歉，所选知识暂无正文内容，无法基于其作答。请选择其他知识，或联系知识管理员补充内容。";

    /** 无来源可展示时的空 sources。 */
    private static final SourcesEvent NO_SOURCES = new SourcesEvent(List.of(), List.of());

    /** 闲聊兜底的提示横幅文案（安全模型第 3 层：UI 显著标注）。 */
    private static final String GENERAL_NOTICE_TEXT =
            "以下为 AI 通用知识，非公司规定，仅供参考；涉及保修、价格、SLA 等具体条款请以公司资料为准。";

    /**
     * 生成失败/截断轮的访问日志来源标记（F-23）。
     *
     * <p>与成功轮（kb / llm_general / faq）区分，供审计与统计识别"不完整回答"，
     * 避免失败轮的部分输出被当成完整知识回答计入成功率。
     */
    private static final String FAILED_SOURCE = "failed";

    private final AiClient aiClient;
    private final FaqCacheService faqCacheService;
    private final DataPermissionEngine dataPermissionEngine;
    private final KnowledgeUnitMapper unitMapper;
    private final QaAccessLogService qaAccessLogService;
    private final GapService gapService;
    private final ClarificationService clarificationService;
    private final BusinessIntentClassifier businessIntentClassifier;
    private final AiProperties aiProperties;
    private final ExecutorService chatExecutor;

    /**
     * 以 SSE 方式流式回答。
     *
     * @param request 问答请求
     * @param user    当前用户
     * @return SSE 发射器
     */
    public SseEmitter stream(ChatRequest request, CurrentUser user) {
        SseEmitter emitter = new SseEmitter(SSE_TIMEOUT_MS);
        // 会话 id 由 UUID 生成（128-bit 熵），避免可预测的 "s_<毫秒时间戳>" 被枚举/串会话
        String sessionId = (request.getSessionId() == null || request.getSessionId().isBlank())
                ? UUID.randomUUID().toString().replace("-", "") : request.getSessionId();
        try {
            chatExecutor.execute(() -> run(request, sessionId, user, emitter));
        } catch (RejectedExecutionException ex) {
            // 线程池饱和（有界队列已满）：明确降级为「服务繁忙」，绝不裸 500，也不阻塞容器线程
            log.warn("chat executor saturated, rejecting userId={}", user.getId());
            sendQuietly(emitter, "error",
                    new ErrorEvent(ErrorCode.SERVICE_BUSY.name(), ErrorCode.SERVICE_BUSY.getDefaultMessage()));
            emitter.complete();
        }
        return emitter;
    }

    /**
     * 编排骨架：按固定顺序串起各段私有步骤，本身不含业务分支细节。
     *
     * <p>纯结构拆分：各步骤内部逻辑与拆分前逐段等价，未改变任何判断条件、执行顺序、
     * SSE 事件顺序、DB 写入次数与顺序、日志文案或异常类型。
     *
     * @param request   问答请求
     * @param sessionId 会话 id
     * @param user      当前用户
     * @param emitter   SSE 发射器
     */
    private void run(ChatRequest request, String sessionId, CurrentUser user, SseEmitter emitter) {
        String question = request.getQuestion();
        // owner_id = sha256(userId + 盐)[:16]：不透明分区键，AI 只当 key，避免暴露 userId
        String ownerId = ownerIdOf(user.getId());
        // 本轮 id：由 Java 生成（重试复用），AI 侧用它做幂等守卫
        String turnId = UUID.randomUUID().toString().replace("-", "");
        long start = System.currentTimeMillis();
        Ctx ctx = new Ctx(request, sessionId, user, emitter, question, ownerId, turnId, start);
        try {
            // 步骤 2：FAQ 缓存命中则直出
            if (tryFaqCache(ctx)) {
                return;
            }
            // 步骤 3 前置：计算用户可访问的 unit 集合，作为 acl_filter 下发（fail-closed）
            if (!loadContext(ctx)) {
                return;
            }
            // 闲聊兜底（全自动）：问题非业务性 + 开关开启。业务问题永不兜底。
            computeChatFallback(ctx);
            // 规则 1（缺现象）：问题含糊（有"有问题/坏了"却没说现象）→ 先追问现象。
            // 与检索结果无关，故放在检索**之前** —— 否则 KB 一有内容就再也不追问了。
            // 点选轮（用户已明确选了知识）不追问，直接作答。
            if (handleVagueClarify(ctx)) {
                return;
            }
            // 澄清点选：跳过检索，直接用选中的知识单元；否则检索 + Java 侧最终过滤兜底（双保险）
            if (buildContexts(ctx)) {
                return;
            }
            // 检索为空 / 无授权内容：澄清追问、闲聊兜底、参考候选或拒答，均在本段收尾
            if (ctx.contexts.isEmpty()) {
                handleEmptyContexts(ctx);
                return;
            }
            // 检索到了内容，但知识库对该话题没有任何实体命中（且问题非业务性）→ 走闲聊兜底，
            // 避免拿不相关资料硬答"不知道"。业务问题不受影响（chatFallbackApplies 已排除）。
            if (ctx.chatFallbackApplies) {
                returnGeneralFallback(ctx.emitter, ctx.sessionId, ctx.user, ctx.question, ctx.answer,
                        ctx.accessibleUnits, ctx.recalled, ctx.authorized, ctx.unauthorized,
                        ctx.start, ctx.retrieveMs, ctx.dbMs, ctx.cacheMs);
                return;
            }
            // 步骤 5/6：sources 先到 → generate（仅 authorized contexts）→ done → 落库
            generateAndRecord(ctx);
        } catch (AiException ex) {
            log.warn("chat stream ai failure userId={} code={}", user.getId(), ex.getAiErrorCode());
            sendQuietly(emitter, "error", new ErrorEvent(
                    ex.getAiErrorCode() == null ? "AI_UNAVAILABLE" : ex.getAiErrorCode(), ex.getMessage()));
            emitter.complete();
        } catch (RuntimeException ex) {
            log.error("chat stream failed userId={}", user.getId(), ex);
            sendQuietly(emitter, "error", new ErrorEvent("INTERNAL_ERROR", "服务异常，请稍后重试"));
            emitter.complete();
        }
    }

    /**
     * 步骤 2：FAQ 缓存命中则直出（sources/delta/done + 落库 + appendTurn）。
     *
     * @return 命中并已收尾返回 {@code true}，未命中返回 {@code false}
     */
    private boolean tryFaqCache(Ctx ctx) {
        long cacheStart = System.currentTimeMillis();
        Optional<String> faqAnswer = faqCacheService.findAnswer(ctx.question);
        ctx.cacheMs = (int) (System.currentTimeMillis() - cacheStart);
        if (faqAnswer.isEmpty()) {
            return false;
        }
        send(ctx.emitter, "sources", new SourcesEvent(List.of(), List.of()));
        send(ctx.emitter, "delta", new DeltaEvent(faqAnswer.get()));
        ctx.answer.append(faqAnswer.get());
        long elapsed = System.currentTimeMillis() - ctx.start;
        send(ctx.emitter, "done", new DoneEvent(ctx.sessionId, 0, elapsed, true));
        qaAccessLogService.record(ctx.sessionId, ctx.user.getId(), ctx.question, ctx.answer.toString(), null,
                "faq",
                List.of(), List.of(), List.of(), 0, 0,
                new LatencySample((int) elapsed, 0, 0, 0, ctx.cacheMs), true);
        appendTurn(ctx.user, ctx.sessionId, "faq", ctx.question, ctx.answer.toString(),
                List.of(), List.of(), null, true);
        ctx.emitter.complete();
        return true;
    }

    /**
     * 步骤 3 前置：加载用户可访问单元集合、计算 aclUnitCodes；无任何授权知识则直接拒答。
     *
     * @return 有授权知识可继续返回 {@code true}；已拒答并收尾返回 {@code false}
     */
    private boolean loadContext(Ctx ctx) {
        long dbStart = System.currentTimeMillis();
        Map<Long, KnowledgeUnit> accessibleUnits = loadRetrievableUnits(ctx.user);
        ctx.dbMs = (int) (System.currentTimeMillis() - dbStart);
        List<String> aclUnitCodes = accessibleUnits.values().stream().map(KnowledgeUnit::getUnitCode).toList();
        ctx.accessibleUnits = accessibleUnits;
        ctx.aclUnitCodes = aclUnitCodes;
        if (aclUnitCodes.isEmpty()) {
            // 无任何授权知识：直接拒答，禁止空 contexts 调 generate 让模型"自己知道"
            returnRefusal(ctx.emitter, ctx.sessionId, ctx.user, ctx.question, ctx.answer,
                    "抱歉，当前账号没有可访问的相关知识，无法回答该问题。请联系管理员配置数据权限。",
                    NO_SOURCES, ctx.start, ctx.chosenUnitId,
                    ctx.recalled, ctx.authorized, ctx.unauthorized, 0, ctx.dbMs, ctx.cacheMs);
            return false;
        }
        return true;
    }

    /** 闲聊兜底判定（全自动）：问题非业务性 + 开关开启。业务问题永不兜底。 */
    private void computeChatFallback(Ctx ctx) {
        ctx.chatFallbackEnabled = !businessIntentClassifier.isBusiness(ctx.question)
                && aiProperties.isChatFallbackEnabled();
        // 强信号：**短问题且知识库无实体命中**（「1+1」「今天天气怎么样」）——即使检索
        // 返回了不相关 chunk，也走通用知识，避免拿不相关资料硬答"不知道"。
        // 用"短问题+实体命中"而非"检索分数阈值"：实测分数区间重叠严重
        // （「备件怎么申请」0.445 vs「1+1」0.461），阈值会误伤正常问题。
        ctx.chatFallbackApplies = ctx.chatFallbackEnabled
                && clarificationService.isShortNoEntity(ctx.question, ctx.accessibleUnits);
    }

    /**
     * 规则 1（缺现象）：问题含糊时先追问现象（点选轮跳过）。
     *
     * @return 已追问并收尾返回 {@code true}，否则 {@code false}
     */
    private boolean handleVagueClarify(Ctx ctx) {
        if (ctx.chosenUnitId != null) {
            return false;
        }
        Optional<ClarifyEvent> vagueAsk = clarificationService.askPhenomenonIfVague(ctx.question);
        if (vagueAsk.isEmpty()) {
            return false;
        }
        ClarifyEvent event = vagueAsk.get();
        send(ctx.emitter, "clarify", event);
        send(ctx.emitter, "delta", new DeltaEvent(event.getPrompt()));
        ctx.answer.append(event.getPrompt());
        long elapsed = System.currentTimeMillis() - ctx.start;
        send(ctx.emitter, "done", new DoneEvent(ctx.sessionId, 0, elapsed, false));
        qaAccessLogService.record(ctx.sessionId, ctx.user.getId(), ctx.question, ctx.answer.toString(), null,
                "clarify", ctx.recalled, ctx.authorized, ctx.unauthorized, 0, 0,
                new LatencySample((int) elapsed, 0, 0, ctx.dbMs, ctx.cacheMs), false);
        appendTurn(ctx.user, ctx.sessionId, "clarify_ask", ctx.question, ctx.answer.toString(),
                List.of(), List.of(), null, false);
        ctx.emitter.complete();
        return true;
    }

    /**
     * 构建 authorizedSources / unauthorizedSources / contexts。
     *
     * <p>点选轮直接取选中单元（不可访问或空正文则拒答）；否则 retrieve 后做 Java 侧最终
     * 过滤兜底（双保险），未命中授权集合的编码批量反查并单列为 unauthorized 来源。
     *
     * @return 点选轮已拒答并收尾返回 {@code true}；否则构建完成返回 {@code false}
     */
    private boolean buildContexts(Ctx ctx) {
        Map<Long, SourceItemVO> authorizedSources = new LinkedHashMap<>();
        Map<Long, SourceItemVO> unauthorizedSources = new LinkedHashMap<>();
        List<GenerateContext> contexts;

        if (ctx.chosenUnitId != null) {
            // 澄清点选：跳过检索，直接用选中的知识单元
            KnowledgeUnit chosen = ctx.accessibleUnits.get(ctx.chosenUnitId);
            if (chosen == null) {
                // 不可访问或不存在：统一话术，不暴露该单元是否存在
                returnRefusal(ctx.emitter, ctx.sessionId, ctx.user, ctx.question, ctx.answer, CHOSEN_UNAVAILABLE,
                        NO_SOURCES, ctx.start, ctx.chosenUnitId,
                        ctx.recalled, ctx.authorized, ctx.unauthorized, 0, ctx.dbMs, ctx.cacheMs);
                return true;
            }
            String body = bodyOf(chosen);
            if (body.isBlank()) {
                returnRefusal(ctx.emitter, ctx.sessionId, ctx.user, ctx.question, ctx.answer, CHOSEN_EMPTY,
                        NO_SOURCES, ctx.start, ctx.chosenUnitId,
                        ctx.recalled, ctx.authorized, ctx.unauthorized, 0, ctx.dbMs, ctx.cacheMs);
                return true;
            }
            contexts = List.of(new GenerateContext(chosen.getUnitCode(), chosen.getTitle(), body));
            ctx.recalled.add(chosen.getId());
            ctx.authorized.add(chosen.getId());
            authorizedSources.put(chosen.getId(),
                    new SourceItemVO(chosen.getId(), chosen.getTitle()));
        } else {
            long retrieveStart = System.currentTimeMillis();
            RetrieveResult retrieveResult = aiClient.retrieve(ctx.question, TOP_K, ctx.aclUnitCodes, List.of(),
                    ctx.sessionId, ctx.ownerId, true);
            ctx.retrieveMs = (int) (System.currentTimeMillis() - retrieveStart);

            // 步骤 4：Java 侧最终过滤兜底（双保险）
            contexts = new ArrayList<>();
            // 预建 unit_code -> KnowledgeUnit 索引，把原线性扫描换成 O(1) 查表
            Map<String, KnowledgeUnit> accessibleByCode = new HashMap<>();
            for (KnowledgeUnit unit : ctx.accessibleUnits.values()) {
                if (unit.getUnitCode() != null) {
                    accessibleByCode.put(unit.getUnitCode(), unit);
                }
            }
            // 未命中授权集合的编码先汇总，再一次性批量反查
            Set<String> missingCodes = new LinkedHashSet<>();
            for (RetrievedChunk chunk : retrieveResult.getChunks()) {
                if (!accessibleByCode.containsKey(chunk.getUnitId())) {
                    missingCodes.add(chunk.getUnitId());
                }
            }
            Map<String, KnowledgeUnit> unauthorizedByCode = new HashMap<>();
            if (!missingCodes.isEmpty()) {
                for (KnowledgeUnit unit : unitMapper.selectByUnitCodes(new ArrayList<>(missingCodes))) {
                    if (unit.getUnitCode() != null) {
                        unauthorizedByCode.put(unit.getUnitCode(), unit);
                    }
                }
            }
            for (RetrievedChunk chunk : retrieveResult.getChunks()) {
                KnowledgeUnit unit = accessibleByCode.get(chunk.getUnitId());
                if (unit == null) {
                    KnowledgeUnit anyUnit = unauthorizedByCode.get(chunk.getUnitId());
                    if (anyUnit != null) {
                        ctx.recalled.add(anyUnit.getId());
                        unauthorizedSources.putIfAbsent(anyUnit.getId(),
                                new SourceItemVO(anyUnit.getId(), anyUnit.getTitle()));
                        // 标题被展示过 -> 记入 referenced，历史过滤时会连整轮一起丢
                        if (anyUnit.getUnitCode() != null) {
                            ctx.unauthorizedCodes.add(anyUnit.getUnitCode());
                        }
                    }
                    continue;
                }
                ctx.recalled.add(unit.getId());
                ctx.authorized.add(unit.getId());
                contexts.add(new GenerateContext(unit.getUnitCode(), unit.getTitle(), chunk.getContent()));
                authorizedSources.putIfAbsent(unit.getId(), new SourceItemVO(unit.getId(), unit.getTitle()));
            }
            ctx.unauthorized.addAll(unauthorizedSources.keySet());
        }
        ctx.authorizedSources = authorizedSources;
        ctx.unauthorizedSources = unauthorizedSources;
        ctx.contexts = contexts;
        return false;
    }

    /**
     * contexts 为空时的收尾：区分"真·无召回"与"有内容但无权限"，依次尝试澄清追问、
     * 闲聊兜底、参考候选，最终拒答。所有路径都在本段内结束该轮。
     */
    private void handleEmptyContexts(Ctx ctx) {
        boolean pureNoRecall = ctx.unauthorizedSources.isEmpty();
        if (pureNoRecall) {
            // 澄清轮不给 sources：此时展示"未命中已授权知识"的来源卡片只会干扰阅读
            //
            // 先判断问题信息是否足够——「检索为空」不等于「需要澄清」：
            // 检索为空最常见的原因是知识还没切片向量化，跟问题是否完整无关。
            // 适用闲聊兜底时**关闭「缺对象」追问**（规则 2），否则「1+1」「今天天气怎么样」
            // 会被追问"是哪个设备"而不是走通用知识回答；规则 1（缺现象）仍保留。
            Optional<ClarifyEvent> ask = clarificationService.askIfInsufficient(
                    ctx.question, ctx.accessibleUnits, !ctx.chatFallbackApplies);
            if (ask.isPresent()) {
                // 缺什么问什么：把追问文案与可点选项推给前端，用户补充后重新检索
                ClarifyEvent event = ask.get();
                send(ctx.emitter, "clarify", event);
                send(ctx.emitter, "delta", new DeltaEvent(event.getPrompt()));
                ctx.answer.append(event.getPrompt());
                long elapsed = System.currentTimeMillis() - ctx.start;
                send(ctx.emitter, "done", new DoneEvent(ctx.sessionId, 0, elapsed, false));
                qaAccessLogService.record(ctx.sessionId, ctx.user.getId(), ctx.question, ctx.answer.toString(), null,
                        "clarify",
                        ctx.recalled, ctx.authorized, ctx.unauthorized, 0, 0,
                        new LatencySample((int) elapsed, ctx.retrieveMs, 0, ctx.dbMs, ctx.cacheMs), false);
                appendTurn(ctx.user, ctx.sessionId, "clarify_ask", ctx.question, ctx.answer.toString(),
                        List.of(), List.of(), null, false);
                ctx.emitter.complete();
                return;
            }

            // 业务性问题（保修/价格/SLA/政策）知识库没覆盖时，坚持说"没找到资料"，
            // 绝不拿通用知识充数 —— 全自动模式下这是防"AI 编保修期"的关键闸门。
            // 注意：unauthorizedSources 非空（有内容但无权限）的分支在此之后，绝不闲聊兜底。
            if (ctx.chatFallbackEnabled) {
                returnGeneralFallback(ctx.emitter, ctx.sessionId, ctx.user, ctx.question, ctx.answer,
                        ctx.accessibleUnits, ctx.recalled, ctx.authorized, ctx.unauthorized,
                        ctx.start, ctx.retrieveMs, ctx.dbMs, ctx.cacheMs);
                return;
            }

            // 问题问清楚了，只是知识库没命中：诚实说明 + 给参考（不是"选了才能答"）
            List<ClarifyOptionVO> references =
                    clarificationService.referenceCandidates(ctx.question, ctx.accessibleUnits);
            if (!references.isEmpty()) {
                send(ctx.emitter, "clarify", new ClarifyEvent("reference", null, references));
                send(ctx.emitter, "delta", new DeltaEvent(NO_MATCH_HINT));
                ctx.answer.append(NO_MATCH_HINT);
                long elapsed = System.currentTimeMillis() - ctx.start;
                send(ctx.emitter, "done", new DoneEvent(ctx.sessionId, 0, elapsed, false));
                qaAccessLogService.record(ctx.sessionId, ctx.user.getId(), ctx.question, ctx.answer.toString(), null,
                        "clarify",
                        ctx.recalled, ctx.authorized, ctx.unauthorized, 0, 0,
                        new LatencySample((int) elapsed, ctx.retrieveMs, 0, ctx.dbMs, ctx.cacheMs), false);
                // 参考轮的候选标题已推给用户 -> 记入 referenced，参与后续 ACL 过滤
                appendTurn(ctx.user, ctx.sessionId, "clarify_reference", ctx.question, ctx.answer.toString(),
                        List.of(), codesOfOptions(references), null, false);
                ctx.emitter.complete();
                return;
            }

            // 问题清楚、也没有参考可给：真·无召回，计入知识缺口
            gapService.recordUnanswered(ctx.question);
        }
        String refusal = pureNoRecall
                ? "抱歉，知识库中暂未检索到可依据的资料，无法回答该问题。建议补充相关文档或换一种问法。"
                : "抱歉，检索到的相关资料您暂无访问权限，无法基于其作答。如需查看请联系知识管理员。";
        returnRefusal(ctx.emitter, ctx.sessionId, ctx.user, ctx.question, ctx.answer, refusal,
                new SourcesEvent(new ArrayList<>(ctx.authorizedSources.values()),
                        new ArrayList<>(ctx.unauthorizedSources.values())),
                ctx.start, ctx.chosenUnitId,
                ctx.recalled, ctx.authorized, ctx.unauthorized, ctx.retrieveMs, ctx.dbMs, ctx.cacheMs);
    }

    /** 步骤 5/6：sources 先到 → generate（仅 authorized contexts）→ done → 落库。 */
    private void generateAndRecord(Ctx ctx) {
        send(ctx.emitter, "sources", new SourcesEvent(
                new ArrayList<>(ctx.authorizedSources.values()), new ArrayList<>(ctx.unauthorizedSources.values())));

        long generateStart = System.currentTimeMillis();
        GenerateTerminal terminal = new GenerateTerminal();
        aiClient.generate(ctx.question, ctx.contexts, ctx.sessionId, AiGenerateMode.KB,
                ctx.ownerId, ctx.turnId, ctx.aclUnitCodes, ctx.chosenUnitId, true,
                streamHandler(ctx.emitter, ctx.answer, ctx.tokens, terminal));
        ctx.generateMs = (int) (System.currentTimeMillis() - generateStart);

        // F-23：仅 done 事件记成功并发 done；error / EOF 截断一律走失败态（含部分输出），
        // 终态互斥，杜绝"同时收到失败和完成信号、部分答案被当完整回答留存"。
        if (terminal.error) {
            recordFailed(ctx, terminal, "kb");
            return;
        }
        if (!terminal.done) {
            sendQuietly(ctx.emitter, "error",
                    new ErrorEvent("STREAM_TRUNCATED", "生成流被中断，答案可能不完整"));
            recordFailed(ctx, null, "kb");
            return;
        }

        long elapsed = System.currentTimeMillis() - ctx.start;
        send(ctx.emitter, "done", new DoneEvent(ctx.sessionId, ctx.tokens[0] + ctx.tokens[1], elapsed, false));
        qaAccessLogService.record(ctx.sessionId, ctx.user.getId(), ctx.question, ctx.answer.toString(),
                ctx.chosenUnitId,
                "kb",
                ctx.recalled, ctx.authorized, ctx.unauthorized, ctx.tokens[0], ctx.tokens[1],
                new LatencySample((int) elapsed, ctx.retrieveMs, ctx.generateMs, ctx.dbMs, ctx.cacheMs), false);
        ctx.emitter.complete();
    }

    /**
     * 生成失败态收尾（F-23）：不发 done（终态互斥）、不记成功；失败日志与部分输出策略
     * （answer_source={@value #FAILED_SOURCE}）落库，供审计区分"失败/截断轮"。
     *
     * @param ctx      本轮上下文
     * @param terminal SSE error 终态；null 表示 EOF 截断（调用方已发 STREAM_TRUNCATED）
     * @param mode     原始答案来源（kb / llm_general）
     */
    private void recordFailed(Ctx ctx, GenerateTerminal terminal, String mode) {
        long elapsed = System.currentTimeMillis() - ctx.start;
        String code = terminal == null ? "STREAM_TRUNCATED" : terminal.errorCode;
        log.warn("chat generate failed sessionId={} userId={} code={} partialAnswerChars={} mode={}",
                ctx.sessionId, ctx.user.getId(), code, ctx.answer.length(), mode);
        qaAccessLogService.record(ctx.sessionId, ctx.user.getId(), ctx.question, ctx.answer.toString(),
                ctx.chosenUnitId,
                FAILED_SOURCE,
                ctx.recalled, ctx.authorized, ctx.unauthorized, ctx.tokens[0], ctx.tokens[1],
                new LatencySample((int) elapsed, ctx.retrieveMs, ctx.generateMs, ctx.dbMs, ctx.cacheMs), false);
        ctx.emitter.complete();
    }

    /**
     * 构造 generate 回调（F-23）：仅 done 置成功终态；error 置失败终态并回传 SSE error。
     *
     * <p>回调不自行发送 done / complete——终态判定与收尾统一由调用方在 generate 返回后进行，
     * 保证 error 与 done 事件对客户端与访问日志互斥可见。
     */
    private GenerateStreamHandler streamHandler(SseEmitter emitter, StringBuilder answer, int[] tokens,
                                                GenerateTerminal terminal) {
        return new GenerateStreamHandler() {
            @Override
            public void onDelta(String text) {
                answer.append(text);
                send(emitter, "delta", new DeltaEvent(text));
            }

            @Override
            public void onDone(int promptTokens, int completionTokens, long elapsedMs, boolean faqHit) {
                tokens[0] = promptTokens;
                tokens[1] = completionTokens;
                terminal.done = true;
            }

            @Override
            public void onError(String code, String message) {
                terminal.error = true;
                terminal.errorCode = code;
                terminal.errorMessage = message;
                send(emitter, "error", new ErrorEvent(code, message));
            }
        };
    }

    /**
     * run(...) 的一次调用上下文：承载跨分支共享的输入、计时、累积列表与中间结果。
     *
     * <p>刻意做成**每次调用新建的局部对象**，而不是实例字段——本类是单例 Bean，
     * 引入实例可变状态会造成并发串扰。
     */
    private static final class Ctx {

        private final String sessionId;
        private final CurrentUser user;
        private final SseEmitter emitter;
        private final String question;
        private final String ownerId;
        private final String turnId;
        private final Long chosenUnitId;
        private final long start;

        private final StringBuilder answer = new StringBuilder();
        private final List<Long> recalled = new ArrayList<>();
        private final List<Long> authorized = new ArrayList<>();
        private final List<Long> unauthorized = new ArrayList<>();
        // 未授权但标题被展示过的单元编码（拒答轮的 referenced：标题同样是泄漏面）
        private final List<String> unauthorizedCodes = new ArrayList<>();
        private final int[] tokens = {0, 0};

        private int retrieveMs = 0;
        private int generateMs = 0;
        private int dbMs = 0;
        private int cacheMs = 0;

        private Map<Long, KnowledgeUnit> accessibleUnits;
        private List<String> aclUnitCodes;
        private boolean chatFallbackEnabled;
        private boolean chatFallbackApplies;
        private Map<Long, SourceItemVO> authorizedSources;
        private Map<Long, SourceItemVO> unauthorizedSources;
        private List<GenerateContext> contexts;

        private Ctx(ChatRequest request, String sessionId, CurrentUser user, SseEmitter emitter,
                   String question, String ownerId, String turnId, long start) {
            this.sessionId = sessionId;
            this.user = user;
            this.emitter = emitter;
            this.question = question;
            this.ownerId = ownerId;
            this.turnId = turnId;
            this.chosenUnitId = request.getChosenUnitId();
            this.start = start;
        }
    }

    /** 统一的拒答出口：sources 先到（无权限来源要能提示出来），再给文本、收尾、落库。 */
    private void returnRefusal(SseEmitter emitter, String sessionId, CurrentUser user, String question,
                               StringBuilder answer, String text, SourcesEvent sources, long start,
                               Long chosenUnitId,
                               List<Long> recalled, List<Long> authorized, List<Long> unauthorized,
                               int retrieveMs, int dbMs, int cacheMs) {
        send(emitter, "sources", sources);
        send(emitter, "delta", new DeltaEvent(text));
        answer.append(text);
        long elapsed = System.currentTimeMillis() - start;
        send(emitter, "done", new DoneEvent(sessionId, 0, elapsed, false));
        qaAccessLogService.record(sessionId, user.getId(), question, answer.toString(), chosenUnitId,
                "refusal",
                recalled, authorized, unauthorized, 0, 0,
                new LatencySample((int) elapsed, retrieveMs, 0, dbMs, cacheMs), false);
        // 拒答轮：展示过的来源标题都记入 referenced（无权限单元的标题同样是泄漏面）
        List<String> referencedCodes = new ArrayList<>(codesOfIds(authorized));
        referencedCodes.addAll(codesOfIds(unauthorized));
        appendTurn(user, sessionId, "refusal", question, answer.toString(),
                List.of(), referencedCodes, chosenUnitId, false);
        emitter.complete();
    }

    /**
     * 闲聊/通用问答兜底：知识库没命中、且问题被判定为**非业务性**时，用 AI 通用知识回答。
     *
     * <p>回答在上、参考资料在下（复用 clarify reference 事件，前端天然渲染在下方）；
     * 落库 {@code answer_source=llm_general}，且**不记知识缺口**（闲聊问题本就不该进售后知识库）。
     *
     * <p>只应在 {@code ai.chat-fallback-enabled=true} 且非业务问题时被调用；权限拦截
     * （{@code unauthorizedSources} 非空）永不走到这里。
     */
    private void returnGeneralFallback(SseEmitter emitter, String sessionId, CurrentUser user, String question,
                                       StringBuilder answer, Map<Long, KnowledgeUnit> accessibleUnits,
                                       List<Long> recalled, List<Long> authorized, List<Long> unauthorized,
                                       long start, int retrieveMs, int dbMs, int cacheMs) {
        // ① 先发 notice：让用户在答案之前就知道"这是通用知识，非公司规定"
        send(emitter, "notice", new NoticeEvent("llm_general", GENERAL_NOTICE_TEXT));
        // ② 参考资料放回答下方（可能为空；为空则不渲染参考卡片）
        List<ClarifyOptionVO> references = clarificationService.referenceCandidates(question, accessibleUnits);
        if (!references.isEmpty()) {
            send(emitter, "clarify", new ClarifyEvent("reference", null, references));
        }
        // ③ 流式生成：contexts 为空、模式 GENERAL（AI 侧 mode="chat"）
        int[] tokens = {0, 0};
        List<String> aclUnitCodes =
                accessibleUnits.values().stream().map(KnowledgeUnit::getUnitCode).toList();
        long generateStart = System.currentTimeMillis();
        aiClient.generate(question, List.of(), sessionId, AiGenerateMode.GENERAL,
                ownerIdOf(user.getId()), UUID.randomUUID().toString().replace("-", ""),
                aclUnitCodes, null, true, new GenerateStreamHandler() {
            @Override
            public void onDelta(String text) {
                answer.append(text);
                send(emitter, "delta", new DeltaEvent(text));
            }

            @Override
            public void onDone(int promptTokens, int completionTokens, long elapsedMs, boolean faqHit) {
                tokens[0] = promptTokens;
                tokens[1] = completionTokens;
            }

            @Override
            public void onError(String code, String message) {
                send(emitter, "error", new ErrorEvent(code, message));
            }
        });
        int generateMs = (int) (System.currentTimeMillis() - generateStart);
        long elapsed = System.currentTimeMillis() - start;
        send(emitter, "done", new DoneEvent(sessionId, tokens[0] + tokens[1], elapsed, false));
        qaAccessLogService.record(sessionId, user.getId(), question, answer.toString(), null,
                "llm_general",
                recalled, authorized, unauthorized, tokens[0], tokens[1],
                new LatencySample((int) elapsed, retrieveMs, generateMs, dbMs, cacheMs), false);
        emitter.complete();
    }

    /**
     * owner_id = {@code sha256(userId + 盐)[:16]}。
     *
     * <p>不透明分区键：AI 只当 Mongo 文档分区 key，绝不解释、绝不据此判权限。
     * 加盐避免直接暴露 userId（与 qa_access_logs 用 user_id 兜底同一目的）。
     */
    private String ownerIdOf(Long userId) {
        String raw = userId + ":" + aiProperties.getSessionOwnerSalt();
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(raw.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(bytes.length * 2);
            for (byte b : bytes) {
                hex.append(Character.forDigit((b >> 4) & 0xF, 16));
                hex.append(Character.forDigit(b & 0xF, 16));
            }
            return hex.substring(0, 16);
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException("SHA-256 不可用", ex);
        }
    }

    /** 由知识单元 id 批量反查业务编码（用于会话记忆的 authorized/referenced）。 */
    private List<String> codesOfIds(Collection<Long> ids) {
        if (ids == null || ids.isEmpty()) {
            return List.of();
        }
        List<String> codes = new ArrayList<>();
        for (KnowledgeUnit unit : unitMapper.selectByIds(new ArrayList<>(ids))) {
            if (unit.getUnitCode() != null) {
                codes.add(unit.getUnitCode());
            }
        }
        return codes;
    }

    /** 澄清参考候选的业务编码。 */
    private List<String> codesOfOptions(List<ClarifyOptionVO> options) {
        List<String> codes = new ArrayList<>();
        for (ClarifyOptionVO option : options) {
            if (option.getUnitCode() != null) {
                codes.add(option.getUnitCode());
            }
        }
        return codes;
    }

    /**
     * 追加一轮会话记忆（best-effort）。
     *
     * <p>只用于**不经过 generate 的轮次**（FAQ / 澄清 ask / 澄清 reference / 拒答）；
     * 正常问答与闲聊兜底由 AI 在 generate 收尾自行写入。任何异常都被吞掉，
     * 绝不影响问答主链路（与 AI 侧 {@code NullSessionStore} 降级互为兜底）。
     */
    private void appendTurn(CurrentUser user, String sessionId, String turnType, String question, String answer,
                            List<String> authorizedCodes, List<String> referencedCodes, Long chosenUnitId,
                            boolean faqHit) {
        try {
            SessionTurn turn = new SessionTurn();
            turn.setSessionId(sessionId);
            turn.setOwnerId(ownerIdOf(user.getId()));
            turn.setTurnId(UUID.randomUUID().toString().replace("-", ""));
            turn.setTurnType(turnType);
            turn.setQuestion(question);
            turn.setAnswer(answer);
            turn.setAuthorizedUnitIds(authorizedCodes);
            turn.setReferencedUnitIds(referencedCodes);
            turn.setChosenUnitId(chosenUnitId == null ? null : String.valueOf(chosenUnitId));
            turn.setFaqHit(faqHit);
            aiClient.appendSessionTurn(turn);
        } catch (RuntimeException ex) {
            log.warn("append session turn failed (ignored) sessionId={} type={}", sessionId, turnType, ex);
        }
    }

    /** 知识正文优先取 content，缺失时退回 summary（种子数据只有一句话摘要）。 */
    private String bodyOf(KnowledgeUnit unit) {
        String content = unit.getContent();
        if (content != null && !content.isBlank()) {
            return content;
        }
        String summary = unit.getSummary();
        return summary == null ? "" : summary;
    }

    private Map<Long, KnowledgeUnit> loadRetrievableUnits(CurrentUser user) {
        // 知识管理员（含系统管理员）按 features.md §1「全部数据可见」，不受四维数据权限限制；
        // 其余用户走 DataPermissionEngine（fail-closed）。
        List<Long> accessibleIds = user.hasPermission("knowledge:perm")
                ? unitMapper.selectRetrievableUnitIds()
                : dataPermissionEngine.accessibleUnitIds(user);
        if (accessibleIds.isEmpty()) {
            return Map.of();
        }
        List<KnowledgeUnit> units = unitMapper.selectByIds(accessibleIds);
        Map<Long, KnowledgeUnit> result = new LinkedHashMap<>();
        for (KnowledgeUnit unit : units) {
            // 检索可见性只认 status=active AND ingest_status=active
            if ("active".equals(unit.getStatus()) && "active".equals(unit.getIngestStatus())) {
                result.put(unit.getId(), unit);
            }
        }
        return result;
    }

    private void send(SseEmitter emitter, String event, Object payload) {
        try {
            emitter.send(SseEmitter.event().name(event).data(JsonUtils.toJson(payload)));
        } catch (IOException ex) {
            throw new IllegalStateException("SSE 发送失败（客户端可能已断开）", ex);
        }
    }

    private void sendQuietly(SseEmitter emitter, String event, Object payload) {
        try {
            emitter.send(SseEmitter.event().name(event).data(JsonUtils.toJson(payload)));
        } catch (IOException | IllegalStateException ex) {
            log.debug("failed to send sse event={}", event);
        }
    }
}

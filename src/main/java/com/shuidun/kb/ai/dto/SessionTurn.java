package com.shuidun.kb.ai.dto;

import java.util.ArrayList;
import java.util.List;
import lombok.Data;

/**
 * 一轮会话记录（Java → AI 的 {@code /internal/session/append} 载荷）。
 *
 * <p>由 Java 负责生成 {@code turnId}（UUID，重试复用）；AI 侧按
 * {@code (ownerId, sessionId)} 双键隔离存储，{@code turnId} 作为幂等守卫。
 *
 * <p>{@code authorizedUnitIds} / {@code referencedUnitIds} 都是**知识单元业务编码**（unitCode），
 * 不是数据库 id —— 它们要与 Java 每轮下发的 {@code aclUnitIds} 做子集判定。
 */
@Data
public class SessionTurn {

    /** 会话 id。 */
    private String sessionId;

    /** 不透明分区键（Java 计算，AI 只当 key，绝不解释）。 */
    private String ownerId;

    /** 本轮 id（UUID，重试复用；AI 用它做幂等守卫）。 */
    private String turnId;

    /** 轮次类型：qa / faq / clarify_ask / clarify_reference / chosen / llm_general / refusal。 */
    private String turnType;

    /** 用户提问。 */
    private String question;

    /** 助手回答（落库文本）。 */
    private String answer;

    /** 本轮实际使用的授权知识单元编码。 */
    private List<String> authorizedUnitIds = new ArrayList<>();

    /** 本轮推给过用户的候选/来源单元编码（标题同样是泄漏面，必须参与 ACL 过滤）。 */
    private List<String> referencedUnitIds = new ArrayList<>();

    /** 澄清点选：本轮选中的知识单元 id（无则 null）。 */
    private String chosenUnitId;

    /** 是否命中 FAQ 缓存。 */
    private boolean faqHit;
}

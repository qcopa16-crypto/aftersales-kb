package com.shuidun.kb.ai;

import com.shuidun.kb.ai.dto.ClarifyEvent;
import com.shuidun.kb.ai.dto.ClarifyOptionVO;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.stereotype.Service;

/**
 * 澄清服务：无召回时决定"该追问什么"或"给什么参考"。
 *
 * <p><b>核心原则：缺什么才问什么。</b>「检索为空」不等于「需要澄清」——
 * 检索为空最常见的原因是知识还没切片向量化，跟用户问题是否完整无关。
 * 所以这里先判断问题信息是否足够，再决定行为：
 *
 * <ul>
 *   <li><b>信息不足 → 追问</b>（{@code kind=ask}）：
 *       「路由器有问题」只说了"有问题"没说现象，就该问"具体是什么现象"，并给出可点的现象维度；
 *       而不是甩一堆知识文档让用户挑。</li>
 *   <li><b>信息足够但没命中 → 给参考</b>（{@code kind=reference}）：
 *       诚实说"没找到直接匹配的资料"，把可能相关的几份列出来供参考，点不点都行。</li>
 * </ul>
 *
 * <p>判断规则（纯规则、零 LLM、毫秒级）：
 * <ol>
 *   <li>含<b>笼统故障词</b>（有问题/坏了/异常…）且不含<b>具体现象词</b> → 缺"现象"；</li>
 *   <li>问题里<b>没有任何知识库实体命中</b>（标题/摘要/正文都没命中）且很短 → 缺"对象"；</li>
 *   <li>其余 → 信息足够。</li>
 * </ol>
 *
 * <p>候选与选项都只来自**当前用户可访问的知识单元**，不会泄露无权限知识的存在。
 */
@Service
public class ClarificationService {

    /** 候选条数上限。 */
    private static final int MAX_OPTIONS = 5;

    /** 进入候选的最低分。 */
    private static final int MIN_SCORE = 3;

    /** 片段长度上限。 */
    private static final int SNIPPET_MAX = 80;

    /** 追问文案里回显原问题的最大长度。 */
    private static final int ECHO_MAX = 30;

    /** "很短的问题"阈值：短且无实体命中时才判定缺对象，避免把正常长问题误拦下来追问。 */
    private static final int SHORT_QUESTION_HAN_MAX = 8;

    /** 标题 / 摘要 / 正文的命中权重。 */
    private static final int W_TITLE = 3;
    private static final int W_SUMMARY = 2;
    private static final int W_CONTENT = 1;

    /** 笼统故障描述：说了"有问题"但没说是什么问题。 */
    private static final List<String> VAGUE_FAULT_WORDS = List.of(
            "有问题", "有故障", "出问题", "坏了", "不好用", "不好使", "异常", "报错",
            "用不了", "不能用", "不行", "无法使用", "不工作", "失灵", "出故障", "有问题");

    /**
     * 具体现象词：出现任意一个就说明用户已经描述了现象，不必再问。
     *
     * <p>前一组刻意与 {@link #PHENOMENON_HINTS} 对齐——用户点了追问里的提示，
     * 就等于把现象说清楚了，下一轮不能再追问同一件事。
     * 宁可多列（误判为"已描述"只会导致不追问，更保守），也不要漏。
     */
    private static final List<String> SPECIFIC_SYMPTOM_WORDS = List.of(
            // 与 PHENOMENON_HINTS 对应
            "无法开机", "无反应", "指示灯异常", "无法联网", "连接失败", "频繁掉线",
            "不稳定", "出现错误码", "功能异常", "结果不对", "发热异常",
            // 常见自述现象
            "不亮", "闪烁", "卡纸", "卡住", "进纸", "无法上网", "连不上", "掉线", "断线",
            "重启", "黑屏", "花屏", "异响", "噪音", "过热", "发烫", "卡顿", "死机",
            "没反应", "错误码", "报错码", "e-0", "w-0", "不打印", "打印不了",
            "偏移", "模糊", "条纹", "不上纸", "慢");

    /** 缺"现象"时给的现象维度（通用，不绑定具体产品）。 */
    private static final List<String> PHENOMENON_HINTS = List.of(
            "无法开机 / 无反应",
            "指示灯异常",
            "无法联网 / 连接失败",
            "频繁掉线 / 不稳定",
            "报错 / 出现错误码",
            "功能异常 / 结果不对",
            "声音或发热异常");

    /** 中文按 2-gram 切分；英文/数字/错误码按整词匹配（如 PWR、E-05、SOP）。 */
    private static final Pattern ASCII_TOKEN = Pattern.compile("[a-z0-9][a-z0-9\\-]{1,}");
    private static final Pattern NON_HAN = Pattern.compile("[^\\p{IsHan}]");
    private static final Pattern WHITESPACE = Pattern.compile("\\s+");

    /**
     * 若问题信息不足，返回追问事件；信息足够则返回空。
     *
     * @param question        用户问题
     * @param accessibleUnits 当前用户可访问的知识单元
     * @return kind=ask 的澄清事件；无需追问时为空
     */
    public Optional<ClarifyEvent> askIfInsufficient(String question,
                                                    Map<Long, KnowledgeUnit> accessibleUnits) {
        return askIfInsufficient(question, accessibleUnits, true);
    }

    /**
     * 同上，但可关闭「规则 2（缺对象）」。
     *
     * <p>闲聊兜底开启且问题**非业务性**时传 {@code allowObjectAsk=false}：像「1+1」
     * 「今天天气怎么样」这类短问题不该被追问「是哪个设备」，应交给通用知识回答。
     * 而「路由器有问题」这类含<b>笼统故障词</b>的（规则 1）仍照常追问现象 ——
     * 它隐含了设备上下文，澄清比泛泛的通用回答更有用。
     *
     * @param question        问题
     * @param accessibleUnits 当前用户可访问的知识单元
     * @param allowObjectAsk  是否允许「缺对象」追问（规则 2）
     * @return 追问事件；无需追问时为空
     */
    public Optional<ClarifyEvent> askIfInsufficient(String question,
                                                    Map<Long, KnowledgeUnit> accessibleUnits,
                                                    boolean allowObjectAsk) {
        if (question == null || question.isBlank()) {
            return Optional.empty();
        }
        String lower = question.toLowerCase(Locale.ROOT);
        boolean vague = containsAny(lower, VAGUE_FAULT_WORDS);
        boolean specific = containsAny(lower, SPECIFIC_SYMPTOM_WORDS);

        // 规则 1：说了"有问题/坏了"这类笼统描述，却没说什么现象 → 缺现象
        if (vague && !specific) {
            return Optional.of(new ClarifyEvent("ask", promptForPhenomenon(question),
                    PHENOMENON_HINTS.stream().map(ClarifyOptionVO::hint).toList()));
        }

        Set<String> keywords = keywordsOf(question);
        boolean hasEntity = hasKnowledgeEntity(keywords, accessibleUnits);
        String han = NON_HAN.matcher(lower).replaceAll("");
        // 规则 2：短问题且没有任何知识库实体命中 → 缺"对象"（是哪个设备/哪份资料）
        if (allowObjectAsk && !hasEntity && han.length() <= SHORT_QUESTION_HAN_MAX) {
            List<ClarifyOptionVO> devices = deviceOptions(accessibleUnits);
            if (!devices.isEmpty()) {
                return Optional.of(new ClarifyEvent("ask", promptForDevice(), devices));
            }
        }
        return Optional.empty();
    }

    /**
     * 规则 1（缺现象）单独暴露：**只看问题本身是否含糊**（有笼统故障词但没说现象），
     * 与检索结果无关。
     *
     * <p>为什么单独提出来：澄清原本只在「检索为空」时触发，但知识库一旦有内容，
     * 检索几乎总能返回 chunk，导致含糊问题也被拿去硬答。而"问题说得够不够清楚"
     * 本就不该由检索结果决定 —— 含糊就先追问现象。
     *
     * @param question 问题
     * @return 追问事件；问题不笼统时为空
     */
    public Optional<ClarifyEvent> askPhenomenonIfVague(String question) {
        if (question == null || question.isBlank()) {
            return Optional.empty();
        }
        String lower = question.toLowerCase(Locale.ROOT);
        boolean vague = containsAny(lower, VAGUE_FAULT_WORDS);
        boolean specific = containsAny(lower, SPECIFIC_SYMPTOM_WORDS);
        if (vague && !specific) {
            return Optional.of(new ClarifyEvent("ask", promptForPhenomenon(question),
                    PHENOMENON_HINTS.stream().map(ClarifyOptionVO::hint).toList()));
        }
        return Optional.empty();
    }

    /**
     * 短问题且知识库**无任何实体命中** —— 闲聊兜底的强信号。
     *
     * <p>用于闲聊兜底判定：像「1+1」「今天天气怎么样」「你好」这类短问题，知识库里
     * 根本没有这个话题，用通用知识回答比拿不相关资料硬答"不知道"更合适。
     *
     * <p>刻意**限定"短问题"**：长问题里几乎必然含「方案」「什么」这类泛词而误判为
     * 命中实体（如「量子计算机的散热方案是什么」会命中「处理方案」）。也比"检索分数
     * 阈值"更稳 —— 实测分数区间重叠严重（「备件怎么申请」0.445 vs「1+1」0.461）。
     *
     * @param question        问题
     * @param accessibleUnits 当前用户可访问的知识单元
     * @return true = 短问题且无实体命中
     */
    public boolean isShortNoEntity(String question, Map<Long, KnowledgeUnit> accessibleUnits) {
        if (question == null || question.isBlank()) {
            return false;
        }
        String han = NON_HAN.matcher(question.toLowerCase(Locale.ROOT)).replaceAll("");
        if (han.length() > SHORT_QUESTION_HAN_MAX) {
            return false;
        }
        return !hasKnowledgeEntity(keywordsOf(question), accessibleUnits);
    }

    /**
     * 信息足够但检索无命中时，给出"可能相关的资料"作为参考。
     *
     * <p>与追问的区别：这里**不要求**用户先选才能得到回答，只是提供参考入口。
     *
     * @param question        用户问题
     * @param accessibleUnits 当前用户可访问的知识单元
     * @return 参考候选；无可信候选时返回空列表
     */
    public List<ClarifyOptionVO> referenceCandidates(String question,
                                                     Map<Long, KnowledgeUnit> accessibleUnits) {
        if (question == null || question.isBlank() || accessibleUnits == null || accessibleUnits.isEmpty()) {
            return List.of();
        }
        Set<String> keywords = keywordsOf(question);
        if (keywords.isEmpty()) {
            return List.of();
        }

        List<ClarifyOptionVO> options = new ArrayList<>();
        for (KnowledgeUnit unit : accessibleUnits.values()) {
            int titleHits = hits(keywords, unit.getTitle());
            int summaryHits = hits(keywords, unit.getSummary());
            int contentHits = hits(keywords, unit.getContent());
            int score = W_TITLE * titleHits + W_SUMMARY * summaryHits + W_CONTENT * contentHits;
            // 只靠正文单点命中不足以作为候选，否则几乎每个问题都会命中一堆单元
            if (score < MIN_SCORE || (titleHits == 0 && summaryHits + contentHits < 2)) {
                continue;
            }
            options.add(ClarifyOptionVO.unit(
                    unit.getId(),
                    unit.getUnitCode(),
                    unit.getTitle() == null ? "" : unit.getTitle(),
                    unit.getCategory() == null ? "" : unit.getCategory(),
                    snippetOf(unit, keywords),
                    score));
        }

        return options.stream()
                .sorted(Comparator.comparingInt(ClarifyOptionVO::getScore).reversed()
                        .thenComparing(ClarifyOptionVO::getUnitId))
                .limit(MAX_OPTIONS)
                .toList();
    }

    /** 缺"对象"时的设备选项：取可访问知识里的产品类单元。 */
    private List<ClarifyOptionVO> deviceOptions(Map<Long, KnowledgeUnit> accessibleUnits) {
        if (accessibleUnits == null || accessibleUnits.isEmpty()) {
            return List.of();
        }
        return accessibleUnits.values().stream()
                .filter(unit -> "product".equals(unit.getCategory()))
                .limit(MAX_OPTIONS)
                .map(unit -> ClarifyOptionVO.unit(
                        unit.getId(),
                        unit.getUnitCode(),
                        unit.getTitle() == null ? "" : unit.getTitle(),
                        unit.getCategory(),
                        snippetOf(unit, Set.of()),
                        0))
                .toList();
    }

    private String promptForPhenomenon(String question) {
        return "「" + echo(question) + "」这个描述还不够定位问题，能补充下具体现象吗？"
                + "（点选下面的选项，或直接描述都可以）";
    }

    private String promptForDevice() {
        return "没找到直接匹配的资料。请问是哪个设备？选择后我基于对应手册作答；也可以换个说法再问。";
    }

    private String echo(String question) {
        String text = question == null ? "" : question.strip();
        return text.length() <= ECHO_MAX ? text : text.substring(0, ECHO_MAX) + "…";
    }

    private boolean containsAny(String haystack, List<String> needles) {
        for (String needle : needles) {
            if (haystack.contains(needle)) {
                return true;
            }
        }
        return false;
    }

    /** 问题里是否命中了任何知识库实体（标题/摘要/正文任一命中即算）。 */
    private boolean hasKnowledgeEntity(Set<String> keywords, Map<Long, KnowledgeUnit> accessibleUnits) {
        if (keywords.isEmpty() || accessibleUnits == null) {
            return false;
        }
        for (KnowledgeUnit unit : accessibleUnits.values()) {
            if (hits(keywords, unit.getTitle()) > 0
                    || hits(keywords, unit.getSummary()) > 0
                    || hits(keywords, unit.getContent()) > 0) {
                return true;
            }
        }
        return false;
    }

    /**
     * 提取匹配关键词。
     *
     * <p>中文没有词边界，用 2-gram 近似（"指示灯绿色" → 指示/示灯/灯绿/绿色）；
     * 英文与错误码按整词提取。刻意**不做停用词删除**——像"什么""怎么"这类词产生的
     * 2-gram 在知识正文里几乎不会出现，自然贡献 0 分；而按子串删停用词会误伤
     * "内存"（含"有"）这类正常词。
     */
    private Set<String> keywordsOf(String question) {
        String lower = question.toLowerCase(Locale.ROOT);
        Set<String> keys = new LinkedHashSet<>();

        Matcher ascii = ASCII_TOKEN.matcher(lower);
        while (ascii.find()) {
            keys.add(ascii.group());
        }

        String han = NON_HAN.matcher(lower).replaceAll("");
        if (han.length() == 1) {
            keys.add(han);
        }
        for (int i = 0; i + 2 <= han.length(); i++) {
            keys.add(han.substring(i, i + 2));
        }
        return keys;
    }

    /** 该字段命中了几个关键词（去重后计数，避免长正文把分数堆高）。 */
    private int hits(Set<String> keywords, String haystack) {
        if (haystack == null || haystack.isEmpty()) {
            return 0;
        }
        String lower = haystack.toLowerCase(Locale.ROOT);
        int count = 0;
        for (String keyword : keywords) {
            if (lower.contains(keyword)) {
                count++;
            }
        }
        return count;
    }

    /** 片段优先取"命中位置附近的正文"，让用户看得出为什么推荐它。 */
    private String snippetOf(KnowledgeUnit unit, Set<String> keywords) {
        String content = unit.getContent() == null ? "" : unit.getContent();
        int hit = firstHitIndex(content, keywords);
        if (hit >= 0) {
            int start = Math.max(0, hit - 10);
            int end = Math.min(content.length(), start + SNIPPET_MAX);
            String window = WHITESPACE.matcher(content.substring(start, end)).replaceAll(" ").trim();
            return (start > 0 ? "…" : "") + window + (end < content.length() ? "…" : "");
        }
        String summary = unit.getSummary();
        if (summary != null && !summary.isBlank()) {
            return trim(summary);
        }
        return trim(WHITESPACE.matcher(content).replaceAll(" "));
    }

    private int firstHitIndex(String content, Set<String> keywords) {
        if (content.isEmpty()) {
            return -1;
        }
        String lower = content.toLowerCase(Locale.ROOT);
        int best = -1;
        for (String keyword : keywords) {
            int idx = lower.indexOf(keyword);
            if (idx >= 0 && (best < 0 || idx < best)) {
                best = idx;
            }
        }
        return best;
    }

    private String trim(String text) {
        String value = text == null ? "" : text.strip();
        return value.length() <= SNIPPET_MAX ? value : value.substring(0, SNIPPET_MAX) + "…";
    }
}

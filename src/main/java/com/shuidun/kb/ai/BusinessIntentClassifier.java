package com.shuidun.kb.ai;

import com.shuidun.kb.config.AiProperties;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

/**
 * 业务性意图分类器（闲聊/通用问答兜底的**唯一安全网**）。
 *
 * <p>纯规则、零 LLM、毫秒级，仿 {@link ClarificationService} 风格。当知识库没命中时，
 * 只有判定为**非业务**的问题才允许走"用 AI 通用知识回答"；一旦被判为业务问题
 * （保修/价格/SLA/政策等），坚持走现有"没找到资料"话术。
 *
 * <p><b>宁枉勿纵</b>：词表刻意做宽。误拦（把闲聊判成业务）的代价只是用户体验差一点
 * （收到现有的"没找到资料"话术）；漏判（把业务判成闲聊）的代价是用户拿到 AI 编造的
 * "保修一年"并拿去承诺客户 —— 这是事故。故用黑名单 + 宽词表，而非白名单。
 *
 * <p>词表可用 {@code ai.chat-business-keywords} 追加（运营补充），与内置词表取**并集**，
 * 不会因为配置覆盖而削弱默认安全网。
 */
@Service
@RequiredArgsConstructor
public class BusinessIntentClassifier {

    /** 内置业务维度关键词（宁枉勿纵，宁可误拦不可漏判）。 */
    private static final List<String> DEFAULT_KEYWORDS = List.of(
            // 保修
            "保修", "质保", "三包", "包修", "包换", "保修期", "保修范围", "保修卡", "报修",
            // 价格
            "价格", "报价", "多少钱", "费用", "收费", "折扣", "折", "结算", "对账", "账单",
            "单价", "总价", "付款", "支付", "货款", "优惠", "便宜",
            "工时费", "人工费", "检测费", "服务费", "材料费",
            // SLA
            "SLA", "时效", "响应时间", "承诺", "时限", "工期", "交付时间", "上门", "上门时间", "保证",
            "多久", "几天", "预约", "周期",
            // 政策
            "政策", "制度", "流程", "标准", "规范", "条款", "规定", "规则", "办法", "手续", "协议",
            // 商务 / 售后
            "合同", "发票", "退款", "退货", "换货", "赔付", "赔偿", "违约金", "索赔", "开票", "税率",
            "售后", "维修", "退换", "发货", "运费", "邮费", "快递", "物流", "备件", "配件",
            // 产品
            "参数", "型号", "规格", "错误码", "故障码", "报错码", "序列号", "功率", "电压", "尺寸",
            "重量", "容量", "接口", "兼容");

    /** 带单位的具体数字 / "多少X"，补词表的漏。 */
    private static final Pattern NUMERIC_PATTERN =
            Pattern.compile("\\d+\\s*(年|个月|月|天|小时|分钟|元|块|折|%)|多少(钱|天|年|月)");

    private final AiProperties aiProperties;

    /**
     * 判断问题是否为业务性问题（涉及公司具体规定）。
     *
     * @param question 用户问题
     * @return true = 业务问题（不得闲聊兜底）
     */
    public boolean isBusiness(String question) {
        if (question == null || question.isBlank()) {
            return false;
        }
        for (String keyword : allKeywords()) {
            if (!keyword.isBlank() && question.contains(keyword)) {
                return true;
            }
        }
        return NUMERIC_PATTERN.matcher(question).find();
    }

    /** 内置词表 + 运营补充词表的并集。 */
    private List<String> allKeywords() {
        List<String> extra = aiProperties.getChatBusinessKeywords();
        if (extra == null || extra.isEmpty()) {
            return DEFAULT_KEYWORDS;
        }
        List<String> all = new ArrayList<>(DEFAULT_KEYWORDS);
        all.addAll(extra);
        return all;
    }
}

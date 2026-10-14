package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.config.AiProperties;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * 业务性意图分类器单测：宽词表 + 正则的命中与不误判。
 *
 * <p>这是闲聊兜底的**唯一安全网**，宁枉勿纵：业务词必须命中，通用问题不得误判。
 */
class BusinessIntentClassifierTest {

    private final BusinessIntentClassifier classifier = new BusinessIntentClassifier(new AiProperties());

    @Test
    void businessKeywordsAreBlocked() {
        assertTrue(classifier.isBusiness("保修期是多久"));
        assertTrue(classifier.isBusiness("这个多少钱"));
        assertTrue(classifier.isBusiness("SLA 响应时间是多少"));
        assertTrue(classifier.isBusiness("退款要走什么流程"));
        assertTrue(classifier.isBusiness("设备参数是什么"));
        assertTrue(classifier.isBusiness("合同发票怎么开"));
    }

    @Test
    void semanticVariantsAreBlockedAfterHardening() {
        // QA 实测的语义漏判样本：补词表后必须全部命中（词表是唯一安全网）
        assertTrue(classifier.isBusiness("多久能上门"));
        assertTrue(classifier.isBusiness("上门费怎么算"));
        assertTrue(classifier.isBusiness("能便宜点吗"));
        assertTrue(classifier.isBusiness("怎么申请售后"));
        assertTrue(classifier.isBusiness("维修要预约吗"));
        assertTrue(classifier.isBusiness("发货要几天"));
        assertTrue(classifier.isBusiness("工时费怎么收"));
        assertTrue(classifier.isBusiness("运费谁承担"));
    }

    @Test
    void numericWithUnitOrHowManyIsBlocked() {
        assertTrue(classifier.isBusiness("一般要 3 天"), "带单位的数字应判为业务");
        assertTrue(classifier.isBusiness("大概 500 元"), "带单位的数字应判为业务");
        assertTrue(classifier.isBusiness("要多少天"), "多少X 应判为业务");
        assertTrue(classifier.isBusiness("打几折"), "折 属于价格维度");
    }

    @Test
    void generalQuestionsAreNotBusiness() {
        assertFalse(classifier.isBusiness("今天天气怎么样"));
        assertFalse(classifier.isBusiness("你好"));
        assertFalse(classifier.isBusiness("量子计算机的散热方案是什么"));
        assertFalse(classifier.isBusiness("什么是 TCP 三次握手"));
        assertFalse(classifier.isBusiness("帮我解释一下什么是递归"));
    }

    @Test
    void blankIsNotBusiness() {
        assertFalse(classifier.isBusiness(null));
        assertFalse(classifier.isBusiness("   "));
    }

    @Test
    void extraKeywordsFromConfigAreAdditiveNotReplacing() {
        AiProperties props = new AiProperties();
        props.setChatBusinessKeywords(List.of("延保"));
        BusinessIntentClassifier custom = new BusinessIntentClassifier(props);
        assertTrue(custom.isBusiness("延保怎么办理"), "配置的补充词应生效");
        assertTrue(custom.isBusiness("保修期是多久"), "内置词表不得被配置覆盖");
    }
}

package com.shuidun.kb.ai;

/**
 * generate SSE 事件回调（AI → Java）。
 */
public interface GenerateStreamHandler {

    /**
     * 收到增量文本。
     *
     * @param text 增量片段
     */
    void onDelta(String text);

    /**
     * 生成结束。
     *
     * @param promptTokens     输入 token
     * @param completionTokens 输出 token
     * @param elapsedMs        服务端耗时
     * @param faqHit           是否命中 FAQ
     */
    void onDone(int promptTokens, int completionTokens, long elapsedMs, boolean faqHit);

    /**
     * 生成失败。
     *
     * @param code    AI 侧错误码
     * @param message 人类可读信息
     */
    void onError(String code, String message);
}

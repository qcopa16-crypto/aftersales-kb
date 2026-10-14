package com.shuidun.kb.ai;

/**
 * AI 生成模式（与 AI 侧 {@code /internal/generate} 的 {@code mode} 字段一一对应）。
 *
 * <ul>
 *   <li>{@link #KB} —— 严格依据知识库资料作答（默认）；</li>
 *   <li>{@link #GENERAL} —— 闲聊/通用问答兜底，用 AI 通用知识作答（AI 侧 {@code mode="chat"}）。</li>
 * </ul>
 */
public enum AiGenerateMode {
    KB,
    GENERAL
}

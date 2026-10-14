package com.shuidun.kb.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;

/**
 * AI 内部接口专用 JSON 映射器。
 *
 * <p>对 AI 的内部 JSON 使用 snake_case（代码设计规范 §2.1），与对前端的 camelCase 严格分离。
 */
@Configuration
public class AiClientConfig {

    /**
     * AI 契约专用 ObjectMapper（snake_case）。
     *
     * @return 使用下划线命名策略的映射器
     */
    @Bean("aiObjectMapper")
    public ObjectMapper aiObjectMapper() {
        return JsonMapper.builder()
                .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
                .build();
    }
}

package com.shuidun.kb.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * JWT 配置绑定（代码设计规范 §3.4）。密钥与过期时间来自配置，禁止硬编码。
 */
@Data
@ConfigurationProperties(prefix = "jwt")
public class JwtProperties {

    /** HS256 签名密钥，长度须 >= 32 字节。 */
    private String secret;

    /** 令牌有效期（秒）。 */
    private long expireSeconds = 7200L;

    /** 签发者。 */
    private String issuer = "aftersales-kb";
}

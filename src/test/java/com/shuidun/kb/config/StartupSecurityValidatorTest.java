package com.shuidun.kb.config;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;

/**
 * 启动期安全校验单元测试（安全审计 F-002 / F-005 修复验证）。
 */
class StartupSecurityValidatorTest {

    private static final String DEV_SECRET =
            "__REDACTED_DEV_SECRET__";

    @Test
    void rejectsDevSecretOutsideDevelopmentProfile() {
        StartupSecurityValidator validator = validator(DEV_SECRET, "real-ai-token", "mysql");
        IllegalStateException ex = assertThrows(IllegalStateException.class, validator::afterPropertiesSet);
        assertTrue(ex.getMessage().contains("JWT_SECRET"), "错误信息应提示设置 JWT_SECRET");
    }

    @Test
    void rejectsDefaultAiTokenOutsideDevelopmentProfile() {
        StartupSecurityValidator validator = validator(
                "a-very-long-and-random-production-secret-key-0123456789", "change-me", "mysql");
        IllegalStateException ex = assertThrows(IllegalStateException.class, validator::afterPropertiesSet);
        assertTrue(ex.getMessage().contains("AI_INTERNAL_TOKEN"));
    }

    @Test
    void rejectsBlankAiTokenOutsideDevelopmentProfile() {
        StartupSecurityValidator validator = validator(
                "a-very-long-and-random-production-secret-key-0123456789", "", "prod");
        assertThrows(IllegalStateException.class, validator::afterPropertiesSet);
    }

    @Test
    void rejectsShortSecret() {
        StartupSecurityValidator validator = validator("too-short", "real-token", "h2");
        IllegalStateException ex = assertThrows(IllegalStateException.class, validator::afterPropertiesSet);
        assertTrue(ex.getMessage().contains("32"));
    }

    @Test
    void rejectsBlankSecret() {
        StartupSecurityValidator validator = validator("", "real-token", "h2");
        assertThrows(IllegalStateException.class, validator::afterPropertiesSet);
    }

    @Test
    void allowsDevelopmentProfileWithBuiltInDefaults() {
        StartupSecurityValidator validator = validator(DEV_SECRET, "change-me", "h2");
        assertDoesNotThrow(validator::afterPropertiesSet);
    }

    @Test
    void allowsProductionProfileWithRealSecrets() {
        StartupSecurityValidator validator = validator(
                "a-very-long-and-random-production-secret-key-0123456789", "long-random-internal-token", "mysql");
        assertDoesNotThrow(validator::afterPropertiesSet);
    }

    /**
     * 安全审计 S4（fail-closed）：未激活任何 profile 时，不得再视为开发环境。
     * 内置默认密钥（dev secret / change-me）应被拒绝，等同于生产标准。
     */
    @Test
    void rejectsDefaultSecretsWhenNoProfileActive() {
        StartupSecurityValidator validator = validator(DEV_SECRET, "change-me");
        IllegalStateException ex = assertThrows(IllegalStateException.class, validator::afterPropertiesSet);
        assertTrue(ex.getMessage().contains("JWT_SECRET"), "空 profile 应按生产标准拒绝默认密钥");
    }

    /**
     * 安全审计 S4：空 profile 下使用真实强密钥与真实 token 应允许启动。
     */
    @Test
    void allowsEmptyProfileWithRealSecrets() {
        StartupSecurityValidator validator = validator(
                "a-very-long-and-random-production-secret-key-0123456789", "long-random-internal-token");
        assertDoesNotThrow(validator::afterPropertiesSet);
    }

    private StartupSecurityValidator validator(String jwtSecret, String aiToken, String... profiles) {
        JwtProperties jwtProperties = new JwtProperties();
        jwtProperties.setSecret(jwtSecret);
        AiProperties aiProperties = new AiProperties();
        aiProperties.setInternalToken(aiToken);
        MockEnvironment environment = new MockEnvironment();
        environment.setActiveProfiles(profiles);
        return new StartupSecurityValidator(jwtProperties, aiProperties, environment);
    }
}

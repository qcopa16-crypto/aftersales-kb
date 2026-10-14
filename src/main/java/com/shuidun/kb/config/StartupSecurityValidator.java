package com.shuidun.kb.config;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.InitializingBean;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * 启动期安全校验（安全审计 F-002 / F-005 修复）。
 *
 * <p>开发 profile（h2 / dev / test）允许使用内置默认密钥以便本地零配置启动；
 * 其他任何 profile 下若仍在使用默认 JWT 密钥或默认 AI Internal Token，则**拒绝启动**，
 * 避免"源码公开的默认密钥"被带到可访问环境。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class StartupSecurityValidator implements InitializingBean {

    /** HS256 要求的最小密钥长度（字节）。 */
    private static final int MIN_SECRET_BYTES = 32;

    /** 开发用默认 JWT 密钥前缀（与 application-h2.yaml 保持一致）。 */
    private static final String DEV_JWT_SECRET_PREFIX = "aftersales-kb-dev-secret";

    /** AI 侧默认 Internal Token 占位值。 */
    private static final String DEFAULT_AI_TOKEN = "change-me";

    private final JwtProperties jwtProperties;
    private final AiProperties aiProperties;
    private final Environment environment;

    /**
     * 校验密钥强度与默认值使用情况。
     */
    @Override
    public void afterPropertiesSet() {
        String secret = jwtProperties.getSecret();
        if (secret == null || secret.isBlank()) {
            throw new IllegalStateException("jwt.secret 未配置：请通过环境变量 JWT_SECRET 提供至少 32 字节的随机密钥");
        }
        if (secret.getBytes(StandardCharsets.UTF_8).length < MIN_SECRET_BYTES) {
            throw new IllegalStateException("jwt.secret 长度不足 " + MIN_SECRET_BYTES + " 字节，HS256 不安全");
        }
        boolean developmentProfile = isDevelopmentProfile();
        if (!developmentProfile && secret.startsWith(DEV_JWT_SECRET_PREFIX)) {
            throw new IllegalStateException(
                    "检测到开发用默认 JWT 密钥，当前 profile 非开发环境，拒绝启动。请设置环境变量 JWT_SECRET");
        }
        if (!developmentProfile && DEFAULT_AI_TOKEN.equals(aiProperties.getInternalToken())) {
            throw new IllegalStateException(
                    "检测到 ai.internal-token 仍为默认值 change-me，当前 profile 非开发环境，拒绝启动。"
                            + "请设置环境变量 AI_INTERNAL_TOKEN");
        }
        if (!developmentProfile
                && (aiProperties.getInternalToken() == null || aiProperties.getInternalToken().isBlank())) {
            throw new IllegalStateException(
                    "ai.internal-token 未配置：请通过环境变量 AI_INTERNAL_TOKEN 提供（须与 AI 侧 INTERNAL_TOKEN 一致）");
        }
        if (developmentProfile) {
            log.warn("当前为开发 profile，使用内置默认密钥与演示口令，严禁用于生产环境");
        }
    }

    /**
     * 判断当前是否为开发 profile。
     *
     * <p>fail-closed（S4）：只有**显式**激活 h2 / dev / test 时才返回 {@code true}；
     * 未激活任何 profile（空数组）一律视为非开发环境（返回 {@code false}），
     * 从而在缺少 profile 配置时按生产标准拒绝内置默认密钥。
     */
    private boolean isDevelopmentProfile() {
        String[] profiles = environment.getActiveProfiles();
        if (profiles.length == 0) {
            return false;
        }
        return Arrays.stream(profiles).anyMatch(profile ->
                "h2".equals(profile) || "dev".equals(profile) || "test".equals(profile));
    }
}

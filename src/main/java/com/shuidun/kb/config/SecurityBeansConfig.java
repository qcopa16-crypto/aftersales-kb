package com.shuidun.kb.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;

/**
 * 密码编码器独立配置。
 *
 * <p>单独成类以避免 SecurityConfig → JwtAuthenticationFilter → AuthService → PasswordEncoder 的构造循环依赖。
 */
@Configuration
public class SecurityBeansConfig {

    /**
     * 密码编码器，统一使用 BCrypt（代码设计规范 §3.4）。
     *
     * @return BCrypt 编码器
     */
    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }
}

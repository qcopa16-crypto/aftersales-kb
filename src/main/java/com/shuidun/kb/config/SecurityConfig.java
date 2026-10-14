package com.shuidun.kb.config;

import com.shuidun.kb.auth.JwtAuthenticationFilter;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.Result;
import jakarta.servlet.DispatcherType;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.CorsConfigurationSource;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;
import tools.jackson.databind.ObjectMapper;

/**
 * Spring Security 配置（代码设计规范 §3.4）。
 *
 * <p>无状态 JWT 鉴权：登录与健康检查放行，其余 /api 需认证；401/403 统一返回 {@link Result} JSON。
 */
@Configuration
@EnableWebSecurity
@RequiredArgsConstructor
public class SecurityConfig {

    private final JwtAuthenticationFilter jwtAuthenticationFilter;
    private final ObjectMapper objectMapper;

    /**
     * CORS 配置，仅允许本地开发服务器访问 /api（生产由 Nginx 同源代理）。
     *
     * @return CORS 配置源
     */
    @Bean
    public CorsConfigurationSource corsConfigurationSource() {
        CorsConfiguration config = new CorsConfiguration();
        // 必须同时覆盖 IPv6 回环：浏览器打开 http://[::1]:5173 时 Origin 就是 [::1]，
        // 若不在白名单，Spring 会以 403 "Invalid CORS request" 拒绝所有 API 调用。
        config.setAllowedOriginPatterns(List.of(
                "http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"));
        config.setAllowedMethods(List.of("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"));
        config.setAllowedHeaders(List.of("*"));
        // 无状态 JWT（Authorization: Bearer），不使用 Cookie，故关闭 credentials：
        // allowCredentials(true) 只会放大 CSRF/凭据泄露面，对 Bearer 鉴权毫无必要。
        config.setAllowCredentials(false);
        UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
        source.registerCorsConfiguration("/**", config);
        return source;
    }

    /**
     * 安全过滤链：无状态、JWT 前置过滤、统一异常出口。
     *
     * @param http HttpSecurity 构建器
     * @return 过滤链
     * @throws Exception 构建失败
     */
    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        http
                .csrf(AbstractHttpConfigurer::disable)
                .cors(Customizer.withDefaults())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        // SSE 等异步请求完成后，容器会以 ASYNC 类型重新派发一次；此时 SecurityContext
                        // 已清空，若仍走鉴权会抛 AccessDenied 且响应已提交，导致连接被硬关闭、
                        // chunked 流缺少结束符（客户端报 "transfer closed with outstanding read data"）。
                        // ASYNC / ERROR 为容器内部派发类型，客户端无法伪造，放行是标准做法。
                        .dispatcherTypeMatchers(DispatcherType.ASYNC, DispatcherType.ERROR).permitAll()
                        .requestMatchers(HttpMethod.OPTIONS, "/**").permitAll()
                        // 仅登录、健康检查与错误转发放行；H2 控制台默认关闭且不再匿名放行（安全审计 F-003）
                        // actuator 仅放行 health/info（生产化 M5）；其余 actuator 端点仍被 anyRequest().denyAll() 拦截
                        .requestMatchers("/api/auth/login", "/health", "/error",
                                "/actuator/health", "/actuator/info").permitAll()
                        .requestMatchers("/api/**").authenticated()
                        // 其余路径一律拒绝，避免未来新增端点被隐式公开
                        .anyRequest().denyAll())
                .exceptionHandling(ex -> ex
                        .authenticationEntryPoint(this::writeUnauthorized)
                        .accessDeniedHandler(this::writeForbidden))
                .headers(headers -> headers.frameOptions(frame -> frame.sameOrigin()))
                .addFilterBefore(jwtAuthenticationFilter, UsernamePasswordAuthenticationFilter.class);
        return http.build();
    }

    private void writeUnauthorized(HttpServletRequest request, HttpServletResponse response,
                                   org.springframework.security.core.AuthenticationException ex) throws IOException {
        writeJson(response, ErrorCode.UNAUTHORIZED);
    }

    private void writeForbidden(HttpServletRequest request, HttpServletResponse response,
                                org.springframework.security.access.AccessDeniedException ex) throws IOException {
        writeJson(response, ErrorCode.FORBIDDEN);
    }

    private void writeJson(HttpServletResponse response, ErrorCode errorCode) throws IOException {
        response.setStatus(errorCode.getHttpStatus());
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding("UTF-8");
        objectMapper.writeValue(response.getWriter(), Result.fail(errorCode));
    }
}

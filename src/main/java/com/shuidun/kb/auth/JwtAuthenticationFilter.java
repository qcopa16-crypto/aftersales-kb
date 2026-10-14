package com.shuidun.kb.auth;

import io.jsonwebtoken.Claims;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.lang.NonNull;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.authentication.WebAuthenticationDetailsSource;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * JWT 认证过滤器（代码设计规范 §3.4）。
 *
 * <p>解析 Authorization: Bearer 令牌，加载当前用户并写入 SecurityContext；令牌缺失/非法时不拦截，交由后续鉴权决定。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class JwtAuthenticationFilter extends OncePerRequestFilter {

    private static final String BEARER_PREFIX = "Bearer ";

    private final JwtTokenProvider jwtTokenProvider;
    private final AuthService authService;

    /**
     * 校验令牌并填充安全上下文。
     *
     * @param request     当前请求
     * @param response    当前响应
     * @param filterChain 过滤链
     * @throws ServletException Servlet 异常
     * @throws IOException      IO 异常
     */
    @Override
    protected void doFilterInternal(@NonNull HttpServletRequest request,
                                    @NonNull HttpServletResponse response,
                                    @NonNull FilterChain filterChain) throws ServletException, IOException {
        String header = request.getHeader("Authorization");
        if (header != null && header.startsWith(BEARER_PREFIX)
                && SecurityContextHolder.getContext().getAuthentication() == null) {
            String token = header.substring(BEARER_PREFIX.length()).trim();
            Claims claims = jwtTokenProvider.parse(token);
            if (claims != null) {
                try {
                    Long userId = Long.valueOf(claims.getSubject());
                    CurrentUser currentUser = authService.loadCurrentUser(userId);
                    if (currentUser != null) {
                        List<SimpleGrantedAuthority> authorities = currentUser.getPermissions().stream()
                                .map(SimpleGrantedAuthority::new)
                                .toList();
                        UsernamePasswordAuthenticationToken authentication =
                                new UsernamePasswordAuthenticationToken(currentUser, null, authorities);
                        authentication.setDetails(new WebAuthenticationDetailsSource().buildDetails(request));
                        SecurityContextHolder.getContext().setAuthentication(authentication);
                    }
                } catch (NumberFormatException ex) {
                    log.warn("invalid jwt subject, skip authentication");
                }
            }
        }
        filterChain.doFilter(request, response);
    }
}

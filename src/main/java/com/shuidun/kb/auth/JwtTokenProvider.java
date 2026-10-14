package com.shuidun.kb.auth;

import com.shuidun.kb.config.JwtProperties;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Date;
import javax.crypto.SecretKey;
import org.springframework.stereotype.Component;

/**
 * JWT 签发与解析（代码设计规范 §3.4）。
 *
 * <p>只承载 userId / username 等身份声明；角色与权限码每次请求从数据库加载，保证授权变更即时生效。
 */
@Component
public class JwtTokenProvider {

    private final JwtProperties jwtProperties;
    private final SecretKey signingKey;

    /**
     * 构造签发器，基于配置密钥初始化 HS256 签名 Key。
     *
     * @param jwtProperties JWT 配置
     */
    public JwtTokenProvider(JwtProperties jwtProperties) {
        this.jwtProperties = jwtProperties;
        byte[] keyBytes = jwtProperties.getSecret().getBytes(StandardCharsets.UTF_8);
        this.signingKey = Keys.hmacShaKeyFor(keyBytes);
    }

    /**
     * 签发访问令牌。
     *
     * @param userId   用户 id
     * @param username 登录名
     * @return JWT 字符串
     */
    public String createToken(Long userId, String username) {
        Instant now = Instant.now();
        Instant expiry = now.plusSeconds(jwtProperties.getExpireSeconds());
        return Jwts.builder()
                .subject(String.valueOf(userId))
                .claim("username", username)
                .issuer(jwtProperties.getIssuer())
                .issuedAt(Date.from(now))
                .expiration(Date.from(expiry))
                .signWith(signingKey)
                .compact();
    }

    /**
     * 解析并校验令牌，返回声明集合。
     *
     * @param token JWT 字符串
     * @return 声明；令牌非法或过期时返回 null
     */
    public Claims parse(String token) {
        try {
            return Jwts.parser()
                    .verifyWith(signingKey)
                    .requireIssuer(jwtProperties.getIssuer())
                    .build()
                    .parseSignedClaims(token)
                    .getPayload();
        } catch (JwtException | IllegalArgumentException ex) {
            return null;
        }
    }

    /**
     * 令牌有效期（秒）。
     *
     * @return 有效期秒数
     */
    public long getExpireSeconds() {
        return jwtProperties.getExpireSeconds();
    }
}

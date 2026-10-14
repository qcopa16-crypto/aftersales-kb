package com.shuidun.kb.auth;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 登录限速配置（{@code app.auth.login-throttle.*}，F-11 / R-14）。
 *
 * <p>按 IP + 规范化账号（小写去空白）双维度计数：任一维度达到失败阈值即锁定并渐进退避；
 * 不做「仅按账号锁定」，避免针对固定账号的拒绝服务式锁号。Redis 不可用时降级为进程内计数
 * （fail-closed：限速不因依赖故障而失效，只是多节点各自计数）。
 *
 * <p><b>R-14 账号维度聚合</b>：在 IP/IP+账号之外，再按「仅账号」累计跨来源风险计数，
 * 达到 {@link #accountMaxFailures} 后施加**封顶的递增延迟**而非硬锁——既让分布式来源的
 * 单账号爆破明显变慢，又不给攻击者"锁死受害者账号"的杠杆。
 */
@Data
@ConfigurationProperties(prefix = "app.auth.login-throttle")
public class LoginThrottleProperties {

    /** 总开关；false = 不限速（不推荐，仅用于紧急回滚）。 */
    private boolean enabled = true;

    /** 窗口内允许的最大连续失败次数（达到即锁定）。 */
    private int maxFailures = 5;

    /** 失败计数窗口（秒）：窗口结束后计数清零。 */
    private long windowSeconds = 600;

    /** 基础锁定时长（秒）：首次锁定时长，之后按失败次数翻倍。 */
    private long lockSeconds = 60;

    /** 锁定时长上限（秒）：渐进退避的天花板，避免永久锁号。 */
    private long maxLockSeconds = 900;

    /**
     * 账号维度（跨 IP 聚合）风险计数阈值（R-14）。
     *
     * <p>刻意高于 IP 阈值：正常用户在换网/多点登录时不应被误伤，只有分散来源的持续爆破
     * 才会累积到该阈值并触发递增延迟。达到阈值只延迟、不锁号。
     */
    private int accountMaxFailures = 20;

    /** 账号维度风险计数窗口（秒）。 */
    private long accountWindowSeconds = 900;

    /** 账号维度递增延迟的基础步长（毫秒）：每超出阈值 1 次增加一个步长。 */
    private long accountBaseDelayMs = 200;

    /** 账号维度递增延迟上限（毫秒）：延迟封顶，避免攻击者制造超长阻塞。 */
    private long accountMaxDelayMs = 3000;
}

package com.shuidun.kb.common;

import java.sql.Timestamp;
import java.time.LocalDateTime;

/**
 * JDBC 结果取值工具。
 *
 * <p>看板与会话历史都直接读 {@code Map<String,Object>} 结果集（为了跨 H2 / MySQL 可移植，
 * 聚合逻辑放在 Java 侧）。这里集中处理两件事：
 *
 * <ul>
 *   <li>数值列可能返回 {@code Integer} / {@code Long} / {@code BigDecimal}，统一成 long；</li>
 *   <li>时间列在 H2 返回 {@link LocalDateTime}、在 MySQL 返回 {@link Timestamp}，统一成 LocalDateTime。</li>
 * </ul>
 */
public final class JdbcValues {

    private JdbcValues() {
    }

    /**
     * 取数值列，非数值返回 0。
     *
     * @param value 列值
     * @return long 值
     */
    public static long longValue(Object value) {
        return value instanceof Number number ? number.longValue() : 0L;
    }

    /**
     * 取文本列，null 返回空串（避免调用方到处判空）。
     *
     * @param value 列值
     * @return 字符串
     */
    public static String stringValue(Object value) {
        return value == null ? "" : String.valueOf(value);
    }

    /**
     * 取时间列，兼容 H2 与 MySQL 的返回类型。
     *
     * @param value 列值
     * @return LocalDateTime；无法识别返回 null
     */
    public static LocalDateTime toLocalDateTime(Object value) {
        if (value instanceof LocalDateTime ldt) {
            return ldt;
        }
        if (value instanceof Timestamp ts) {
            return ts.toLocalDateTime();
        }
        if (value instanceof java.util.Date date) {
            return new Timestamp(date.getTime()).toLocalDateTime();
        }
        return null;
    }
}

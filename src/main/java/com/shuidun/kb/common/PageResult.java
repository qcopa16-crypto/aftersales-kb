package com.shuidun.kb.common;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 统一分页响应体（代码设计规范 §3.3）。字段名固定为 total/page/size/records。
 *
 * @param <T> 记录类型
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
public class PageResult<T> {

    /** 总记录数。 */
    private long total;

    /** 当前页（从 1 起）。 */
    private int page;

    /** 每页条数。 */
    private int size;

    /** 当前页记录。 */
    private List<T> records;

    /**
     * 构造分页结果。
     *
     * @param total   总记录数
     * @param page    当前页
     * @param size    每页条数
     * @param records 当前页记录
     * @return 分页结果
     */
    public static <T> PageResult<T> of(long total, int page, int size, List<T> records) {
        return new PageResult<>(total, page, size, records);
    }
}

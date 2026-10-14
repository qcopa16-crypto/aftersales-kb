package com.shuidun.kb.knowledge;

import java.util.Set;

/**
 * 知识领域枚举白名单（代码设计规范 §5.1）。越界即视为非法。
 */
public final class KnowledgeEnums {

    /** 分类枚举，仅此五值。 */
    public static final Set<String> CATEGORIES = Set.of("product", "policy", "sop", "sales", "finance");

    /** 业务可用性状态，仅此四值。 */
    public static final Set<String> STATUSES = Set.of("active", "disabled", "deleting", "superseded");

    /** 文件类型，仅此四值。 */
    public static final Set<String> FILE_TYPES = Set.of("pdf", "docx", "md", "txt");

    private KnowledgeEnums() {
    }
}

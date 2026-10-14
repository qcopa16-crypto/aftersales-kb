package com.shuidun.kb.ai.dto;

import lombok.Data;

/**
 * parse 响应中的图片条目（aftersales-kb-ai/schemas/ingest.py::ParseImage）。
 *
 * <p>Java 侧仅用于把 {@code relative_path} 透传给 review（VLM 抽检按相对路径定位图片字节），
 * 不落库、不参与向量化。
 */
@Data
public class ParseImage {

    /** AI 侧图片 id。 */
    private String imageId;

    /** 相对路径（如 images/xxx.jpg），VLM 抽检据此定位缓存字节。 */
    private String relativePath;

    /** VLM 图注状态：skipped / ok / failed。 */
    private String status;

    /** VLM 图注文本（可选）。 */
    private String caption;
}

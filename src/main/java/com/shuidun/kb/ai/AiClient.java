package com.shuidun.kb.ai;

import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.ai.dto.EmbedResult;
import com.shuidun.kb.ai.dto.GenerateContext;
import com.shuidun.kb.ai.dto.ParseResult;
import com.shuidun.kb.ai.dto.RetrieveResult;
import com.shuidun.kb.ai.dto.ReviewRequest;
import com.shuidun.kb.ai.dto.ReviewResult;
import com.shuidun.kb.ai.dto.SessionTurn;
import java.util.List;

/**
 * AI 服务统一出口（代码设计规范 §3.8）。
 *
 * <p>所有对 FastAPI 的调用必须收敛到本接口，禁止各处散落裸调 HTTP。
 */
public interface AiClient {

    /**
     * 解析并切片文档。
     *
     * @param content   文件字节
     * @param filename  原始文件名
     * @param fileType  pdf / docx / md / txt
     * @param unitId    unit 业务编码
     * @param chunkSize 切片长度
     * @param useVlm    是否启用 VLM 图注
     * @param ingestId  审计 id
     * @return 解析结果（含 chunks 与 quality）
     */
    ParseResult parse(byte[] content, String filename, String fileType, String unitId,
                      int chunkSize, boolean useVlm, String ingestId);

    /**
     * 入库质检 AI 判定（POST /internal/review）。
     *
     * <p>AI 只返回 verdict / confidence / tiers 等结论，决策与落库由 Java 负责。
     * 档位级失败走 HTTP 200 + {@code tiers}（非 pass 档位由 Java fail-closed 转人工）；
     * 请求级失败抛 {@link AiException}（含 400/503/504）。
     *
     * @param request 判定请求（数据取自内存 parse 结果）
     * @return 判定响应
     */
    ReviewResult review(ReviewRequest request);

    /**
     * 向量化入库（幂等：旧向量原子替换）。
     *
     * @param unitId     unit 业务编码
     * @param chunks     切片
     * @param contentHash 原文哈希（审计）
     * @param chunksHash 切片哈希（一致性校验）
     * @param ingestId   审计 id
     * @return 入库结果
     */
    EmbedResult embed(String unitId, List<AiChunk> chunks, String contentHash, String chunksHash, String ingestId);

    /**
     * 混合检索（fail-closed：必须下发 ACL 过滤集合）。
     *
     * @param question     问题
     * @param topK         返回条数
     * @param aclUnitIds   授权 unit 业务编码集合
     * @param denyUnitIds  显式拒绝集合，可为空
     * @param sessionId    会话 id（检索期指代消解用；无则空串）
     * @param ownerId      不透明分区键（AI 只当 key）
     * @param enableSessionRewrite 是否允许 AI 用会话历史改写 query
     * @return 检索结果
     */
    RetrieveResult retrieve(String question, int topK, List<String> aclUnitIds, List<String> denyUnitIds,
                            String sessionId, String ownerId, boolean enableSessionRewrite);

    /**
     * 流式生成（仅使用调用方传入的 authorized contexts）。
     *
     * @param question     问题
     * @param contexts     已授权上下文；闲聊兜底时传空列表
     * @param sessionId    会话 id
     * @param mode         {@link AiGenerateMode#KB} 依据资料作答；{@link AiGenerateMode#GENERAL} 用通用知识作答
     * @param ownerId      不透明分区键（会话记忆分区用）
     * @param turnId       本轮 id（AI 侧幂等守卫）
     * @param aclUnitIds   当前用户可访问的 unit 业务编码集合（AI 据此做历史 ACL 过滤）
     * @param chosenUnitId 澄清点选：本轮选中的知识单元 id；未点选为 null
     * @param writeHistory 是否允许 AI 在本轮收尾写入会话记忆
     * @param handler      SSE 事件处理器
     */
    void generate(String question, List<GenerateContext> contexts, String sessionId, AiGenerateMode mode,
                  String ownerId, String turnId, List<String> aclUnitIds, Long chosenUnitId,
                  boolean writeHistory, GenerateStreamHandler handler);

    /**
     * 追加一轮会话记录（best-effort：实现必须吞掉所有异常，绝不抛）。
     *
     * <p>用于**不经过 generate 的轮次**（FAQ 命中 / 澄清 ask / 澄清 reference / 拒答）。
     *
     * @param turn 会话轮次
     */
    void appendSessionTurn(SessionTurn turn);

    /**
     * 删除 unit 的全部向量（幂等；失败不得假成功）。
     *
     * @param unitId unit 业务编码
     * @return 删除的向量数
     */
    int deleteVectors(String unitId);

    /**
     * 单图图注。
     *
     * @param content     图片字节
     * @param filename    文件名
     * @param contentType MIME 类型
     * @return 中文图注
     */
    String vlmDescribe(byte[] content, String filename, String contentType);
}

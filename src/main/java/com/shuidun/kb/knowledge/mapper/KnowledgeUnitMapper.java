package com.shuidun.kb.knowledge.mapper;

import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 知识单元 Mapper。
 */
@Mapper
public interface KnowledgeUnitMapper {

    /**
     * 分页查询（默认排除 deleting，除非显式指定 status）。
     *
     * @param keyword         标题/摘要关键字，空传 null
     * @param category        分类，可空
     * @param status          状态，可空
     * @param allowedUnitIds  四维数据权限允许的 unit id（非空列表）；null 表示不限制（知识管理员）
     * @param offset          偏移
     * @param size            条数
     * @return 知识单元列表
     */
    @Select("<script>"
            + "SELECT * FROM knowledge_units WHERE 1=1 "
            + "<if test='keyword != null'>AND (title LIKE CONCAT('%', #{keyword}, '%') "
            + "OR summary LIKE CONCAT('%', #{keyword}, '%')) </if>"
            + "<if test='category != null'>AND category = #{category} </if>"
            + "<if test='status != null'>AND status = #{status} </if>"
            + "<if test='status == null'>AND status != 'deleting' </if>"
            + "<if test='allowedUnitIds != null'>AND id IN "
            + "<foreach collection='allowedUnitIds' item='uid' open='(' separator=',' close=')'>#{uid}</foreach> </if>"
            + "ORDER BY updated_at DESC, id DESC LIMIT #{size} OFFSET #{offset}"
            + "</script>")
    List<KnowledgeUnit> selectPage(@Param("keyword") String keyword,
                                   @Param("category") String category,
                                   @Param("status") String status,
                                   @Param("allowedUnitIds") List<Long> allowedUnitIds,
                                   @Param("offset") int offset,
                                   @Param("size") int size);

    /**
     * 统计总数（与 {@link #selectPage} 过滤条件一致）。
     *
     * @param keyword        关键字
     * @param category       分类
     * @param status         状态
     * @param allowedUnitIds 四维数据权限允许的 unit id；null 表示不限制
     * @return 记录数
     */
    @Select("<script>"
            + "SELECT COUNT(*) FROM knowledge_units WHERE 1=1 "
            + "<if test='keyword != null'>AND (title LIKE CONCAT('%', #{keyword}, '%') "
            + "OR summary LIKE CONCAT('%', #{keyword}, '%')) </if>"
            + "<if test='category != null'>AND category = #{category} </if>"
            + "<if test='status != null'>AND status = #{status} </if>"
            + "<if test='status == null'>AND status != 'deleting' </if>"
            + "<if test='allowedUnitIds != null'>AND id IN "
            + "<foreach collection='allowedUnitIds' item='uid' open='(' separator=',' close=')'>#{uid}</foreach> </if>"
            + "</script>")
    long countPage(@Param("keyword") String keyword,
                   @Param("category") String category,
                   @Param("status") String status,
                   @Param("allowedUnitIds") List<Long> allowedUnitIds);

    /**
     * 按主键查询。
     *
     * @param id 主键
     * @return 知识单元；不存在返回 null
     */
    @Select("SELECT * FROM knowledge_units WHERE id = #{id}")
    KnowledgeUnit selectById(Long id);

    /**
     * R-03 业务编码序列：<b>非锁定</b>存在性检查（热路径用）。
     *
     * <p>用于避免对已存在行执行 {@code INSERT IGNORE}：InnoDB 对已存在行做重复键检查时会加
     * <b>共享锁（S）</b>，随后 {@code SELECT ... FOR UPDATE} 需升级为<b>排他锁（X）</b>，
     * 两个并发事务在「S→X 升级」上互等即形成<b>锁升级死锁</b>。因此热路径只做一致性快照读
     * （REPEATABLE READ 下不加锁），行已存在则直接跳到 {@code FOR UPDATE}；仅行缺失时才 bootstrap。
     *
     * @param seqName 序列名
     * @return 行数（0/1）
     */
    @Select("SELECT COUNT(*) FROM knowledge_unit_code_seq WHERE seq_name = #{seqName}")
    int countUnitCodeSequence(@Param("seqName") String seqName);

    /**
     * R-03 业务编码序列：确保计数行存在（幂等）。
     *
     * <p>仅用于<b>行缺失时的 bootstrap</b>（V10/schema 已保证正常部署行存在，该分支极罕见）。
     * 行已存在时 {@code INSERT IGNORE} 不产生副作用，但会加 S 锁，故<b>不得在热路径调用</b>。
     *
     * @param seqName 序列名
     * @param initial 初始值（须高于既有编码）
     * @return 影响行数（1=新建，0=已存在）
     */
    @Insert("INSERT IGNORE INTO knowledge_unit_code_seq(seq_name, next_value) VALUES(#{seqName}, #{initial})")
    int initUnitCodeSequence(@Param("seqName") String seqName, @Param("initial") long initial);

    /**
     * R-03 业务编码序列：事务内锁定并读取当前下一个可用值。
     *
     * <p>{@code SELECT ... FOR UPDATE} 对计数行加排他锁，直到本事务提交；并发导入因此串行化，
     * 不会再读到同一最大值。行不存在时返回 null，调用方需先执行 {@link #initUnitCodeSequence}。
     *
     * @param seqName 序列名
     * @return 下一个可用值；行不存在返回 null
     */
    @Select("SELECT next_value FROM knowledge_unit_code_seq WHERE seq_name = #{seqName} FOR UPDATE")
    Long lockUnitCodeSequence(@Param("seqName") String seqName);

    /**
     * R-03 业务编码序列：按段推进计数器（预留 count 个连续编码）。
     *
     * @param seqName 序列名
     * @param count   预留数量（&gt;=1）
     * @return 影响行数
     */
    @Update("UPDATE knowledge_unit_code_seq SET next_value = next_value + #{count} WHERE seq_name = #{seqName}")
    int bumpUnitCodeSequence(@Param("seqName") String seqName, @Param("count") int count);

    /**
     * R-03 纵深防御：查询某业务编码是否已存在于墓碑表 {@code knowledge_unit_tombstones}。
     *
     * <p>用于分配侧规避墓碑编码：AI 侧向量以 unit_code 为键，未清扫墓碑会被删除补偿持续重放
     * {@code deleteVectors(unit_code)}；若把该编码分配给新单元，其向量会被迟到重放误删
     * （DB active 但检索静默缺失）。因此分配候选编码命中墓碑时必须换号。
     *
     * @param code 候选业务编码（如 {@code U-1091}）
     * @return 命中数（{@code unit_code} 无唯一约束，按 &gt;0 判定命中）
     */
    @Select("SELECT COUNT(*) FROM knowledge_unit_tombstones WHERE unit_code = #{code}")
    int countTombstoneByCode(@Param("code") String code);

    /**
     * 批量按主键查询。
     *
     * @param ids 主键列表（非空）
     * @return 知识单元列表
     */
    @Select("<script>SELECT * FROM knowledge_units WHERE id IN "
            + "<foreach collection='ids' item='id' open='(' separator=',' close=')'>#{id}</foreach>"
            + "</script>")
    List<KnowledgeUnit> selectByIds(@Param("ids") List<Long> ids);

    /**
     * 批量按业务编码查询（用于未授权召回的兜底判定，避免逐条单查）。
     *
     * <p>调用方须保证 {@code unitCodes} 非空（空集合会生成非法 {@code IN ()}）。
     * {@code unit_code} 有唯一约束，故结果按编码至多一条。
     *
     * @param unitCodes 业务编码列表（非空）
     * @return 知识单元列表
     */
    @Select("<script>SELECT * FROM knowledge_units WHERE unit_code IN "
            + "<foreach collection='unitCodes' item='code' open='(' separator=',' close=')'>#{code}</foreach>"
            + "</script>")
    List<KnowledgeUnit> selectByUnitCodes(@Param("unitCodes") List<String> unitCodes);

    /**
     * 新增知识单元。
     *
     * @param unit 实体
     * @return 影响行数
     */
    @Insert("INSERT INTO knowledge_units(unit_code, title, content, summary, category, source_file_name, "
            + "file_type, file_size, storage_path, use_vlm, status, ingest_status, batch_id, creator_id, created_at, updated_at) "
            + "VALUES(#{unitCode}, #{title}, #{content}, #{summary}, #{category}, #{sourceFileName}, #{fileType}, "
            + "#{fileSize}, #{storagePath}, #{useVlm}, #{status}, #{ingestStatus}, #{batchId}, #{creatorId}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(KnowledgeUnit unit);

    /**
     * 更新标题/正文/摘要/分类/状态与流水线状态（R-02 生命周期版本 CAS）。
     *
     * <p>仅当数据库中该单元的 {@code lifecycle_version} 仍等于调用方读到的版本，且单元仍处于
     * {@code active/disabled}（即未被并发删除抢占为 deleting）时才写入，并在同一语句递增版本。
     * 影响行数为 0 表示读后被并发修改（典型为并发删除 markDeleting），调用方必须视为并发冲突
     * 并要求重读，绝不能覆盖新状态。
     *
     * @param unit 实体（须携带读取时的 lifecycleVersion）
     * @return 影响行数：1 成功，0 并发冲突
     */
    @Update("UPDATE knowledge_units SET title = #{title}, content = #{content}, summary = #{summary}, "
            + "category = #{category}, status = #{status}, ingest_status = #{ingestStatus}, "
            + "lifecycle_version = lifecycle_version + 1, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND lifecycle_version = #{lifecycleVersion} "
            + "AND status IN ('active', 'disabled')")
    int update(KnowledgeUnit unit);

    /**
     * 仅更新分类（R-02：Worker 分类采纳等流水线内部更新，不得整行覆盖业务状态）。
     *
     * <p>不触碰 status/ingest_status/title/content，仅当单元仍为 active/disabled（未被删除抢占）
     * 时更新分类并递增生命周期版本。
     *
     * @param id       主键
     * @param category 新分类
     * @return 影响行数：1 成功，0 单元已进入删除等不可修改状态
     */
    @Update("UPDATE knowledge_units SET category = #{category}, lifecycle_version = lifecycle_version + 1, "
            + "updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND status IN ('active', 'disabled')")
    int updateCategory(@Param("id") Long id, @Param("category") String category);

    /**
     * 仅更新状态。
     *
     * @param id     主键
     * @param status 业务状态
     * @return 影响行数
     */
    @Update("UPDATE knowledge_units SET status = #{status}, lifecycle_version = lifecycle_version + 1, "
            + "updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateStatus(@Param("id") Long id, @Param("status") String status);

    /**
     * 删除前置 CAS（F-03/F-04）：仅当单元仍处于 active/disabled 时才原子进入 deleting。
     *
     * <p>影响行数为 0 表示已有并发删除在进行（幂等保护），调用方不得重复触发删除流程。
     *
     * @param id 主键
     * @return 影响行数：1 抢占成功，0 已在删除中/已终态
     */
    @Update("UPDATE knowledge_units SET status = 'deleting', lifecycle_version = lifecycle_version + 1, "
            + "updated_at = CURRENT_TIMESTAMP WHERE id = #{id} AND status IN ('active', 'disabled')")
    int markDeleting(@Param("id") Long id);

    /**
     * 入库完成发布 CAS（F-24/F-03）：仅当单元仍为 active 且仍由当前作业驱动
     * （{@code ingest_status=embedding}，即入库期间未被禁用/删除/重新入队）时才发布为可检索。
     *
     * <p>影响行数为 0 表示入库期间单元状态被变更，Worker 必须终止发布而不是覆盖之。
     *
     * @param id 主键
     * @return 影响行数：1 发布成功，0 状态已变、拒绝发布
     */
    @Update("UPDATE knowledge_units SET status = 'active', ingest_status = 'active', "
            + "lifecycle_version = lifecycle_version + 1, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND status = 'active' AND ingest_status = 'embedding'")
    int publishIfActive(@Param("id") Long id);

    /**
     * 仅更新流水线状态。
     *
     * @param id           主键
     * @param ingestStatus 流水线状态
     * @return 影响行数
     */
    @Update("UPDATE knowledge_units SET ingest_status = #{ingestStatus}, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateIngestStatus(@Param("id") Long id, @Param("ingestStatus") String ingestStatus);

    /**
     * 回填业务编码（插入后按主键生成 U-xxxx）。
     *
     * @param id       主键
     * @param unitCode 业务编码
     * @return 影响行数
     */
    @Update("UPDATE knowledge_units SET unit_code = #{unitCode}, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateUnitCode(@Param("id") Long id, @Param("unitCode") String unitCode);

    /**
     * 记录本地存储路径。
     *
     * @param id          主键
     * @param storagePath 相对路径
     * @return 影响行数
     */
    @Update("UPDATE knowledge_units SET storage_path = #{storagePath}, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateStoragePath(@Param("id") Long id, @Param("storagePath") String storagePath);

    /**
     * 记录 parse 返回的内容/切片哈希（embed 幂等校验）。
     *
     * @param id          主键
     * @param contentHash 原文哈希
     * @param chunksHash  切片哈希
     * @return 影响行数
     */
    @Update("UPDATE knowledge_units SET content_hash = #{contentHash}, chunks_hash = #{chunksHash}, "
            + "updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateHashes(@Param("id") Long id, @Param("contentHash") String contentHash,
                     @Param("chunksHash") String chunksHash);

    /**
     * R-03 reconcile：查询处于 deleting 但尚无删除 outbox 任务的单元（崩溃窗口修复）。
     *
     * <p>覆盖「markDeleting 已提交、outbox task 未创建」的进程中断窗口：定时扫描据此补建任务。
     *
     * @param limit 条数上限
     * @return 需要补建任务的单元列表
     */
    @Select("SELECT u.* FROM knowledge_units u LEFT JOIN knowledge_delete_tasks t ON t.unit_id = u.id "
            + "WHERE u.status = 'deleting' AND t.id IS NULL ORDER BY u.id LIMIT #{limit}")
    List<KnowledgeUnit> selectDeletingWithoutTask(@Param("limit") int limit);

    /**
     * 物理删除。
     *
     * @param id 主键
     * @return 影响行数
     */
    @Delete("DELETE FROM knowledge_units WHERE id = #{id}")
    int deleteById(Long id);

    /**
     * 统计业务可用的知识单元数。
     *
     * @return 记录数
     */
    @Select("SELECT COUNT(*) FROM knowledge_units WHERE status = 'active'")
    long countActive();

    /**
     * 检索可见的 unit 业务编码（status=active AND ingest_status=active）。
     *
     * @return unit_code 列表
     */
    @Select("SELECT unit_code FROM knowledge_units WHERE status = 'active' AND ingest_status = 'active'")
    List<String> selectRetrievableUnitCodes();

    /**
     * 检索可见的 unit 主键（status=active AND ingest_status=active）。
     *
     * <p>供知识管理员（含系统管理员）在问答链路取"全部数据可见"的候选集合。
     *
     * @return unit id 列表
     */
    @Select("SELECT id FROM knowledge_units WHERE status = 'active' AND ingest_status = 'active'")
    List<Long> selectRetrievableUnitIds();

    /**
     * 批次列表（按批次聚合，按当前用户数据范围过滤）。
     *
     * <p>范围口径（F-01）：{@code allowedUnitIds == null} 表示知识管理员，不受限；
     * 否则仅返回「本人创建」或其单元在授权集合内的批次。{@code allowedUnitIds} 由调用方保证
     * 非空（无授权单元时传只含不存在哨兵值的列表，从而只剩创建人过滤）。
     *
     * @param creatorId      当前用户 id（创建人口径）
     * @param allowedUnitIds 四维数据权限允许的 unit id；null 表示不限制（知识管理员）
     * @param offset         偏移
     * @param size           条数
     * @return 批次摘要（列名保持 snake_case，由 map-underscore-to-camel-case 映射）
     */
    @Select("<script>SELECT batch_id, COUNT(*) AS file_count, "
            + "SUM(CASE WHEN ingest_status = 'active' THEN 1 ELSE 0 END) AS active_count, "
            + "SUM(CASE WHEN ingest_status = 'failed' THEN 1 ELSE 0 END) AS failed_count, "
            + "MIN(created_at) AS created_at, MAX(creator_id) AS creator_id "
            + "FROM knowledge_units WHERE batch_id IS NOT NULL "
            + "<if test='allowedUnitIds != null'>AND (creator_id = #{creatorId} OR id IN "
            + "<foreach collection='allowedUnitIds' item='uid' open='(' separator=',' close=')'>#{uid}</foreach>) </if>"
            + "GROUP BY batch_id ORDER BY MIN(created_at) DESC, batch_id DESC LIMIT #{size} OFFSET #{offset}</script>")
    List<com.shuidun.kb.knowledge.dto.BatchSummaryVO> selectBatchSummaries(
            @Param("creatorId") Long creatorId,
            @Param("allowedUnitIds") List<Long> allowedUnitIds,
            @Param("offset") int offset,
            @Param("size") int size);

    /**
     * 统计批次数（与 {@link #selectBatchSummaries} 同一数据范围口径）。
     *
     * @param creatorId      当前用户 id（创建人口径）
     * @param allowedUnitIds 四维数据权限允许的 unit id；null 表示不限制（知识管理员）
     * @return 记录数
     */
    @Select("<script>SELECT COUNT(DISTINCT batch_id) FROM knowledge_units WHERE batch_id IS NOT NULL "
            + "<if test='allowedUnitIds != null'>AND (creator_id = #{creatorId} OR id IN "
            + "<foreach collection='allowedUnitIds' item='uid' open='(' separator=',' close=')'>#{uid}</foreach>) </if>"
            + "</script>")
    long countBatches(@Param("creatorId") Long creatorId,
                      @Param("allowedUnitIds") List<Long> allowedUnitIds);
}

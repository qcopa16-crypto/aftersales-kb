package com.shuidun.kb.permission.mapper;

import com.shuidun.kb.permission.entity.UnitPermission;
import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * 知识单元数据权限 Mapper（四维 OR 组合查询）。
 */
@Mapper
public interface UnitPermissionMapper {

    /**
     * 在候选 unit 集合内，查询用户可访问的 unit id（OR 组合放行）。
     *
     * @param unitIds      候选 unit id（非空）
     * @param userId       用户 id
     * @param departmentId 用户部门 id，可为 null
     * @param roleIds      用户角色 id（非空，空时由调用方传哨兵值）
     * @return 授权 unit id 列表
     */
    @Select("<script>"
            + "SELECT DISTINCT unit_id FROM unit_permissions "
            + "WHERE unit_id IN "
            + "<foreach collection='unitIds' item='uid' open='(' separator=',' close=')'>#{uid}</foreach> "
            + "AND ( target_type = 'global' "
            + "<if test='departmentId != null'>OR (target_type = 'department' AND target_id = #{departmentId}) </if>"
            + "OR (target_type = 'role' AND target_id IN "
            + "<foreach collection='roleIds' item='rid' open='(' separator=',' close=')'>#{rid}</foreach>) "
            + "OR (target_type = 'user' AND target_id = #{userId}) )"
            + "</script>")
    List<Long> selectAuthorizedUnitIds(@Param("unitIds") List<Long> unitIds,
                                       @Param("userId") Long userId,
                                       @Param("departmentId") Long departmentId,
                                       @Param("roleIds") List<Long> roleIds);

    /**
     * 查询用户全部可访问的 unit id（用于问答链路下发 acl_filter）。
     *
     * @param userId       用户 id
     * @param departmentId 用户部门 id，可为 null
     * @param roleIds      用户角色 id（非空，空时由调用方传哨兵值）
     * @return 授权 unit id 列表
     */
    @Select("<script>"
            + "SELECT DISTINCT unit_id FROM unit_permissions WHERE ( target_type = 'global' "
            + "<if test='departmentId != null'>OR (target_type = 'department' AND target_id = #{departmentId}) </if>"
            + "OR (target_type = 'role' AND target_id IN "
            + "<foreach collection='roleIds' item='rid' open='(' separator=',' close=')'>#{rid}</foreach>) "
            + "OR (target_type = 'user' AND target_id = #{userId}) )"
            + "</script>")
    List<Long> selectAllAuthorizedUnitIds(@Param("userId") Long userId,
                                          @Param("departmentId") Long departmentId,
                                          @Param("roleIds") List<Long> roleIds);

    /**
     * 查询某 unit 的全部权限记录。
     *
     * @param unitId unit id
     * @return 权限记录列表
     */
    @Select("SELECT * FROM unit_permissions WHERE unit_id = #{unitId} ORDER BY target_type, target_id")
    List<UnitPermission> selectByUnitId(Long unitId);

    /**
     * 清空某 unit 的权限记录。
     *
     * @param unitId unit id
     * @return 影响行数
     */
    @Delete("DELETE FROM unit_permissions WHERE unit_id = #{unitId}")
    int deleteByUnitId(Long unitId);

    /**
     * 批量写入 unit 权限记录。
     *
     * @param unitId unit id
     * @param items  权限项（非空）
     * @return 影响行数
     */
    @Insert("<script>INSERT INTO unit_permissions(unit_id, target_type, target_id, created_at) VALUES "
            + "<foreach collection='items' item='it' separator=','>"
            + "(#{unitId}, #{it.targetType}, #{it.targetId}, CURRENT_TIMESTAMP)</foreach>"
            + "</script>")
    int insertBatch(@Param("unitId") Long unitId, @Param("items") List<UnitPermission> items);
}

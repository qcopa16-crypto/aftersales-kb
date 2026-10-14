package com.shuidun.kb.org.mapper;

import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * 用户-角色关联表 Mapper。
 */
@Mapper
public interface UserRoleMapper {

    /**
     * 查询用户已分配的角色 id。
     *
     * @param userId 用户 id
     * @return 角色 id 列表
     */
    @Select("SELECT role_id FROM user_roles WHERE user_id = #{userId}")
    List<Long> selectRoleIdsByUserId(Long userId);

    /**
     * 清空用户角色。
     *
     * @param userId 用户 id
     * @return 影响行数
     */
    @Delete("DELETE FROM user_roles WHERE user_id = #{userId}")
    int deleteByUserId(Long userId);

    /**
     * 批量写入用户角色。
     *
     * @param userId  用户 id
     * @param roleIds 角色 id 列表（非空）
     * @return 影响行数
     */
    @Insert("<script>INSERT INTO user_roles(user_id, role_id) VALUES "
            + "<foreach collection='roleIds' item='rid' separator=','>(#{userId}, #{rid})</foreach>"
            + "</script>")
    int insertBatch(@Param("userId") Long userId, @Param("roleIds") List<Long> roleIds);
}

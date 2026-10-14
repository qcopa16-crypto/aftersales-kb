package com.shuidun.kb.org.mapper;

import com.shuidun.kb.org.entity.Role;
import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 角色与操作权限表 Mapper。
 */
@Mapper
public interface RoleMapper {

    /**
     * 查询用户拥有的角色。
     *
     * @param userId 用户 id
     * @return 角色列表
     */
    @Select("SELECT r.* FROM roles r JOIN user_roles ur ON ur.role_id = r.id "
            + "WHERE ur.user_id = #{userId} ORDER BY r.id")
    List<Role> selectByUserId(Long userId);

    /**
     * 查询用户角色编码列表。
     *
     * @param userId 用户 id
     * @return 角色编码列表
     */
    @Select("SELECT r.role_code FROM roles r JOIN user_roles ur ON ur.role_id = r.id "
            + "WHERE ur.user_id = #{userId} ORDER BY r.id")
    List<String> selectRoleCodesByUserId(Long userId);

    /**
     * 查询用户的操作权限码集合。
     *
     * @param userId 用户 id
     * @return 权限码列表
     */
    @Select("SELECT DISTINCT rp.permission_code FROM role_permissions rp "
            + "JOIN user_roles ur ON ur.role_id = rp.role_id WHERE ur.user_id = #{userId}")
    List<String> selectPermissionCodesByUserId(Long userId);

    /**
     * 查询全部角色。
     *
     * @return 角色列表
     */
    @Select("SELECT * FROM roles ORDER BY id")
    List<Role> selectAll();

    /**
     * 按主键查询角色。
     *
     * @param id 角色 id
     * @return 角色；不存在返回 null
     */
    @Select("SELECT * FROM roles WHERE id = #{id}")
    Role selectById(Long id);

    /**
     * 按编码查询角色（用于按分类映射默认数据权限）。
     *
     * @param roleCode 角色编码
     * @return 角色；不存在返回 null
     */
    @Select("SELECT * FROM roles WHERE role_code = #{roleCode}")
    Role selectByCode(String roleCode);

    /**
     * 新增角色。
     *
     * @param role 角色实体
     * @return 影响行数
     */
    @Insert("INSERT INTO roles(role_name, role_code, description, created_at) "
            + "VALUES(#{roleName}, #{roleCode}, #{description}, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(Role role);

    /**
     * 更新角色。
     *
     * @param role 角色实体
     * @return 影响行数
     */
    @Update("UPDATE roles SET role_name = #{roleName}, description = #{description} WHERE id = #{id}")
    int update(Role role);

    /**
     * 查询角色已分配的操作权限码。
     *
     * @param roleId 角色 id
     * @return 权限码列表
     */
    @Select("SELECT permission_code FROM role_permissions WHERE role_id = #{roleId} ORDER BY permission_code")
    List<String> selectPermissionCodesByRoleId(Long roleId);

    /**
     * 清空角色权限。
     *
     * @param roleId 角色 id
     * @return 影响行数
     */
    @Delete("DELETE FROM role_permissions WHERE role_id = #{roleId}")
    int deleteRolePermissions(Long roleId);

    /**
     * 批量写入角色权限码。
     *
     * @param roleId 角色 id
     * @param codes  权限码列表（非空）
     * @return 影响行数
     */
    @Insert("<script>INSERT INTO role_permissions(role_id, permission_code, permission_type) VALUES "
            + "<foreach collection='codes' item='code' separator=','>(#{roleId}, #{code}, 'api')</foreach>"
            + "</script>")
    int insertRolePermissions(@Param("roleId") Long roleId, @Param("codes") List<String> codes);

    /**
     * 统计角色编码是否已存在。
     *
     * @param roleCode 角色编码
     * @return 记录数
     */
    @Select("SELECT COUNT(*) FROM roles WHERE role_code = #{roleCode}")
    int countByCode(String roleCode);
}

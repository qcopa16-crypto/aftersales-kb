package com.shuidun.kb.org.mapper;

import com.shuidun.kb.org.entity.User;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 用户表 Mapper（单表读写，无业务判断）。
 */
@Mapper
public interface UserMapper {

    /**
     * 按登录名查询用户。
     *
     * @param username 登录名
     * @return 用户；不存在返回 null
     */
    @Select("SELECT * FROM users WHERE username = #{username}")
    User selectByUsername(String username);

    /**
     * 按主键查询用户。
     *
     * @param id 用户 id
     * @return 用户；不存在返回 null
     */
    @Select("SELECT * FROM users WHERE id = #{id}")
    User selectById(Long id);

    /**
     * 按主键批量查询用户（§4.1：部门树批量加载负责人，消除 N+1）。
     *
     * @param ids 用户 id 列表（非空）
     * @return 命中的用户列表
     */
    @Select("<script>SELECT * FROM users WHERE id IN "
            + "<foreach collection='ids' item='id' open='(' separator=',' close=')'>#{id}</foreach>"
            + "</script>")
    List<User> selectByIds(@Param("ids") List<Long> ids);

    /**
     * 分页查询用户。
     *
     * @param keyword 关键字（已由 Service 归一化，空值传 null）
     * @param offset  偏移量
     * @param size    每页条数
     * @return 用户列表
     */
    @Select("<script>"
            + "SELECT * FROM users WHERE 1=1 "
            + "<if test='keyword != null'>AND (username LIKE CONCAT('%', #{keyword}, '%') "
            + "OR display_name LIKE CONCAT('%', #{keyword}, '%')) </if>"
            + "ORDER BY id DESC LIMIT #{size} OFFSET #{offset}"
            + "</script>")
    List<User> selectPage(@Param("keyword") String keyword, @Param("offset") int offset, @Param("size") int size);

    /**
     * 统计用户总数。
     *
     * @param keyword 关键字（已由 Service 归一化，空值传 null）
     * @return 记录数
     */
    @Select("<script>"
            + "SELECT COUNT(*) FROM users WHERE 1=1 "
            + "<if test='keyword != null'>AND (username LIKE CONCAT('%', #{keyword}, '%') "
            + "OR display_name LIKE CONCAT('%', #{keyword}, '%')) </if>"
            + "</script>")
    long countPage(@Param("keyword") String keyword);

    /**
     * 新增用户。
     *
     * @param user 用户实体
     * @return 影响行数
     */
    @Insert("INSERT INTO users(username, password_hash, display_name, department_id, status, created_at, updated_at) "
            + "VALUES(#{username}, #{passwordHash}, #{displayName}, #{departmentId}, #{status}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(User user);

    /**
     * 更新用户基本信息。
     *
     * @param user 用户实体
     * @return 影响行数
     */
    @Update("UPDATE users SET display_name = #{displayName}, department_id = #{departmentId}, "
            + "status = #{status}, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int update(User user);

    /**
     * 更新密码散列。
     *
     * @param id           用户 id
     * @param passwordHash BCrypt 散列
     * @return 影响行数
     */
    @Update("UPDATE users SET password_hash = #{passwordHash}, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updatePassword(@Param("id") Long id, @Param("passwordHash") String passwordHash);

    /**
     * 统计同名用户数。
     *
     * @param username 登录名
     * @return 记录数
     */
    @Select("SELECT COUNT(*) FROM users WHERE username = #{username}")
    int countByUsername(String username);
}

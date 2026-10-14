package com.shuidun.kb.org.mapper;

import com.shuidun.kb.org.entity.Department;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 部门表 Mapper。
 */
@Mapper
public interface DepartmentMapper {

    /**
     * 查询全部部门（按排序值升序）。
     *
     * @return 部门列表
     */
    @Select("SELECT * FROM departments ORDER BY sort_order, id")
    List<Department> selectAll();

    /**
     * 按主键查询部门。
     *
     * @param id 部门 id
     * @return 部门；不存在返回 null
     */
    @Select("SELECT * FROM departments WHERE id = #{id}")
    Department selectById(Long id);

    /**
     * 按名称查询部门（用于按分类映射默认数据权限）。
     *
     * @param name 部门名称
     * @return 部门；不存在返回 null
     */
    @Select("SELECT * FROM departments WHERE name = #{name}")
    Department selectByName(String name);

    /**
     * 新增部门。
     *
     * @param department 部门实体
     * @return 影响行数
     */
    @Insert("INSERT INTO departments(parent_id, name, leader_id, sort_order, created_at) "
            + "VALUES(#{parentId}, #{name}, #{leaderId}, #{sortOrder}, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(Department department);

    /**
     * 更新部门。
     *
     * @param department 部门实体
     * @return 影响行数
     */
    @Update("UPDATE departments SET parent_id = #{parentId}, name = #{name}, "
            + "leader_id = #{leaderId}, sort_order = #{sortOrder} WHERE id = #{id}")
    int update(Department department);
}

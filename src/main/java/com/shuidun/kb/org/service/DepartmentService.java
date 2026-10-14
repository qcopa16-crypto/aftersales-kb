package com.shuidun.kb.org.service;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.org.dto.DepartmentRequest;
import com.shuidun.kb.org.dto.DepartmentVO;
import com.shuidun.kb.org.entity.Department;
import com.shuidun.kb.org.entity.User;
import com.shuidun.kb.org.mapper.DepartmentMapper;
import com.shuidun.kb.org.mapper.UserMapper;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 部门管理服务（树形结构，docs/services/features.md §3.2）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class DepartmentService {

    private final DepartmentMapper departmentMapper;
    private final UserMapper userMapper;

    /** 部门树最大上溯深度（F-10：防御既有脏数据中的环）。 */
    private static final int MAX_DEPTH = 64;

    /**
     * 查询部门树。
     *
     * @return 根节点列表
     */
    public List<DepartmentVO> tree() {
        List<Department> all = departmentMapper.selectAll();
        // §4.1：负责人一次性批量加载，消除"每个部门 toVO 再查一次 user"的 N+1
        Map<Long, String> leaderNames = loadLeaderNames(all);
        Map<Long, DepartmentVO> nodes = new LinkedHashMap<>();
        for (Department department : all) {
            nodes.put(department.getId(), toVO(department, leaderNames));
        }
        List<DepartmentVO> roots = new ArrayList<>();
        for (Department department : all) {
            DepartmentVO node = nodes.get(department.getId());
            Long parentId = department.getParentId();
            if (parentId == null || parentId == 0L || !nodes.containsKey(parentId)) {
                roots.add(node);
            } else {
                nodes.get(parentId).getChildren().add(node);
            }
        }
        return roots;
    }

    /**
     * 批量加载部门负责人的显示名（§4.1）。
     *
     * @param departments 部门列表
     * @return leaderId → 显示名
     */
    private Map<Long, String> loadLeaderNames(List<Department> departments) {
        List<Long> leaderIds = departments.stream()
                .map(Department::getLeaderId)
                .filter(java.util.Objects::nonNull)
                .distinct()
                .toList();
        if (leaderIds.isEmpty()) {
            return Map.of();
        }
        Map<Long, String> names = new java.util.HashMap<>();
        for (User leader : userMapper.selectByIds(leaderIds)) {
            names.put(leader.getId(),
                    leader.getDisplayName() == null ? leader.getUsername() : leader.getDisplayName());
        }
        return names;
    }

    /**
     * 新增部门。
     *
     * @param request 部门请求
     * @return 新部门 id
     */
    @Transactional
    public Long create(DepartmentRequest request) {
        Long parentId = request.getParentId() == null ? 0L : request.getParentId();
        if (parentId != null && parentId != 0L && departmentMapper.selectById(parentId) == null) {
            // F-10 配套：新建时父节点也必须存在，避免产生游离/悬空分支
            throw new BusinessException(ErrorCode.PARAM_INVALID, "父部门不存在");
        }
        Department department = new Department();
        department.setParentId(parentId);
        department.setName(request.getName());
        department.setLeaderId(request.getLeaderId());
        department.setSortOrder(request.getSortOrder() == null ? 0 : request.getSortOrder());
        departmentMapper.insert(department);
        log.info("create department departmentId={}", department.getId());
        return department.getId();
    }

    /**
     * 判断 {@code candidateId} 是否为 {@code ancestorId} 的后代（F-10）。
     *
     * <p>沿父链上溯并带深度保护：即使既有数据中已存在环，也不会无限循环。
     *
     * @param candidateId 待判定的节点
     * @param ancestorId  祖先节点
     * @return true 表示 candidate 位于 ancestor 的子树中
     */
    private boolean isDescendantOf(Long candidateId, Long ancestorId) {
        Long cursor = candidateId;
        int guard = 0;
        while (cursor != null && cursor != 0L && guard++ < MAX_DEPTH) {
            if (cursor.equals(ancestorId)) {
                return true;
            }
            Department current = departmentMapper.selectById(cursor);
            if (current == null) {
                return false;
            }
            cursor = current.getParentId();
        }
        return false;
    }

    /**
     * 更新部门。
     *
     * @param id      部门 id
     * @param request 部门请求
     */
    @Transactional
    public void update(Long id, DepartmentRequest request) {
        Department existing = departmentMapper.selectById(id);
        if (existing == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "部门不存在");
        }
        Long targetParentId = request.getParentId() == null ? existing.getParentId() : request.getParentId();
        if (targetParentId != null && targetParentId.equals(id)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "父部门不能是自身");
        }
        if (targetParentId != null && targetParentId != 0L) {
            // F-10：父节点必须存在，且不能是当前节点的后代
            // （否则 A→B→C 后再设 A.parent=C 会形成无根闭环，tree() 将丢失这些部门）
            if (departmentMapper.selectById(targetParentId) == null) {
                throw new BusinessException(ErrorCode.PARAM_INVALID, "父部门不存在");
            }
            if (isDescendantOf(targetParentId, id)) {
                throw new BusinessException(ErrorCode.PARAM_INVALID, "父部门不能是当前部门的后代");
            }
        }
        existing.setParentId(targetParentId);
        existing.setName(request.getName());
        existing.setLeaderId(request.getLeaderId());
        existing.setSortOrder(request.getSortOrder() == null ? existing.getSortOrder() : request.getSortOrder());
        departmentMapper.update(existing);
        log.info("update department departmentId={}", id);
    }

    private DepartmentVO toVO(Department department, Map<Long, String> leaderNames) {        DepartmentVO vo = new DepartmentVO();
        vo.setId(department.getId());
        vo.setParentId(department.getParentId());
        vo.setName(department.getName());
        vo.setLeaderId(department.getLeaderId());
        vo.setSortOrder(department.getSortOrder());
        if (department.getLeaderId() != null) {
            vo.setLeaderName(leaderNames.get(department.getLeaderId()));
        }
        return vo;
    }
}

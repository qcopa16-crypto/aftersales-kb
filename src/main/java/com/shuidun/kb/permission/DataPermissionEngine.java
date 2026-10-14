package com.shuidun.kb.permission;

import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.org.mapper.UserRoleMapper;
import com.shuidun.kb.permission.mapper.UnitPermissionMapper;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;

/**
 * 四维数据权限引擎（代码设计规范 §3.5）。
 *
 * <p>唯一的数据权限判定入口：OR 组合放行（global / department / role / user 任一命中即授权）。
 * 问答链路只允许把 {@link PermissionDecision#authorized()} 的 contexts 送入 generate。
 */
@Component
@RequiredArgsConstructor
public class DataPermissionEngine {

    /** 用户无角色时用于 IN 查询的哨兵值，避免产生非法空 IN 列表。 */
    private static final List<Long> NO_ROLE_SENTINEL = List.of(-1L);

    private final UnitPermissionMapper unitPermissionMapper;
    private final UserRoleMapper userRoleMapper;

    /**
     * 在候选 unit 集合内做四维权限过滤。
     *
     * @param user            当前用户
     * @param candidateUnitIds 候选 unit id（可能为 null/空）
     * @return 授权与未授权集合
     */
    public PermissionDecision filter(CurrentUser user, List<Long> candidateUnitIds) {
        if (candidateUnitIds == null || candidateUnitIds.isEmpty()) {
            return new PermissionDecision(List.of(), List.of());
        }
        List<Long> distinctCandidates = candidateUnitIds.stream().distinct().toList();
        List<Long> authorized = unitPermissionMapper.selectAuthorizedUnitIds(
                distinctCandidates, user.getId(), user.getDepartmentId(), resolveRoleIds(user.getId()));
        Set<Long> authorizedSet = new HashSet<>(authorized);
        List<Long> unauthorized = new ArrayList<>();
        for (Long unitId : distinctCandidates) {
            if (!authorizedSet.contains(unitId)) {
                unauthorized.add(unitId);
            }
        }
        return new PermissionDecision(authorized, unauthorized);
    }

    /**
     * 查询用户全部可访问的 unit id（问答链路用于下发 acl_filter，fail-closed）。
     *
     * @param user 当前用户
     * @return 授权 unit id 列表
     */
    public List<Long> accessibleUnitIds(CurrentUser user) {
        return unitPermissionMapper.selectAllAuthorizedUnitIds(
                user.getId(), user.getDepartmentId(), resolveRoleIds(user.getId()));
    }

    /**
     * 判断用户是否可访问指定 unit。
     *
     * @param user   当前用户
     * @param unitId unit id
     * @return 可访问返回 true
     */
    public boolean canAccess(CurrentUser user, Long unitId) {
        if (unitId == null) {
            return false;
        }
        return !filter(user, List.of(unitId)).authorized().isEmpty();
    }

    private List<Long> resolveRoleIds(Long userId) {
        List<Long> roleIds = userRoleMapper.selectRoleIdsByUserId(userId);
        return (roleIds == null || roleIds.isEmpty()) ? NO_ROLE_SENTINEL : roleIds;
    }
}

package com.shuidun.kb.org.service;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.org.dto.RolePermissionRequest;
import com.shuidun.kb.org.dto.RoleRequest;
import com.shuidun.kb.org.dto.RoleVO;
import com.shuidun.kb.org.entity.Role;
import com.shuidun.kb.org.mapper.RoleMapper;
import java.util.ArrayList;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 角色与操作权限管理服务（docs/services/features.md §3.2）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class RoleService {

    private static final String MODE_ADD = "add";

    private final RoleMapper roleMapper;

    /**
     * 查询全部角色（含已分配权限码）。
     *
     * @return 角色视图列表
     */
    public List<RoleVO> list() {
        List<Role> roles = roleMapper.selectAll();
        List<RoleVO> result = new ArrayList<>(roles.size());
        for (Role role : roles) {
            result.add(toVO(role));
        }
        return result;
    }

    /**
     * 新增角色。
     *
     * @param request 角色请求
     * @return 新角色 id
     */
    @Transactional
    public Long create(RoleRequest request) {
        if (request.getRoleCode() == null || request.getRoleCode().isBlank()) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "角色编码不能为空");
        }
        if (roleMapper.countByCode(request.getRoleCode()) > 0) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "角色编码已存在");
        }
        Role role = new Role();
        role.setRoleName(request.getRoleName());
        role.setRoleCode(request.getRoleCode());
        role.setDescription(request.getDescription());
        roleMapper.insert(role);
        log.info("create role roleId={} code={}", role.getId(), role.getRoleCode());
        return role.getId();
    }

    /**
     * 更新角色（角色编码不可改）。
     *
     * @param id      角色 id
     * @param request 角色请求
     */
    @Transactional
    public void update(Long id, RoleRequest request) {
        Role existing = roleMapper.selectById(id);
        if (existing == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "角色不存在");
        }
        existing.setRoleName(request.getRoleName());
        existing.setDescription(request.getDescription());
        roleMapper.update(existing);
        log.info("update role roleId={}", id);
    }

    /**
     * 查询角色的操作权限码。
     *
     * @param roleId 角色 id
     * @return 权限码列表
     */
    public List<String> permissions(Long roleId) {
        if (roleMapper.selectById(roleId) == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "角色不存在");
        }
        return roleMapper.selectPermissionCodesByRoleId(roleId);
    }

    /**
     * 分配角色操作权限（replace 覆盖 / add 追加）。
     *
     * @param roleId  角色 id
     * @param request 分配请求
     */
    @Transactional
    public void assignPermissions(Long roleId, RolePermissionRequest request) {
        if (roleMapper.selectById(roleId) == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "角色不存在");
        }
        List<String> codes = request.getPermissions() == null ? List.of() : request.getPermissions();
        if (MODE_ADD.equalsIgnoreCase(request.getMode())) {
            if (codes.isEmpty()) {
                return;
            }
            // 追加模式：先剔除已存在的，避免唯一键冲突
            List<String> existing = roleMapper.selectPermissionCodesByRoleId(roleId);
            List<String> toInsert = new ArrayList<>();
            for (String code : codes) {
                if (!existing.contains(code)) {
                    toInsert.add(code);
                }
            }
            if (!toInsert.isEmpty()) {
                roleMapper.insertRolePermissions(roleId, toInsert);
            }
        } else {
            roleMapper.deleteRolePermissions(roleId);
            if (!codes.isEmpty()) {
                roleMapper.insertRolePermissions(roleId, codes);
            }
        }
        log.info("assign role permissions roleId={} mode={} count={}", roleId, request.getMode(), codes.size());
    }

    private RoleVO toVO(Role role) {
        RoleVO vo = new RoleVO();
        vo.setId(role.getId());
        vo.setRoleName(role.getRoleName());
        vo.setRoleCode(role.getRoleCode());
        vo.setDescription(role.getDescription());
        vo.setPermissions(roleMapper.selectPermissionCodesByRoleId(role.getId()));
        return vo;
    }
}

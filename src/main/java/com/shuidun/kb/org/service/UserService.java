package com.shuidun.kb.org.service;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.org.dto.UserCreateRequest;
import com.shuidun.kb.org.dto.UserUpdateRequest;
import com.shuidun.kb.org.dto.UserVO;
import com.shuidun.kb.org.entity.Department;
import com.shuidun.kb.org.entity.Role;
import com.shuidun.kb.org.entity.User;
import com.shuidun.kb.org.mapper.DepartmentMapper;
import com.shuidun.kb.org.mapper.RoleMapper;
import com.shuidun.kb.org.mapper.UserMapper;
import com.shuidun.kb.org.mapper.UserRoleMapper;
import java.util.ArrayList;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 用户管理服务（docs/services/features.md §3.2）。
 *
 * <p>密码只以 BCrypt 散列存储；任何对外视图都不返回密码字段。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class UserService {

    private static final int MAX_PAGE_SIZE = 100;

    /** 新密码最小长度（F-12：不再服务端生成明文回显，改为管理员显式设置并校验强度下限）。 */
    private static final int MIN_PASSWORD_LENGTH = 8;

    private final UserMapper userMapper;
    private final UserRoleMapper userRoleMapper;
    private final RoleMapper roleMapper;
    private final DepartmentMapper departmentMapper;
    private final PasswordEncoder passwordEncoder;

    /**
     * 分页查询用户。
     *
     * @param page    页码（从 1 起）
     * @param size    每页条数（1–100）
     * @param keyword 用户名/显示名关键字，可为空
     * @return 分页结果
     */
    public PageResult<UserVO> list(int page, int size, String keyword) {
        int normalizedPage = Math.max(page, 1);
        int normalizedSize = Math.min(Math.max(size, 1), MAX_PAGE_SIZE);
        String normalizedKeyword = (keyword == null || keyword.isBlank()) ? null : keyword.trim();
        long offset = (long) (normalizedPage - 1) * normalizedSize;
        if (offset > Integer.MAX_VALUE) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "page 超出范围");
        }
        long total = userMapper.countPage(normalizedKeyword);
        List<User> users = userMapper.selectPage(normalizedKeyword,
                (int) offset, normalizedSize);
        List<UserVO> records = new ArrayList<>(users.size());
        for (User user : users) {
            records.add(toVO(user));
        }
        return PageResult.of(total, normalizedPage, normalizedSize, records);
    }

    /**
     * 新增用户并分配角色。
     *
     * @param request 新增请求
     * @return 新用户 id
     */
    @Transactional
    public Long create(UserCreateRequest request) {
        if (userMapper.countByUsername(request.getUsername()) > 0) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "用户名已存在");
        }
        User user = new User();
        user.setUsername(request.getUsername());
        user.setPasswordHash(passwordEncoder.encode(request.getPassword()));
        user.setDisplayName(request.getDisplayName());
        user.setDepartmentId(request.getDepartmentId());
        user.setStatus(1);
        userMapper.insert(user);
        replaceRoles(user.getId(), request.getRoleIds());
        log.info("create user userId={} username={}", user.getId(), user.getUsername());
        return user.getId();
    }

    /**
     * 更新用户基本信息与角色。
     *
     * @param id      用户 id
     * @param request 更新请求
     */
    @Transactional
    public void update(Long id, UserUpdateRequest request) {
        User existing = userMapper.selectById(id);
        if (existing == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "用户不存在");
        }
        existing.setDisplayName(request.getDisplayName());
        existing.setDepartmentId(request.getDepartmentId());
        if (request.getStatus() != null) {
            existing.setStatus(request.getStatus());
        }
        userMapper.update(existing);
        if (request.getRoleIds() != null) {
            replaceRoles(id, request.getRoleIds());
        }
        log.info("update user userId={}", id);
    }

    /**
     * 重置密码；未指定新密码时生成随机密码。
     *
     * @param id          用户 id
     * @param rawPassword 新密码，可为空
     * @return 实际生效的密码
     */
    @Transactional
    public void resetPassword(Long id, String rawPassword) {
        User existing = userMapper.selectById(id);
        if (existing == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "用户不存在");
        }
        // F-12：不再由服务端生成明文密码并回显——密码不得出现在任何响应/日志中。
        // 管理员必须显式设置新密码（经带外渠道告知用户），并由用户首次登录后自行修改。
        if (rawPassword == null || rawPassword.isBlank()) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "请显式提供新密码（服务端不再生成并回显明文）");
        }
        if (rawPassword.length() < MIN_PASSWORD_LENGTH) {
            throw new BusinessException(ErrorCode.PARAM_INVALID,
                    "新密码长度不能少于 " + MIN_PASSWORD_LENGTH + " 位");
        }
        userMapper.updatePassword(id, passwordEncoder.encode(rawPassword));
        log.info("reset password userId={}", id);
    }

    private void replaceRoles(Long userId, List<Long> roleIds) {
        userRoleMapper.deleteByUserId(userId);
        if (roleIds != null && !roleIds.isEmpty()) {
            userRoleMapper.insertBatch(userId, roleIds);
        }
    }

    private UserVO toVO(User user) {
        UserVO vo = new UserVO();
        vo.setId(user.getId());
        vo.setUsername(user.getUsername());
        vo.setDisplayName(user.getDisplayName());
        vo.setDepartmentId(user.getDepartmentId());
        vo.setStatus(user.getStatus());
        if (user.getDepartmentId() != null) {
            Department department = departmentMapper.selectById(user.getDepartmentId());
            if (department != null) {
                vo.setDepartmentName(department.getName());
            }
        }
        vo.setRoleIds(userRoleMapper.selectRoleIdsByUserId(user.getId()));
        List<Role> roles = roleMapper.selectByUserId(user.getId());
        vo.setRoles(roles.stream().map(Role::getRoleCode).toList());
        vo.setRoleNames(roles.stream().map(Role::getRoleName).toList());
        return vo;
    }
}

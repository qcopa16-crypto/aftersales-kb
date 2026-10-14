package com.shuidun.kb.auth;

import com.shuidun.kb.auth.dto.LoginRequest;
import com.shuidun.kb.auth.dto.LoginResponse;
import com.shuidun.kb.auth.dto.UserInfoVO;
import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.org.entity.Department;
import com.shuidun.kb.org.entity.Role;
import com.shuidun.kb.org.entity.User;
import com.shuidun.kb.org.mapper.DepartmentMapper;
import com.shuidun.kb.org.mapper.RoleMapper;
import com.shuidun.kb.org.mapper.UserMapper;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

/**
 * 认证服务（代码设计规范 §3.4）。负责登录校验、令牌签发与当前用户装载。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserMapper userMapper;
    private final RoleMapper roleMapper;
    private final DepartmentMapper departmentMapper;
    private final PasswordEncoder passwordEncoder;
    private final JwtTokenProvider jwtTokenProvider;
    private final LoginThrottleService loginThrottleService;

    /**
     * 用户名密码登录，校验 BCrypt 并签发 JWT。
     *
     * <p>限速（F-11）：先于密码校验按 IP + 规范化账号检查锁定状态（顺带缓解 BCrypt 计算型 DoS）；
     * 失败计数在鉴权失败后落库，成功登录清零。错误文案对「用户不存在」与「密码错误」完全一致。
     *
     * @param request  登录请求
     * @param clientIp 客户端 IP（由控制器从请求提取）
     * @return 登录响应（令牌 + 用户信息）
     */
    public LoginResponse login(LoginRequest request, String clientIp) {
        String username = request.getUsername();
        loginThrottleService.checkAllowed(clientIp, username);
        User user = userMapper.selectByUsername(username);
        // 统一以同一错误码响应，避免暴露"用户名是否存在"
        if (user == null || user.getStatus() == null || user.getStatus() != 1
                || user.getPasswordHash() == null
                || !passwordEncoder.matches(request.getPassword(), user.getPasswordHash())) {
            loginThrottleService.recordFailure(clientIp, username);
            log.warn("login failed username={}", username);
            throw new BusinessException(ErrorCode.UNAUTHORIZED, "用户名或密码错误");
        }
        loginThrottleService.recordSuccess(clientIp, username);
        String token = jwtTokenProvider.createToken(user.getId(), user.getUsername());
        log.info("login success userId={} username={}", user.getId(), user.getUsername());
        return new LoginResponse(token, jwtTokenProvider.getExpireSeconds(), buildUserInfo(toCurrentUser(user)));
    }

    /**
     * 按用户 id 装载当前用户（含角色与权限码），供过滤器与 /me 使用。
     *
     * @param userId 用户 id
     * @return 当前用户；用户不存在或已停用返回 null
     */
    public CurrentUser loadCurrentUser(Long userId) {
        User user = userMapper.selectById(userId);
        if (user == null || user.getStatus() == null || user.getStatus() != 1) {
            return null;
        }
        return toCurrentUser(user);
    }

    /**
     * 组装对前端的用户信息视图（不含敏感字段）。
     *
     * @param currentUser 当前用户
     * @return 用户信息视图
     */
    public UserInfoVO buildUserInfo(CurrentUser currentUser) {
        UserInfoVO vo = new UserInfoVO();
        vo.setId(currentUser.getId());
        vo.setUsername(currentUser.getUsername());
        vo.setDisplayName(currentUser.getDisplayName());
        vo.setDepartmentId(currentUser.getDepartmentId());
        if (currentUser.getDepartmentId() != null) {
            Department department = departmentMapper.selectById(currentUser.getDepartmentId());
            if (department != null) {
                vo.setDepartmentName(department.getName());
            }
        }
        vo.setRoles(currentUser.getRoles());
        vo.setRoleNames(roleMapper.selectByUserId(currentUser.getId()).stream()
                .map(Role::getRoleName)
                .toList());
        vo.setPermissions(new ArrayList<>(currentUser.getPermissions()));
        return vo;
    }

    private CurrentUser toCurrentUser(User user) {
        List<String> roles = roleMapper.selectRoleCodesByUserId(user.getId());
        Set<String> permissions = new LinkedHashSet<>(roleMapper.selectPermissionCodesByUserId(user.getId()));
        return new CurrentUser(user.getId(), user.getUsername(), user.getDisplayName(),
                user.getDepartmentId(), roles, permissions);
    }
}

package com.shuidun.kb.permission;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.org.entity.Department;
import com.shuidun.kb.org.entity.Role;
import com.shuidun.kb.org.entity.User;
import com.shuidun.kb.org.mapper.DepartmentMapper;
import com.shuidun.kb.org.mapper.RoleMapper;
import com.shuidun.kb.org.mapper.UserMapper;
import com.shuidun.kb.permission.dto.PermissionItem;
import com.shuidun.kb.permission.dto.PermissionSummaryVO;
import com.shuidun.kb.permission.dto.UnitPermissionRequest;
import com.shuidun.kb.permission.entity.UnitPermission;
import com.shuidun.kb.permission.mapper.UnitPermissionMapper;
import java.util.ArrayList;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 知识单元数据权限配置服务（代码设计规范 §3.5 / DESIGN.md §6.4）。
 *
 * <p>权限写入时机由调用方保证：embed 成功后、active 前写入。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class UnitPermissionService {

    private static final String MODE_ADD = "add";
    private static final String DEPT_AFTER_SALES = "售后部";

    private final UnitPermissionMapper unitPermissionMapper;
    private final DepartmentMapper departmentMapper;
    private final RoleMapper roleMapper;
    private final UserMapper userMapper;

    /**
     * 查询 unit 的权限记录。
     *
     * @param unitId unit id
     * @return 权限记录列表
     */
    public List<UnitPermission> listByUnit(Long unitId) {
        return unitPermissionMapper.selectByUnitId(unitId);
    }

    /**
     * 查询 unit 的权限摘要（含可读标签）。
     *
     * @param unitId unit id
     * @return 摘要列表
     */
    public List<PermissionSummaryVO> summaries(Long unitId) {
        List<UnitPermission> permissions = unitPermissionMapper.selectByUnitId(unitId);
        List<PermissionSummaryVO> result = new ArrayList<>(permissions.size());
        for (UnitPermission permission : permissions) {
            result.add(new PermissionSummaryVO(permission.getTargetType(), permission.getTargetId(),
                    resolveLabel(permission.getTargetType(), permission.getTargetId())));
        }
        return result;
    }

    /**
     * 按请求配置权限（replace 覆盖 / add 追加）。
     *
     * @param unitId  unit id
     * @param request 配置请求
     */
    @Transactional
    public void configure(Long unitId, UnitPermissionRequest request) {
        List<PermissionItem> items = normalize(request.getItems());
        if (MODE_ADD.equalsIgnoreCase(request.getMode())) {
            add(unitId, items);
        } else {
            replace(unitId, items);
        }
        log.info("configure unit permissions unitId={} mode={} count={}", unitId, request.getMode(), items.size());
    }

    /**
     * 覆盖式写入权限（先删后插）。
     *
     * @param unitId unit id
     * @param items  权限项
     */
    @Transactional
    public void replace(Long unitId, List<PermissionItem> items) {
        unitPermissionMapper.deleteByUnitId(unitId);
        insertIfNotEmpty(unitId, items);
    }

    /**
     * 按分类应用默认权限（DESIGN.md §6.4）。
     *
     * <p>product→global；policy/sop→售后部；sales→role=sales；finance→role=finance。
     * 目标部门/角色不存在时跳过该项（fail-closed，交由管理员在质检页调整）。
     *
     * @param unitId   unit id
     * @param category 分类
     */
    @Transactional
    public void applyDefaultByCategory(Long unitId, String category) {
        List<PermissionItem> items = defaultItems(category);
        replace(unitId, items);
        log.info("apply default unit permissions unitId={} category={} count={}", unitId, category, items.size());
    }

    /**
     * 计算分类对应的默认权限项。
     *
     * @param category 分类
     * @return 权限项列表
     */
    public List<PermissionItem> defaultItems(String category) {
        List<PermissionItem> items = new ArrayList<>();
        if (category == null) {
            return items;
        }
        switch (category) {
            case "product" -> items.add(globalItem());
            case "policy", "sop" -> {
                Department department = departmentMapper.selectByName(DEPT_AFTER_SALES);
                if (department != null) {
                    items.add(departmentItem(department.getId()));
                } else {
                    log.warn("default permission target department not found name={}", DEPT_AFTER_SALES);
                }
            }
            case "sales" -> addRoleItem(items, "sales");
            case "finance" -> addRoleItem(items, "finance");
            default -> log.warn("unknown category for default permission category={}", category);
        }
        return items;
    }

    private void addRoleItem(List<PermissionItem> items, String roleCode) {
        Role role = roleMapper.selectByCode(roleCode);
        if (role != null) {
            PermissionItem item = new PermissionItem();
            item.setTargetType(PermissionTargetType.ROLE);
            item.setTargetId(role.getId());
            items.add(item);
        } else {
            log.warn("default permission target role not found roleCode={}", roleCode);
        }
    }

    private PermissionItem globalItem() {
        PermissionItem item = new PermissionItem();
        item.setTargetType(PermissionTargetType.GLOBAL);
        item.setTargetId(0L);
        return item;
    }

    private PermissionItem departmentItem(Long departmentId) {
        PermissionItem item = new PermissionItem();
        item.setTargetType(PermissionTargetType.DEPARTMENT);
        item.setTargetId(departmentId);
        return item;
    }

    private void add(Long unitId, List<PermissionItem> items) {
        List<UnitPermission> existing = unitPermissionMapper.selectByUnitId(unitId);
        List<PermissionItem> toInsert = new ArrayList<>();
        for (PermissionItem item : items) {
            boolean duplicated = existing.stream().anyMatch(e -> e.getTargetType().equals(item.getTargetType())
                    && e.getTargetId().equals(item.getTargetId()));
            if (!duplicated) {
                toInsert.add(item);
            }
        }
        insertIfNotEmpty(unitId, toInsert);
    }

    private void insertIfNotEmpty(Long unitId, List<PermissionItem> items) {
        if (items == null || items.isEmpty()) {
            return;
        }
        List<UnitPermission> entities = new ArrayList<>(items.size());
        for (PermissionItem item : items) {
            UnitPermission entity = new UnitPermission();
            entity.setUnitId(unitId);
            entity.setTargetType(item.getTargetType());
            entity.setTargetId(item.getTargetId());
            entities.add(entity);
        }
        unitPermissionMapper.insertBatch(unitId, entities);
    }

    private List<PermissionItem> normalize(List<PermissionItem> items) {
        if (items == null) {
            return List.of();
        }
        List<PermissionItem> normalized = new ArrayList<>(items.size());
        for (PermissionItem item : items) {
            if (!PermissionTargetType.isValid(item.getTargetType())) {
                throw new BusinessException(ErrorCode.PARAM_INVALID, "非法授权维度: " + item.getTargetType());
            }
            if (PermissionTargetType.GLOBAL.equals(item.getTargetType())) {
                // global 维度 targetId 固定为 0（代码设计规范 §3.5）
                item.setTargetId(0L);
            } else if (item.getTargetId() == null || item.getTargetId() <= 0) {
                throw new BusinessException(ErrorCode.PARAM_INVALID, "非 global 维度必须指定 targetId");
            }
            normalized.add(item);
        }
        return normalized;
    }

    private String resolveLabel(String targetType, Long targetId) {
        return switch (targetType) {
            case PermissionTargetType.GLOBAL -> "全局公开";
            case PermissionTargetType.DEPARTMENT -> {
                Department department = departmentMapper.selectById(targetId);
                yield "部门:" + (department == null ? targetId : department.getName());
            }
            case PermissionTargetType.ROLE -> {
                Role role = roleMapper.selectById(targetId);
                yield "角色:" + (role == null ? targetId : role.getRoleName());
            }
            case PermissionTargetType.USER -> {
                User user = userMapper.selectById(targetId);
                yield "用户:" + (user == null ? targetId : user.getDisplayName());
            }
            default -> targetType + ":" + targetId;
        };
    }
}

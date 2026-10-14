package com.shuidun.kb.permission;

import java.util.List;

/**
 * 数据权限判定结果（授权 / 未授权 unit 集合）。
 *
 * @param authorized   已授权 unit id
 * @param unauthorized 未授权 unit id
 */
public record PermissionDecision(List<Long> authorized, List<Long> unauthorized) {
}

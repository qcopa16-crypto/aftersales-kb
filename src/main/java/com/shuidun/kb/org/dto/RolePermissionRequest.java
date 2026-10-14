package com.shuidun.kb.org.dto;

import java.util.List;
import lombok.Data;

/**
 * 角色操作权限分配请求。
 *
 * <p>mode=replace 覆盖；mode=add 增量追加。默认 replace。
 */
@Data
public class RolePermissionRequest {

    /** 分配模式：replace / add。 */
    private String mode = "replace";

    /** 操作权限码列表。 */
    private List<String> permissions;
}

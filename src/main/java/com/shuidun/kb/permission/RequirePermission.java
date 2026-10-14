package com.shuidun.kb.permission;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * 操作权限码校验注解（代码设计规范 §3.4）。
 *
 * <p>标注在 Controller 方法或类上，由 {@link PermissionInterceptor} 校验当前用户是否拥有其中任一权限码。
 * 禁止在 Service 里硬编码"admin 全放行"。
 */
@Target({ElementType.METHOD, ElementType.TYPE})
@Retention(RetentionPolicy.RUNTIME)
public @interface RequirePermission {

    /**
     * 所需操作权限码，多个为「任一命中即放行」。
     *
     * @return 权限码数组
     */
    String[] value();
}

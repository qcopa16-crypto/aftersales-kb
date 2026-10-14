package com.shuidun.kb.ai;

/**
 * 单次 generate 流的终态容器（F-23）。
 *
 * <p>SSE 回调（{@link GenerateStreamHandler}）只负责把终态写入本对象，不自行发送
 * done / complete；调用方在 generate 返回后依据本对象判定终态并统一收尾，保证
 * error 与 done 事件对客户端与访问日志**互斥可见**——修复"AI 发出 error 后 Java
 * 仍发送 done 并把部分文本记为成功回答"的缺陷。
 *
 * <p>刻意做成每次调用新建的可变对象（而非实例字段）：本类承载并发请求各自的
 * 局部终态，避免单例 Bean 上的共享可变状态串扰。
 */
final class GenerateTerminal {

    /** 是否收到 done 事件（正常收尾）。 */
    boolean done;

    /** 是否收到 error 事件（终态失败）。 */
    boolean error;

    /** 失败错误码（error=true 时有效）。 */
    String errorCode;

    /** 失败错误信息（error=true 时有效）。 */
    String errorMessage;
}

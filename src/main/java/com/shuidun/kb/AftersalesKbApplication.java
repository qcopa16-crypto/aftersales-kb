package com.shuidun.kb;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 售后知识库业务服务启动类。
 *
 * <p>启用 {@code @ConfigurationProperties} 扫描与定时任务（入库 Worker 轮询）。
 */
@SpringBootApplication
@ConfigurationPropertiesScan
@EnableScheduling
public class AftersalesKbApplication {

    /**
     * 应用入口。
     *
     * @param args 启动参数
     */
    public static void main(String[] args) {
        SpringApplication.run(AftersalesKbApplication.class, args);
    }

}

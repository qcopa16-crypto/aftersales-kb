package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.knowledge.service.KnowledgeImportService;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;

/**
 * R-03 死锁回归护栏：序列行已存在时，热路径<b>不得</b>再执行 {@code INSERT IGNORE}
 * （InnoDB 对已存在行做重复键检查会加 S 锁，随后 {@code FOR UPDATE} 升级 X 锁会死锁）。
 *
 * <p>H2 无此锁升级语义、无法复现死锁，故此处以「调用契约」断言：正常导入只走非锁定存在性检查
 * （{@code countUnitCodeSequence}）而绝不触发 bootstrap 的 {@code initUnitCodeSequence}。
 * 真实 MySQL 并发死锁验证见交付说明。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_seq_hotpath_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}",
        "app.ingest.worker.enabled=false"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeUnitCodeSequenceHotPathTest {

    @Autowired
    private KnowledgeImportService service;

    @MockitoSpyBean
    private KnowledgeUnitMapper unitMapper;

    @Test
    void hotPathDoesNotInsertIgnoreExistingSequenceRow() {
        // schema 种子已保证计数行存在
        assertEquals(1, unitMapper.countUnitCodeSequence("unit_code"));

        MockMultipartFile file = new MockMultipartFile("files", "hotpath.md", "text/markdown",
                "# hotpath\n\n正文".getBytes(StandardCharsets.UTF_8));
        service.importFiles(List.of(file), null, false, 1L);

        // 热路径使用非锁定存在性检查，且绝不执行 INSERT IGNORE（死锁根因）
        verify(unitMapper, atLeastOnce()).countUnitCodeSequence("unit_code");
        verify(unitMapper, never()).initUnitCodeSequence(anyString(), anyLong());
    }
}

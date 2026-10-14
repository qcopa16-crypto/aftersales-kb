package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.knowledge.service.KnowledgeImportService;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.annotation.DirtiesContext;

/**
 * R-07 回归：请求级总大小上限——单个文件未超限但多文件累计超限时必须整请求拒绝。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_upload_limit_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}",
        "app.ingest.worker.enabled=false",
        "app.upload.max-request-bytes=10"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeUploadRequestLimitTest {

    @Autowired
    private KnowledgeImportService service;

    @Test
    void requestTotalSizeExceededRejected() {
        MockMultipartFile file = new MockMultipartFile(
                "files", "a.md", "text/markdown", new byte[20]);
        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.importFiles(List.of(file), null, false, 1L));
        assertEquals(ErrorCode.UPLOAD_TOO_LARGE.getCode(), ex.getCode());
    }
}

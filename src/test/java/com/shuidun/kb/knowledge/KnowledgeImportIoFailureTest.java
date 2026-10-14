package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doThrow;

import com.shuidun.kb.job.IngestJobService;
import com.shuidun.kb.knowledge.dto.ImportItemVO;
import com.shuidun.kb.knowledge.dto.ImportResponseVO;
import com.shuidun.kb.knowledge.service.KnowledgeImportService;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.web.multipart.MultipartFile;

/**
 * R-01 回归：上传流打开 / 读取 / 关闭分别抛 IOException 时，
 * 数据库不得留下「无 storage_path、无 ingest job」的孤立 unit，磁盘也不得留下孤儿文件。
 *
 * <p>使用独立 H2 库与独立上传目录，避免影响其它测试。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_import_io_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}",
        "app.ingest.worker.enabled=false"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeImportIoFailureTest {

    private static final Path STORAGE_ROOT;

    static {
        try {
            STORAGE_ROOT = Files.createTempDirectory("kb-import-io-");
        } catch (IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
    }

    @DynamicPropertySource
    static void storageProps(DynamicPropertyRegistry registry) {
        registry.add("app.storage.local-path", () -> STORAGE_ROOT.toString());
    }

    @Autowired
    private KnowledgeImportService service;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    /** R-01 回滚方向：文件已落盘但 DB 后续失败时，用于注入失败。 */
    @MockitoSpyBean
    private IngestJobService ingestJobService;

    @BeforeEach
    void cleanStorage() throws IOException {
        clearStorage();
    }

    @AfterEach
    void cleanUp() throws IOException {
        clearStorage();
    }

    /** getInputStream() 抛 IOException：不得留下孤立 unit，也不得留下文件。 */
    @Test
    void openStreamFailureLeavesNoOrphanUnitOrFile() {
        ImportResponseVO response = service.importFiles(List.of(new OpenThrowingFile()), null, false, 1L);

        assertEquals(1, response.getRejectedCount(), "打开流失败的文件应被拒绝");
        assertEquals(0, response.getAcceptedCount());
        assertNoUnitOrJob(response.getBatchId());
        assertEquals(0, countStoredFiles(), "不应留下任何孤儿文件");
    }

    /** 读取（复制）过程抛 IOException：不得留下孤立 unit，也不得留下文件。 */
    @Test
    void readFailureLeavesNoOrphanUnitOrFile() {
        ImportResponseVO response = service.importFiles(List.of(new ReadThrowingFile()), null, false, 1L);

        assertEquals(1, response.getRejectedCount(), "读取失败的文件应被拒绝");
        assertEquals(0, response.getAcceptedCount());
        assertNoUnitOrJob(response.getBatchId());
        assertEquals(0, countStoredFiles(), "不应留下任何孤儿文件（含 .part 临时文件）");
    }

    /** 落盘成功但 close 抛 IOException：不得留下无 storage_path/job 的 unit，文件已被引用不算孤儿。 */
    @Test
    void closeFailureStillCommitsReferencedUnitAndFile() {
        ImportResponseVO response = service.importFiles(List.of(new CloseThrowingFile()), null, false, 1L);

        assertEquals(1, response.getAcceptedCount(), "文件已完整落盘，close 失败不应使该文件被拒绝");
        assertEquals(0, response.getRejectedCount());
        ImportItemVO item = response.getItems().get(0);
        assertNotNull(item.getUnitId());

        Long units = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM knowledge_units WHERE batch_id = ?", Long.class, response.getBatchId());
        assertEquals(1L, units, "应保留一条 unit 记录");
        String storagePath = jdbcTemplate.queryForObject(
                "SELECT storage_path FROM knowledge_units WHERE id = ?", String.class, item.getUnitId());
        assertNotNull(storagePath, "unit 必须已写入 storage_path");
        Long jobs = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM ingest_jobs WHERE batch_id = ?", Long.class, response.getBatchId());
        assertEquals(1L, jobs, "必须已创建 ingest job");
        assertEquals(1, countStoredFiles(), "已落盘文件应被 unit 引用（非孤儿）");
    }

    /**
     * R-01 回滚方向：文件已落盘但随后建 job（DB）失败 → 整批事务回滚，
     * unit 不存在、磁盘无该文件（registerRollbackCleanup 的 afterCompletion 生效）。
     */
    @Test
    void dbFailureAfterFileSavedRollsBackUnitAndFile() {
        String name = "rollback-enqueue-" + System.nanoTime() + ".md";
        MockMultipartFile file = new MockMultipartFile("files", name, "text/markdown",
                "# rollback\n\nbody".getBytes(StandardCharsets.UTF_8));
        doThrow(new IllegalStateException("simulated enqueue failure"))
                .when(ingestJobService).enqueue(anyString(), any(), any());

        assertThrows(RuntimeException.class, () -> service.importFiles(List.of(file), null, false, 1L));

        Long units = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM knowledge_units WHERE title = ?", Long.class, name);
        assertEquals(0L, units, "事务回滚后不得残留 unit");
        assertEquals(0, countStoredFiles(), "事务回滚后不得残留孤儿文件");
    }

    private void assertNoUnitOrJob(String batchId) {
        Long units = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM knowledge_units WHERE batch_id = ?", Long.class, batchId);
        Long jobs = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM ingest_jobs WHERE batch_id = ?", Long.class, batchId);
        assertEquals(0L, units, "不得留下无 storage_path/job 的孤立 unit");
        assertEquals(0L, jobs, "不得留下孤立 ingest job");
    }

    private void clearStorage() throws IOException {
        if (!Files.exists(STORAGE_ROOT)) {
            return;
        }
        try (Stream<Path> walk = Files.walk(STORAGE_ROOT)) {
            walk.sorted(Comparator.reverseOrder())
                    .filter(p -> !p.equals(STORAGE_ROOT))
                    .forEach(p -> {
                        try {
                            Files.deleteIfExists(p);
                        } catch (IOException ignored) {
                            // 清理失败不影响断言
                        }
                    });
        }
    }

    private int countStoredFiles() {
        if (!Files.exists(STORAGE_ROOT)) {
            return 0;
        }
        try (Stream<Path> walk = Files.walk(STORAGE_ROOT)) {
            return (int) walk.filter(Files::isRegularFile).count();
        } catch (IOException ex) {
            throw new IllegalStateException(ex);
        }
    }

    /** 正常内容，供 close 失败用例使用。 */
    private static byte[] content() {
        return "# title\n\nbody".getBytes(StandardCharsets.UTF_8);
    }

    /** getInputStream() 直接抛 IOException。 */
    private static final class OpenThrowingFile implements MultipartFile {
        @Override
        public String getName() {
            return "files";
        }

        @Override
        public String getOriginalFilename() {
            return "open-bad.md";
        }

        @Override
        public String getContentType() {
            return "text/markdown";
        }

        @Override
        public boolean isEmpty() {
            return false;
        }

        @Override
        public long getSize() {
            return content().length;
        }

        @Override
        public byte[] getBytes() {
            return content();
        }

        @Override
        public InputStream getInputStream() throws IOException {
            throw new IOException("simulated open failure");
        }

        @Override
        public void transferTo(File dest) throws IOException {
            throw new IOException("not supported");
        }
    }

    /** 输入流在 read 时抛 IOException（模拟读取/复制中途失败）。 */
    private static final class ReadThrowingFile extends MockMultipartFile {
        ReadThrowingFile() {
            super("files", "read-bad.md", "text/markdown", content());
        }

        @Override
        public InputStream getInputStream() {
            return new InputStream() {
                @Override
                public int read() throws IOException {
                    throw new IOException("simulated read failure");
                }

                @Override
                public int read(byte[] b, int off, int len) throws IOException {
                    throw new IOException("simulated read failure");
                }
            };
        }
    }

    /** 输入流可完整读取，但 close() 抛 IOException。 */
    private static final class CloseThrowingFile extends MockMultipartFile {
        CloseThrowingFile() {
            super("files", "close-bad.md", "text/markdown", content());
        }

        @Override
        public InputStream getInputStream() {
            return new ByteArrayInputStream(content()) {
                @Override
                public void close() throws IOException {
                    throw new IOException("simulated close failure");
                }
            };
        }
    }
}

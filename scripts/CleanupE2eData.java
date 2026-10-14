import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 清理联调/演示过程中产生的测试数据（F-16 安全加固版）。
 *
 * <p><b>默认 dry-run</b>：不带 {@code --apply} 时只打印将要执行的动作，不写任何数据。
 *
 * <p>删除范围（只动测试数据，不碰种子数据）：
 * <ul>
 *   <li>{@code knowledge_units} 中 {@code source_file_name LIKE 'e2e-%'} 或标题含 e2e 的单元，及其切片/权限/作业</li>
 *   <li>这些单元在 Milvus 中的向量（AI 服务 {@code DELETE /internal/units/{code}}）——
 *       <b>仅对向量删除成功的单元才删除 SQL 行（fail-stop）</b>，避免留下孤儿向量</li>
 * </ul>
 *
 * <p>演示基线复位（{@code --reset-demo-baseline}，需与 {@code --apply} 同时给出）是破坏面更大的操作，
 * 单独拆出并要求多重确认：<b>只允许 {@code *_demo}/{@code *_test} 库</b>（标准业务库名 {@code aftersales_kb}
 * 不得用于破坏性复位），且必须设置环境变量 {@code CLEANUP_CONFIRM=<dbname>} 与目标库名一致；
 * 复位前打印目标库名、每张全局表可删主键下界与影响行数、种子 ID 摘要。清空
 * {@code knowledge_gaps}/{@code knowledge_gap_samples}/{@code faqs}、删除 id &gt; 9014 的访问日志、还原种子单元权限。
 *
 * <p>环境白名单：仅允许连接 {@code 127.0.0.1}/{@code localhost}/{@code ::1} 或
 * {@code CLEANUP_ALLOWED_HOSTS}（逗号分隔）中列出的主机；数据库名必须为 {@code aftersales_kb}
 * 或以 {@code _demo}/{@code _test} 结尾；任一不满足立即拒绝执行（不连接、不写入）。
 *
 * <p>用法（需 MySQL 驱动在 classpath）：
 * <pre>
 * # 预览（默认，不写数据）
 * java -cp &lt;mysql-connector-j.jar&gt; scripts/CleanupE2eData.java &lt;host&gt; &lt;port&gt; &lt;user&gt; &lt;password&gt; &lt;token-file&gt;
 * # 执行单元清理（仅删向量删除成功的单元）
 * java -cp &lt;mysql-connector-j.jar&gt; scripts/CleanupE2eData.java --apply &lt;host&gt; &lt;port&gt; &lt;user&gt; &lt;password&gt; &lt;token-file&gt;
 * # 执行单元清理 + 演示基线复位（破坏面最大；仅 *_demo/*_test 库，需 CLEANUP_CONFIRM=&lt;dbname&gt;）
 * CLEANUP_DB=aftersales_kb_demo CLEANUP_CONFIRM=aftersales_kb_demo \
 *   java -cp &lt;mysql-connector-j.jar&gt; scripts/CleanupE2eData.java --apply --reset-demo-baseline &lt;host&gt; &lt;port&gt; &lt;user&gt; &lt;password&gt; &lt;token-file&gt;
 * </pre>
 */
public class CleanupE2eData {

    /** 种子演示访问日志的最大 id（见 data-mysql.sql：9001~9014）。 */
    private static final long SEED_LOG_MAX_ID = 9014L;

    /** 种子知识缺口最大 id：种子数据没有缺口，故下界为 0（仅删除演示期新增行）。 */
    private static final long SEED_GAP_MAX_ID = 0L;

    /** 种子 FAQ 最大 id：种子数据没有 FAQ，故下界为 0（仅删除演示期新增行）。 */
    private static final long SEED_FAQ_MAX_ID = 0L;

    /** 种子知识单元 id（见 data-mysql.sql：U-1001~U-1005）。 */
    private static final String SEED_UNIT_IDS = "(1,2,3,4,5)";

    /** 允许普通清理的数据库名（精确或以 _demo/_test 结尾）。 */
    private static final String EXPECTED_DB = "aftersales_kb";

    /** 破坏性基线复位只允许这些后缀的库（R-13：标准业务库名不得用于 destructive reset）。 */
    private static final String[] RESET_DB_SUFFIXES = {"_demo", "_test"};

    /** 基线复位的额外环境确认变量名：值必须等于目标库名。 */
    private static final String RESET_CONFIRM_ENV = "CLEANUP_CONFIRM";

    public static void main(String[] args) throws Exception {
        boolean apply = false;
        boolean resetBaseline = false;
        List<String> positional = new ArrayList<>();
        for (String arg : args) {
            switch (arg) {
                case "--apply" -> apply = true;
                case "--reset-demo-baseline" -> resetBaseline = true;
                default -> positional.add(arg);
            }
        }
        if (positional.size() < 5) {
            System.err.println("用法: [--apply] [--reset-demo-baseline] <host> <port> <user> <password> <token-file>");
            System.exit(2);
        }
        String host = positional.get(0);
        String port = positional.get(1);
        String user = positional.get(2);
        String password = positional.get(3);
        String tokenFile = positional.get(4);
        String database = System.getenv().getOrDefault("CLEANUP_DB", EXPECTED_DB);

        // ---- 环境护栏：主机/库名白名单，不匹配直接拒绝（不连接、不写入）----
        if (!hostAllowed(host)) {
            System.err.println("拒绝执行：host=" + host + " 不在允许列表（本机回环或 CLEANUP_ALLOWED_HOSTS）中。");
            System.exit(3);
        }
        if (!databaseAllowed(database)) {
            System.err.println("拒绝执行：database=" + database + " 不是预期的演示/测试库。");
            System.exit(3);
        }
        if (resetBaseline && !apply) {
            System.err.println("拒绝执行：--reset-demo-baseline 必须与 --apply 同时给出。");
            System.exit(3);
        }
        // R-13：破坏性基线复位只允许明确的 *_demo/*_test 库，并要求额外环境确认值。
        // 这些检查全部在建立连接之前完成——对标准业务库名（如 aftersales_kb）执行 reset 参数
        // 会在连接前被拒绝，绝不触碰任何数据。
        if (resetBaseline) {
            if (!databaseAllowedForReset(database)) {
                System.err.println("拒绝执行：--reset-demo-baseline 只允许 *_demo / *_test 库，"
                        + "当前 database=" + database + "。标准业务库名不得用于破坏性复位。");
                System.exit(3);
            }
            String confirm = System.getenv(RESET_CONFIRM_ENV);
            if (confirm == null || !confirm.trim().equalsIgnoreCase(database.trim())) {
                System.err.println("拒绝执行：--reset-demo-baseline 需设置环境变量 "
                        + RESET_CONFIRM_ENV + "=" + database + " 作为额外确认。");
                System.exit(3);
            }
        }

        String token = Files.readString(Path.of(tokenFile)).trim();
        String url = "jdbc:mysql://" + host + ":" + port + "/" + database
                + "?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai"
                + "&allowPublicKeyRetrieval=true&useSSL=false";
        HttpClient http = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(10))
                .build();

        System.out.println("模式: " + (apply ? "APPLY（会写数据）" : "DRY-RUN（只读预览）")
                + (resetBaseline ? " + 演示基线复位" : ""));
        System.out.println("目标: " + host + ":" + port + "/" + database);

        try (Connection conn = DriverManager.getConnection(url, user, password)) {
            List<Long> ids = new ArrayList<>();
            List<String> codes = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT id, unit_code FROM knowledge_units "
                            + "WHERE source_file_name LIKE 'e2e-%' OR title LIKE '%e2e%'")) {
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        ids.add(rs.getLong(1));
                        codes.add(rs.getString(2));
                    }
                }
            }
            System.out.println("待清理测试单元数 = " + ids.size() + " -> " + codes);
            if (!apply) {
                System.out.println("DRY-RUN：未执行任何写入。加 --apply 才会真正删除。");
                return;
            }

            // ---- 先删向量，只有成功的单元才允许删除其 SQL 行（fail-stop，杜绝孤儿向量）----
            List<Long> sqlDeletable = new ArrayList<>();
            List<Long> vectorFailed = new ArrayList<>();
            for (int i = 0; i < codes.size(); i++) {
                String code = codes.get(i);
                HttpRequest req = HttpRequest.newBuilder()
                        .uri(URI.create("http://127.0.0.1:8000/internal/units/" + code))
                        .header("X-Internal-Token", token)
                        .timeout(Duration.ofSeconds(30))
                        .DELETE().build();
                boolean ok = false;
                try {
                    HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
                    ok = resp.statusCode() < 300;
                    if (!ok) {
                        System.out.println("  向量删除失败 " + code + " -> HTTP " + resp.statusCode());
                    }
                } catch (Exception ex) {
                    System.out.println("  向量删除失败 " + code + ": " + ex.getMessage());
                }
                if (ok) {
                    sqlDeletable.add(ids.get(i));
                } else {
                    vectorFailed.add(ids.get(i));
                }
            }
            System.out.println("向量删除成功 = " + sqlDeletable.size() + "/" + codes.size()
                    + (vectorFailed.isEmpty() ? "" : "；保留 SQL 行的失败单元 id=" + vectorFailed));

            try (Statement st = conn.createStatement()) {
                if (!sqlDeletable.isEmpty()) {
                    String list = join(sqlDeletable);
                    System.out.println("chunks=" + st.executeUpdate(
                            "DELETE FROM knowledge_chunks WHERE unit_id IN (" + list + ")"));
                    System.out.println("perms =" + st.executeUpdate(
                            "DELETE FROM unit_permissions WHERE unit_id IN (" + list + ")"));
                    System.out.println("jobs  =" + st.executeUpdate(
                            "DELETE FROM ingest_jobs WHERE unit_id IN (" + list + ")"));
                    System.out.println("units =" + st.executeUpdate(
                            "DELETE FROM knowledge_units WHERE id IN (" + list + ")"));
                } else {
                    System.out.println("无向量删除成功的单元：跳过 SQL 主数据删除（fail-stop）。");
                }

                if (resetBaseline) {
                    // 破坏面更大的演示基线复位：仅显式二次开关 + demo/test 库 + 环境确认后执行。
                    // 复位前打印目标库名、每张全局表可删主键下界与影响行数、种子 ID 摘要。
                    System.out.println("--- 演示基线复位（--reset-demo-baseline）---");
                    System.out.println("目标库 = " + database + "（仅 *_demo/*_test，确认 "
                            + RESET_CONFIRM_ENV + "=" + database + "）");
                    System.out.println("可删主键下界：qa_access_logs>" + SEED_LOG_MAX_ID
                            + " knowledge_gaps>" + SEED_GAP_MAX_ID
                            + " knowledge_gap_samples>" + SEED_GAP_MAX_ID
                            + " faqs>" + SEED_FAQ_MAX_ID);
                    System.out.println("种子摘要：访问日志 id<=" + SEED_LOG_MAX_ID
                            + "，种子知识单元 id " + SEED_UNIT_IDS + "（保留）");
                    System.out.println("影响行数预览：logs=" + countAbove(st, "qa_access_logs", SEED_LOG_MAX_ID)
                            + " gaps=" + countAbove(st, "knowledge_gaps", SEED_GAP_MAX_ID)
                            + " gap-samples=" + countAbove(st, "knowledge_gap_samples", SEED_GAP_MAX_ID)
                            + " faqs=" + countAbove(st, "faqs", SEED_FAQ_MAX_ID));
                    System.out.println("extra-logs=" + st.executeUpdate(
                            "DELETE FROM qa_access_logs WHERE id > " + SEED_LOG_MAX_ID));
                    System.out.println("extra-gaps=" + st.executeUpdate(
                            "DELETE FROM knowledge_gaps WHERE id > " + SEED_GAP_MAX_ID));
                    System.out.println("extra-gap-samples=" + st.executeUpdate(
                            "DELETE FROM knowledge_gap_samples WHERE id > " + SEED_GAP_MAX_ID));
                    System.out.println("extra-faqs=" + st.executeUpdate(
                            "DELETE FROM faqs WHERE id > " + SEED_FAQ_MAX_ID));
                    System.out.println("seed-perms-del=" + st.executeUpdate(
                            "DELETE FROM unit_permissions WHERE unit_id IN " + SEED_UNIT_IDS));
                    System.out.println("seed-perms-add=" + st.executeUpdate(
                            "INSERT INTO unit_permissions (unit_id, target_type, target_id) VALUES "
                                    + "(1,'department',2),(2,'department',2),(3,'role',4),(4,'role',5),(5,'global',0)"));
                } else {
                    System.out.println("未指定 --reset-demo-baseline：保留访问日志/缺口/FAQ 与种子权限（不触碰全局基线）。");
                }
            }

            try (Statement st = conn.createStatement();
                 ResultSet rs = st.executeQuery("SELECT COUNT(*) FROM knowledge_units")) {
                rs.next();
                System.out.println("剩余知识单元 = " + rs.getInt(1));
            }
            try (Statement st = conn.createStatement();
                 ResultSet rs = st.executeQuery("SELECT COUNT(*) FROM qa_access_logs")) {
                rs.next();
                System.out.println("剩余访问日志 = " + rs.getInt(1));
            }
        }
    }

    /** 主机白名单：回环地址，或 CLEANUP_ALLOWED_HOSTS 中显式列出的主机。 */
    private static boolean hostAllowed(String host) {
        String normalized = host == null ? "" : host.trim().toLowerCase(Locale.ROOT);
        Set<String> allowed = new LinkedHashSet<>(Arrays.asList("127.0.0.1", "localhost", "::1"));
        String extra = System.getenv("CLEANUP_ALLOWED_HOSTS");
        if (extra != null && !extra.isBlank()) {
            for (String item : extra.split(",")) {
                if (!item.isBlank()) {
                    allowed.add(item.trim().toLowerCase(Locale.ROOT));
                }
            }
        }
        return allowed.contains(normalized);
    }

    /** 库名白名单：预期库名，或以 _demo / _test 结尾的库。 */
    private static boolean databaseAllowed(String database) {
        String normalized = database == null ? "" : database.trim().toLowerCase(Locale.ROOT);
        return normalized.equals(EXPECTED_DB)
                || normalized.endsWith("_demo")
                || normalized.endsWith("_test");
    }

    /** 基线复位白名单：只允许 *_demo / *_test 库（标准业务库名不得用于 destructive reset）。 */
    private static boolean databaseAllowedForReset(String database) {
        String normalized = database == null ? "" : database.trim().toLowerCase(Locale.ROOT);
        for (String suffix : RESET_DB_SUFFIXES) {
            if (normalized.endsWith(suffix)) {
                return true;
            }
        }
        return false;
    }

    /** 统计主键大于下界的行数（复位前影响面预览）。表名为内部常量，无注入面。 */
    private static long countAbove(Statement st, String table, long seedMaxId) throws Exception {
        try (ResultSet rs = st.executeQuery(
                "SELECT COUNT(*) FROM " + table + " WHERE id > " + seedMaxId)) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private static String join(List<Long> values) {
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < values.size(); i++) {
            builder.append(i == 0 ? "" : ",").append(values.get(i));
        }
        return builder.toString();
    }
}

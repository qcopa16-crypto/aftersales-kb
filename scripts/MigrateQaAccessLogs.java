import java.sql.*;
import java.util.*;

/**
 * qa_access_logs 表结构幂等迁移 + 演示数据对齐：
 *
 * <ol>
 *   <li>按需补充列：{@code db_ms} / {@code cache_ms}（阶段耗时）、{@code chosen_unit_id}（澄清点选）；</li>
 *   <li>把演示行 id 9001~9010 完整对齐到 {@code data-mysql.sql} 的种子定义
 *       （含 response/retrieve/generate/db/cache/tokens/faq_hit），保证 FAQ 直出行语义真实。</li>
 * </ol>
 *
 * <p>可重复执行：列已存在则跳过，演示行按主键 UPDATE（不会新增/删除行）。
 *
 * <pre>
 * java -cp &lt;mysql-connector-j.jar&gt; scripts/MigrateQaAccessLogs.java \
 *      &lt;host&gt; &lt;port&gt; &lt;user&gt; &lt;password&gt;
 * # 亦可使用环境变量：MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD
 * </pre>
 *
 * <p>安全审计 M3：连接凭据不再硬编码，改为从命令行参数或环境变量读取（前者优先）。
 */
public class MigrateQaAccessLogs {

    /**
     * 演示行：id -> {sessionId, userId, question, answer, recalled, authorized, prompt, completion,
     * total, response, retrieve, generate, db, cache, faqHit}
     */
    private static final List<Object[]> DEMO = new ArrayList<>();

    private static void row(long id, String session, long userId, String q, String a, String recalled,
                            int prompt, int completion, int response, int retrieve, int generate,
                            int db, int cache, int faq) {
        DEMO.add(new Object[]{id, session, userId, q, a, recalled,
                prompt, completion, prompt + completion, response, retrieve, generate, db, cache, faq});
    }

    static {
        row(9001, "s_demo_1", 2, "保修期是多久？", "整机保修期为一年，人为损坏不在免费保修范围内。[1]", "[1]", 618, 46, 3180, 470, 2450, 48, 3, 0);
        row(9002, "s_demo_2", 2, "客户屏幕碎了能保修吗？", "人为损坏不在免费保修范围内，建议走付费维修流程。[1]", "[1]", 642, 58, 3520, 520, 2700, 52, 3, 0);
        row(9003, "s_demo_3", 5, "工单多久响应？", "接单后 30 分钟内响应，24 小时内给出处理方案。[1]", "[2]", 538, 42, 2960, 430, 2260, 44, 2, 0);
        row(9004, "s_demo_4", 2, "备件申请要走什么流程？", "在工单系统提交申请，由备件管理员审批。[1]", "[2]", 560, 44, 3040, 450, 2320, 46, 2, 0);
        row(9005, "s_demo_5", 3, "路由器指示灯绿色代表什么？", "PWR 指示灯常亮绿色表示设备自检通过。[1]", "[5]", 512, 36, 2760, 400, 2100, 41, 3, 0);
        row(9006, "s_demo_6", 2, "保修期是多久？", "整机保修期为一年。[1]", "[1]", 0, 0, 32, 0, 0, 0, 6, 1);
        row(9007, "s_demo_7", 2, "客户屏幕碎了能保修吗？", "人为损坏不在免费保修范围内。[1]", "[1]", 636, 52, 3300, 500, 2560, 50, 3, 0);
        row(9008, "s_demo_8", 3, "工单超时了怎么办？", "超时工单会自动升级到主管，并计入考核。[1]", "[2]", 566, 48, 3120, 470, 2400, 47, 2, 0);
        row(9009, "s_demo_9", 2, "保修期是多久？", "整机保修期为一年。[1]", "[1]", 0, 0, 29, 0, 0, 0, 5, 1);
        row(9010, "s_demo_10", 5, "路由器上电后灯不亮怎么办？", "先确认电源适配器，若 PWR 仍不亮需返修。[1]", "[5]", 548, 54, 3220, 490, 2480, 49, 3, 0);
    }

    public static void main(String[] args) throws Exception {
        String host = resolve(args, 0, "MYSQL_HOST", null);
        String port = resolve(args, 1, "MYSQL_PORT", "3306");
        String user = resolve(args, 2, "MYSQL_USER", null);
        String password = resolve(args, 3, "MYSQL_PASSWORD", null);
        String url = "jdbc:mysql://" + host + ":" + port + "/aftersales_kb"
                + "?useUnicode=true&characterEncoding=UTF-8"
                + "&serverTimezone=Asia/Shanghai&allowPublicKeyRetrieval=true&useSSL=false";
        try (Connection c = DriverManager.getConnection(url, user, password)) {
            System.out.println("[1] ensure columns");
            boolean hasDb = hasColumn(c, "qa_access_logs", "db_ms");
            boolean hasCache = hasColumn(c, "qa_access_logs", "cache_ms");
            boolean hasChosen = hasColumn(c, "qa_access_logs", "chosen_unit_id");
            boolean hasAnswerSource = hasColumn(c, "qa_access_logs", "answer_source");
            System.out.println("    db_ms=" + hasDb + " cache_ms=" + hasCache
                    + " chosen_unit_id=" + hasChosen + " answer_source=" + hasAnswerSource);
            try (Statement st = c.createStatement()) {
                if (!hasDb) {
                    st.execute("ALTER TABLE qa_access_logs ADD COLUMN db_ms INT NOT NULL DEFAULT 0 "
                            + "COMMENT '数据库耗时（MySQL：四维权限过滤 + 知识单元加载）' AFTER generate_ms");
                    System.out.println("    + added db_ms");
                }
                if (!hasCache) {
                    st.execute("ALTER TABLE qa_access_logs ADD COLUMN cache_ms INT NOT NULL DEFAULT 0 "
                            + "COMMENT '缓存耗时（Redis：FAQ 命中查询）' AFTER db_ms");
                    System.out.println("    + added cache_ms");
                }
                if (!hasChosen) {
                    st.execute("ALTER TABLE qa_access_logs ADD COLUMN chosen_unit_id BIGINT DEFAULT NULL "
                            + "COMMENT '澄清点选：本轮由用户从候选中选中的知识单元 id；未点选为 NULL' "
                            + "AFTER cache_ms");
                    System.out.println("    + added chosen_unit_id");
                }
                if (!hasAnswerSource) {
                    st.execute("ALTER TABLE qa_access_logs ADD COLUMN answer_source VARCHAR(16) NOT NULL "
                            + "DEFAULT 'kb' COMMENT '答案来源：kb/faq/llm_general/clarify/refusal' "
                            + "AFTER chosen_unit_id");
                    System.out.println("    + added answer_source");
                }
            }

            System.out.println("[2] align demo rows 9001-9010");
            int updated = 0;
            try (PreparedStatement ps = c.prepareStatement(
                    "UPDATE qa_access_logs SET session_id=?, user_id=?, question=?, answer=?, "
                    + "recalled_unit_ids_json=?, authorized_unit_ids_json=?, unauthorized_unit_ids_json=?, "
                    + "prompt_tokens=?, completion_tokens=?, total_tokens=?, response_time_ms=?, retrieve_ms=?, "
                    + "generate_ms=?, db_ms=?, cache_ms=?, faq_hit=?, answer_source=? WHERE id=?")) {
                for (Object[] r : DEMO) {
                    int i = 1;
                    ps.setString(i++, (String) r[1]);
                    ps.setLong(i++, (Long) r[2]);
                    ps.setString(i++, (String) r[3]);
                    ps.setString(i++, (String) r[4]);
                    ps.setString(i++, (String) r[5]);
                    ps.setString(i++, (String) r[5]);
                    ps.setString(i++, "[]");
                    ps.setInt(i++, (Integer) r[6]);
                    ps.setInt(i++, (Integer) r[7]);
                    ps.setInt(i++, (Integer) r[8]);
                    ps.setInt(i++, (Integer) r[9]);
                    ps.setInt(i++, (Integer) r[10]);
                    ps.setInt(i++, (Integer) r[11]);
                    ps.setInt(i++, (Integer) r[12]);
                    ps.setInt(i++, (Integer) r[13]);
                    ps.setInt(i++, (Integer) r[14]);
                    // FAQ 直出的演示行（9006 / 9009，tokens=0 且 cache 命中）标 faq，其余标 kb
                    long id = (Long) r[0];
                    ps.setString(i++, (id == 9006L || id == 9009L) ? "faq" : "kb");
                    ps.setLong(i, (Long) r[0]);
                    updated += ps.executeUpdate();
                }
            }
            System.out.println("    updated " + updated + " rows");

            System.out.println("[3] verify");
            try (Statement st = c.createStatement()) {
                ResultSet rs = st.executeQuery("SELECT id, response_time_ms, retrieve_ms, generate_ms, db_ms, "
                        + "cache_ms, faq_hit FROM qa_access_logs WHERE id BETWEEN 9001 AND 9010 ORDER BY id");
                while (rs.next()) {
                    System.out.printf("    #%d resp=%dms ret=%dms gen=%dms db=%dms cache=%dms faq=%d%n",
                            rs.getLong(1), rs.getInt(2), rs.getInt(3), rs.getInt(4), rs.getInt(5), rs.getInt(6), rs.getInt(7));
                }
            }
            System.out.println("[OK] migration done");
        }
    }

    private static boolean hasColumn(Connection c, String table, String column) throws SQLException {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                + "AND TABLE_NAME=? AND COLUMN_NAME=?")) {
            ps.setString(1, table);
            ps.setString(2, column);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getInt(1) > 0;
            }
        }
    }

    /**
     * 解析连接参数：优先命令行参数，其次环境变量；两者皆空则抛出明确错误（禁止硬编码凭据）。
     */
    private static String resolve(String[] args, int index, String envName, String defaultValue) {
        String fromArg = args.length > index ? args[index] : null;
        if (fromArg != null && !fromArg.isBlank()) {
            return fromArg.trim();
        }
        String fromEnv = System.getenv(envName);
        if (fromEnv != null && !fromEnv.isBlank()) {
            return fromEnv.trim();
        }
        if (defaultValue != null) {
            return defaultValue;
        }
        throw new IllegalStateException(
                "缺少数据库连接参数 " + envName + "：请通过命令行第 " + (index + 1)
                        + " 个参数或环境变量 " + envName + " 提供（禁止硬编码凭据）");
    }
}

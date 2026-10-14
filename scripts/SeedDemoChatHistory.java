import java.sql.*;

/**
 * 为演示库补两条 admin 的历史会话（对齐 {@code data-mysql.sql} 的 9011~9014 行）。
 *
 * <p>目的：让「AI 问答」页的历史会话列表**开箱即有内容**——否则 admin 从未提问过，
 * 列表一直是空的，看起来就像功能没做。
 *
 * <p>幂等：使用 {@code INSERT ... ON DUPLICATE KEY UPDATE}，重复执行只覆盖这 4 行，
 * 不会新增也不会影响其他日志。
 *
 * <pre>
 * java -cp &lt;mysql-connector-j.jar&gt; scripts/SeedDemoChatHistory.java \
 *      &lt;host&gt; &lt;port&gt; &lt;user&gt; &lt;password&gt;
 * # 亦可使用环境变量：MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD
 * </pre>
 *
 * <p>安全审计 M3：连接凭据不再硬编码，改为从命令行参数或环境变量读取（前者优先）。
 */
public class SeedDemoChatHistory {

    /**
     * 演示会话：id, sessionId, question, answer, authorizedUnitIds, tokens, response,
     * retrieve, generate, db, cache, 距今天数/小时。
     */
    private static final Object[][] ROWS = {
        {9011L, "s_demo_admin_1", "保修期是多久？", "整机保修期为一年，人为损坏不在免费保修范围内。[1]",
            "[1]", 618, 46, 3180, 470, 2450, 48, 3, "DAY", 2},
        {9012L, "s_demo_admin_1", "人为损坏能保修吗？", "人为损坏不在免费保修范围内，建议走付费维修流程。[1]",
            "[1]", 640, 52, 3260, 490, 2520, 49, 3, "HOUR", 47},
        {9013L, "s_demo_admin_2", "工单多久响应？", "接单后 30 分钟内响应，24 小时内给出处理方案。[1]",
            "[2]", 538, 42, 2960, 430, 2260, 44, 2, "DAY", 1},
        {9014L, "s_demo_admin_2", "路由器指示灯绿色代表什么？", "PWR 指示灯常亮绿色表示设备自检通过。[1]",
            "[5]", 512, 36, 2760, 400, 2100, 41, 3, "HOUR", 23},
    };

    public static void main(String[] args) throws Exception {
        String host = resolve(args, 0, "MYSQL_HOST", null);
        String port = resolve(args, 1, "MYSQL_PORT", "3306");
        String user = resolve(args, 2, "MYSQL_USER", null);
        String password = resolve(args, 3, "MYSQL_PASSWORD", null);
        String url = "jdbc:mysql://" + host + ":" + port + "/aftersales_kb"
                + "?useUnicode=true&characterEncoding=UTF-8"
                + "&serverTimezone=Asia/Shanghai&allowPublicKeyRetrieval=true&useSSL=false";
        try (Connection c = DriverManager.getConnection(url, user, password)) {
            String sql = "INSERT INTO qa_access_logs "
                    + "(id, session_id, user_id, question, answer, recalled_unit_ids_json, "
                    + "authorized_unit_ids_json, unauthorized_unit_ids_json, prompt_tokens, completion_tokens, "
                    + "total_tokens, response_time_ms, retrieve_ms, generate_ms, db_ms, cache_ms, faq_hit, created_at) "
                    + "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, "
                    + "DATE_SUB(NOW(), INTERVAL ? " + "%UNIT%)) "
                    + "ON DUPLICATE KEY UPDATE session_id=VALUES(session_id), user_id=VALUES(user_id), "
                    + "question=VALUES(question), answer=VALUES(answer), "
                    + "authorized_unit_ids_json=VALUES(authorized_unit_ids_json), "
                    + "recalled_unit_ids_json=VALUES(recalled_unit_ids_json), "
                    + "total_tokens=VALUES(total_tokens), response_time_ms=VALUES(response_time_ms), "
                    + "retrieve_ms=VALUES(retrieve_ms), generate_ms=VALUES(generate_ms), "
                    + "db_ms=VALUES(db_ms), cache_ms=VALUES(cache_ms), faq_hit=VALUES(faq_hit)";

            int affected = 0;
            for (Object[] r : ROWS) {
                String unit = (String) r[12];
                String statement = sql.replace("%UNIT%", unit);
                try (PreparedStatement ps = c.prepareStatement(statement)) {
                    int i = 1;
                    ps.setLong(i++, (Long) r[0]);
                    ps.setString(i++, (String) r[1]);
                    ps.setLong(i++, 1L); // user_id = admin
                    ps.setString(i++, (String) r[2]);
                    ps.setString(i++, (String) r[3]);
                    ps.setString(i++, (String) r[4]);
                    ps.setString(i++, (String) r[4]);
                    ps.setString(i++, "[]");
                    ps.setInt(i++, (Integer) r[5]);
                    ps.setInt(i++, (Integer) r[6]);
                    ps.setInt(i++, (Integer) r[5] + (Integer) r[6]);
                    ps.setInt(i++, (Integer) r[7]);
                    ps.setInt(i++, (Integer) r[8]);
                    ps.setInt(i++, (Integer) r[9]);
                    ps.setInt(i++, (Integer) r[10]);
                    ps.setInt(i++, (Integer) r[11]);
                    ps.setInt(i++, 0);
                    ps.setInt(i, (Integer) r[13]);
                    affected += ps.executeUpdate();
                }
            }
            System.out.println("[OK] demo chat history seeded, rows affected = " + affected);

            try (PreparedStatement ps = c.prepareStatement(
                    "SELECT session_id, COUNT(*) AS rounds FROM qa_access_logs "
                    + "WHERE user_id = 1 AND session_id IS NOT NULL GROUP BY session_id ORDER BY session_id");
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    System.out.println("    admin session " + rs.getString(1) + " rounds=" + rs.getInt(2));
                }
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

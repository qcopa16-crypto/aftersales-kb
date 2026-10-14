import java.sql.*;

/**
 * 为 E2E 准备一条 status=pending_review 的 FAQ，并打印其 id 与问题文本。
 * 输出格式：{id}\t{question}
 *
 * <p>安全审计 M3：连接凭据不再硬编码，改为从命令行参数或环境变量读取（前者优先）：
 * <pre>
 * java -cp &lt;mysql-connector-j.jar&gt; scripts/SeedPendingFaq.java \
 *      &lt;host&gt; &lt;port&gt; &lt;user&gt; &lt;password&gt;
 * # 亦可使用环境变量：MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD
 * </pre>
 */
public class SeedPendingFaq {
    public static void main(String[] args) throws Exception {
        String host = resolve(args, 0, "MYSQL_HOST", null);
        String port = resolve(args, 1, "MYSQL_PORT", "3306");
        String user = resolve(args, 2, "MYSQL_USER", null);
        String password = resolve(args, 3, "MYSQL_PASSWORD", null);
        String url = "jdbc:mysql://" + host + ":" + port + "/aftersales_kb"
                + "?useUnicode=true&characterEncoding=UTF-8"
                + "&serverTimezone=Asia/Shanghai&allowPublicKeyRetrieval=true&useSSL=false";
        String question = "E2E 保修期是多久？";
        try (Connection c = DriverManager.getConnection(url, user, password)) {
            try (PreparedStatement del = c.prepareStatement("DELETE FROM faqs WHERE question=?")) {
                del.setString(1, question);
                del.executeUpdate();
            }
            long id;
            try (PreparedStatement ps = c.prepareStatement(
                    "INSERT INTO faqs(question, answer, category, source_type, status) VALUES(?,?,?,?,?)",
                    Statement.RETURN_GENERATED_KEYS)) {
                ps.setString(1, question);
                ps.setString(2, "自动挖掘答案：整机保修一年。");
                ps.setString(3, "保修");
                ps.setString(4, "auto_mined");
                ps.setString(5, "pending_review");
                ps.executeUpdate();
                try (ResultSet keys = ps.getGeneratedKeys()) {
                    keys.next();
                    id = keys.getLong(1);
                }
            }
            System.out.println(id + "\t" + question);
        }
    }

    /**
     * 解析连接参数：优先命令行参数，其次环境变量；两者皆空则抛出明确错误（禁止硬编码凭据）。
     *
     * @param args         命令行参数
     * @param index        参数下标
     * @param envName      环境变量名
     * @param defaultValue 兜底默认值（null 表示必填）
     * @return 解析出的参数值
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

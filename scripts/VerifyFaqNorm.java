import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.Locale;

/**
 * FAQ 规范化键一致性回归：验证 V4 迁移里 MySQL 侧规范化表达式与
 * {@code FaqCacheService.normalize} 的等价性。
 *
 * <p>断言口径为「除 Unicode 特例等价」：
 * <ul>
 *   <li>一般样例：逐字节一致（PASS）；</li>
 *   <li>U+0130 'İ'：迁移已用显式映射修正，应逐字节一致（PASS）；</li>
 *   <li>希腊词尾 sigma：Java SpecialCasing 产出 'ς'(U+03C2)，MySQL LOWER 产出 'σ'(U+03C3)，
 *       属已知差异（KNOWN_DIFF），不视为失败。</li>
 * </ul>
 *
 * <p>两类校验：
 * <ol>
 *   <li>样例矩阵（18 组，含全角/中文标点/反斜杠/反引号/VT/İ/希腊词尾）；</li>
 *   <li>若目标库存在 faqs 表，逐行比对 {@code question_norm} 与 Java normalize（回填正确性）。</li>
 * </ol>
 *
 * <pre>
 * java -cp &lt;mysql-connector-j.jar&gt; scripts/VerifyFaqNorm.java \
 *      &lt;host&gt; &lt;port&gt; &lt;user&gt; &lt;password&gt; [db]
 * # 亦可使用环境变量：MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB
 * </pre>
 *
 * <p>退出码：0 = 无 FAIL；1 = 存在非已知差异的不一致。
 */
public class VerifyFaqNorm {

    /** 与 V4__faq_question_norm.sql 完全一致的字符类（ICU 正则，转义见迁移注释）。 */
    private static final String PAT =
            "CONVERT(CONCAT(" +
            "'['," +
            "CHAR(32),CHAR(9),CHAR(10),CHAR(13),CHAR(12),CHAR(11)," +
            "'!\"#$%&''()*+,./:;<=>?@'," +
            "CHAR(92),'['," +
            "CHAR(92),CHAR(92)," +
            "CHAR(92),']'," +
            "'^_'," +
            "CHAR(96)," +
            "'{|}~'," +
            "'，。！？；：、“”‘’（）《》【】'," +
            "'-'," +
            "']') USING utf8mb4)";

    /** İ(U+0130) 显式映射：'İ' → "i"+U+0307（与 Java toLowerCase 一致）。 */
    private static final String IDOT_UPPER = "CONVERT(UNHEX('C4B0') USING utf8mb4)";
    private static final String IDOT_LOWER = "CONVERT(UNHEX('69CC87') USING utf8mb4)";

    /** 与 V4 第 2 步一致的规范化表达式（? 为原始 question）。 */
    private static final String NORM_EXPR =
            "LOWER(REGEXP_REPLACE(REPLACE(CONVERT(? USING utf8mb4), "
            + IDOT_UPPER + ", " + IDOT_LOWER + "), " + PAT + ", ''))";

    private static final char FINAL_SIGMA = '\u03C2';   // ς
    private static final char SMALL_SIGMA = '\u03C3';   // σ

    private static final String[] SAMPLES = {
            "如何 退货？", "How to RETURN?", "价格：￥100，含税！", "产品-A/B 测试（新版）",
            "什么是“售后”？", "abc\tdef\nghi  x", "全角％＃＠符号、破折—省略…点·间隔～",
            "Ｑ：可以吗？Ａ：可以。", "A1_B2-C3.D4", "（1）【重点】《手册》‘引号’",
            "a\u000bb", "  mixed\tＣase: ？", "a\\b[c]^d_e`f{g}|h~i", "逗号,句号.斜杠/反斜杠\\",
            "İstanbul 怎么走？", "İİ test", "ΣΟΦΟΣ", "ΟΔΟΣ 路线",
    };

    /** Java 侧规则（与 FaqCacheService.normalize 一致）。 */
    private static String javaNorm(String q) {
        return q.toLowerCase(Locale.ROOT)
                .replaceAll("[\\s\\p{Punct}，。！？；：、“”‘’（）《》【】]", "");
    }

    private static String hex(String s) {
        if (s == null) {
            return "NULL";
        }
        StringBuilder b = new StringBuilder();
        for (byte x : s.getBytes(java.nio.charset.StandardCharsets.UTF_8)) {
            b.append(String.format("%02X", x));
        }
        return b.toString();
    }

    private static String classify(String javaNorm, String sqlNorm) {
        if (javaNorm.equals(sqlNorm)) {
            return "PASS";
        }
        if (javaNorm.replace(FINAL_SIGMA, SMALL_SIGMA).equals(sqlNorm)) {
            return "KNOWN_DIFF(greek-final-sigma)";
        }
        return "FAIL";
    }

    public static void main(String[] args) throws Exception {
        String host = resolve(args, 0, "MYSQL_HOST", "127.0.0.1");
        String port = resolve(args, 1, "MYSQL_PORT", "3306");
        String user = resolve(args, 2, "MYSQL_USER", "root");
        String password = resolve(args, 3, "MYSQL_PASSWORD", "");
        String db = resolve(args, 4, "MYSQL_DB", "aftersales_kb");
        String url = "jdbc:mysql://" + host + ":" + port + "/" + db
                + "?useUnicode=true&characterEncoding=UTF-8"
                + "&serverTimezone=UTC&allowPublicKeyRetrieval=true&useSSL=false";

        int pass = 0;
        int known = 0;
        int fail = 0;
        try (Connection c = DriverManager.getConnection(url, user, password)) {
            System.out.println("[1] 样例矩阵（" + SAMPLES.length + " 组）");
            try (PreparedStatement ps = c.prepareStatement("SELECT " + NORM_EXPR)) {
                for (int i = 0; i < SAMPLES.length; i++) {
                    ps.setString(1, SAMPLES[i]);
                    try (ResultSet rs = ps.executeQuery()) {
                        rs.next();
                        String sql = rs.getString(1);
                        String jv = javaNorm(SAMPLES[i]);
                        String status = classify(jv, sql);
                        if (status.equals("PASS")) {
                            pass++;
                        } else if (status.startsWith("KNOWN_DIFF")) {
                            known++;
                        } else {
                            fail++;
                        }
                        System.out.printf("  %2d %-28s java=%s sql=%s%n", i, status, hex(jv), hex(sql));
                    }
                }
            }

            System.out.println("[2] faqs 表回填一致性（逐行）");
            if (hasTable(c, db, "faqs")) {
                int rows = 0;
                int rowFail = 0;
                try (Statement s = c.createStatement();
                     ResultSet rs = s.executeQuery("SELECT id, question, question_norm FROM faqs")) {
                    while (rs.next()) {
                        rows++;
                        String jv = javaNorm(rs.getString(2));
                        String status = classify(jv, rs.getString(3));
                        if (status.equals("FAIL")) {
                            rowFail++;
                            System.out.println("  id=" + rs.getLong(1) + " FAIL java=" + hex(jv)
                                    + " stored=" + hex(rs.getString(3)));
                        }
                    }
                }
                fail += rowFail;
                System.out.println("  行数=" + rows + " 不一致=" + rowFail);
            } else {
                System.out.println("  (faqs 表不存在，跳过)");
            }
        }

        System.out.println("SUMMARY pass=" + pass + " known_diff=" + known + " fail=" + fail);
        if (fail > 0) {
            System.exit(1);
        }
    }

    private static boolean hasTable(Connection c, String db, String table) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=? AND TABLE_NAME=?")) {
            ps.setString(1, db);
            ps.setString(2, table);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getInt(1) > 0;
            }
        }
    }

    private static String resolve(String[] args, int idx, String env, String def) {
        if (args.length > idx && args[idx] != null && !args[idx].isEmpty()) {
            return args[idx];
        }
        String v = System.getenv(env);
        return (v != null && !v.isEmpty()) ? v : def;
    }
}

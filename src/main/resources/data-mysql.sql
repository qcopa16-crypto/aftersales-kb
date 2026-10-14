-- ============================================================================
-- 售后知识库 · MySQL 8 种子数据（与 data-h2.sql 内容一致）
-- 使用 INSERT IGNORE 保证可重复执行（幂等）。
-- 账号与组织对齐 docs/services/database.md §3；演示口令见 §3.3。
-- ============================================================================

USE aftersales_kb;

-- 部门：总经办 / 售后部 / 销售部 / 财务部
INSERT IGNORE INTO departments (id, parent_id, name, leader_id, sort_order) VALUES
  (1, 0, '总经办', 1, 1),
  (2, 0, '售后部', 2, 2),
  (3, 0, '销售部', 3, 3),
  (4, 0, '财务部', 4, 4);

-- 角色
INSERT IGNORE INTO roles (id, role_name, role_code, description) VALUES
  (1, '系统管理员', 'admin', '用户/角色/部门管理与系统配置'),
  (2, '知识管理员', 'knowledge_admin', '知识导入维护与数据权限配置'),
  (3, '售后工程师', 'after_sales', 'AI 问答与售后资料查看'),
  (4, '销售人员', 'sales', 'AI 问答与销售政策查看'),
  (5, '财务人员', 'finance', 'AI 问答与财务制度查看');

-- 用户（BCrypt 散列，明文口令为演示口令）
INSERT IGNORE INTO users (id, username, password_hash, display_name, department_id, status) VALUES
  (1, 'admin',    '$2a$10$RbvSskz4FXMGZhJaMzwAVOuCdD6ERDymAqgqFbnkLI2TtRU4.QxAC', '系统管理员', 1, 1),
  (2, 'zhangsan', '$2a$10$RbvSskz4FXMGZhJaMzwAVOuCdD6ERDymAqgqFbnkLI2TtRU4.QxAC', '张三',       2, 1),
  (3, 'lisi',     '$2a$10$RbvSskz4FXMGZhJaMzwAVOuCdD6ERDymAqgqFbnkLI2TtRU4.QxAC', '李四',       3, 1),
  (4, 'wangwu',   '$2a$10$RbvSskz4FXMGZhJaMzwAVOuCdD6ERDymAqgqFbnkLI2TtRU4.QxAC', '王五',       4, 1),
  (5, 'zhaoliu',  '$2a$10$RbvSskz4FXMGZhJaMzwAVOuCdD6ERDymAqgqFbnkLI2TtRU4.QxAC', '赵六',       2, 1);

INSERT IGNORE INTO user_roles (user_id, role_id) VALUES
  (1, 1), (2, 3), (3, 4), (4, 5), (5, 3);

-- 角色操作权限码（对齐 docs/services/features.md §4）
INSERT IGNORE INTO role_permissions (role_id, permission_code, permission_type) VALUES
  -- admin
  (1, 'org:user:list', 'api'), (1, 'org:user:edit', 'api'),
  (1, 'org:dept:list', 'api'), (1, 'org:dept:edit', 'api'),
  (1, 'org:role:list', 'api'), (1, 'org:role:edit', 'api'),
  (1, 'knowledge:list', 'api'), (1, 'knowledge:import', 'api'),
  (1, 'knowledge:edit', 'api'), (1, 'knowledge:delete', 'api'),
  (1, 'knowledge:perm', 'api'), (1, 'knowledge:ai', 'api'),
  (1, 'faq:review', 'api'), (1, 'gap:view', 'api'), (1, 'dashboard:view', 'api'),
  -- knowledge_admin
  (2, 'knowledge:list', 'api'), (2, 'knowledge:import', 'api'),
  (2, 'knowledge:edit', 'api'), (2, 'knowledge:delete', 'api'),
  (2, 'knowledge:perm', 'api'), (2, 'knowledge:ai', 'api'),
  (2, 'faq:review', 'api'), (2, 'gap:view', 'api'), (2, 'dashboard:view', 'api'),
  -- after_sales
  (3, 'knowledge:list', 'api'), (3, 'knowledge:ai', 'api'),
  (3, 'gap:view', 'api'), (3, 'dashboard:view', 'api'),
  -- sales
  (4, 'knowledge:list', 'api'), (4, 'knowledge:ai', 'api'), (4, 'dashboard:view', 'api'),
  -- finance
  (5, 'knowledge:list', 'api'), (5, 'knowledge:ai', 'api'), (5, 'dashboard:view', 'api');

-- 演示知识单元（对齐 database.md §3.2 与 features.md §5）
INSERT IGNORE INTO knowledge_units
    (id, unit_code, title, content, summary, category, file_type, status, ingest_status, creator_id) VALUES
  (1, 'U-1001', '整机保修政策', '整机保修期为一年，人为损坏不在免费保修范围内。', '保修范围与期限', 'policy', 'md', 'active', 'active', 1),
  (2, 'U-1002', '工单处理 SOP', '接单后 30 分钟内响应，24 小时内给出处理方案。', '售后工单标准流程', 'sop', 'md', 'active', 'active', 1),
  (3, 'U-1003', '渠道价格政策', '渠道商拿货价为建议零售价的七折，禁止跨区窜货。', '渠道价格与窜货规则', 'sales', 'md', 'active', 'active', 1),
  (4, 'U-1004', '收费标准与费用制度', '上门服务费 200 元/次，非质保维修按配件成本加收 30%。', '售后收费与报销标准', 'finance', 'md', 'active', 'active', 1),
  (5, 'U-1005', 'H3C 路由器用户手册', '设备上电后 PWR 指示灯常亮绿色表示正常。', '路由器安装与指示灯说明', 'product', 'pdf', 'active', 'active', 1);

-- 四维数据权限（OR 组合放行）
INSERT IGNORE INTO unit_permissions (unit_id, target_type, target_id) VALUES
  (1, 'department', 2),   -- 保修政策 → 售后部
  (2, 'department', 2),   -- 工单 SOP → 售后部
  (3, 'role', 4),         -- 渠道价格 → 销售角色
  (4, 'role', 5),         -- 财务制度 → 财务角色
  (5, 'global', 0);       -- 产品手册 → 全局公开

-- 演示用问答访问日志：让看板的「高频问题 / 热门知识 / 趋势 / 耗时」有真实可看的数据。
-- 显式 id + INSERT IGNORE，保证重复执行不会重复累加。
-- 注意：faq_hit=1 的行不经过检索与生成，因此 retrieve_ms/generate_ms 为 0、端到端极短（体现 FAQ 直出优势）。
INSERT IGNORE INTO qa_access_logs
  (id, session_id, user_id, question, answer, recalled_unit_ids_json, authorized_unit_ids_json,
   unauthorized_unit_ids_json, prompt_tokens, completion_tokens, total_tokens,
   response_time_ms, retrieve_ms, generate_ms, db_ms, cache_ms, faq_hit, created_at) VALUES
  (9001, 's_demo_1',  2, '保修期是多久？',           '整机保修期为一年，人为损坏不在免费保修范围内。[1]',
   '[1]', '[1]', '[]', 618, 46, 664, 3180, 470, 2450, 48, 3, 0, DATE_SUB(NOW(), INTERVAL 6 DAY)),
  (9002, 's_demo_2',  2, '客户屏幕碎了能保修吗？',   '人为损坏不在免费保修范围内，建议走付费维修流程。[1]',
   '[1]', '[1]', '[]', 642, 58, 700, 3520, 520, 2700, 52, 3, 0, DATE_SUB(NOW(), INTERVAL 5 DAY)),
  (9003, 's_demo_3',  5, '工单多久响应？',           '接单后 30 分钟内响应，24 小时内给出处理方案。[1]',
   '[2]', '[2]', '[]', 538, 42, 580, 2960, 430, 2260, 44, 2, 0, DATE_SUB(NOW(), INTERVAL 5 DAY)),
  (9004, 's_demo_4',  2, '备件申请要走什么流程？',   '在工单系统提交申请，由备件管理员审批。[1]',
   '[2]', '[2]', '[]', 560, 44, 604, 3040, 450, 2320, 46, 2, 0, DATE_SUB(NOW(), INTERVAL 4 DAY)),
  (9005, 's_demo_5',  3, '路由器指示灯绿色代表什么？', 'PWR 指示灯常亮绿色表示设备自检通过。[1]',
   '[5]', '[5]', '[]', 512, 36, 548, 2760, 400, 2100, 41, 3, 0, DATE_SUB(NOW(), INTERVAL 4 DAY)),
  (9006, 's_demo_6',  2, '保修期是多久？',           '整机保修期为一年。[1]',
   '[1]', '[1]', '[]', 0, 0, 0, 32, 0, 0, 0, 6, 1, DATE_SUB(NOW(), INTERVAL 3 DAY)),
  (9007, 's_demo_7',  2, '客户屏幕碎了能保修吗？',   '人为损坏不在免费保修范围内。[1]',
   '[1]', '[1]', '[]', 636, 52, 688, 3300, 500, 2560, 50, 3, 0, DATE_SUB(NOW(), INTERVAL 2 DAY)),
  (9008, 's_demo_8',  3, '工单超时了怎么办？',       '超时工单会自动升级到主管，并计入考核。[1]',
   '[2]', '[2]', '[]', 566, 48, 614, 3120, 470, 2400, 47, 2, 0, DATE_SUB(NOW(), INTERVAL 2 DAY)),
  (9009, 's_demo_9',  2, '保修期是多久？',           '整机保修期为一年。[1]',
   '[1]', '[1]', '[]', 0, 0, 0, 29, 0, 0, 0, 5, 1, DATE_SUB(NOW(), INTERVAL 1 DAY)),
  (9010, 's_demo_10', 5, '路由器上电后灯不亮怎么办？', '先确认电源适配器，若 PWR 仍不亮需返修。[1]',
   '[5]', '[5]', '[]', 548, 54, 602, 3220, 490, 2480, 49, 3, 0, DATE_SUB(NOW(), INTERVAL 8 HOUR)),
  -- admin(系统管理员) 的两次演示会话：让「AI 问答」页的历史会话列表开箱即有内容。
  -- 同一 session_id 的多行即该会话的多轮问答，历史会话列表按 session_id 聚合重建。
  (9011, 's_demo_admin_1', 1, '保修期是多久？',           '整机保修期为一年，人为损坏不在免费保修范围内。[1]',
   '[1]', '[1]', '[]', 618, 46, 664, 3180, 470, 2450, 48, 3, 0, DATE_SUB(NOW(), INTERVAL 2 DAY)),
  (9012, 's_demo_admin_1', 1, '人为损坏能保修吗？',       '人为损坏不在免费保修范围内，建议走付费维修流程。[1]',
   '[1]', '[1]', '[]', 640, 52, 692, 3260, 490, 2520, 49, 3, 0, DATE_SUB(NOW(), INTERVAL 47 HOUR)),
  (9013, 's_demo_admin_2', 1, '工单多久响应？',           '接单后 30 分钟内响应，24 小时内给出处理方案。[1]',
   '[2]', '[2]', '[]', 538, 42, 580, 2960, 430, 2260, 44, 2, 0, DATE_SUB(NOW(), INTERVAL 1 DAY)),
  (9014, 's_demo_admin_2', 1, '路由器指示灯绿色代表什么？', 'PWR 指示灯常亮绿色表示设备自检通过。[1]',
   '[5]', '[5]', '[]', 512, 36, 548, 2760, 400, 2100, 41, 3, 0, DATE_SUB(NOW(), INTERVAL 23 HOUR));

-- FAQ 直出的两行（9006/9009，tokens=0 且 cache 命中）来源标 faq，与迁移脚本对齐。
UPDATE qa_access_logs SET answer_source = 'faq' WHERE id IN (9006, 9009);

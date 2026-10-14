package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.knowledge.dto.KnowledgeUnitUpdateRequest;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.knowledge.service.KnowledgeUnitService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.annotation.DirtiesContext;

/**
 * R-02 回归：知识单元通用更新必须走 {@code lifecycle_version} CAS，且不得排除 deleting 状态。
 *
 * <p>核心场景：编辑请求读到 status=active 后，并发删除先把单元改为 deleting；编辑请求随后
 * 用旧快照整行写回，必须影响 0 行（并发冲突），库内保持 deleting，禁止"复活"。
 */
@SpringBootTest(properties = {
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_unit_cas_isolated;"
                + "MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}"
})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class KnowledgeUnitLifecycleCasTest {

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private KnowledgeUnitService unitService;

    /** 旧快照整行更新不得把 deleting 覆盖回 active。 */
    @Test
    void staleRowUpdateCannotResurrectDeletingUnit() {
        KnowledgeUnit unit = newUnit("cas-deleting");
        long version = unit.getLifecycleVersion() == null ? 0L : unit.getLifecycleVersion();
        assertEquals(1, unitMapper.markDeleting(unit.getId()));

        KnowledgeUnit stale = unitMapper.selectById(unit.getId());
        stale.setLifecycleVersion(version); // 旧快照版本
        stale.setStatus("active");
        stale.setIngestStatus("active");
        stale.setTitle("被旧快照篡改");

        assertEquals(0, unitMapper.update(stale), "旧版本整行更新必须被拒绝");
        KnowledgeUnit after = unitMapper.selectById(unit.getId());
        assertEquals("deleting", after.getStatus(), "库内必须保持 deleting");
    }

    /** 两个并发读者：后写者用旧版本必须影响 0 行。 */
    @Test
    void staleVersionUpdateRejectedForActiveUnit() {
        KnowledgeUnit unit = newUnit("cas-concurrent");
        long version = unit.getLifecycleVersion() == null ? 0L : unit.getLifecycleVersion();

        KnowledgeUnit readerA = unitMapper.selectById(unit.getId());
        readerA.setTitle("A 的标题");
        readerA.setLifecycleVersion(version);
        assertEquals(1, unitMapper.update(readerA));

        KnowledgeUnit readerB = unitMapper.selectById(unit.getId());
        readerB.setTitle("B 的标题");
        readerB.setLifecycleVersion(version); // 旧版本
        assertEquals(0, unitMapper.update(readerB), "读后被改过，必须并发冲突");
        assertEquals("A 的标题", unitMapper.selectById(unit.getId()).getTitle());
    }

    /** 服务层：单元已在删除中，更新必须报冲突。 */
    @Test
    void serviceUpdateRejectsDeletingUnit() {
        KnowledgeUnit unit = newUnit("cas-service-deleting");
        assertEquals(1, unitMapper.markDeleting(unit.getId()));
        KnowledgeUnitUpdateRequest request = new KnowledgeUnitUpdateRequest();
        request.setTitle("新标题");
        assertThrows(BusinessException.class, () -> unitService.update(unit.getId(), request));
    }

    /** 服务层：客户端携带过期版本必须报冲突。 */
    @Test
    void serviceUpdateRejectsStaleClientVersion() {
        KnowledgeUnit unit = newUnit("cas-service-version");
        KnowledgeUnitUpdateRequest request = new KnowledgeUnitUpdateRequest();
        request.setTitle("新标题");
        request.setLifecycleVersion(999L);
        assertThrows(BusinessException.class, () -> unitService.update(unit.getId(), request));
    }

    /** 服务层：携带当前版本可成功更新，并递增版本。 */
    @Test
    void serviceUpdateSucceedsWithCurrentVersion() {
        KnowledgeUnit unit = newUnit("cas-service-ok");
        KnowledgeUnitUpdateRequest request = new KnowledgeUnitUpdateRequest();
        request.setTitle("更新后的标题");
        request.setLifecycleVersion(unit.getLifecycleVersion());
        unitService.update(unit.getId(), request);
        KnowledgeUnit after = unitMapper.selectById(unit.getId());
        assertEquals("更新后的标题", after.getTitle());
        assertTrue(after.getLifecycleVersion() > unit.getLifecycleVersion());
    }

    /** Worker 分类更新只改目标字段，不得覆盖业务状态。 */
    @Test
    void categoryUpdateDoesNotTouchBusinessStatus() {
        KnowledgeUnit unit = newUnit("cas-category");
        unitMapper.updateIngestStatus(unit.getId(), "embedding");

        assertEquals(1, unitMapper.updateCategory(unit.getId(), "policy"));
        KnowledgeUnit after = unitMapper.selectById(unit.getId());
        assertEquals("policy", after.getCategory());
        assertEquals("active", after.getStatus(), "业务状态不得被分类更新覆盖");
        assertEquals("embedding", after.getIngestStatus(), "流水线状态不得被分类更新覆盖");
    }

    private KnowledgeUnit newUnit(String title) {
        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setUnitCode("U-CAS-" + System.nanoTime());
        unit.setTitle(title);
        unit.setCategory("sop");
        unit.setStatus("active");
        unit.setIngestStatus("active");
        unit.setBatchId("B-CAS-" + System.nanoTime());
        unit.setUseVlm(0);
        unitMapper.insert(unit);
        return unitMapper.selectById(unit.getId());
    }
}

package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * R-05 / R-07 回归：流式落盘、字节数组兼容入口、多实例共享卷强校验、磁盘容量保护。
 *
 * <p>纯单元测试（不启动 Spring），直接验证 {@link FileStorageService} 的行为。
 */
class FileStorageServiceTest {

    @TempDir
    Path tempDir;

    /** R-07：流式保存写临时文件后原子改名，内容一致。 */
    @Test
    void saveStreamWritesContentAndReturnsRelativePath() throws Exception {
        FileStorageService service = newService();
        String relative;
        try (InputStream in = new ByteArrayInputStream("hello-流式".getBytes(StandardCharsets.UTF_8))) {
            relative = service.saveStream("B-1", 7L, "a.md", in);
        }
        assertTrue(relative.endsWith("a.md"), relative);
        assertArrayEquals("hello-流式".getBytes(StandardCharsets.UTF_8), service.read(relative));
        // 不残留临时 .part 文件
        assertTrue(Files.list(tempDir.resolve("B-1")).noneMatch(p -> p.getFileName().toString().contains(".part-")),
                "不应残留临时文件");
    }

    /** 字节数组入口保持兼容。 */
    @Test
    void saveByteArrayStillWorks() {
        FileStorageService service = newService();
        String relative = service.save("B-2", 8L, "b.txt", "bytes".getBytes(StandardCharsets.UTF_8));
        assertArrayEquals("bytes".getBytes(StandardCharsets.UTF_8), service.read(relative));
    }

    /** R-05：多实例但未声明共享卷 → 启动自检失败（不 warn）。 */
    @Test
    void multiInstanceWithoutSharedVolumeFailsFast() throws Exception {
        FileStorageService service = newService();
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", false);
        assertThrows(IllegalStateException.class, service::validateStorage);
    }

    /** R-05/R-02：多实例 + 声明共享卷 + 稳定 node-id + 完整成员名单 + 目录可读写 → 探测通过。 */
    @Test
    void multiInstanceWithSharedVolumeProbesSuccessfully() throws Exception {
        FileStorageService service = newService();
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", true);
        setField(service, "configuredNodeId", "node-a");
        setField(service, "expectedNodeIdsRaw", "node-a,node-b");
        service.validateStorage();
    }

    /** R-05：单实例恒视为集群就绪（不因缺少对端 marker 而暂停作业分配）。 */
    @Test
    void singleInstanceIsAlwaysClusterReady() {
        assertTrue(newService().isClusterReady());
    }

    /** R-02：单实例（multi-instance=false，默认）不受成员名单门禁影响——空名单也能正常启动。 */
    @Test
    void singleInstanceUnaffectedByEmptyMemberList() throws Exception {
        FileStorageService service = newService();
        // 默认 multiInstance=false，且名单为空：不得因 R-02 门禁而启动失败
        service.validateStorage();
        assertTrue(service.isClusterReady(), "单实例必须能正常启动并就绪");
    }

    /** R-02：multi-instance=true 但预期名单为空 → 启动失败（fail-closed，不再仅 WARN）。 */
    @Test
    void multiInstanceEmptyExpectedNodeIdsFailsStartup() throws Exception {
        FileStorageService service = newService();
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", true);
        setField(service, "configuredNodeId", "node-a");
        setField(service, "expectedNodeIdsRaw", "");
        assertThrows(IllegalStateException.class, service::validateStorage);
    }

    /** R-02：multi-instance=true 但名单不含本节点 → 启动失败。 */
    @Test
    void multiInstanceExpectedNodeIdsMissingSelfFailsStartup() throws Exception {
        FileStorageService service = newService();
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", true);
        setField(service, "configuredNodeId", "node-a");
        setField(service, "expectedNodeIdsRaw", "node-b,node-c");
        assertThrows(IllegalStateException.class, service::validateStorage);
    }

    /** R-02：multi-instance=true 但名单含重复项 → 启动失败。 */
    @Test
    void multiInstanceExpectedNodeIdsWithDuplicateFailsStartup() throws Exception {
        FileStorageService service = newService();
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", true);
        setField(service, "configuredNodeId", "node-a");
        setField(service, "expectedNodeIdsRaw", "node-a,node-b,node-b");
        assertThrows(IllegalStateException.class, service::validateStorage);
    }

    /** R-02：multi-instance=true 但名单仅含本节点（无对端）→ 启动失败。 */
    @Test
    void multiInstanceExpectedNodeIdsOnlySelfFailsStartup() throws Exception {
        FileStorageService service = newService();
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", true);
        setField(service, "configuredNodeId", "node-a");
        setField(service, "expectedNodeIdsRaw", "node-a");
        assertThrows(IllegalStateException.class, service::validateStorage);
    }

    /** R-02：multi-instance=true 但未配置稳定 node-id → 启动失败。 */
    @Test
    void multiInstanceMissingStableNodeIdFailsStartup() throws Exception {
        FileStorageService service = newService();
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", true);
        setField(service, "configuredNodeId", "");
        setField(service, "expectedNodeIdsRaw", "node-a,node-b");
        assertThrows(IllegalStateException.class, service::validateStorage);
    }

    /**
     * R-02：即便绕过启动（如运行期配置漂移），名单不合法时 {@link FileStorageService#refreshPeerProbe()}
     * 也 fail-closed 返回未就绪，禁止领取入库作业（不再退化为「看到任意 peer 就 ready」的弱探测）。
     */
    @Test
    void invalidMemberListMakesRefreshFailClosed() throws Exception {
        FileStorageService nodeA = newNodeAtExpected(tempDir, "node-a", "");
        Path dir = tempDir.resolve(".kb-shared-nodes");
        Files.createDirectories(dir);
        // 即使存在一个新鲜 peer marker，空名单也不得据此判定就绪
        Files.writeString(dir.resolve("node-b.probe"), "node-b|tok|" + System.currentTimeMillis());
        assertFalse(nodeA.refreshPeerProbe(), "空名单必须 fail-closed，不得退化为弱探测");
    }

    /** R-05：同一共享卷上的两个节点互相发现 marker → 双方均确认跨节点可见。 */
    @Test
    void crossNodeProbeDetectsPeerOnSharedVolume() throws Exception {
        String expected = "node-a,node-b";
        FileStorageService nodeA = newNodeAtExpected(tempDir, "node-a", expected);
        FileStorageService nodeB = newNodeAtExpected(tempDir, "node-b", expected);

        // 首节点启动时尚无对端：不抛异常，但集群未就绪（入库作业分配应暂停）
        nodeA.validateStorage();
        assertFalse(nodeA.isClusterReady(), "尚无其他节点 marker 时不得视为跨节点可见");

        // 第二个节点启动，能读到 A 的 marker → 就绪
        nodeB.validateStorage();
        assertTrue(nodeB.isClusterReady(), "B 应能发现 A 写入的 marker");

        // A 重新探测应能看到 B 的 marker
        assertTrue(nodeA.refreshPeerProbe(), "A 应能发现 B 写入的 marker");
    }

    /** R-05：两节点挂载到不同本地盘（不同目录）→ 互相看不到对方 marker → 均不就绪。 */
    @Test
    void crossNodeProbeFailsWhenMountsDiffer() throws Exception {
        String expected = "node-a,node-b";
        FileStorageService nodeA = newNodeAtExpected(tempDir.resolve("a"), "node-a", expected);
        FileStorageService nodeB = newNodeAtExpected(tempDir.resolve("b"), "node-b", expected);

        nodeA.validateStorage();
        nodeB.validateStorage();

        assertFalse(nodeA.isClusterReady(), "不同挂载不得误判为共享可见");
        assertFalse(nodeB.isClusterReady(), "不同挂载不得误判为共享可见");
    }

    /** R-05：对端 marker 新鲜度优先取显式心跳时间戳（而非文件 mtime）。 */
    @Test
    void peerMarkerFreshnessUsesExplicitHeartbeatTimestamp() throws Exception {
        FileStorageService nodeB = newNodeAtExpected(tempDir, "node-b", "node-a,node-b");
        Path dir = tempDir.resolve(".kb-shared-nodes");
        Files.createDirectories(dir);
        Path peer = dir.resolve("node-a.probe");

        // 显式时间戳远超保鲜窗口（默认 300s）→ 视为陈旧，不得据此判定跨节点可见
        Files.writeString(peer, "node-a|tok|" + (System.currentTimeMillis() - 600_000L));
        assertFalse(nodeB.refreshPeerProbe(), "陈旧心跳不应判定为跨节点可见");

        // 刷新为当前时间戳 → 可见
        Files.writeString(peer, "node-a|tok|" + System.currentTimeMillis());
        assertTrue(nodeB.refreshPeerProbe(), "新鲜心跳应判定为跨节点可见");
    }

    /**
     * R-05：配置预期成员集合后，只有「预期集合内全部节点可见」才就绪；分区子集不得各自 ready。
     *
     * <p>三节点集群预期 {node-a, node-b, node-c}：a、b 只互相可见时都不得就绪，直到 c 也出现。
     */
    @Test
    void expectedMemberSetRequiresAllNodesVisible() throws Exception {
        String expected = "node-a,node-b,node-c";
        FileStorageService nodeA = newNodeAtExpected(tempDir, "node-a", expected);
        FileStorageService nodeB = newNodeAtExpected(tempDir, "node-b", expected);
        FileStorageService nodeC = newNodeAtExpected(tempDir, "node-c", expected);

        nodeA.validateStorage();
        assertFalse(nodeA.isClusterReady(), "仅本节点时不得就绪");

        nodeB.validateStorage();
        assertFalse(nodeB.isClusterReady(), "只见 a（缺 c）不得就绪");

        // a 重探：此时只有 b 可见，仍缺 c → 不得就绪（这正是「看到任意 peer 就 ready」的缺陷）
        assertFalse(nodeA.refreshPeerProbe(), "预期集合未全可见不得就绪（分区子集防护）");

        nodeC.validateStorage();
        assertTrue(nodeC.isClusterReady(), "三节点全部可见后就绪");

        assertTrue(nodeA.refreshPeerProbe(), "a 看到 b、c 全部后应就绪");
    }

    /** R-05：预期集合内某节点 marker 陈旧（对端下线）→ 不满足全部可见 → 不 ready。 */
    @Test
    void expectedMemberSetNotReadyWhenPeerMarkerStale() throws Exception {
        String expected = "node-a,node-b";
        FileStorageService nodeA = newNodeAtExpected(tempDir, "node-a", expected);
        Path dir = tempDir.resolve(".kb-shared-nodes");
        Files.createDirectories(dir);
        // b 的 marker 陈旧（超过保鲜窗口）
        Files.writeString(dir.resolve("node-b.probe"),
                "node-b|tok|" + (System.currentTimeMillis() - 600_000L));

        assertFalse(nodeA.refreshPeerProbe(), "预期对端 marker 陈旧（下线）时不得就绪");
    }

    /** R-05：预期名单解析对空格/空项/自身项健壮——仅保留其他节点。 */
    @Test
    void expectedNodeIdsParsingToleratesWhitespaceAndEmptyEntries() throws Exception {
        // 空格、空项、本节点项都应被过滤，仅保留 node-b 作为预期对端
        FileStorageService nodeA = newNodeAtExpected(tempDir, "node-a", " , node-a , node-b , ");
        Path dir = tempDir.resolve(".kb-shared-nodes");
        Files.createDirectories(dir);
        Files.writeString(dir.resolve("node-b.probe"), "node-b|tok|" + System.currentTimeMillis());

        assertTrue(nodeA.refreshPeerProbe(), "空格/空项/自身已过滤，仅需 node-b 可见即就绪");
    }

    /** R-02：名单仅含自身 → fail-closed（不再退化为弱探测）；即使能看到 peer 也不得就绪。 */
    @Test
    void expectedNodeIdsOnlySelfFailsClosed() throws Exception {
        FileStorageService nodeA = newNodeAtExpected(tempDir, "node-a", "node-a");
        Path dir = tempDir.resolve(".kb-shared-nodes");
        Files.createDirectories(dir);
        Files.writeString(dir.resolve("node-b.probe"), "node-b|tok|" + System.currentTimeMillis());

        assertFalse(nodeA.refreshPeerProbe(), "名单仅含自身时必须 fail-closed，不得退化为弱探测");
    }

    /** R-07：磁盘容量充足时不阻断。 */
    @Test
    void ensureCapacityPassesForSmallFile() throws Exception {
        FileStorageService service = newService();
        setField(service, "minFreeBytes", 1L);
        service.ensureCapacity(1L);
    }

    private FileStorageService newService() {
        return new FileStorageService(tempDir.toString());
    }

    private FileStorageService newNodeAt(Path root, String nodeId) throws Exception {
        FileStorageService service = new FileStorageService(root.toString());
        setField(service, "multiInstance", true);
        setField(service, "sharedVolume", true);
        setField(service, "configuredNodeId", nodeId);
        setField(service, "peerProbeIntervalMs", 0L);
        setField(service, "peerProbeMaxAgeMs", 300_000L);
        return service;
    }

    private FileStorageService newNodeAtExpected(Path root, String nodeId, String expectedNodeIds) throws Exception {
        FileStorageService service = newNodeAt(root, nodeId);
        setField(service, "expectedNodeIdsRaw", expectedNodeIds);
        return service;
    }

    private static void setField(Object target, String name, Object value) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }
}

package com.shuidun.kb.knowledge;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.FileStore;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Stream;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 上传文件本地存储（DESIGN.md §6.2「落存储」）。
 *
 * <p>本项目使用本地文件系统落盘，避免引入 MinIO 客户端依赖；生产可替换为对象存储实现。
 *
 * <p>R-07：上传改为「流式写临时文件 → 原子改名」，不再把整文件读入堆内存；
 * R-05：多实例部署强制校验共享卷（未开启或探测失败直接启动失败），并增加磁盘容量保护；
 * R-05（本轮）：增加部署级跨节点探测——各节点在共享卷写入带随机 token 与显式心跳时间戳的 marker，
 * 并读取其他节点的新鲜 marker 以确认「同一挂载跨节点可见」；无法确认时 {@link #isClusterReady()}
 * 返回 false，由入库 Worker 在领取作业前 fail-fast 停止分配，而不是仅告警后继续跑。
 *
 * <p><b>R-05 可用性权衡（fail-safe，务必知悉）：</b>多实例下，只要 {@code app.storage.expected-node-ids}
 * 预期集合内<b>缺任一</b>对端的新鲜 marker（在 {@code peer-probe-max-age-ms}（默认 300s）保鲜窗口内
 * 不可见）——对端进程死亡、网络分区、各节点时钟偏差超过保鲜窗口、或各节点实际挂载到不同本地盘——
 * 该节点就会把 {@link #clusterReady} 置为 false（判据为「预期集合全部可见」，即 {@code missing.isEmpty()}），
 * 从而<b>暂停本节点全部入库作业分配</b>（{@code IngestWorker.poll()} 直接返回，不领取任何作业）。
 * 这是「宁可暂停也不读到错误文件」的 fail-safe：2 节点集群掉 1 节点后，存活节点在超过保鲜窗口后也会
 * 停止分配入库作业，需要运维介入（恢复对端，或临时降级为单实例 {@code multi-instance=false}）。存活节点
 * 会持续刷新自身 marker，对端恢复后无需重启即可自动恢复作业分配。
 *
 * <p><b>实现程度（partial）：</b>跨节点可见性依赖各节点互相发现 marker，属于「最终一致」的部署级探测，
 * 不是原子 CAS。{@code app.storage.expected-node-ids} 预期成员集合为<b>多实例强制门禁</b>：
 * <b>R-02 起</b>当 {@code app.storage.multi-instance=true} 时必须同时提供跨重启稳定的
 * {@code app.storage.node-id}，且预期名单<b>非空、无重复、含本节点、至少含一个对端</b>，否则
 * {@link #validateStorage()} 启动直接失败；即便绕过启动（或运行期配置漂移），{@link #refreshPeerProbe()}
 * 也会 fail-closed 返回未就绪，从而禁止本节点领取任何入库作业。<b>不再</b>退化为「看到任意一个 peer
 * 就 ready」的弱探测（该弱探测无法识别分区子集，两组各自看到 peer、却互不可见时会各自误判就绪）。
 * marker 新鲜度优先取内容中的显式心跳时间戳（由写入方时钟产生），因此节点间需保证 NTP 同步，否则
 * 时钟偏差仍可能误判。真正的对象存储实现超出本轮范围。
 *
 * <p><b>首发建议：</b>若暂时无法满足「稳定 node-id + 完整成员名单 + 所有节点对同一卷可见」，请保持
 * 单实例（{@code multi-instance=false}）或把上传文件改为集中式对象存储，切勿以「名单留空 + 多实例」
 * 的方式上线。
 */
@Slf4j
@Component
public class FileStorageService {

    /** 共享卷跨节点探测 marker 目录（位于上传根目录下的隐藏子目录）。 */
    private static final String NODE_PROBE_DIR = ".kb-shared-nodes";

    private final Path root;

    /**
     * 多实例部署标记（F-13 / R-05）。
     *
     * <p>为 true 时启动强制校验：必须显式声明共享卷且探测通过，否则启动失败——绝不静默让
     * 节点 B 领取节点 A 上传的作业时读不到文件。
     */
    @Value("${app.storage.multi-instance:false}")
    private boolean multiInstance;

    /**
     * 共享卷显式声明（R-05）：运维确认上传目录已挂载为所有 Java 节点可见的共享卷/对象存储。
     *
     * <p>多实例部署必须为 true，作为「确实共享」的人工契约；配合启动期读写探测，避免把本地目录
     * 误配为多实例共享路径。
     */
    @Value("${app.storage.shared-volume:false}")
    private boolean sharedVolume;

    /** 落盘前要求的最小剩余空间（字节）；低于该阈值拒绝上传（R-07 磁盘容量保护）。 */
    @Value("${app.storage.min-free-bytes:1073741824}")
    private long minFreeBytes;

    /**
     * 本节点标识（R-05 跨节点探测）：多实例<b>必须显式注入跨重启稳定的 ID</b>（R-02 起为强制门禁）；
     * 单实例可留空，缺省用 主机名+随机后缀（随机值不参与任何跨节点一致性判定）。
     */
    @Value("${app.storage.node-id:}")
    private String configuredNodeId;

    /** R-05 其他节点 marker 的保鲜时长（毫秒）：超过该时长视为陈旧，不计入可见性判定。 */
    @Value("${app.storage.peer-probe-max-age-ms:300000}")
    private long peerProbeMaxAgeMs;

    /** R-05 跨节点探测最小刷新间隔（毫秒）：避免每次取用都扫描 marker 目录。 */
    @Value("${app.storage.peer-probe-interval-ms:10000}")
    private long peerProbeIntervalMs;

    /**
     * R-05/R-02 预期集群节点 ID 集合（逗号分隔）：多实例<b>强制门禁</b>——必须非空、无重复、含本节点、
     * 且至少含一个对端，并与部署副本成员集合一致。配置后要求「全部预期节点对同一卷可见」才置就绪，
     * 从而识别分区子集（两组各自看到 peer、却互不可见的孤岛各自 ready 的缺陷）。
     *
     * <p>R-02：不再退化为「看到任意一个新鲜 peer」的弱探测；多实例下名单缺失/仅含自身/含重复时
     * 启动失败（见 {@link #validateStorage()}），运行期 {@link #refreshPeerProbe()} 亦 fail-closed。
     */
    @Value("${app.storage.expected-node-ids:}")
    private String expectedNodeIdsRaw;

    /** 本节点标识（延迟解析，便于测试注入）。 */
    private String nodeId;

    /** R-05 集群共享卷是否已确认跨节点可见（volatile：多线程读，后台刷新写）。 */
    private volatile boolean clusterReady;

    /** 上次跨节点探测时间（毫秒）。 */
    private volatile long lastPeerProbeAt;

    /**
     * 构造存储服务。
     *
     * @param rootPath 存储根目录
     */
    public FileStorageService(@Value("${app.storage.local-path:./data/uploads}") String rootPath) {
        this.root = Paths.get(rootPath).toAbsolutePath().normalize();
    }

    /**
     * 启动期存储自检（F-13 / R-05 / R-02）：多实例下强制共享卷声明 + 稳定 node-id + 完整成员名单 +
     * 本节点读写探测 + 跨节点可见性探测。
     *
     * <p><b>R-02 fail-closed：</b>多实例（{@code multi-instance=true}）时必须提供跨重启稳定的
     * {@code node-id} 与<b>非空、无重复、含本节点、至少含一个对端</b>的
     * {@code expected-node-ids}；任一不满足则<b>启动直接失败</b>（不再仅 WARN）。这样「名单留空 +
     * 多实例」的危险拓扑无法上线。单实例（{@code multi-instance=false}，默认）不受影响，正常启动。
     *
     * <p>跨节点探测在启动时可能尚无其他节点（滚动发布/首节点），此时不抛异常以免自锁死，
     * 而是把 {@link #clusterReady} 置为 false；由 {@link #isClusterReady()} 在后台周期刷新，
     * 入库 Worker 据此 fail-fast 暂停作业分配，直到确认跨节点可见。
     */
    @jakarta.annotation.PostConstruct
    void validateStorage() {
        if (!multiInstance) {
            clusterReady = true;
            return;
        }
        if (!sharedVolume) {
            throw new IllegalStateException(
                    "app.storage.multi-instance=true 但 app.storage.shared-volume 未开启：多实例部署必须使用"
                            + "所有节点可见的共享卷（或共享对象存储）。请将上传目录挂载为共享卷并设置"
                            + " app.storage.shared-volume=true，否则其他节点领取作业后无法读取该文件。");
        }
        // R-02 fail-closed：多实例必须有稳定 node-id 与完整成员名单，否则无法识别分区子集，禁止启动。
        String configError = memberConfigurationError();
        if (configError != null) {
            throw new IllegalStateException("R-02 fail-closed：app.storage.multi-instance=true 但 " + configError
                    + "。多实例部署必须显式配置跨重启稳定的 app.storage.node-id，以及包含全部副本、含本节点、"
                    + "无重复、至少含一个对端的 app.storage.expected-node-ids；否则无法识别网络/挂载分区子集，"
                    + "可能领取到本节点读不到的源文件。若当前无法满足，请保持单实例（multi-instance=false）"
                    + "或改用集中式对象存储。");
        }
        try {
            Files.createDirectories(root);
            Path probe = root.resolve(".kb-shared-volume-probe-" + UUID.randomUUID());
            byte[] payload = ("probe-" + System.currentTimeMillis()).getBytes(StandardCharsets.UTF_8);
            Files.write(probe, payload);
            byte[] readBack = Files.readAllBytes(probe);
            Files.deleteIfExists(probe);
            if (!java.util.Arrays.equals(payload, readBack)) {
                throw new IllegalStateException("多实例共享卷读写探测不一致 root=" + root);
            }
            log.info("多实例共享卷本节点读写探测通过 root={}", root);
        } catch (IOException ex) {
            throw new IllegalStateException("多实例共享卷探测失败 root=" + root + ": " + ex.getMessage(), ex);
        }
        refreshPeerProbe();
    }

    /**
     * R-02：校验多实例成员名单配置是否合法（fail-closed 判据，纯校验不改状态）。
     *
     * <p>单实例（{@code multi-instance=false}）恒合法（返回 null）。多实例要求：
     * <ol>
     *   <li>{@code app.storage.node-id} 已显式配置（跨重启稳定，否则名单无法稳定匹配）；</li>
     *   <li>{@code app.storage.expected-node-ids} 非空；</li>
     *   <li>名单无重复项；</li>
     *   <li>名单包含本节点；</li>
     *   <li>名单至少含一个对端节点（仅含本节点等同于弱探测，必须拒绝）。</li>
     * </ol>
     *
     * @return null 表示合法；否则返回人类可读的错误原因
     */
    private String memberConfigurationError() {
        if (!multiInstance) {
            return null;
        }
        if (configuredNodeId == null || configuredNodeId.isBlank()) {
            return "app.storage.node-id 未配置（多实例必须提供跨重启稳定的节点 ID）";
        }
        List<String> ids = rawExpectedNodeIdList();
        if (ids.isEmpty()) {
            return "app.storage.expected-node-ids 未配置";
        }
        Set<String> distinct = new HashSet<>(ids);
        if (distinct.size() != ids.size()) {
            return "app.storage.expected-node-ids 含重复项: " + ids;
        }
        String self = resolveNodeId();
        if (!distinct.contains(self)) {
            return "app.storage.expected-node-ids 不含本节点 node-id=" + self;
        }
        if (distinct.size() < 2) {
            return "app.storage.expected-node-ids 仅含本节点（至少需包含一个对端节点）";
        }
        return null;
    }

    /**
     * R-02：按逗号切分预期节点名单并去掉空白项（<b>不去重、不排除自身</b>，供重复/自身校验使用）。
     *
     * @return 原始节点 ID 列表（保留重复，便于识别重复项）
     */
    private List<String> rawExpectedNodeIdList() {
        List<String> ids = new ArrayList<>();
        if (expectedNodeIdsRaw != null) {
            for (String part : expectedNodeIdsRaw.split(",")) {
                String id = part.trim();
                if (!id.isEmpty()) {
                    ids.add(id);
                }
            }
        }
        return ids;
    }

    /**
     * R-05：判断集群共享卷是否已确认跨节点可见（供入库 Worker 在领取作业前 fail-fast）。
     *
     * <p>单实例（未开启 multi-instance）恒为 true；多实例按刷新间隔做一次跨节点探测，
     * 探测失败（无法确认其他节点看到同一挂载）返回 false，调用方应暂停作业分配。
     *
     * @return true 表示可安全在多实例下分配作业
     */
    public boolean isClusterReady() {
        if (!multiInstance) {
            return true;
        }
        long now = System.currentTimeMillis();
        if (now - lastPeerProbeAt > Math.max(peerProbeIntervalMs, 0)) {
            refreshPeerProbe();
        }
        return clusterReady;
    }

    /**
     * R-05：部署级跨节点探测——写入本节点 marker（含显式心跳时间戳），并读取其他节点的新鲜 marker。
     *
     * <p><b>R-02 fail-closed：</b>多实例下若成员名单配置不合法（缺失/仅含自身/含重复/不含本节点/
     * node-id 未配置），本方法直接置 {@link #clusterReady}=false 并返回 false，<b>禁止</b>本节点领取入库
     * 作业；不再退化为「看到任意一个新鲜 peer 就 ready」的弱探测（该弱探测无法识别分区子集，两组各自
     * 看到 peer、却互不可见时会各自误判就绪，从而读到本节点不可见的文件）。
     *
     * <p><b>成员集合校验：</b>名单合法时，只有「预期集合内<b>全部</b>其他节点都可见」才置
     * {@link #clusterReady}=true；任一预期节点缺失（对端下线、网络/挂载分区、时钟偏差过大）即置 false。
     *
     * <p>每次调用都会刷新本节点 marker 的心跳时间戳，因此存活节点不会被对端误判为陈旧；
     * 对端真正下线后其 marker 超过 {@link #peerProbeMaxAgeMs} 变陈旧，本节点随即暂停作业分配
     * （见类注释「R-05 可用性权衡」）。
     *
     * @return true 表示已确认跨节点可见（且满足预期成员集合）
     */
    synchronized boolean refreshPeerProbe() {
        lastPeerProbeAt = System.currentTimeMillis();
        String self = resolveNodeId();
        // R-02 fail-closed：成员名单不合法 → 直接未就绪，禁止领取作业（不再弱探测）
        String configError = memberConfigurationError();
        if (configError != null) {
            clusterReady = false;
            log.warn("R-02 fail-closed：多实例成员名单不合法 node={} reason={}，已禁止本节点领取入库作业。",
                    self, configError);
            return false;
        }
        try {
            Path dir = root.resolve(NODE_PROBE_DIR);
            Files.createDirectories(dir);
            // 写入本节点 marker：内容 = nodeId|随机token|显式心跳时间戳，供对端读取确认
            Path selfMarker = dir.resolve(self + ".probe");
            String token = self + "|" + UUID.randomUUID() + "|" + System.currentTimeMillis();
            Files.write(selfMarker, token.getBytes(StandardCharsets.UTF_8));

            Set<String> visiblePeers = new HashSet<>();
            long now = System.currentTimeMillis();
            try (Stream<Path> files = Files.list(dir)) {
                for (Path marker : (Iterable<Path>) files::iterator) {
                    String name = marker.getFileName().toString();
                    if (!name.endsWith(".probe")) {
                        continue;
                    }
                    String peerNode = name.substring(0, name.length() - ".probe".length());
                    if (peerNode.equals(self)) {
                        continue;
                    }
                    String content = Files.readString(marker, StandardCharsets.UTF_8).trim();
                    if (content.isEmpty()) {
                        continue;
                    }
                    long age = peerMarkerAgeMs(marker, content, now);
                    if (age > peerProbeMaxAgeMs) {
                        continue;
                    }
                    log.info("检测到其他节点共享卷 marker node={} ageMs={}", peerNode, age);
                    visiblePeers.add(peerNode);
                }
            }

            Set<String> expectedPeers = expectedPeerNodeIds(self);
            boolean ready;
            if (!expectedPeers.isEmpty()) {
                Set<String> missing = new HashSet<>(expectedPeers);
                missing.removeAll(visiblePeers);
                ready = missing.isEmpty();
                if (!ready) {
                    log.warn("多实例共享卷未满足预期成员集合 node={} expected={} visible={} missing={}："
                                    + "可能是分区子集、对端下线或各节点挂载到不同本地盘。入库作业分配已暂停。",
                            self, expectedPeers, visiblePeers, missing);
                }
            } else {
                // 单实例（multiInstance=false）恒就绪；多实例空名单已在上方 fail-closed 拦截，不会走到这里。
                ready = true;
            }
            clusterReady = ready;
            return ready;
        } catch (IOException ex) {
            clusterReady = false;
            log.warn("跨节点共享卷探测失败 root={}: {}", root, ex.getMessage());
            return false;
        }
    }

    /**
     * R-05：解析预期集群节点集合（排除本节点）。
     *
     * @param self 本节点 id
     * @return 除本节点外的预期 peer 集合；未配置时为空集合
     */
    private Set<String> expectedPeerNodeIds(String self) {
        Set<String> expected = new HashSet<>();
        for (String id : rawExpectedNodeIdList()) {
            if (!id.equals(self)) {
                expected.add(id);
            }
        }
        return expected;
    }

    /**
     * R-05：计算对端 marker 的新鲜度（毫秒）。优先使用 marker 内容里显式写入的心跳时间戳
     * （格式 {@code nodeId|token|epochMs}），解析失败时退回文件 mtime。
     *
     * <p><b>时钟偏差说明：</b>时间戳由写入方时钟产生，若各节点时钟偏差超过保鲜窗口仍可能误判；
     * 生产须保证节点间 NTP 同步。相较仅用 mtime，显式时间戳不依赖网络文件系统的 mtime 粒度与语义。
     *
     * @param marker  marker 文件
     * @param content marker 内容
     * @param now     本地当前时间（毫秒）
     * @return 距今毫秒数；无法判定时返回 {@link Long#MAX_VALUE}（视为陈旧）
     */
    private long peerMarkerAgeMs(Path marker, String content, long now) {
        int sep = content.lastIndexOf('|');
        if (sep >= 0 && sep < content.length() - 1) {
            try {
                long stamp = Long.parseLong(content.substring(sep + 1).trim());
                return now - stamp;
            } catch (NumberFormatException ignored) {
                // 内容无有效时间戳：退回文件 mtime
            }
        }
        try {
            return now - Files.getLastModifiedTime(marker).toMillis();
        } catch (IOException ex) {
            return Long.MAX_VALUE;
        }
    }

    /** 解析本节点标识：优先配置值，否则用 主机名+随机后缀。 */
    private String resolveNodeId() {
        if (nodeId == null) {
            if (configuredNodeId != null && !configuredNodeId.isBlank()) {
                nodeId = configuredNodeId.trim();
            } else {
                String host;
                try {
                    host = InetAddress.getLocalHost().getHostName();
                } catch (java.net.UnknownHostException ex) {
                    host = "unknown-host";
                }
                nodeId = host + "-" + UUID.randomUUID().toString().substring(0, 8);
            }
        }
        return nodeId;
    }

    /**
     * 保存上传文件（字节数组兼容入口），返回相对路径。
     *
     * @param batchId      批次 id
     * @param unitId       unit id
     * @param originalName 原始文件名
     * @param content      文件字节
     * @return 相对存储路径
     */
    public String save(String batchId, Long unitId, String originalName, byte[] content) {
        return saveStream(batchId, unitId, originalName, new ByteArrayInputStream(content));
    }

    /**
     * R-07 流式保存上传文件：写入临时文件后原子改名，全程不把整文件读入堆内存。
     *
     * <p>失败时删除临时文件，不留下半成品；调用方负责在事务回滚时补偿删除最终文件。
     *
     * @param batchId      批次 id
     * @param unitId       unit id
     * @param originalName 原始文件名
     * @param in           输入流（由调用方负责关闭）
     * @return 相对存储路径
     */
    public String saveStream(String batchId, Long unitId, String originalName, InputStream in) {
        Path temp = null;
        try {
            Path dir = root.resolve(sanitize(batchId));
            Files.createDirectories(dir);
            Path target = dir.resolve(unitId + "_" + sanitize(originalName));
            temp = dir.resolve(target.getFileName() + ".part-" + UUID.randomUUID());
            Files.copy(in, temp, StandardCopyOption.REPLACE_EXISTING);
            try {
                Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (AtomicMoveNotSupportedException ex) {
                Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING);
            }
            String relative = root.relativize(target).toString().replace('\\', '/');
            log.info("stored upload file unitId={} path={}", unitId, relative);
            return relative;
        } catch (IOException ex) {
            deleteQuietly(temp);
            throw new BusinessException(ErrorCode.SYSTEM_ERROR, "文件落盘失败: " + ex.getMessage());
        }
    }

    /**
     * R-07 磁盘容量保护：落盘前校验可用空间是否充足。
     *
     * @param incomingBytes 本次将要写入的字节数
     */
    public void ensureCapacity(long incomingBytes) {
        try {
            Files.createDirectories(root);
            FileStore store = Files.getFileStore(root);
            long usable = store.getUsableSpace();
            if (usable - Math.max(incomingBytes, 0) < minFreeBytes) {
                throw new BusinessException(ErrorCode.SYSTEM_ERROR, "上传目录可用空间不足，请清理磁盘后重试");
            }
        } catch (IOException ex) {
            // 部分文件系统不支持容量探测：不阻断上传，仅告警
            log.warn("磁盘容量探测失败 root={}: {}", root, ex.getMessage());
        }
    }

    /**
     * 读取已保存文件。
     *
     * @param relativePath 相对路径
     * @return 文件字节
     */
    public byte[] read(String relativePath) {
        try {
            return Files.readAllBytes(resolveStored(relativePath));
        } catch (IOException ex) {
            throw new BusinessException(ErrorCode.SYSTEM_ERROR, "读取上传文件失败: " + ex.getMessage());
        }
    }

    /**
     * R-04：返回已保存对象的**实际存储长度**（字节），作为解析内存准入的唯一依据。
     *
     * <p>本地文件用 {@link Files#size}；若未来替换为对象存储，必须以 HEAD/metadata 返回
     * Content-Length，且<b>无法确认长度时必须抛异常</b>（不得返回 0 或 -1 让调用方误判为
     * 「无需内存预算」）。DB 中的 {@code file_size} 仅作一致性检查，不作为准入依据。
     *
     * @param relativePath 相对路径
     * @return 实际字节长度（调用方须校验 &gt;0）
     * @throws BusinessException 路径非法或无法确认长度（fail-closed）
     */
    public long storedSize(String relativePath) {
        if (relativePath == null || relativePath.isBlank()) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "存储路径为空，无法确认对象大小");
        }
        Path target = resolveStored(relativePath);
        try {
            return Files.size(target);
        } catch (IOException ex) {
            throw new BusinessException(ErrorCode.SYSTEM_ERROR, "无法确认存储对象大小: " + ex.getMessage());
        }
    }

    /** 解析并校验存储相对路径：拒绝越界路径（含 null 由调用方先行校验）。 */
    private Path resolveStored(String relativePath) {
        Path target = root.resolve(relativePath).normalize();
        if (!target.startsWith(root)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "非法存储路径");
        }
        return target;
    }

    /**
     * 复制到目标路径（供测试或迁移）。
     *
     * @param relativePath 相对路径
     * @param target       目标文件
     */
    public void copyTo(String relativePath, Path target) {
        try {
            Files.copy(root.resolve(relativePath).normalize(), target, StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException ex) {
            throw new BusinessException(ErrorCode.SYSTEM_ERROR, "复制上传文件失败: " + ex.getMessage());
        }
    }

    /**
     * 补偿删除已保存文件（F-13 / R-07）。
     *
     * <p>DB 事务回滚或入队失败时调用，避免留下未被任何记录引用的孤儿文件；文件本就不存在、
     * 路径非法或删除失败都只记日志，不向上抛异常（补偿路径不应掩盖原始失败）。
     *
     * @param relativePath 相对存储路径；null/空则直接返回
     */
    public void deleteQuietly(String relativePath) {
        if (relativePath == null || relativePath.isBlank()) {
            return;
        }
        deleteQuietly(root.resolve(relativePath).normalize());
    }

    /** 删除绝对路径文件（补偿用，静默失败）。 */
    private void deleteQuietly(Path target) {
        if (target == null) {
            return;
        }
        try {
            if (!target.startsWith(root)) {
                log.warn("跳过非法存储路径的补偿删除 path={}", target);
                return;
            }
            boolean removed = Files.deleteIfExists(target);
            log.info("compensating delete upload file path={} removed={}", target, removed);
        } catch (IOException ex) {
            log.warn("补偿删除上传文件失败 path={} err={}", target, ex.getMessage());
        }
    }

    private String sanitize(String name) {
        if (name == null || name.isBlank()) {
            return "unnamed";
        }
        return name.replaceAll("[^A-Za-z0-9._\\-\\u4e00-\\u9fa5]", "_");
    }
}

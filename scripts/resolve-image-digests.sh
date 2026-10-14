#!/usr/bin/env bash
# =============================================================================
# 解析并记录镜像 digest（P1-2）
#
# 用途：把发布涉及的镜像 tag 解析为**不可变 digest**，产出可直接粘入
#       docs/operations/release-manifest-template.md 的清单行。
#
# 用法：
#   scripts/resolve-image-digests.sh                     # 解析内置默认镜像清单
#   scripts/resolve-image-digests.sh mysql:8.4 redis:7   # 解析指定镜像
#   RELEASE_TAG=v1.2.3 scripts/resolve-image-digests.sh  # 解析本项目镜像到指定 tag
#
# 解析策略（按可用性自动降级）：
#   1. docker buildx imagetools inspect <img> --format '{{json .Manifest.Digest}}'
#      —— 远程解析，**无需先 pull**，最推荐；
#   2. docker inspect --format '{{index .RepoDigests 0}}'
#      —— 仅对本机已 pull 的镜像有效（离线兜底）；
#   3. 无 Docker：打印「需由 CI 产出」提示，不伪造 digest。
#
# 输出：`<image>|<tag>|<digest 或 (UNRESOLVED)>|<来源>`
#
# ⚠️ 生产按 digest 拉取；本脚本只负责解析/记录，不做部署。
# =============================================================================
set -u

RELEASE_TAG="${RELEASE_TAG:-local}"

DEFAULT_IMAGES=(
  "aftersales-kb-java:${RELEASE_TAG}"
  "aftersales-kb-ai:${RELEASE_TAG}"
  "aftersales-kb-frontend:${RELEASE_TAG}"
  "nginx:1.27-alpine"
  "mysql:8.4"
  "redis:7-alpine"
  "quay.io/coreos/etcd:v3.5.16"
  "minio/minio:RELEASE.2024-12-18T13-15-44Z"
  "milvusdb/milvus:v2.4.17"
)

if [ "$#" -gt 0 ]; then
  IMAGES=("$@")
else
  IMAGES=("${DEFAULT_IMAGES[@]}")
fi

has_docker=0
if command -v docker >/dev/null 2>&1; then
  has_docker=1
fi

resolve_one() {
  local img="$1"
  local digest="" source=""

  # 1) 远程解析（首选）
  if [ "$has_docker" -eq 1 ]; then
    digest="$(docker buildx imagetools inspect "$img" --format '{{json .Manifest.Digest}}' 2>/dev/null | tr -d '"' | tr -d '\r')"
    if [ -n "$digest" ] && [ "$digest" != "null" ]; then
      echo "${img}|${img##*:}|${digest}|buildx-imagetools"
      return
    fi
  fi

  # 2) 本机已 pull 镜像的 RepoDigests 兜底
  if [ "$has_docker" -eq 1 ]; then
    local repodigest
    repodigest="$(docker inspect --format '{{index .RepoDigests 0}}' "$img" 2>/dev/null | tr -d '\r')"
    if [ -n "$repodigest" ]; then
      echo "${img}|${img##*:}|${repodigest#*@}|docker-inspect-local"
      return
    fi
  fi

  # 3) 无法解析
  echo "${img}|${img##*:}|(UNRESOLVED)|no-docker-or-not-pulled"
}

echo "# image|tag|digest|source   (生成时间: $(date -u +%Y-%m-%dT%H:%M:%SZ), RELEASE_TAG=${RELEASE_TAG})"
if [ "$has_docker" -eq 0 ]; then
  echo "# ⚠️ 本机无 docker：以下 digest 无法解析，请在 CI（含 docker buildx）中运行本脚本产出并归档。" >&2
fi

unresolved=0
for img in "${IMAGES[@]}"; do
  line="$(resolve_one "$img")"
  echo "$line"
  case "$line" in
    *UNRESOLVED*) unresolved=$((unresolved + 1)) ;;
  esac
done

if [ "$unresolved" -gt 0 ]; then
  echo "# 提示：${unresolved} 个镜像未解析到 digest —— 请在有 docker/网络的环境或 CI 中重跑。" >&2
  exit 1
fi
exit 0

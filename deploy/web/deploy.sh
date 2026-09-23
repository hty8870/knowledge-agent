#!/usr/bin/env bash
# BioData Agent 网页版部署/回退脚本（2026-08-25）
# 安装位置：/opt/biodata-web/deploy.sh（与 deploy/web/deploy.sh 同源）。
#
# 用法：sudo /opt/biodata-web/deploy.sh <image-tag>
# 日常远程发布经受限 deploy 账号直接 sudo 调用本脚本（sudoers 白名单三条之一，2026-08-30 起）。
#
# 行为（幂等，重复执行同一 tag = 重新 up -d 并重新等 healthy）：
#   1. 校验镜像 biodata-web:<tag> 已存在于本机（部署从不隐式 pull，版本真源是本机镜像库）；
#   2. 写入 /opt/biodata-web/.env 的 IMAGE_TAG；
#   3. docker compose up -d，轮询容器 healthcheck 直到 healthy（上限 120s），
#      再断言容器内 /api/health 的 account.required==true（公网护栏确实开着，2026-08-26）；
#   4. 任一失败 → 自动写回上一 tag、重新 up（自动回退），非零退出；
#   5. 成功 → 追加 RELEASES.log（时间/当前 tag/上一 tag），清理只保留最近 5 个 biodata-web 镜像。
set -euo pipefail

TAG_RE='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[deploy] 必须以 root 运行（远程 deploy 账号经 sudoers 白名单调用）。" >&2
  exit 2
fi

# 部署根目录可用 BIODATA_DEPLOY_DIR 覆盖（默认 /opt/biodata-web，与生产布局一致）。
BASE="${BIODATA_DEPLOY_DIR:-/opt/biodata-web}"
ENV_FILE="$BASE/.env"
COMPOSE_FILE="$BASE/docker-compose.web.yml"
RELEASES_LOG="$BASE/RELEASES.log"
CONTAINER=biodata-web
HEALTH_TIMEOUT=120
HEALTH_INTERVAL=5
KEEP_IMAGES=5

new_tag="${1:-}"
if [ -z "$new_tag" ] || ! [[ "$new_tag" =~ $TAG_RE ]]; then
  echo "usage: $0 <valid-image-tag>" >&2
  exit 2
fi

[ -f "$ENV_FILE" ] || { echo "[deploy] 缺少 $ENV_FILE" >&2; exit 2; }
[ -f "$COMPOSE_FILE" ] || { echo "[deploy] 缺少 $COMPOSE_FILE" >&2; exit 2; }

compose() {
  /usr/bin/docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -p biodata-web "$@"
}

# 只读 IMAGE_TAG 行（不打印 .env 其他内容——那里有密钥）
current_tag() {
  grep -E '^IMAGE_TAG=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true
}

write_image_tag() {
  local tag="$1" tmp
  [[ "$tag" =~ $TAG_RE ]] || return 2
  tmp="$(mktemp "$BASE/.env.tmp.XXXXXX")"
  if ! awk -v tag="$tag" '
      BEGIN { written = 0 }
      /^IMAGE_TAG=/ {
        if (!written) { print "IMAGE_TAG=" tag; written = 1 }
        next
      }
      { print }
      END { if (!written) print "IMAGE_TAG=" tag }
    ' "$ENV_FILE" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 600 "$tmp"
  chown root:root "$tmp"
  mv -f "$tmp" "$ENV_FILE"
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) status
  while [ "$SECONDS" -lt "$deadline" ]; do
    status="$(/usr/bin/docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
    if [ "$status" = "healthy" ]; then
      return 0
    fi
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# 部署后断言（公网护栏硬化，2026-08-26）：healthy 只证明进程活着，不证明账号护栏开着——
# 漏配 BIODATA_REQUIRE_ACCOUNT 的镜像/环境会把公网实例跑成无认证形态。容器内 python 自检
# health 的 account.required==true（镜像内无 curl，与 healthcheck 同径；容器内 loopback
# 天然过 Host 守卫，无需带白名单 Host 头）。断言失败 → 走与 wait_healthy 失败相同的自动回退。
assert_guard_required() {
  /usr/bin/docker exec "$CONTAINER" python -c \
    "import json, sys, urllib.request; d = json.load(urllib.request.urlopen('http://127.0.0.1:8510/api/health', timeout=5)); sys.exit(0 if (d.get('account') or {}).get('required') is True else 1)"
}

# 只保留最近 KEEP_IMAGES 个 biodata-web 镜像（按创建时间新→旧、同 ID 去重）。
# 当前部署的 tag 恒保留；rmi 被拒（占用/共享 ID）只告警不失败。
prune_images() {
  local deployed targets t
  deployed="$(current_tag)"
  targets="$(/usr/bin/docker images --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}\t{{.ID}}' biodata-web \
    | sort -r | awk -F'\t' '!seen[$3]++' | tail -n +$((KEEP_IMAGES + 1)) | cut -f2 || true)"
  [ -n "$targets" ] || return 0
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if [ "$t" = "biodata-web:$deployed" ]; then
      echo "[deploy] 保留当前部署镜像 $t"
      continue
    fi
    if /usr/bin/docker image rm "$t" >/dev/null 2>&1; then
      echo "[deploy] 清理旧镜像 $t"
    else
      echo "[deploy] 跳过 $t（被占用或与其他 tag 共享镜像 ID）"
    fi
  done <<< "$targets"
}

prev_tag="$(current_tag)"

if ! /usr/bin/docker image inspect "biodata-web:$new_tag" >/dev/null 2>&1; then
  echo "[deploy] 镜像 biodata-web:$new_tag 本机不存在；先按 deploy/web/README.md §3 构建再部署。" >&2
  exit 1
fi

echo "[deploy] biodata-web: ${prev_tag:-<none>} -> $new_tag"
write_image_tag "$new_tag"

if compose up -d && wait_healthy && assert_guard_required; then
  echo "[deploy] healthy：biodata-web:$new_tag"
  printf '%s deployed %s (previous %s)\n' "$(date '+%F %T')" "$new_tag" "${prev_tag:-none}" >> "$RELEASES_LOG"
  prune_images
  compose ps
  exit 0
fi

echo "[deploy] $new_tag 部署失败、${HEALTH_TIMEOUT}s 内未达 healthy 或护栏断言（health account.required）未过，容器最近日志：" >&2
/usr/bin/docker logs --tail 30 "$CONTAINER" 2>&1 || true

# ---- 自动回退到上一 tag ----
if [ -z "$prev_tag" ]; then
  echo "[deploy] 无上一版本可回退；停止容器退出（人工介入：docker logs $CONTAINER）。" >&2
  compose down || true
  exit 1
fi
echo "[deploy] 自动回退到 biodata-web:$prev_tag" >&2
write_image_tag "$prev_tag"
compose up -d || true
if wait_healthy && assert_guard_required; then
  echo "[deploy] 已回退并 healthy：biodata-web:$prev_tag"
  printf '%s rollback: %s failed, restored %s\n' "$(date '+%F %T')" "$new_tag" "$prev_tag" >> "$RELEASES_LOG"
else
  echo "[deploy] 回退后仍未 healthy 或护栏断言未过——立即人工介入：docker logs $CONTAINER" >&2
fi
exit 1

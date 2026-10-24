#!/usr/bin/env bash
# 在 CI runner 上启动一个本地 MinIO 并准备好 bucket。
#
# 由 object-storage 与 backup-restore 两个 job 共用：同一段启动逻辑写成两份，
# 迟早会出现“桶只建在一边、策略只清了一边”这种只在一个任务里暴露的问题。
#
# 用法：start-minio.sh <bucket> [更多 bucket...]
#   不传位置参数时读 MINIO_BUCKET，两者都没有直接报错退出。
#
# 可覆盖项（默认值与调用方原来的内联步骤逐字一致）：
#   MINIO_VERSION           MinIO release tag
#   MINIO_BINARY            MinIO 可执行文件路径
#   MINIO_DATA_DIR          数据目录
#   MINIO_LOG               日志文件
#   MINIO_ADDRESS           服务监听地址（host:port）
#   MINIO_CONSOLE_ADDRESS   控制台监听地址（host:port）
#   MINIO_ROOT_USER         根账号
#   MINIO_ROOT_PASSWORD     根密码
#
# 为什么不用 service 容器跑 MinIO：Docker Hub 上的 minio/minio 已被官方撤下，迁移后的
# 镜像源在 CI 里拉取不稳定（401 或仓库不存在），失败信息指向基础设施而不是被测代码。
# 改为直接下载 GitHub Releases 的官方二进制启动，版本仍钉在同一个 release tag 上，
# 也与本地演练用的方式一致。MinIO 只是后台进程，job 结束时随 runner 一起回收。
set -euo pipefail

if [ "$#" -eq 0 ]; then
  if [ -z "${MINIO_BUCKET:-}" ]; then
    echo "usage: start-minio.sh <bucket> [more buckets...] (or set MINIO_BUCKET)" >&2
    exit 2
  fi
  set -- "$MINIO_BUCKET"
fi

VERSION="${MINIO_VERSION:-RELEASE.2025-09-07T16-13-09Z}"
WORK="${RUNNER_TEMP:-/tmp}"
BINARY="${MINIO_BINARY:-$WORK/minio}"
DATA_DIR="${MINIO_DATA_DIR:-$WORK/minio-data}"
LOG="${MINIO_LOG:-$WORK/minio.log}"
ADDRESS="${MINIO_ADDRESS:-127.0.0.1:9000}"
CONSOLE_ADDRESS="${MINIO_CONSOLE_ADDRESS:-127.0.0.1:9001}"
ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin123}"

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$DATA_DIR"
if [ ! -x "$BINARY" ]; then
  curl -fsSL --retry 3 -o "$BINARY" \
    "https://github.com/minio/minio/releases/download/${VERSION}/minio.linux-${ARCH}.${VERSION}"
  chmod +x "$BINARY"
fi

MINIO_ROOT_USER="$ROOT_USER" MINIO_ROOT_PASSWORD="$ROOT_PASSWORD" \
  "$BINARY" server "$DATA_DIR" \
  --address "$ADDRESS" --console-address "$CONSOLE_ADDRESS" \
  > "$LOG" 2>&1 &
echo "minio started (pid $!), log: $LOG"

python - "$ADDRESS" "$ROOT_USER" "$ROOT_PASSWORD" "$@" <<'PY'
import sys
import time

import boto3

address, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
buckets = sys.argv[4:]
client = boto3.client(
    's3',
    endpoint_url=f'http://{address}',
    aws_access_key_id=user,
    aws_secret_access_key=password,
)

names = set()
for _ in range(30):
    try:
        names = {b['Name'] for b in client.list_buckets()['Buckets']}
        break
    except Exception as exc:
        print('waiting for minio:', type(exc).__name__)
        time.sleep(2)
else:
    raise SystemExit('MinIO 未在 60 秒内就绪')

for bucket in buckets:
    if bucket not in names:
        client.create_bucket(Bucket=bucket)
    # 桶必须保持私有，否则“未授权访问被拒”的断言会失去意义。
    client.delete_bucket_policy(Bucket=bucket)
    print('bucket ready:', bucket)
PY

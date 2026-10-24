#!/usr/bin/env bash
# 版本护栏的负向用例：冒充一个 PostgreSQL 17 的 pg_dump。
#
# 备份必须因为「客户端主版本与服务端不一致」而拒绝生成 dump。这一条复现的是
# 本地真实踩过的事故：17 客户端生成 dump、清单校验也全部通过，直到灌回 16 的
# 服务端才被拒绝——等到恢复阶段才发现，成本比在备份阶段就拒绝高得多。
if [ "$1" = "--version" ]; then
  echo "pg_dump (PostgreSQL) 17.0"
  exit 0
fi
exit 1

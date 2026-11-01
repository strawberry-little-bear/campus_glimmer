# 备份与恢复

<p align="right"><a href="../README.md">返回项目主页</a> · <a href="README.md">完整手册</a></p>

生产备份覆盖 PostgreSQL 业务数据和对象存储中的用户上传文件。Redis 只作为缓存和 `cached_db` 会话缓存，不是唯一业务事实源，因此不进入备份清单；Redis 自身开启 AOF，用于进程重启后的缓存连续性，但不能替代数据库备份。

> 默认备份写入宿主机目录 `./backups/`，可以再配置一个独立端点的 S3 兼容 bucket 做异地镜像。镜像不是"上传成功"就算完成：每个对象都会读回并按 manifest 里的 SHA-256 逐字节比对，远端 key 集合也要与本地归档对齐。异地目标留空即完全关闭，只填其中几项会直接报错——"以为开了异地备份"比明确没开危险得多。

## 备份内容

```text
backup-YYYYMMDDTHHMMSSZ/
├── database.dump       # pg_dump custom format
├── objects/            # 对象镜像
└── manifest.json       # 时间、bucket、prefix、大小和 SHA-256
```

`database.dump` 使用 PostgreSQL custom format，恢复时由 `pg_restore` 同时处理表、索引、约束和序列。对象镜像不是把 S3 文件平铺成没有来源的随机文件；`manifest.json` 保存原始 key 与镜像文件的对应关系，也保存每个文件的 SHA-256 和大小。

备份镜像里的 `pg_dump` / `pg_restore` 固定为 PostgreSQL 16，并与 `docker-compose.prod.yml` 的数据库主版本一致。脚本在 dump 前会读取服务端 `server_version_num`，发现客户端与服务端主版本不一致时直接终止，避免生成“看似成功、目标数据库却无法恢复”的归档。

## 备份流程

```mermaid
sequenceDiagram
    participant S as backup sidecar
    participant PG as PostgreSQL
    participant S3 as MinIO / S3
    participant D as 备份目录
    participant O as 异地 bucket
    S->>D: 创建临时目录 .backup-*
    S->>PG: pg_dump --format=custom
    PG-->>S: database.dump
    S->>S3: 列举 media/ 前缀并下载对象
    S3-->>S: 对象字节流
    S->>D: 写 objects/ 与 manifest.json
    S->>S: verify_backup 校验大小与 SHA-256
    S->>D: 原子改名为 backup-*
    S->>D: 按保留天数清理已过期本地备份
    opt 配置了 BACKUP_OFFSITE_ENDPOINT_URL
        S->>O: 上传 database.dump、manifest.json 与每个对象镜像
        S->>O: 读回每个对象，按 manifest 的 SHA-256 逐字节比对
        S->>O: 比对远端 key 集合与本地归档，缺一个都算失败
        S->>O: 按保留天数清理异地过期归档
    end
```

关键行为：

- 先写临时目录，校验通过后才原子改名，失败不会留下“看起来可用”的半成品；
- `--verify` 会逐文件检查数据库 dump 和对象镜像的大小、SHA-256；
- 对象前缀按目录边界匹配，`media` 不会误匹配 `media-other`；
- 保留期只删除名称以 `backup-` 开头且 manifest 可解析的目录；
- 异地镜像只在 `BACKUP_OFFSITE_ENDPOINT_URL` 配置后执行；只填其中几项会直接报错，不会静默跳过；
- 异地副本必须"读回比对过"：上传后重新下载、按 manifest 的 SHA-256 逐字节比对，再比对远端 key 集合，缺一个都算这次备份失败；
- 异地保留期只删每个归档自己的 `manifest.json` 里读到的创建时间已过期的目录；读不到或不可解析就跳过——宁可多留一天，也不凭 key 名猜时间去删别人的数据；
- 异地镜像失败会向上抛，`run_once` 不会因为本地备份成功就假装这次任务成功；
- 守护进程模式下这一次失败只记进日志并让 sidecar 继续存活，下一个周期才重试；这期间本地归档是完整的、异地副本是缺的，必须靠日志或告警发现，不能指望容器退出；
- 对象恢复是增量上传，不删除备份外的新对象，避免旧备份覆盖更新内容；
- 数据库与对象不是同一事务快照，极端情况下可能出现引用存在但对象缺失，或对象存在但数据库引用已删除；
- PostgreSQL 客户端主版本必须与服务端一致，升级 PostgreSQL 主版本时要同步更新镜像的 `POSTGRES_MAJOR` 并重新做恢复演练。

## 边界由测试钉住

备份脚本最容易出问题的分支不需要真实 PostgreSQL 或 MinIO 就能验证，因此它们由 campus_glimmer/test_backup_script.py 固定下来：

- URL 解析：非 PostgreSQL scheme、缺主机或缺库名必须显式失败；pg_dump 参数里不出现密码，密码只通过 PGPASSWORD 传给子进程；
- 版本护栏：客户端主版本读不出来、服务端主版本读不出来、两者不一致，都以 RuntimeError 结束，不生成目标库无法恢复的 dump；
- 校验：大小不符、SHA-256 不符、清单版本不受支持、清单不可读、清单路径越权都要报错，并一次汇总所有问题；
- 清理：只删除过期的 `backup-` 目录；manifest 不可解析的目录跳过，不会中断其余清理；
- 异地镜像：未配置返回 `None`；配了一半（有其它变量却没有端点、缺 bucket、缺任一凭证、保留期不是整数）报错；上传后读回逐字节比对；远端 key 集合缺项或多项都失败；镜像前先跑本地 `verify_backup`；保留期小于 1 天不删；坏 manifest 跳过不删；`run_once` 在镜像失败时向上抛；
- 失败路径：dump 或对象下载失败时 staging 目录必须被清空，不留下"看起来可用"的半成品。

这些用例是纯本地的：_dump_database、_download_objects 和 S3 客户端都被替换，因此不引入对数据库或对象存储的依赖。把 dump 灌回 PostgreSQL 的那一段由下面的 CI 恢复演练覆盖，正式环境的恢复演练仍要按「恢复步骤」人工执行。

## CI 恢复演练

`backup-restore` 任务把人工路径里能自动化的部分搬进了 CI：`migrate` 之后真实生成一份备份，灌进一个全新的隔离数据库，再逐项比对。它不写 fixture——库里已有的 `django_migrations`、`django_content_type` 与 `auth_permission` 就足够回答「dump 能不能原样恢复」，也不需要一份会随模型演进而过时的数据文件。

比对的是三类东西，而不是只看 `pg_restore` 有没有返回 0：

- `public` schema 下每一张表的行数，源库与恢复库逐张对齐；
- `pg_indexes` 的索引总数与 `pg_constraint` 的约束总数；
- 上传到 MinIO 的一个对象，恢复到第二个桶后按原 key 与原字节比对。

同一个任务还包含一条负向用例：往 `PATH` 最前面放一个只对 `--version` 报告 17 的假 `pg_dump`，备份必须失败。这一条复现的是本地真实踩过的事故——17 客户端生成的 dump 能通过清单校验，直到灌回 16 的服务端才被拒绝。

异地镜像没有进这个任务：CI runner 上没有第二个独立的 S3 端点，伪造一个只会把"能连上"这件事重新演一遍。它的失败分支由 `campus_glimmer/test_backup_script.py` 里的内存假客户端钉住，真实端点的连通性、账号权限和跨主机恢复耗时仍要按「恢复步骤」人工演练。

## 手动创建与校验

在 Compose 环境中执行：

```powershell
docker compose --env-file .env.production -f docker-compose.prod.yml exec backup python docker/backup.py --once
docker compose --env-file .env.production -f docker-compose.prod.yml exec backup python docker/backup.py --verify /backups/backup-YYYYMMDDTHHMMSSZ
```

把一份已经校验过的本地归档镜像到异地，或单独清理异地过期归档：

```powershell
docker compose --env-file .env.production -f docker-compose.prod.yml exec backup python docker/backup.py --mirror /backups/backup-YYYYMMDDTHHMMSSZ
docker compose --env-file .env.production -f docker-compose.prod.yml exec backup python docker/backup.py --prune-offsite
```

本地运行脚本时，需要安装 PostgreSQL 客户端并配置 `DJANGO_DATABASE_URL`、`DJANGO_S3_*` 和 `BACKUP_DIR`：

```powershell
python docker/backup.py --once
python docker/backup.py --verify backups/backup-YYYYMMDDTHHMMSSZ
```

## 恢复步骤

恢复应作为一次受控发布执行，不要在 Web 容器仍然处理写入时直接覆盖数据库。

1. 停止 Web 写入并记录当前版本、备份目录、数据库和 bucket；
2. 用 `pg_restore --clean --if-exists --no-owner --no-acl` 恢复到目标 PostgreSQL；先确认目标库和备份来源一致；
3. 执行对象增量恢复：

   ```powershell
   docker compose --env-file .env.production -f docker-compose.prod.yml exec backup `
     python docker/backup.py --restore-objects /backups/backup-YYYYMMDDTHHMMSSZ `
     --restore-bucket campus-glimmer
   ```

4. 运行 `python manage.py check` 与 `python manage.py migrate --check`；
5. 启动 Web，确认 `/healthz/` 返回 200，`/readyz/` 的 database、cache、object_storage 均为 ok；
6. 人工验证登录、商品图、收藏、预约、订单、通知和运营看板；
7. 记录恢复耗时、缺失对象、人工修复和后续改进。

对象恢复不会删除目标 bucket 中不属于备份的文件。若目标环境有残留对象，应先明确清理范围，不要用“恢复”命令顺便做删除。

## 保留与异地策略

Compose 示例默认 `BACKUP_RETENTION_DAYS=14`、`BACKUP_INTERVAL_SECONDS=86400`，异地镜像默认关闭。启用异地需要七项，缺一项就会在启动时报错：

| 变量 | 作用 |
| --- | --- |
| `BACKUP_OFFSITE_ENDPOINT_URL` | 异地 S3 兼容端点。留空即完全关闭异地镜像 |
| `BACKUP_OFFSITE_BUCKET` | 异地 bucket |
| `BACKUP_OFFSITE_ACCESS_KEY_ID` / `BACKUP_OFFSITE_SECRET_ACCESS_KEY` | 异地账号凭证，与 `DJANGO_S3_*` 分开 |
| `BACKUP_OFFSITE_REGION` | 区域，缺省 `us-east-1` |
| `BACKUP_OFFSITE_PREFIX` | 异地 key 前缀，缺省 `campus-glimmer-backups` |
| `BACKUP_OFFSITE_RETENTION_DAYS` | 异地保留天数，留空沿用 `BACKUP_RETENTION_DAYS` |

生产至少应确认：

- 每日备份是否完整通过 `--verify`；
- 异地副本是否配置了独立于主机的端点与账号，而不是同一台机器的另一个目录；
- 异地镜像是否真的读回比对过（失败时 `run_once` 会非零退出，应当触发告警）；
- 数据库和对象备份是否覆盖同一发布版本；
- 是否有从异地副本出发的恢复演练，而不只是从本机目录恢复；
- 备份目录权限是否限制到运维账号；
- 备份、校验和恢复结果是否进入告警。

## 当前边界

- 备份不是 PostgreSQL PITR；归档日志和持续恢复需要数据库层方案；
- 不自动执行 `pg_restore`，恢复必须人工确认目标库；CI 里的隔离恢复只针对一次性数据库，不碰任何真实环境；
- 不提供跨区域复制、加密托管或密钥轮换；
- 异地镜像默认未启用；启用后副本与业务卷仍在同一个运维账号的控制范围内，主机损坏不再同时丢掉两边，但独立主机、加密托管与密钥轮换仍未覆盖；
- 异地副本只能先整体拉回宿主机再按本地路径恢复，脚本没有"直接从异地 bucket 恢复"的入口；真要从异地恢复，第一步仍然是人工把归档取回 `BACKUP_DIR`；
- Redis 不备份，因为业务事实应能从 PostgreSQL 和对象存储重建。

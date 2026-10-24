# 对象存储 CI 集成设计

## 为什么先做需求

路线图里长期挂着一条：对象存储只经过 Compose 冒烟验证，没有进入 CI 的真实上传集成测试。这句话的实际后果不是"少一个绿色勾"，而是文档与实现之间出现一道说不清的缝：

- README 与[生产部署基线](production-deployment.md)都写着"上传文件进入对象存储""`/readyz/` 检查对象存储"，读者会以为这条链路和被两千多项测试覆盖的业务逻辑一样有人守。
- 实际上 `quality` 与 `production-deps` 两个任务的默认存储都是 filesystem。S3 配置层任何一处回归——环境变量名拼错、`PublicEndpointS3Storage` 改了签名行为、备份脚本的 `restore_objects` 换了 key 规则——CI 全绿，问题只在部署当天暴露。
- 本地 Compose 冒烟只证明"服务能起来"，既不断言匿名访问被拒绝，也不断言过期签名失效，更不覆盖备份恢复路径。

对象存储的故障形态和业务 bug 不一样：业务 bug 一般当场可见，对象存储配错往往要等到第一个用户上传头像失败、或者第一次恢复备份时才被发现。所以这一轮把它单独做成一个真实集成的 CI 任务，而不是继续在文档里写"后续可扩展"。

## 目标

- 用真实的 S3 兼容服务（MinIO）执行上传、回源、删除，而不是 mock。
- 验证权限语义：私有桶上的匿名访问必须被拒绝，签名 URL 必须可用，被改写和已过期的签名必须被拒绝。
- 验证备份链路里属于对象存储的那一半：`restore_objects` 能按原 key 把字节写回桶里。
- 本地与 `quality` / `production-deps` 任务保持零额外服务：没有配置端点时整套用例静默跳过，不让两千多项业务测试依赖一台 S3。
- 对象存储不可用必须表现为红色，不能表现为"三个用例被跳过"的绿色。

## 为什么单独一个 job

三个任务的分工：

| 任务 | 数据库 | 缓存 | 默认存储 | 目的 |
| --- | --- | --- | --- | --- |
| `quality` | SQLite | LocMem | filesystem | 语法、`check`、迁移一致性与完整业务测试 |
| `production-deps` | PostgreSQL 16 | Redis 7 | filesystem | 业务逻辑在生产数据库与缓存下的行为 |
| `object-storage` | SQLite | LocMem | s3（MinIO） | 对象存储链路本身 |

两个候选方案都试过，都不成立：

- **并进 `production-deps`**：这个任务的意义是"业务代码在 PostgreSQL 上对不对"。混入 S3 之后，一次 MinIO 启动抖动会让整片业务测试变红，排查时要先分辨红的是业务还是存储；反过来，几条 S3 用例失败也会被两千多项业务测试的量淹没。
- **并进 `quality`**：`quality` 跑的是本地零服务的同一套命令。加一个 S3 依赖，本地 `manage.py test` 就不再是"拿下来就能跑"，与项目一直坚持的零服务本地体验冲突。

单独一个 job 的代价是 CI 多几分钟（主要是下载 MinIO 二进制），换来的是三类故障各有一片属于自己的红。

## 为什么不用 service 容器跑 MinIO

最初的写法是把 MinIO 作为 GitHub Actions 的 service 容器，`image: minio/minio:RELEASE.2025-09-07T16-13-09Z`。第一轮 CI 立刻失败在 `Initialize containers`：

```text
Error response from daemon: pull access denied for minio/minio,
repository does not exist or may require 'docker login'
```

Docker Hub 上的 `minio/minio` 已被官方撤下（该仓库现在返回 404），迁移后的镜像源也拉不到——`quay.io/minio/minio` 对同版本 tag 返回 401 或 not found，`ghcr.io/minio/minio` 直接拒绝访问。这不是网络抖动，换一个 runner 再跑也不会好。

于是改成在步骤里直接下载 GitHub Releases 的官方二进制启动：

- 版本仍然钉在同一个 release tag 上，和本地演练用的是同一份产物，不是"另一个发行渠道"；
- 不依赖任何容器镜像 registry 的可用性与配额，少一类与业务无关的红；
- 启动方式与本地演练逐字一致，出问题时本地能原样复现。

MinIO 仍然只是后台进程：`RUNNER_TEMP` 下建数据目录，日志写到文件避免 step 挂起，job 结束时随 runner 一起回收，不需要额外的清理步骤。另有一个 `if: failure()` 的步骤输出 MinIO 日志，否则启动类失败只能看到一句"未在 60 秒内就绪"。

建桶与清桶策略和二进制启动放在同一步骤："桶不存在"和"桶策略被放开"这两种故障都会在测试开始前暴露，错误信息直接来自 boto3。重试 60 秒后仍不可达则直接失败，不降级为跳过——CI 里端点不可达说明这个 job 自己的 MinIO 没起来，属于必须修的问题。

## 跳过与失败的边界

`_probe()` 是整套用例的开关，它区分三种状态：

| 状态 | 判定方式 | 结果 |
| --- | --- | --- |
| 未配置 `DJANGO_S3_ENDPOINT_URL` | 环境变量为空 | 跳过 |
| 端点不可达 | TCP 连接失败 | 跳过 |
| 端点可达但桶不可用 | `head_bucket` 失败 | **失败** |

本地不配置变量时整体跳过，`quality` 与 `production-deps` 两个 job 因此完全不受影响。反过来，CI 里如果哪一步忘了建桶，或者桶策略被误改成公开，用例会以 `AssertionError` 失败并带上具体原因，而不是安静地跳过——一次静默跳过会让"对象存储已验证"这句话继续留在文档里，而实际上什么都没上传。

桶保持私有是断言的前提：建桶后显式 `delete_bucket_policy`，否则"匿名访问应被拒绝"会因为桶本身公开而永远不成立。

## 覆盖范围

`campus_glimmer/test_object_storage_integration.py` 共 12 项，分四组：

- **配置层（2 项，永远运行）**：环境变量能拼出 `PublicEndpointS3Storage`；`location` 前缀会加到每个 key 上。这两项不碰网络，任何 job 里都能跑。
- **上传与回源（3 项）**：`default_storage.save/open/exists/size` 回环；用裸 boto3 client 确认对象真的落在桶里且字节一致；删除后 `head_object` 取不到。
- **权限与签名（5 项）**：签名 URL 指向 public endpoint 且能直接下载；`expire=` 参数透出到 `X-Amz-Expires`；同一对象未签名访问被拒而签名可用；签名被改写后拒绝；过期签名拒绝。
- **备份恢复（1 项）**：按 `docker/backup.py` 的 manifest 结构构造归档，先删掉对象再调 `restore_objects`，断言对象按原 key 写回且字节一致。

每个进程用 `ci-smoke/<UTC 时间戳>-<uuid8>` 作为 key 前缀，`tearDownClass` 只清理自己前缀下的对象。并行 worker 与连续两次运行互不干扰，也不会碰到桶里既有的其它数据。

## 为什么本地不用 mock 验证

`moto` 与 `s3rver` 都能在本地起一个 S3 兼容端点，跑通上传回环没有问题，但它们**不实现签名语义**：`moto` 不校验 `X-Amz-Signature`，`s3rver` 不实现 `X-Amz-Expires` 过期。用它们本地验证"未签名被拒""过期签名被拒"，只会得到假失败或假通过。

所以这几条权限用例的结论以 CI 里的 MinIO 为准。本地演练下载的是同一版本的真实 MinIO 二进制，12 项同样全部通过；本地跑它只是为了改动时能快速反馈，不作为 CI 的替代。

## 边界

这一轮**没有**覆盖的部分，需要如实说明：

- **数据库 dump 的恢复不在这个任务里。** `object-storage` 只验证对象镜像恢复；dump 灌回 PostgreSQL 的那一段由 `backup-restore` 任务覆盖，见[备份与恢复](backup-restore.md)。
- 只验证 MinIO，不验证真实 AWS S3 或其它兼容实现在细节上的差异（region 处理、分片上传阈值等）。
- 不覆盖 multipart 大文件上传，也不覆盖 `DJANGO_S3_CUSTOM_DOMAIN` 与 CDN 域名路径。
- 不覆盖 `collectstatic` 到对象存储的部署路径。


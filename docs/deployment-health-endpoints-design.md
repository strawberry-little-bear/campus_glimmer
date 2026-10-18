# 部署健康检查拆分设计

## 为什么先做需求

项目已经有 `GET /healthz/`，但它只证明 Django 能处理一个请求并返回 JSON。数据库锁住、连接配置错误或迁移后的表不可读时，这个端点仍然返回 200。README 又把它称作“健康检查”，容器或反向代理按这个地址做探活时，会把一个不能正常服务流量的实例继续留在池子里。

这不是把 `/healthz/` 直接改成查库就能解决的问题。部署系统里“进程还活着”和“现在能接流量”是两个不同问题：

- **存活检查**不该查数据库。数据库短暂抖动时，若存活探针失败，编排系统可能重启所有 worker，反而把一次存储故障放大成应用故障。
- **就绪检查**应该查最小依赖。它失败时实例应被移出流量池，但进程可以继续运行，等待依赖恢复或人工处理。

因此这一轮把两个问题拆成两个端点：`/healthz/` 保持轻量存活检查，新增 `/readyz/` 做数据库就绪检查。下面的“初始范围”保留了这一步的设计语境；生产持久化依赖接入后，端点又按同一原则扩展了 Redis 与对象存储检查。

## 目标

- `GET /healthz/`：证明请求处理链路可用，不访问数据库，成功时返回 200。
- `GET /readyz/`：对默认数据库执行一次最小 `SELECT 1`，成功时返回 200，失败时返回 503。
- 两个端点都返回稳定 JSON，不依赖登录状态，不需要 CSRF。
- 响应带 `Cache-Control: no-store`，避免反向代理或浏览器缓存健康状态。
- 失败响应只说明组件不可用，不输出异常文本、连接串、文件路径或堆栈。
- 数据库失败只记录异常类名的 WARNING 日志，方便运维检索，不把原始异常消息写进日志或响应。
- 保持轻量：不执行迁移、不收集静态文件、不检查业务数据、不检查定时任务是否按时运行。

## 响应形状

存活检查：

```json
{
  "status": "ok",
  "service": "campus-glimmer"
}
```

就绪成功：

```json
{
  "status": "ok",
  "service": "campus-glimmer",
  "checks": {
    "database": "ok"
  }
}
```

就绪失败：

```json
{
  "status": "unavailable",
  "service": "campus-glimmer",
  "checks": {
    "database": "unavailable"
  }
}
```

## 边界

- 不新增表、字段和迁移。
- 不引入 Redis、缓存或外部监控依赖。
- 不把 `/readyz/` 当作完整监控：它只回答默认数据库此刻能否执行最小查询。
- 不检查媒体目录可写、外链对象存储可用或搜索索引健康；这些依赖进入项目后再逐个加入 `checks`。
- 不自动重启服务，不摘除实例，不修改负载均衡配置；流量控制由部署系统按状态码决定。
- 不在请求间缓存结果，避免上一次成功掩盖当前故障。

## 测试计划

- 存活检查返回 200、稳定 JSON 和 `Cache-Control: no-store`。
- 即使数据库连接抛错，存活检查也不访问数据库并保持 200。
- 就绪检查在默认数据库可用时返回 200，并报告 database 为 ok。
- 模拟数据库连接失败时返回 503，状态和组件均为 unavailable。
- 失败响应不包含异常消息，避免泄露部署细节。
- URL 反向解析继续使用 `health_check` 与 `ready_check`，部署文档和容器配置可以稳定引用。


## 后续演进：从数据库扩展到完整持久化依赖

生产化接入 PostgreSQL、Redis 与 S3 兼容对象存储后，就绪检查在同一响应结构内增加了 `cache` 和 `object_storage`。这不是把 `/healthz/` 变重，而是让“能否接流量”覆盖真正决定页面与上传是否可用的依赖。

| 检查项 | 何时出现 | 实际动作 | 失败后果 |
| --- | --- | --- | --- |
| `database` | 始终 | 对默认数据库执行 `SELECT 1` | 返回 `503` |
| `cache` | 始终 | 经 Django cache 写入并读回短周期探针值 | 返回 `503` |
| `object_storage` | `DJANGO_STORAGE_BACKEND` 为 `s3` / `minio` | 对配置的 bucket 执行 `head_bucket` | 返回 `503`；本地文件存储不执行该检查 |

完整的就绪响应示例：

```json
{
  "status": "ok",
  "service": "campus-glimmer",
  "checks": {
    "database": "ok",
    "cache": "ok",
    "object_storage": "ok"
  }
}
```

部分依赖失败时仍返回稳定的组件状态，不返回异常文本：

```json
{
  "status": "unavailable",
  "service": "campus-glimmer",
  "checks": {
    "database": "ok",
    "cache": "unavailable",
    "object_storage": "ok"
  }
}
```

### 演进后的边界

- 对象存储探针只读 bucket 元数据，不在每次健康检查时创建临时对象，也不修改 ACL。
- 本地 `filesystem` 存储不会凭空要求一个对象存储服务，因此 `checks` 中不出现 `object_storage`。
- 探针仍不检查磁盘剩余空间、邮件服务、异步任务队列、搜索索引或证书有效期；这些需要独立监控，不应全部塞进一次 HTTP 就绪请求。
- Redis 探针验证连接与基本读写，不判断缓存命中率；探针失败只说明该实例暂不应接流量，不改变 PostgreSQL 中的业务事实。
- 测试覆盖三个组件的成功、失败与隐私边界；新增关键依赖时应同步更新 `campus_glimmer/test_deployment.py`、部署文档和容器的 `HEALTHCHECK`。

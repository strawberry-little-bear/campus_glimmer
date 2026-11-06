# 集中日志设计

[返回文档地图](README.md) · [生产部署](production-deployment.md)

> 目标：把 Web、一次性迁移任务和备份 sidecar 的 stdout 汇到一处，保留请求关联 ID，
> 同时不把日志系统本身变成新的公网入口。这个叠加层面向单机、小规模部署，不声称
> 已经提供高可用日志平台。

## 数据链路

```text
Django / Gunicorn
       │  stdout，一行一个 JSON
       ▼
Docker json-file 驱动（10m × 5 轮转）
       │  Docker API
       ▼
Grafana Alloy ── 只采集 campus-glimmer-prod ──▶ Loki（本地文件系统，保留 7 天）
                                                    │
                                                    ▼
                                             Grafana（127.0.0.1:3000）
```

PostgreSQL、Redis、MinIO 的日志仍由各自容器输出；Alloy 按容器标签一起采集，但
它们不是本项目的 JSON 契约，查询时需要按各自服务的原生格式阅读。

## JSON 日志契约

生产编排通过 `DJANGO_LOG_FORMAT=json` 打开逐行 JSON。本地直接运行或测试时默认
仍为 `text`，因此不会把可读的本地开发日志变成一屏 JSON。

Django 日志的稳定字段：

| 字段 | 含义 |
| --- | --- |
| `timestamp` | UTC ISO-8601，毫秒精度，以 `Z` 结尾 |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `logger` | Python logger 名，例如 `django.request` |
| `message` | 已格式化的人类可读消息，保留中文 |
| `request_id` | `X-Request-ID`；没有请求上下文时为 `-` |
| `service` | `DJANGO_METRICS_SERVICE_NAME`，默认 `campus-glimmer` |
| `pid` | 输出日志的进程 ID |
| `exception` | 有异常时追加的堆栈 |
| `stack` | 有 `stack_info` 时追加的调用栈 |

Gunicorn access log 另用 `event: "http_request"` 标识，并包含 `remote_addr`、
`method`、`path`、`protocol`、`status`、`bytes`、`duration_seconds` 和
`user_agent`。它刻意**不记录原始 URI、查询字符串、Cookie 或 Authorization**：
查询报名、搜索和登录链路里可能有一次性令牌，日志采集不应复制它们。

示例：

```json
{"timestamp":"2026-11-06T12:00:00.123Z","level":"INFO","logger":"gunicorn.access","event":"http_request","message":"request","request_id":"trace-42","service":"campus-glimmer","pid":17,"remote_addr":"203.0.113.9","method":"GET","path":"/listings/42/","protocol":"HTTP/1.1","status":200,"bytes":0,"duration_seconds":0.123456,"user_agent":"Mozilla/5.0"}
```

## 启动

先保证主站按[生产部署基线](production-deployment.md)启动，再叠加日志组件：

```powershell
docker compose --env-file .env.production `
  -f docker-compose.prod.yml `
  -f docker-compose.observability.yml `
  up -d --build
```

首次启动前必须在 `.env.production` 中替换
`GRAFANA_ADMIN_PASSWORD`。默认入口如下，全部只绑定宿主机 loopback：

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| Grafana | `http://127.0.0.1:3000` | 查询与浏览日志 |
| Loki | `http://127.0.0.1:3100` | 日志写入与查询 API |
| Alloy UI | `http://127.0.0.1:12345` | 查看采集组件自身状态 |

Loki 数据保存在 `loki_data` 卷，Grafana 数据在 `grafana_data` 卷。Loki 的
`retention_period` 为 `168h`，由 compactor 清理；容器 stdout 仍保留 `10m × 5`
的轮转上限，避免 Docker 日志无限增长。

## 常用查询

在 Grafana 的 Explore 中选择自动配置的 Loki 数据源：

```logql
# 按请求关联 ID 找回一次投诉的全部日志
{project="campus-glimmer-prod"} | json | request_id="trace-42"

# 只看 Web 的错误
{project="campus-glimmer-prod", service="web"} | json | level="ERROR"

# 看 5xx 访问日志
{project="campus-glimmer-prod", service="web"} | json | event="http_request" | status >= 500

# 看备份任务的输出
{project="campus-glimmer-prod", service="backup"}
```

如果只需要原样查看某一行，可去掉 `| json`；JSON 解析失败不会影响原始日志行。

## 安全与故障边界

- 挂载 `/var/run/docker.sock` 即使使用 `:ro`，Docker API 仍具备接近宿主机 root
  的控制能力。这个叠加层只能部署在受信任的运维宿主机上，不能把 socket 暴露给
  公网、租户或不可信容器。
- Alloy 用 `com.docker.compose.project=campus-glimmer-prod` 过滤，只采集本项目
  容器；如果修改 Compose 项目名，必须同步修改 `config.alloy`。
- Loki 在内部网络关闭鉴权，默认只通过 loopback 暴露；远程访问应经过带 TLS 与
  身份认证的反向代理，不要把 `:3100` 或 Grafana 管理端口直接开放。
- Grafana 关闭匿名访问与注册，并要求显式密码。密码只放在未入库的
  `.env.production`。
- 这是单机日志方案：Loki 磁盘损坏、宿主故障或卷误删仍可能丢日志；它不替代
  备份、审计归档或跨可用区集中日志平台。需要这些能力时，把 Alloy 的目标改为
  外部托管的 Loki，并移除本地 Loki/Grafana 服务。
- 日志中不应主动打印密码、令牌、Cookie、完整请求头或用户联系方式；JSON
  结构化只解决可查询性，不会自动完成敏感信息脱敏。

## 验证与回滚

只校验编排语法而不启动容器：

```powershell
docker compose --env-file .env.production.example `
  -f docker-compose.prod.yml `
  -f docker-compose.observability.yml config --quiet
```

启动后检查：

```powershell
Invoke-RestMethod http://127.0.0.1:3100/ready
Invoke-WebRequest http://127.0.0.1:3000/api/health
```

回滚只停止叠加层并恢复基础编排即可，卷不会自动删除：

```powershell
docker compose --env-file .env.production `
  -f docker-compose.prod.yml `
  -f docker-compose.observability.yml stop alloy loki grafana

docker compose --env-file .env.production `
  -f docker-compose.prod.yml up -d
```

相关契约测试在 `campus_glimmer/test_request_logging.py` 与
`campus_glimmer/test_observability.py`，分别覆盖 JSON 字段、敏感查询串排除、
Gunicorn 选项、端口只绑 loopback、固定镜像版本与保留期。

# docker · 容器运行与健康检查

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> 提供轻量部署入口：镜像启动前执行迁移和静态文件收集，再由 Gunicorn 提供 Django 服务。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `entrypoint.sh` | 执行 `migrate`、`collectstatic`，最后启动 Gunicorn |
| `healthcheck.py` | 请求容器内 `/readyz/`，供 Docker `HEALTHCHECK` 使用 |
| `../Dockerfile` | Python 3.12 镜像、依赖安装和应用复制 |
| `../docker-compose.yml` | 本地演示端口、环境变量和数据卷 |

## 容器启动链路

```text
docker build
  -> pip install -r requirements.txt
  -> entrypoint.sh
  -> python manage.py migrate --noinput
  -> python manage.py collectstatic --noinput
  -> gunicorn campus_glimmer.wsgi:application
```

## 常用命令

```powershell
docker compose up --build
docker compose exec web python manage.py check
docker compose exec web python manage.py test
curl http://127.0.0.1:8000/healthz/
curl http://127.0.0.1:8000/readyz/
docker compose down
```

`docker compose down` 默认保留命名卷；除非明确确认不再需要数据，否则不要附加 `-v`。

## 数据与部署边界

- SQLite 位于 `/app/data/`，上传文件位于 `/app/media/`，两者都由 Compose 数据卷持久化；
- 示例 Compose 中的密钥只适合本地演示，生产环境必须替换；
- `/healthz/` 表示进程存活，`/readyz/` 表示数据库可用；
- 当前配置面向开发、演示和轻量部署，不宣称已经具备大规模高可用架构；
- TLS、反向代理、备份和日志采集应在外层部署环境中完成。

## 延伸阅读

- [部署健康端点设计](../docs/deployment-health-endpoints-design.md)
- [后台与定时任务](../docs/scheduled-tasks.md)
- [当前取舍与边界](../docs/product-boundaries.md)

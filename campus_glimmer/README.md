# campus_glimmer · Django 项目配置

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> 这里是 Django 项目包，不承载具体业务。它负责配置、根路由、请求上下文、健康检查和 WSGI / ASGI 入口。

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `settings.py` | 应用注册、数据库、模板、静态/媒体文件、邮件和安全配置 |
| `urls.py` | 根路由与各业务应用挂载 |
| `context_processors.py` | 全站模板共享的未读计数等上下文 |
| `health.py` | `/healthz/` 与 `/readyz/` 探针 |
| `wsgi.py` / `asgi.py` | 部署入口 |
| `tests.py` | 根路由、健康检查和配置基线测试 |

## 根路由

| 路径 | 去向 |
| --- | --- |
| `/` | 首页 |
| `/admin/` | Django 管理后台 |
| `/accounts/` | 账户与校园身份 |
| `/listings/` | 商品、订单、通知与运营业务 |
| `/messages/` | 留言、私信与内容治理 |
| `/healthz/` | 进程存活检查 |
| `/readyz/` | 数据库就绪检查 |

## 健康检查语义

- `/healthz/` 只证明 Django 进程还能响应请求；
- `/readyz/` 会执行最小的数据库查询，数据库不可用时返回 `503`；
- 响应设置 `Cache-Control: no-store`，失败时不泄露数据库或部署细节；
- 容器健康检查使用 `/readyz/`，不要把两个探针合并成一个含义模糊的接口。

## 配置约定

- 本地默认值用于开发，生产环境必须显式覆盖密钥、域名和安全开关；
- 环境变量样例见[`.env.example`](../.env.example)；
- 新增配置优先采用环境变量并提供安全默认值；
- 修改根路由、认证、Cookie、安全响应头或静态文件配置时，应同步补 `tests.py`；
- 部署与探针设计见[部署健康端点设计](../docs/deployment-health-endpoints-design.md)。

## 验证

```powershell
python manage.py check
python manage.py test campus_glimmer
python manage.py makemigrations --check --dry-run
```

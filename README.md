# 拾光校园 · Campus Glimmer

> 把校园里暂时闲置的物品，交给真正用得上的人。

[![Django](https://img.shields.io/badge/Django-5.1%2B-0c4b33?style=flat-square&logo=django)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/strawberry-little-bear/campus_glimmer/django.yml?branch=main&style=flat-square&label=CI)](https://github.com/strawberry-little-bear/campus_glimmer/actions)
[![License](https://img.shields.io/badge/License-MIT-1d8a68?style=flat-square)](LICENSE)

## 这是什么

拾光校园（Campus Glimmer）是一个围绕校园生活设计的轻量级二手交易社区。它不试图复制大型电商平台，而是把注意力放在校园交易中更常见、也更实际的问题上：物品在哪里、什么时候方便见面、双方如何沟通，以及交易完成后如何留下可信的记录。

项目从商品发布和浏览开始，逐步加入校园地点、收藏、私信、举报、订单、浏览行为和个性化推荐等能力。目前仍在持续迭代中，每一项功能都尽量落到完整的使用链路上：数据模型、页面交互、权限判断、后台管理、迁移和基础测试一起演进，而不是只做一个孤立的页面。

## 为什么做校园二手交易

校园里的交易有自己的场景：商品距离近、交易频率高、见面地点相对固定，用户通常也共享相似的生活节奏。一个更贴近校园的交易工具，应该让用户能够：

- 用熟悉的分类快速找到教材、数码、宿舍用品、服装和体育用品；
- 通过校区、楼栋或具体地点，优先查看方便见面的商品；
- 在站内完成留言、私信和交易状态确认，减少信息散落在不同平台；
- 通过举报、审核、订单状态和后续评价，逐步建立更可靠的交易环境；
- 在使用一段时间后，得到基于自己真实兴趣的推荐，而不是完全随机的商品列表。

## 当前功能

### 商品发布与发现

- 发布、编辑和删除商品，支持分类、价格、成色、描述和多图上传；
- 支持“在售 / 已预订 / 已售出”状态；
- 按关键词、分类、价格和发布时间筛选、排序并分页浏览；
- 按校园地点、校区或楼栋筛选，优先发现方便线下交付的商品；
- 商品详情页展示图片、卖家资料、交易地点、留言和相近商品；
- 首页提供分类入口、热门地点、最新商品和个性化推荐。

### 用户与社区互动

- 注册、登录、退出和个人资料维护；
- 编辑头像、个人简介和校园身份信息；
- 收藏商品、维护心愿单；
- 在商品下留言，或通过站内私信联系其他用户；
- 查看会话列表、收件箱和未读消息；
- 记录登录用户的最近浏览商品与浏览次数，支持后续的兴趣分析。

### 交易与信任

- 买家可以从商品详情页发起预约交易；
- 订单包含成交价格、交付地点、买家备注和交易状态；
- 订单会记录从预约、确认、交付到完成 / 取消的状态变化和操作说明，方便双方回看交易过程；
- 交易流程覆盖“待卖家确认 → 卖家已确认 → 待当面交付 → 交易完成”，也支持取消；
- 交易状态会同步影响商品的可售状态，避免同一件商品被重复预约；
- 用户可以举报商品，系统会拦截同一用户对同一商品的重复举报；
- 管理员可以查看举报、更新审核状态并记录处理备注。

### 个性化推荐

推荐模块采用轻量、可解释的评分方式，而不是依赖不可见的黑盒模型。当前会综合考虑：

- 用户收藏过的商品分类和交易地点；
- 最近浏览过的商品分类和交易地点；
- 历史订单中的兴趣方向；
- 商品的新鲜度和收藏热度。

每条推荐都会附带类似“与你收藏或交易过的分类相近”“就在你关注过的校园地点附近”的原因，方便用户理解推荐从何而来，也方便后续继续调整策略。

### 管理与部署

- Django Admin 管理用户、商品、图片、分类和校园地点；
- 可调整地点的启用状态和展示顺序，适应不同校区的实际情况；
- 支持通过环境变量配置密钥、调试模式、允许访问的域名、数据库路径和 HTTPS 安全选项；
- 提供 Docker Compose 配置、数据卷和 `/healthz/` 健康检查；
- GitHub Actions 会在推送和 Pull Request 中执行系统检查、迁移检查和自动化测试。

## 一次完整的使用流程

1. 注册账号并完善个人资料；
2. 发布商品，填写价格、成色、图片和方便见面的校园地点；
3. 浏览者通过搜索、分类或地点筛选发现商品；
4. 双方在商品留言或站内私信中确认细节；
5. 买家发起预约交易，卖家确认后推进订单状态；
6. 双方按约定地点完成当面交付，并将订单标记为完成；
7. 如果发现异常内容，可以提交举报，由管理员在后台跟进处理。

## 技术栈

- **后端**：Python、Django 5.1+
- **数据库**：SQLite（默认开发与演示环境）
- **前端**：Django Templates、Bootstrap 5、Bootstrap Icons、原生 CSS / JavaScript
- **图片处理**：Pillow
- **生产服务**：Gunicorn、Docker Compose
- **质量保障**：Django TestCase、Django system check、GitHub Actions

## 项目结构

```text
campus_glimmer/
├── accounts/                 # 注册、登录、个人资料与头像
├── listings/                 # 商品、地点、收藏、举报、订单与推荐
├── chat_messages/            # 商品留言、私信与会话
├── campus_glimmer/           # Django 配置、路由、WSGI / ASGI
├── templates/                # 页面模板
├── static/                   # CSS、JavaScript 和静态资源
├── media/                    # 用户上传的头像与商品图片
├── docker/                   # 容器启动脚本
├── .github/workflows/        # 持续集成配置
├── init_data.py              # 本地演示数据初始化脚本
├── requirements.txt          # Python 依赖
├── Dockerfile                # Web 容器构建配置
├── docker-compose.yml        # 本地 / 演示部署编排
├── manage.py
└── README.md
```

## 本地运行

### 1. 获取代码并创建虚拟环境

```bash
git clone https://github.com/strawberry-little-bear/campus_glimmer.git
cd campus_glimmer
python -m venv .venv
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
source .venv/bin/activate
```

### 2. 安装依赖并初始化数据库

```bash
python -m pip install -r requirements.txt
python manage.py migrate
```

如果希望快速准备一组本地演示数据，可以运行：

```bash
python init_data.py
```

### 3. 创建管理员并启动开发服务器

```bash
python manage.py createsuperuser
python manage.py runserver
```

启动后可以访问：

- 前台首页：<http://127.0.0.1:8000/>
- 商品发现：<http://127.0.0.1:8000/listings/>
- 站内消息：<http://127.0.0.1:8000/messages/>
- 管理后台：<http://127.0.0.1:8000/admin/>
- 健康检查：<http://127.0.0.1:8000/healthz/>

## 配置

开发环境可以直接使用默认配置。部署或多人协作时，可以复制示例文件并按环境调整：

```bash
cp .env.example .env
```

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django 密钥，生产环境必须替换 | 本地开发占位值 |
| `DJANGO_DEBUG` | 是否开启调试模式 | `1` |
| `DJANGO_ALLOWED_HOSTS` | 允许访问的域名，逗号分隔 | `127.0.0.1,localhost,testserver` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | HTTPS 或反向代理场景下的可信来源 | 空 |
| `DJANGO_DB_PATH` | SQLite 数据库文件路径 | `db.sqlite3` |
| `DJANGO_SECURE_SSL_REDIRECT` | 是否强制跳转 HTTPS | `0` |
| `DJANGO_SESSION_COOKIE_SECURE` | 是否只通过 HTTPS 发送会话 Cookie | `0` |
| `DJANGO_CSRF_COOKIE_SECURE` | 是否只通过 HTTPS 发送 CSRF Cookie | `0` |

正式部署时，还需要结合实际规模考虑 PostgreSQL、静态文件服务、媒体文件存储、HTTPS 和密钥管理。

## Docker 运行

项目提供了面向本地演示和小规模部署的容器配置：

```bash
docker compose up --build
```

启动后访问 <http://127.0.0.1:8000/>。数据库和上传文件分别通过 `campus_data`、`campus_media` 数据卷持久化，容器重建时不会因为容器本身被替换而丢失。

当前 Docker 配置默认使用 SQLite，适合演示和轻量部署。若面向更大的用户规模，建议将数据库和媒体文件迁移到独立服务，并保留应用容器的无状态特性。

## 开发与验证

提交一项功能前，建议至少执行：

```bash
python manage.py check
python manage.py test
python manage.py makemigrations --check --dry-run
```

项目按相对独立的功能迭代，每次迭代尽量包含：

- 清晰的数据模型和数据库迁移；
- 对应的页面、权限和状态流转；
- 关键路径的基础测试；
- 必要的后台管理和文档更新。

这样既方便从历史提交中回溯，也为后续接入真实校园场景、替换基础设施或扩展前端形态保留空间。

## 后续迭代方向

拾光校园会沿着“更贴近校园场景、更完整的交易闭环、更可靠的社区治理”继续推进。接下来可能会逐步完善：

- 交易完成后的双方评价与信誉展示；
- 更细的校区、楼栋、时间段和距离筛选；
- 交付确认和异常处理记录；
- 敏感词过滤、举报分级和更清晰的审核工作台；
- 搜索与推荐策略的效果统计和持续调优；
- PostgreSQL、对象存储、缓存和更完整的生产部署方案；
- 移动端体验、无障碍细节和消息提醒。

这些方向会以小步迭代的方式逐项落地，优先解决真实使用中的问题，不以一次性重做为目标。

## 参与开发

欢迎提出校园交易场景中的具体问题，也欢迎提交代码、测试或文档改进。比较适合的贡献方向包括：

- 改进搜索、筛选和推荐体验；
- 增加交易安全与社区治理能力；
- 优化移动端页面和交互细节；
- 补充模型、视图和关键流程测试；
- 完善部署、监控和数据维护方案；
- 根据真实校园运营经验调整分类、地点和交易流程。

## License

本项目采用 [MIT License](LICENSE)。

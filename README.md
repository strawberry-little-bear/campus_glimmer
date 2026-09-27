# 拾光校园 · Campus Glimmer

> 让闲置在校园里继续流转，也让每一次相遇都更有回应。

[![Django](https://img.shields.io/badge/Django-5.1%2B-0c4b33?style=flat-square&logo=django)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/strawberry-little-bear/campus_glimmer/django.yml?branch=main&style=flat-square&label=CI)](https://github.com/strawberry-little-bear/campus_glimmer/actions)
[![License](https://img.shields.io/badge/License-MIT-1d8a68?style=flat-square)](LICENSE)

## 项目简介

**拾光校园（Campus Glimmer）** 是一个面向校园场景的二手交易社区。项目从“发布一件闲置、找到一个买家”这个最小闭环开始，沿着真实使用中逐渐出现的问题持续补齐能力：交易地点怎么约、双方如何沟通、订单进行到哪一步、交易后能不能留下可信的反馈，以及错过消息后如何及时回来处理。

项目保持小步迭代的节奏。每次功能扩展都会尽量同时覆盖数据模型、业务规则、页面交互、权限控制、后台管理、数据库迁移和关键路径测试，让功能真正接入已有的交易链路。

## 目前能做什么

### 商品与校园地点

- 发布、编辑、删除商品，支持分类、价格、成色、描述和多图上传；
- 支持“在售 / 已预订 / 已售出”状态；
- 支持按关键词、分类、地点、成色和价格区间组合筛选，按最新发布、价格或相关度排序并分页；
- 按已配置的校区、楼栋或交易地点筛选，优先找到方便线下交付的商品；
- 商品详情展示卖家资料、交易地点、留言、收藏状态和相近商品；
- 首页提供分类入口、热门地点、最新商品和个性化推荐；
- 搜索时记录搜索词、筛选条件和结果数量，为后续的需求分析、运营看板和推荐优化保留结构化数据。
- 管理员可访问运营数据看板，按 7 / 30 / 90 天或 1 年查看商品发布、搜索需求、交易状态、分类地点分布和待处理举报等指标。

> 当前的“就近”是基于校园地点数据的筛选，不依赖 GPS，也不把用户位置上传到服务端。后续可以在此基础上继续扩展距离或时间段策略。

### 用户与社区互动

- 注册、登录、退出和个人资料维护；
- 编辑头像、个人简介和校园身份信息；
- 收藏商品，维护自己的心愿单；
- 在商品下留言，或通过站内私信联系其他用户；
- 会话列表展示最近消息和未读数量；
- 通知中心集中展示交易、评价、留言和私信提醒，并支持单条或全部标记已读；
- 关注的搜索支持保存关键词、分类、地点、成色和价格条件；新商品发布后会按条件匹配并聚合生成站内提醒，也可以随时暂停或恢复。
- 记录登录用户的最近浏览商品与浏览次数，为推荐和兴趣分析提供依据。

### 交易、评价与信任

- 买家可以从商品详情页发起预约交易；
- 订单记录成交价格、交易地点、买家备注、买家和卖家；
- 订单状态覆盖“待卖家确认 → 卖家已确认 → 待当面交付 → 交易完成”，也支持取消；
- 订单状态变化会写入交易时间线，双方可以回看是谁在什么时候推进了哪一步；
- 交易状态会同步影响商品可售状态，避免同一件商品被重复预约；
- 交易完成后，双方可以互相进行 1～5 星评价并留下文字反馈；
- 当订单进入待当面交付后，买卖双方需要分别确认交付，系统才会自动完成订单；
- 支持提交交易争议，平台工作人员可以在争议队列中记录处理结果并通知双方；
- 商品详情会展示卖家平均评分、评价数量和近期评价，帮助用户做出更有依据的选择；
- 用户可以举报商品，系统会拦截同一用户对同一商品的重复举报；
- 管理员可以查看举报、评价、订单事件、通知和搜索记录，并记录审核结果。

### 个性化推荐

推荐模块采用轻量、可解释的评分方式，而不是不可见的黑盒模型。当前会综合考虑：

- 用户收藏过的商品分类和交易地点；
- 最近浏览过的商品分类和交易地点；
- 历史订单中的兴趣方向；
- 商品的新鲜度和收藏热度。

推荐结果会附带“与你收藏过的分类相近”“就在你关注过的地点附近”等原因。推荐并不追求一开始就复杂，而是先把行为记录下来，再逐步调整策略并验证效果。

## 一次完整的交易链路

```text
发布商品
   ↓
按分类 / 关键词 / 校园地点发现商品
   ↓
留言或私信沟通
   ↓
买家发起交易预约
   ↓
卖家确认 → 待当面交付 → 交易完成
   ↓
状态时间线、通知提醒、双方评价与信誉沉淀
```

这条链路是项目持续演进的主线。新的功能优先接入这条链路，而不是单独增加一个与业务无关的页面。

## 技术实现

- **Web 框架**：Django 5.1+
- **运行环境**：Python 3.11+
- **页面层**：Django Templates、Bootstrap 5、Bootstrap Icons 和少量项目 CSS
- **数据库**：默认 SQLite；通过环境变量保留切换到其他数据库的空间
- **媒体文件**：本地 `media/`，生产环境可替换为对象存储
- **部署**：Docker Compose + Gunicorn
- **质量保障**：Django system check、迁移检查、Django TestCase、GitHub Actions CI

### 目录结构

```text
campus_glimmer/
├── accounts/                 # 注册、登录、个人资料和用户扩展信息
├── listings/                 # 商品、地点、订单、评价、通知和推荐
│   ├── migrations/           # 数据库迁移
│   ├── notifications.py      # 统一创建站内通知
│   ├── saved_searches.py     # 关注搜索匹配与智能提醒
│   └── recommendations.py   # 可解释的推荐评分
├── chat_messages/            # 商品留言、私信和会话
├── campus_glimmer/           # Django 配置、路由、上下文处理器和 ASGI/WSGI
├── templates/                # 页面模板
├── static/                   # CSS、JavaScript 和静态资源
├── media/                    # 本地上传的头像与商品图片
├── docker/                   # 容器启动相关脚本
├── .github/workflows/        # 持续集成配置
├── init_data.py              # 本地演示数据初始化脚本
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── manage.py
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

### 2. 安装依赖、迁移数据库

```bash
python -m pip install -r requirements.txt
python manage.py migrate
```

如果希望快速准备一组本地演示数据，可以运行：

```bash
python init_data.py
```

### 3. 创建管理员并启动服务

```bash
python manage.py createsuperuser
python manage.py runserver
```

启动后可以访问：

| 页面 | 地址 |
| --- | --- |
| 首页 | <http://127.0.0.1:8000/> |
| 商品发现 | <http://127.0.0.1:8000/listings/> |
| 站内消息 | <http://127.0.0.1:8000/messages/inbox/> |
| 通知中心 | <http://127.0.0.1:8000/listings/notifications/> |
| 关注的搜索 | <http://127.0.0.1:8000/listings/saved-searches/> |
| 运营看板（管理员） | <http://127.0.0.1:8000/listings/operations/> |
| 交易争议队列（管理员） | <http://127.0.0.1:8000/listings/disputes/> |
| 管理后台 | <http://127.0.0.1:8000/admin/> |
| 健康检查 | <http://127.0.0.1:8000/healthz/> |

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

正式部署时，还需要结合实际规模考虑 PostgreSQL、静态文件服务、媒体文件存储、HTTPS 和密钥管理。不要把生产密钥、用户上传文件或数据库文件提交到仓库。

## Docker 运行

项目提供了面向本地演示和小规模部署的容器配置：

```bash
docker compose up --build
```

启动后访问 <http://127.0.0.1:8000/>。数据库和上传文件分别通过 `campus_data`、`campus_media` 数据卷持久化，容器重建时不会因为容器本身被替换而丢失。

当前 Docker 配置默认使用 SQLite，适合演示和轻量部署。若面向更大的用户规模，建议将数据库、缓存和媒体文件迁移到独立服务，并保留应用容器的无状态特性。

## 开发与验证

提交一项功能前，建议至少执行：

```bash
python manage.py check
python manage.py test
python manage.py makemigrations --check --dry-run
git diff --check
```

项目按相对独立的功能迭代。一个完整迭代通常包含：

- 清晰的数据模型和数据库迁移；
- 页面、权限和状态流转；
- 关键路径的基础测试；
- 必要的后台管理和文档更新；
- 一个能够独立回溯的中文提交记录。

GitHub Actions 会在推送到 `main` 或提交 Pull Request 时执行系统检查、迁移检查和测试。

## 一些实现上的取舍

- **先保证交易一致性，再扩展体验**：创建订单时使用事务和行级锁，尽量避免同一件商品被并发预约；
- **让业务记录可回看**：订单事件、浏览历史、评价和通知都保留结构化数据，而不是只依赖页面提示；
- **权限靠服务端判断**：商品编辑、举报、订单操作、评价和通知读取都在视图层再次校验用户身份与归属；
- **约束重复操作**：收藏、举报、浏览记录和交易评价使用数据库唯一约束或幂等逻辑；
- **推荐保持可解释**：先采用可读的分类、地点、热度和新鲜度信号，方便调试和后续替换策略。

## 后续迭代方向

项目会继续围绕“更贴近校园场景、更完整的交易闭环、更可靠的社区治理”推进，优先考虑：

- 更深入的运营分析：转化漏斗、无结果搜索的补给建议、活跃用户分层和异常行为识别；
- 搜索分析：在已有搜索记录基础上增加热门词、无结果搜索、筛选偏好和时段趋势，帮助运营判断校园里的真实需求；
- 交付确认与异常处理：双方确认、爽约记录和订单争议的结构化记录；
- 社区治理：敏感词过滤、举报分级、审核工作台和更清晰的处理反馈；
- 基础设施：PostgreSQL、缓存、对象存储、日志和更完整的生产部署方案；
- 体验完善：移动端细节、无障碍、消息提醒策略和更细致的校园配置。

这些方向会继续以小步迭代的方式逐项落地，不以一次性推倒重来为目标。

## 参与开发

欢迎提出具体的校园交易场景，也欢迎提交代码、测试、设计或文档改进。比较适合的贡献方向包括：

- 改进搜索、筛选和推荐体验；
- 增加交易安全与社区治理能力；
- 优化移动端页面和交互细节；
- 补充模型、视图和关键流程测试；
- 完善部署、监控和数据维护方案；
- 根据真实校园运营经验调整分类、地点和交易流程。

如果你准备提交功能，建议先说明使用场景、影响的业务链路和验证方式，再按一个独立功能提交，便于评审和后续回溯。

## License

本项目采用 [MIT License](LICENSE)。

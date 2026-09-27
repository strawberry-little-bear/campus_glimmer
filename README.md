# 拾光校园 · Campus Glimmer

> 面向校园生活的轻量级闲置交易社区，让物品在熟悉的校园里继续流转。

[![Django](https://img.shields.io/badge/Django-5.1%2B-0c4b33?style=flat-square&logo=django)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-1d8a68?style=flat-square)](LICENSE)

## 项目简介

拾光校园（Campus Glimmer）是一个围绕高校日常生活设计的二手交易平台。项目以“发布闲置、发现好物、就近交流、安心交易”为主要使用路径，服务于教材、数码产品、宿舍用品、服装、体育用品等校园高频场景。

它既可以作为一个可运行的校园二手交易产品，也可以作为 Django 全栈项目的持续迭代样例：功能从商品管理逐步延伸到社区沟通、地点筛选、内容治理和交易流程，代码结构尽量保持清晰，方便继续扩展。

## 产品目标

- **让发布更简单**：用较少的步骤完成商品信息、成色、价格和图片的填写。
- **让发现更高效**：通过搜索、分类、价格排序和校园地点筛选，快速找到合适的商品。
- **让沟通更自然**：提供商品留言、私信和卖家名片，降低第一次联系的成本。
- **让线下交易更清晰**：使用校园里的真实交易地点，减少“在哪里见面”的沟通成本。
- **让社区更可信**：通过商品状态、举报入口和后台审核，为后续的社区治理留下基础。

## 当前功能

### 商品与浏览

- 发布、编辑、删除商品
- 商品分类、价格、成色、描述和多图上传
- 在售、已预订、已售出三种商品状态
- 关键词搜索、分类筛选、价格排序和分页
- 校园地点 / 校区 / 楼栋维度的交易地点筛选
- 商品详情页图片切换、卖家信息、留言讨论和同类推荐
- 首页分类入口、热门地点和最新商品展示

### 用户与社区

- 注册、登录、退出和个人资料维护
- 头像、个人简介、校园身份信息等资料编辑
- 商品收藏与心愿单
- 商品留言和用户私信
- 收件箱、会话列表和未读消息
- 商品举报、重复举报拦截和管理员审核状态
- 预约交易、订单详情和交易状态流转
- 买卖双方可推进确认、当面交付、完成或取消交易
- 基于收藏、历史交易、地点偏好和商品热度的可解释推荐

### 管理后台

- 用户与用户资料管理
- 商品、商品图片、分类管理
- 校园地点管理，可控制启用状态和展示顺序
- 收藏、留言、私信记录管理
- 举报记录查看、审核状态更新和审核备注
- 商品列表中显示举报数量，方便管理员定位风险内容

## 典型使用流程

### 发布者

1. 注册并登录账号。
2. 发布商品，填写分类、价格、成色、描述和图片。
3. 选择方便见面的校园地点。
4. 通过留言或私信与感兴趣的同学沟通。
5. 根据交易进展更新商品状态。

### 浏览者

1. 在首页或发现页浏览商品。
2. 按分类、关键词、价格和校园地点缩小范围。
3. 收藏感兴趣的商品，或进入详情页联系卖家。
4. 在发现异常内容时提交举报。

### 管理员

1. 在后台维护分类和校园交易地点。
2. 查看商品、用户和社区互动记录。
3. 处理举报，并留下审核结果和备注。
4. 根据校园运营情况持续调整分类、地点和治理规则。

## 技术栈

- **后端**：Python、Django 5.1+
- **数据库**：SQLite（默认开发环境）
- **前端**：Django Templates、Bootstrap 5、Bootstrap Icons、原生 CSS / JavaScript
- **图片处理**：Pillow
- **测试**：Django TestCase
- **部署基础**：支持通过环境变量配置密钥、调试模式和允许访问的域名

## 项目结构

```text
campus_glimmer/
├── accounts/                 # 注册、登录、个人资料与头像
├── listings/                 # 商品、分类、地点、收藏、举报与交易核心逻辑
├── chat_messages/            # 商品留言、私信与会话
├── campus_glimmer/           # Django 项目配置、路由和 WSGI / ASGI
├── templates/                # 页面模板
├── static/                   # CSS、JavaScript 和静态图片
├── media/                    # 用户上传的头像与商品图片
├── init_data.py              # 本地演示数据初始化脚本
├── requirements.txt          # Python 依赖
├── manage.py
└── README.md
```

## 快速开始

### 1. 获取代码并创建虚拟环境

```bash
git clone https://github.com/strawberry-little-bear/campus_glimmer.git
cd campus_glimmer
python -m venv .venv
```

Windows：

```bash
.venv\Scripts\activate
```

macOS / Linux：

```bash
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 初始化数据库

```bash
python manage.py migrate
```

如果需要快速创建演示分类、测试用户和商品：

```bash
python init_data.py
```

### 4. 创建管理员并启动

```bash
python manage.py createsuperuser
python manage.py runserver
```

然后访问：

- 前台首页：<http://127.0.0.1:8000/>
- 商品发现：<http://127.0.0.1:8000/listings/>
- 管理后台：<http://127.0.0.1:8000/admin/>

## 配置说明

开发环境可以直接使用默认配置。部署或多人协作时，建议复制 `.env.example` 为 `.env`，并根据实际环境设置变量：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django 密钥，生产环境应替换 | 本地开发占位值 |
| `DJANGO_DEBUG` | 是否开启调试模式 | `1` |
| `DJANGO_ALLOWED_HOSTS` | 允许访问的域名，使用逗号分隔 | `127.0.0.1,localhost,testserver` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 反向代理或 HTTPS 场景下的可信来源 | 空 |
| `DJANGO_DB_PATH` | SQLite 数据库文件路径 | `db.sqlite3` |
| `DJANGO_SECURE_SSL_REDIRECT` | 是否强制跳转 HTTPS | `0` |

生产环境还应配合正式数据库、静态文件服务、媒体文件存储、HTTPS 和安全的密钥管理方案。

## Docker 部署

项目提供了一个适合演示环境和小规模部署的 Docker 配置：

```bash
docker compose up --build
```

启动后访问 <http://127.0.0.1:8000/>，健康检查地址为 <http://127.0.0.1:8000/healthz/>。数据库和上传文件通过 Docker volume 持久化，容器重建不会自动丢失数据。

`docker-compose.yml` 使用 SQLite 作为默认部署演示方案。若用于正式生产环境，建议将数据库替换为 PostgreSQL，并把媒体文件迁移到对象存储或独立文件服务。

## 持续集成

`.github/workflows/django.yml` 会在推送到 `main` 或创建 Pull Request 时自动执行：

- Django 系统检查
- 数据库迁移检查
- 自动化测试

这样可以在合并代码前尽早发现配置、迁移和回归问题。

## 开发检查

提交代码前建议运行：

```bash
python manage.py check
python manage.py test
python manage.py makemigrations --check --dry-run
```

项目的功能开发按相对独立的迭代推进，每个功能尽量配套模型迁移、页面交互和基础测试，便于回溯和继续扩展。

## 后续路线

拾光校园会围绕“校园场景更真实、交易过程更完整、社区治理更可靠”持续推进，计划逐步加入：

- 买卖双方的交易确认和交付记录
- 浏览历史与个性化推荐
- 更细的校区、楼栋和时间段筛选
- 敏感词过滤、举报自动分级和审核工作台
- PostgreSQL、对象存储和生产环境部署配置
- Docker、CI 自动测试和基础监控
- 更完善的移动端体验与无障碍细节

## 参与开发

欢迎围绕校园交易场景提出建议或提交改进。比较适合的贡献方向包括：

- 改进商品搜索和推荐逻辑
- 增加校园交易安全能力
- 优化移动端页面和交互
- 补充测试、文档和部署方案
- 结合真实校园运营经验完善地点与分类设计

## License

本项目采用 [MIT License](LICENSE)。

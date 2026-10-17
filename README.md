<a id="top"></a>
<div align="center">

<img src="docs/assets/brand-mark.svg" width="92" alt="拾光校园品牌图标" />

# 拾光校园 · Campus Glimmer

**让闲置在校园里继续流转，也让每一次相遇都更有回应。**

面向校园的二手交易与社区协作平台。从发现一件好物，到预约、交付、反馈与治理，
把校园里经常被忽略的细节，接成一条可以回看的交易链路。

[![Django](https://img.shields.io/badge/Django-5.1%2B-1d8a68?style=flat-square&logo=django&logoColor=white)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/strawberry-little-bear/campus_glimmer/django.yml?branch=main&style=flat-square&label=CI)](https://github.com/strawberry-little-bear/campus_glimmer/actions/workflows/django.yml)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ed?style=flat-square&logo=docker&logoColor=white)](docker-compose.yml)
[![License](https://img.shields.io/badge/License-MIT-f6aa61?style=flat-square)](LICENSE)

[产品预览](#preview) · [功能地图](#features) · [快速开始](#quick-start) · [工程与测试](#engineering) · [文档地图](#docs)

</div>

---

<p align="center">
  <img src="docs/screenshots/home.png" alt="拾光校园首页，展示校园闲置交易主视觉、导航与在售商品概览" width="100%" />
</p>

<p align="center"><sub>界面预览 · 本地演示数据</sub></p>

## 🌿 关于项目

校园二手交易和普通电商并不完全相同。用户活动范围集中，交易更依赖当面沟通和时间协调；
商品生命周期会随开学、考试周和毕业季快速变化；平台除了撮合交易，还要处理通知、举报、
内容治理和运营复盘。

拾光校园从“发布一件闲置、找到一个买家”出发，持续补齐这些容易被忽略的环节：交易地点、
预约是否有效、订单进行到哪一步、消息是否被看到、问题由谁处理，以及运营者如何理解站内
真实需求。

项目按真实业务链路长期迭代。一个功能通常会经过设计说明、数据模型、页面交互、权限边界、
数据库迁移、后台运营和自动化测试，再进入主分支。README 负责快速了解与进入，完整规则
维护在 [`docs/`](docs/)。

<table>
<tr>
<td width="50%" valign="top">

<h3>🧭 发现</h3>

按分类、价格、成色和校园地点筛选；收藏、关注搜索、价格轨迹与供需信号帮助用户找到合适的好物。

</td>
<td width="50%" valign="top">

<h3>🤝 交易</h3>

覆盖出售、免费赠送、限期借用、预约、改期、交付确认、评价、争议和借用归还。

</td>
</tr>
<tr>
<td width="50%" valign="top">

<h3>💬 社区</h3>

留言、私信、站内通知、未读提醒、通知偏好、举报回访与内容申诉连接起每一次互动。

</td>
<td width="50%" valign="top">

<h3>📊 运营</h3>

从搜索、浏览、收藏、预约和交付中形成可解释指标，支持需求跟进、运营告警和效果复盘。

</td>
</tr>
</table>

---

<a id="preview"></a>
## 📸 产品预览

### 发现校园好物

<p align="center">
  <img src="docs/screenshots/discover.png" alt="商品发现页，包含分类、校园地点筛选、商品卡片和排序" width="100%" />
</p>

<p align="center"><sub>分类、交易地点与商品状态在同一个筛选入口内完成 · 本地演示数据</sub></p>

<table>
<tr>
<td width="50%" valign="top">
<img src="docs/screenshots/item-detail.png" alt="商品详情页，展示商品图片、价格、校园地点和价格参考" width="100%" />
<p align="center"><strong>商品详情</strong><br /><sub>图片、地点、价格参考与交易操作</sub></p>
</td>
<td width="50%" valign="top">
<img src="docs/screenshots/notifications.png" alt="通知中心，展示未读统计、类型分布、筛选和批量延后操作" width="100%" />
<p align="center"><strong>通知中心</strong><br /><sub>未读统计、类型分布、筛选与批量处理</sub></p>
</td>
</tr>
</table>

<p align="center"><sub>界面预览 · 本地演示数据</sub></p>

---

<a id="features"></a>
## 🗺️ 功能地图

功能按八条业务链路组织。下面只保留能力概览，完整规则、指标口径和边缘情况从
[功能手册](docs/feature-handbook.md)进入。

| 业务链路 | 现在能做什么 |
| :--- | :--- |
| 🛍️ **商品发布与校园地点** | 出售 / 赠送 / 借用、多图、地点、刷新额度、批量发布与学期续期 |
| 🔎 **搜索、保存与推荐** | 组合筛选、关注的搜索、价格轨迹、推荐反馈、机会流与供需信号 |
| 🧭 **失物招领与智能匹配** | 失物 / 招领线索、匹配确认、领取反馈与结果回访 |
| 📦 **交易预约与订单状态** | 预约、改期、交付确认、借用归还、评价、争议与逾期催收 |
| 💬 **私信、留言与通知中心** | 会话、未读提醒、偏好、汇总降噪、单条与批量延后和恢复 |
| 🛡️ **社区治理与后台处理** | 举报、风险分级、内容申诉、误判放行与治理队列 |
| 📈 **运营看板与运营告警** | 供给、需求、搜索、交付、治理、通知、推荐与任务台账 |
| 🎓 **校园身份与可信交易** | 校园邮箱认证、公开档案、安全提示与确认记录 |

### 一条交易链路

<p align="center">
  <strong>发现</strong> → <strong>筛选</strong> → <strong>了解</strong> → <strong>沟通</strong> → <strong>预约</strong> → <strong>交付</strong> → <strong>反馈</strong> → <strong>治理</strong>
</p>

出售、免费赠送和限期借用会在预约之后进入不同状态分支；求购匹配和失物招领是两条并行支线。
完整流程图见[校园交易闭环](docs/business-flow.svg)，业务规则见[功能手册](docs/feature-handbook.md)。

> **运营纪律：**看板与治理工作台只读消费业务事实，不自动修改交易状态。
> 指标会明确样本、分母、周期和缺失值，避免用不可解释的数字替代判断。

---

## 🧱 技术栈

| 层次 | 当前方案 |
| :--- | :--- |
| Web | Django 5.1+ · Django Templates · 轻量 JavaScript |
| 数据 | SQLite 默认，模型与查询保持可替换数据库的边界 |
| 图片 | Pillow · EXIF 方向 · WebP 重编码 · 缩略图与响应式候选 |
| 部署 | Docker Compose · Gunicorn · `/healthz/` 与 `/readyz/` |
| 质量 | GitHub Actions · Django check · 迁移检查 · 自动化测试 |

项目保持 Django 应用的自然边界：

```text
accounts/         注册、登录、个人资料与校园认证
chat_messages/    留言、私信、内容审核与申诉
listings/         商品、订单、搜索、通知、运营与治理
templates/        页面模板
static/           CSS、JavaScript 与静态资源
docs/             功能、设计、架构和运营文档
```

模块、测试和设计文档索引见[模块与测试地图](docs/module-map.md)。

---

<a id="quick-start"></a>
## 🚀 快速开始

### 1. 准备环境

```bash
git clone https://github.com/strawberry-little-bear/campus_glimmer.git
cd campus_glimmer
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
source .venv/bin/activate
```

### 2. 安装并启动

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

需要快速准备演示数据时，可在迁移后运行：

```bash
python init_data.py
```

打开 <http://127.0.0.1:8000/> 即可访问。

### 3. 常用页面

| 页面 | 地址 |
| :--- | :--- |
| 首页 | <http://127.0.0.1:8000/> |
| 发现好物 | <http://127.0.0.1:8000/listings/> |
| 搜索 | <http://127.0.0.1:8000/listings/search/> |
| 我的交易 | <http://127.0.0.1:8000/listings/orders/> |
| 消息中心 | <http://127.0.0.1:8000/messages/inbox/> |
| 通知中心 | <http://127.0.0.1:8000/listings/notifications/> |
| 运营看板 | <http://127.0.0.1:8000/listings/operations/> |
| 管理后台 | <http://127.0.0.1:8000/admin/> |

<details>
<summary><strong>环境变量与本地配置</strong></summary>

复制配置模板：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

| 变量 | 作用 | 默认值 |
| :--- | :--- | :--- |
| `DJANGO_SECRET_KEY` | Django 密钥，生产环境必须替换 | 本地开发占位值 |
| `DJANGO_DEBUG` | 是否开启调试模式 | `1` |
| `DJANGO_ALLOWED_HOSTS` | 允许访问的域名，逗号分隔 | `127.0.0.1,localhost,testserver` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 反向代理场景下的可信来源 | 空 |
| `DJANGO_DB_PATH` | SQLite 数据库文件路径 | `db.sqlite3` |
| `DJANGO_SECURE_SSL_REDIRECT` | 是否强制跳转 HTTPS | `0` |
| `DJANGO_SESSION_COOKIE_SECURE` | 是否只通过 HTTPS 发送会话 Cookie | `0` |
| `DJANGO_CSRF_COOKIE_SECURE` | 是否只通过 HTTPS 发送 CSRF Cookie | `0` |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS 的 max-age 秒数 | `0` |
| `DJANGO_EMAIL_BACKEND` | 邮件后端 | DEBUG 时使用 console |

</details>

---

## 🐳 Docker 与部署

```bash
docker compose up --build
```

访问 <http://127.0.0.1:8000/>。容器默认使用 SQLite，数据库和上传文件分别通过
`campus_data`、`campus_media` 数据卷持久化。

```bash
docker compose down
```

仅在确认不需要保留数据后执行：

```bash
docker compose down -v
```

镜像内置健康检查：

- `/healthz/`：存活检查，确认 Django 进程可以处理请求；
- `/readyz/`：就绪检查，对数据库执行轻量查询，失败时返回 `503`。

<details>
<summary><strong>生产部署检查清单</strong></summary>

- 替换 `DJANGO_SECRET_KEY`，关闭调试模式并配置允许域名；
- 启用 HTTPS 后配置安全重定向、Session/CSRF Cookie 和可信来源；
- 域名与回滚窗口确认后再设置 `DJANGO_SECURE_HSTS_SECONDS`；
- 根据规模配置反向代理、限流、备份、日志和媒体文件策略；
- 不提交生产数据库、上传文件、密钥或包含用户隐私的日志；
- 当前默认配置面向开发和轻量部署，不代表大规模生产架构。

</details>

---

<a id="engineering"></a>
## 🧪 工程与测试

提交前建议运行：

```bash
python -m compileall -q accounts campus_glimmer chat_messages listings
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
git diff --check
```

GitHub Actions 在推送到 `main` 或提交 Pull Request 时执行语法检查、Django check、
迁移一致性检查和完整测试集。目标不是只看“能启动”，而是让每次迭代都能独立验证和回溯。

### 定时任务

周期任务统一封装成 Django management command，可接入 cron、系统计划任务或容器调度。

| 命令 | 用途 | 建议 |
| :--- | :--- | :--- |
| `process_order_timeouts` | 预约超时、归还提醒与逾期催收 | 周期性运行 |
| `expire_items` | 下架到期商品并通知发布者 | 每日运行 |
| `send_operations_digest` | 生成管理员运营告警日报 | 按统计周期运行 |
| `send_saved_search_digest` | 发送关注搜索汇总 | 按日 / 周运行 |
| `send_lifecycle_reminders` | 发送商品生命周期提醒 | 每日运行 |
| `sync_academic_calendar` | 补齐学期阶段并刷新快照 | 开学前与每周运行 |
| `backfill_item_thumbnails` | 回填历史图片变体和宽高 | 上线辅助、分批运行 |

参数、幂等边界和运行台账见[后台与定时任务](docs/scheduled-tasks.md)。

### 权限与安全

- 普通用户只能编辑自己的商品，查看和操作自己参与的订单、私信、通知与收藏；
- 管理员可以处理治理事项，但不能绕过服务端权限检查代替用户完成交易操作；
- 项目默认启用 `nosniff`、`Referrer-Policy`、`Cross-Origin-Opener-Policy`、
  `X-Frame-Options` 和 HttpOnly Cookie；
- 订单创建使用事务和锁，争议证据、通知读取、运营数据和订单操作均在服务端校验身份。

详细边界见[当前取舍与边界](docs/product-boundaries.md)。

---

## 🗓️ 后续路线图

- **供给侧与提醒**：完善刷新台账覆盖和生命周期提醒效果；
- **搜索、供需与机会流**：补齐空结果诊断、需求承接和推荐解释；
- **交易、交付与借用**：强化交付异常、归还质量、催收效果和安全确认；
- **治理与通知**：继续做治理分组、申诉质量和通知降噪；
- **校园场景与信任**：完善认证体验、可信档案和地点维护；
- **运营与数据质量**：加强导出一致性、样本审计、指标血缘和告警；
- **基础设施与体验**：优化移动端、无障碍、图片加载、备份和可观测性。

每个方向都拆成可独立验证、独立回滚的小迭代。详细待办、已落地范围和不做某项功能的理由见
[后续路线图](docs/roadmap.md)。

---

<a id="docs"></a>
## 📚 文档地图

| 想了解什么 | 从这里开始 |
| :--- | :--- |
| 八条业务链路的完整规则、指标与边缘情况 | [功能手册](docs/feature-handbook.md) |
| 核心 ORM 模型、关键字段与约束 | [核心数据模型](docs/data-model.md) |
| 代码模块、测试与设计文档索引 | [模块与测试地图](docs/module-map.md) |
| 周期任务、幂等边界与运行台账 | [后台与定时任务](docs/scheduled-tasks.md) |
| 学期阶段、统计快照与交易高峰 | [学期日历与交易高峰](docs/academic-calendar.md) |
| 当前取舍、隐私边界与实现纪律 | [当前取舍与边界](docs/product-boundaries.md) |
| 后续方向与拆分原则 | [后续路线图](docs/roadmap.md) |
| 具体功能的设计过程与历史取舍 | [`docs/` 设计说明](docs/) |

README 只保留项目级入口，详细口径继续在深文档中维护。这样新增能力时不必反复拉长首页，
也不会把长期积累的设计理由混进快速说明。

---

## 🤝 参与开发

欢迎提出具体的校园交易场景，也欢迎提交代码、测试、设计和文档改进。适合的贡献方向包括
搜索与推荐、交易安全、治理、通知、移动端、无障碍、部署、监控和运营分析。

建议的提交方式：

1. 先说明要解决的使用场景和受影响的业务链路；
2. 将一个独立功能的模型、页面、权限、测试和文档放在同一组改动中；
3. 提交前运行本地检查和测试；
4. 使用清晰的中文提交信息，保持每个提交可独立理解和回溯；
5. 不提交本地数据库、上传文件、密钥或临时调试产物。

<div align="center">

---

**拾光校园 · 让闲置流转，让好物遇见对的人。**

[返回顶部](#top) · [MIT License](LICENSE)

</div>

<a id="top"></a>
<div align="center">

<img src="docs/assets/brand-mark.svg" width="94" alt="拾光校园品牌图标" />

# 拾光校园 · Campus Glimmer

**让闲置在校园里继续流转，也让每一次相遇都更有回应。**

面向校园的二手交易与互助协作平台。围绕发布、发现、沟通、预约、交付、
评价和治理，把一次校园交易中容易被忽略的细节连接成可回看的完整链路。

[![Django](https://img.shields.io/badge/Django-5.1%2B-1d8a68?style=flat-square&logo=django&logoColor=white)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/strawberry-little-bear/campus_glimmer/django.yml?branch=main&style=flat-square&label=CI)](https://github.com/strawberry-little-bear/campus_glimmer/actions/workflows/django.yml)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ed?style=flat-square&logo=docker&logoColor=white)](docker-compose.yml)
[![License](https://img.shields.io/badge/License-MIT-f6aa61?style=flat-square)](LICENSE)

[产品预览](#preview) · [能力地图](#features) · [快速开始](#quick-start) · [工程与测试](#engineering) · [文档地图](#docs) · [完整手册](docs/README.md)

</div>

---

<a id="preview"></a>
## 📸 产品预览

<p align="center">
  <a href="docs/screenshots/home.png"><img src="docs/screenshots/home.png" alt="拾光校园首页，展示品牌主视觉、导航与在售商品概览" width="100%" /></a>
</p>

<p align="center"><strong>首页 · 从校园场景进入交易与互助</strong><br /><sub>品牌主视觉、全局入口与正在流转的好物 · 本地演示数据</sub></p>

<table>
<tr>
<td width="50%" valign="top">
<a href="docs/screenshots/discover.png"><img src="docs/screenshots/discover.png" alt="发现页，展示关键词、分类、校园地点、商品状态和排序筛选" width="100%" /></a>
<p align="center"><strong>发现好物</strong><br /><sub>分类、价格、成色与校园地点组合筛选</sub></p>
</td>
<td width="50%" valign="top">
<a href="docs/screenshots/item-detail.png"><img src="docs/screenshots/item-detail.png" alt="商品详情页，展示商品图片、价格、校园地点和价格参考" width="100%" /></a>
<p align="center"><strong>商品详情</strong><br /><sub>交易信息、地点、价格参考与操作入口</sub></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="docs/screenshots/inbox.png"><img src="docs/screenshots/inbox.png" alt="消息工作台，展示未读统计、搜索筛选、会话列表和批量已读" width="100%" /></a>
<p align="center"><strong>消息工作台</strong><br /><sub>会话整理、未读聚焦、收到与发出视图</sub></p>
</td>
<td width="50%" valign="top">
<a href="docs/screenshots/profile.png"><img src="docs/screenshots/profile.png" alt="账户中心，展示校园认证、互助贡献、联系方式和个人活动入口" width="100%" /></a>
<p align="center"><strong>账户中心</strong><br /><sub>校园身份、可信资料、贡献进度与活动入口</sub></p>
</td>
</tr>
</table>

<p align="center">
  <a href="docs/screenshots/demands.png"><img src="docs/screenshots/demands.png" alt="求购广场，展示需求结果数、已启用条件、关键词和校园地点筛选与求购卡片" width="100%" /></a>
</p>

<p align="center"><strong>求购广场 · 需求筛选工作台</strong><br /><sub>结果数与已启用条件可读，关键词、分类和地点支持逐项移除 · 本地演示数据</sub></p>

<p align="center">
  <a href="docs/screenshots/my-items.png"><img src="docs/screenshots/my-items.png" alt="我的商品卖家工作台，展示生命周期诊断、批量专题与续期工具、状态页签和商品管理表格" width="100%" /></a>
</p>

<p align="center"><strong>我的商品 · 卖家工作台</strong><br /><sub>生命周期诊断、五类状态页签独立分页、专题与续期批量工具和逐件操作集中管理 · 本地演示数据</sub></p>

<p align="center">
  <a href="docs/screenshots/my-orders.png"><img src="docs/screenshots/my-orders.png" alt="我的交易工作台，展示买入卖出计数、状态筛选、第二页订单、结果区间和分页控件" width="100%" /></a>
</p>

<p align="center"><strong>我的交易 · 买入与卖出总览</strong><br /><sub>待推进、已完成与已取消分栏；统计按全量订单计算，结果每页 10 笔并保留当前筛选 · 本地演示数据</sub></p>

<p align="center">
  <a href="docs/screenshots/order-detail.png"><img src="docs/screenshots/order-detail.png" alt="订单详情交易工作台，展示订单状态、四步交易进度、下一步操作、交付安排和状态记录" width="100%" /></a>
</p>

<p align="center"><strong>订单详情 · 交易工作台</strong><br /><sub>订单状态、语义化四步进度、下一步动作、交付安排与安全记录保持同一阅读层级 · 本地演示数据</sub></p>

<table>
<tr>
<td width="50%" valign="top">
<a href="docs/screenshots/moderation-queue.png"><img src="docs/screenshots/moderation-queue.png" alt="内容审核值班台，展示待办入口、分级复核时限、组合筛选和审核记录" width="100%" /></a>
<p align="center"><strong>内容审核值班台</strong><br /><sub>四项待办、三级时限与组合筛选同屏，审核动作留在记录原位</sub></p>
</td>
<td width="50%" valign="top">
<a href="docs/screenshots/operations.png"><img src="docs/screenshots/operations.png" alt="运营看板，展示供给、搜索、预约、互助和看板构建观测" width="100%" /></a>
<p align="center"><strong>运营看板</strong><br /><sub>供给、需求、搜索、交付与告警共用同一套指标口径</sub></p>
</td>
</tr>
</table>

<table>
<tr>
<td width="50%" valign="top">
<a href="docs/screenshots/lost-found-report-queue.png"><img src="docs/screenshots/lost-found-report-queue.png" alt="失物招领举报审核队列，展示状态筛选、分页区间与已结案记录的处理备注" width="100%" /></a>
<p align="center"><strong>失物招领 · 举报审核队列</strong><br /><sub>状态筛选、分页区间与处理入口同屏 · 本地演示数据</sub></p>
</td>
<td width="50%" valign="top">
<a href="docs/screenshots/lost-found-report-form.png"><img src="docs/screenshots/lost-found-report-form.png" alt="举报失物招领记录表单，展示被举报记录摘要、固定举报理由与补充说明" width="100%" /></a>
<p align="center"><strong>失物招领 · 提交举报</strong><br /><sub>固定理由加补充说明，隐私边界写在同一屏 · 本地演示数据</sub></p>
</td>
</tr>
</table>

<p align="center"><sub>全部截图来自当前本地页面与演示数据；登录、通知、订单详情、贡献中心、公开档案、失物招领与举报审核等更多界面见 <a href="docs/screenshots/README.md">系统界面预览</a></sub></p>

---

## 🧭 这是一个怎样的项目

校园二手交易不是普通电商的缩小版。活动范围集中，交易更依赖当面沟通和时间协调；
商品生命周期会随开学、考试周和毕业季快速变化；平台除了撮合交易，还要处理通知、
举报、内容治理和运营复盘。

拾光校园从“发布一件闲置、找到一个买家”出发，持续补齐交易地点、预约是否有效、
订单进行到哪一步、消息是否被看到、问题由谁处理，以及运营者如何理解站内真实需求。
项目按真实业务链路长期迭代：一个功能通常会经过场景设计、数据模型、页面交互、权限边界、
数据库迁移、后台运营和自动化测试，再进入主分支。

> **当前状态：** 持续维护中。核心交易、消息通知、社区治理、专题运营和运营看板已形成闭环；
> 近期迭代重点放在交付预约异常处理链、账户与消息工作台、搜索空结果诊断、移动端体验、界面一致性和生产环境可靠性。

<table>
<tr>
<td width="50%" valign="top">
<h3>🛍️ 校园交易闭环</h3>
<p>出售、赠送、借用、求购与失物招领共用一条清晰的发布和发现入口；商品与求购发布/编辑页提供分段填写、进度提示和响应式单列布局，预约、交付、归还、评价与争议都有对应状态。</p>
</td>
<td width="50%" valign="top">
<h3>🔎 搜索与供需判断</h3>
<p>组合筛选、可逐项调整的空结果诊断、关注的搜索，以及带着当前条件直接发布求购的承接链路；“我的求购”再按生命周期查看需求、响应待办和重新上架入口；当前确认商品在交易前失效时，列表卡片会直接提示并进入详情重新选择响应。配合最近命中预览、价格轨迹、推荐反馈和供需信号，让“有没有人需要”不再只靠感觉判断。</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<h3>💬 消息与社区治理</h3>
<p>留言、私信、通知、未读提醒、通知偏好、举报回访与内容申诉连接起每一次互动，并保留处理边界；登录注册、资料编辑、发布、消息、交易以及举报审核、争议和预约异常处置等关键表单的错误与帮助信息都支持键盘与读屏访问。</p>
</td>
<td width="50%" valign="top">
<h3>📊 运营与基础设施</h3>
<p>专题目标、用户漏斗、运营告警、定时任务、PostgreSQL、Redis、对象存储与备份恢复共同支撑长期运行。</p>
</td>
</tr>
</table>

### 一条交易如何走完

<p align="center">
  <strong>发布</strong> → <strong>发现</strong> → <strong>沟通</strong> → <strong>预约</strong> → <strong>交付</strong> → <strong>评价</strong> → <strong>治理</strong>
</p>

交付安排会显示当前由谁等待回应，并在订单详情按时间列出真实发生的提议、改期、接受、拒绝和取消。最新提议等待满 24 小时后，系统会向等待回应的一方发送一次站内提醒，并把每次提醒写入关联具体提议、通知和接收角色的不可变台账，提醒只打开订单，不自动确认、拒绝或改期。运营看板的“交付安排提醒触达与回应”以这份台账为稳定分母，固定观察发送后的 72 小时，按买家与卖家分别展示通知阅读、接受、拒绝和再次改期；未满窗口的样本保留为观察中，窗口后的动作单列，旧通知与已删除通知也明确标出。阅读与窗口动作另以 2×2 交叉表观察：只纳入阅读状态可判定且已过窗的样本，删除通知或 `is_read` 与 `read_at` 冲突时都不归入未读，窗后动作按窗内未行动计；通知阅读率只以可判定阅读状态的样本为分母。每个阅读状态下的行动与未行动格都至少 12 条才标为可比较。另有一张时点表区分先读后动、先动后读、同一时刻和未读行动，合计不足 12 条只报数；它只说明先后关系，不把先后写成因果，也不自动调频。只有从首次提议开始记录的事件链才作为完整历史；旧预约和起点缺失的记录会明确标出边界，不补造上线前的时间、地点或发起人。仍无同一回合动作的提醒会进入“提醒后待人工核对”：已过窗未行动优先，观察中样本继续等待，窗后已有动作不进入。运营可为提醒追加“继续等待、已处理、数据异常、无需跟进”四类人工结论并填写说明；选择“数据异常”时还必须从通知缺失、阅读状态冲突、预约历史不完整、提醒台账不一致或其他数据问题中选择结构化原因，旧记录没有原因时按空值兼容。继续等待或数据异常必须安排晚于本次复核的未来复查时间，已处理或无需跟进会自动关闭复查；到期复查在清单中按原复查时间从早到晚排列，并固定分为“24 小时内、1–3 天、3–7 天、7 天以上”四档；恰好落在 24 小时、3 天或 7 天时归入后一档。分诊区同时显示最久逾期记录与各条逾期时长合计，同一批记录也可通过独立筛选查看，旧记录没有复查时间时标为“历史记录未安排复查”，每次安排同时进入不可变复核历史。每次复核保留复核人、时间和独立历史，页面显示最新结论并可展开完整复核历史，筛选仍按最新结论并默认聚焦未核对；页面、懒加载分区和 CSV 共用同一筛选，页面前 20 条、CSV 导出当前筛选全量并带最新异常类型与历史次数；CSV 另含到期分档、最久逾期、各条逾期合计，以及每一行的逾期时长和分档。人工结论不改订单、预约、通知或提醒发送台账，页面也不展示用户名，不排名、不自动处置。看板另设“到期复查覆盖与吞吐”：周期内每条 `next_review_at` 视为一次到期机会，只看其后的第一条复核；到期后至期末出现为已承接，仍无复核为期末待处理，到期前出现为提前重排并从覆盖率分母排除。覆盖率 = 已承接 ÷（已承接 + 期末待处理），空分母显示 `—`；处理吞吐按已承接 ÷ 实际观察天数计算。当前积压仍看上方快照。

出售、免费赠送和限期借用会在预约之后进入不同状态分支；求购匹配确认后，求购发布者可从匹配结果直接继续预约、借用或申请领取，新交易会保留来源响应。运营看板会先区分领取申请是否在等待卖家选择、未选中、已撤回或已选中缺订单，再判断商品和求购是否仍可推进；领取申请不等于订单转化。如果确认商品在交易前失效且尚未形成订单或领取申请，发布者可以重新选择响应；每次重新选择都会留下不可变事件，看板与 CSV 可追踪事件后的首次再次确认、订单或领取申请、未接出交易阶段和两段时延，历史 `superseded` 只做兼容回填并单列披露；三个严格相邻周期分别达到分母门槛后，连续两次各下降至少 8 个百分点才进入运营提醒，提醒只链接人工核对漏斗。失物招领是另一条并行支线。失物招领支持一条记录附一张可选照片，用于辅助辨认物品，照片不参与匹配评分，发布者可以随时移除。看到可疑记录时可以提交举报，举报只对运营可见，审核结论只回给举报人，不自动下架。
预约异常从提交、开始处理到确认或驳回，会以不可改写的动作记录保存操作人、状态变化、处理说明与发生时间。处理人可以直接下结论，系统仍会补记同一经办人的“开始处理”，避免行动链缺段；治理工作台显示紧凑链路，订单双方可在订单页回看完整时间线。处理记录启用前的存量异常会明确标记“未留存处理动作”，平台不会按当前状态倒推或伪造历史。对周期内已有事件但无法判定的异常，治理工作台另列事件链排查清单，用九个稳定原因代码指出缺失首报、缺少开始处理或结论、动作重复、顺序或状态快照冲突，并支持按原因筛选和从最近事件较早的记录开始人工核查；页面与 CSV 共用同一结果，历史不可知继续单列，全程只读、不显示用户名、不排名、不补造事件，也不修改异常或业务状态。

完整流程图见[校园交易闭环](docs/business-flow.svg)，规则与边缘情况见[功能手册](docs/feature-handbook.md)，预约异常的质量指标与处理链边界见[预约异常质量与处理链设计](docs/meeting-incident-quality-design.md)。

> **运营纪律：** 看板与治理工作台只读消费业务事实，不自动修改交易状态。
> 指标会明确样本、分母、周期和缺失值，避免用不可解释的数字替代判断。

---

<a id="features"></a>
## 🗺️ 功能地图

功能按九条业务链路组织。下面只保留能力概览，完整规则、指标口径和边缘情况从
[功能手册](docs/feature-handbook.md)进入。

| 业务链路 | 现在能做什么 |
| :--- | :--- |
| 🛍️ **商品发布与校园地点** | 分区发布 / 编辑工作台、出售 / 赠送 / 借用、多图预览、地点、刷新额度、批量发布与学期续期 |
| 🔎 **搜索、保存与推荐** | 组合筛选、关注的搜索与最近命中预览、价格轨迹、推荐反馈、机会流与供需信号 |
| 🧭 **失物招领与智能匹配** | 可解释筛选工作台、可选物品照片、失物 / 招领线索、匹配确认、领取反馈、结果回访与异常举报 |
| 📦 **交易预约与订单状态** | 求购匹配确认后直接承接预约 / 借用 / 领取申请；确认商品在交易前失效时可重新选择响应，再进入可追溯的提议与改期、到场、双方交付确认、归还、评价、争议与逾期催收 |
| 💬 **私信、留言与通知中心** | 会话、未读提醒、偏好、汇总降噪、单条与批量延后和恢复 |
| 🛡️ **社区治理与后台处理** | 举报与争议分页处理、可解释优先级、风险分级、内容申诉、误判放行与失物招领举报审核（与治理工作台、运营看板同口径） |
| 📈 **运营看板与运营告警** | 供给、求购确认后的预约与交付阶段、交付安排提醒触达与回应、免费领取、重新选择后的响应闭环、搜索、治理、通知、推荐、任务台账和专题目标节奏 |
| 🎓 **校园身份与可信交易** | 校园邮箱认证、公开档案、安全提示与确认记录 |
| 🎪 **校园专题与个人足迹** | 专题浏览、加入与退出、参与区间、目标进度、严格用户漏斗、已结束专题对比和运营导出 |

**专题详情商品区**按创建时间和主键稳定倒序分页，每页 12 件；顶部“当前在售”始终展示全量计数，非法页码回到第一页、越界页码收敛到末页。加入、退出和摘要设置保留当前页码，操作后回到 `#campaign-items`。

**个人历史、专题足迹与领取申请**统一使用服务端分页：最近浏览每页 24 件；我的领取申请、发布者管理队列和专题历史参与每页 12 条。专题当前参与始终显示，历史按退出时间倒序分页；申请按待处理、已选中、未选中、已撤回的业务优先级排序。发布者顶部的待处理数始终按全量计算，专题订阅与退出、申请撤回都会安全回到原页码。

### 业务主线与旁路

```mermaid
flowchart LR
    Browse["发现 / 搜索"] --> Talk["沟通与预约"]
    Talk --> Deal["交付 / 归还"]
    Deal --> Trust["评价与可信记录"]
    Govern["治理与申诉"] -. 留痕 .-> Talk
    Ops["运营看板"] -. 只读观测 .-> Browse
    Ops -. 只读观测 .-> Deal
```

出售、免费赠送和限期借用会在预约后进入不同状态分支；求购匹配与失物招领是两条并行支线。失物招领的可疑记录可以进入专门的举报审核队列，
完整流程图见[校园交易闭环](docs/business-flow.svg)，业务规则见[功能手册](docs/feature-handbook.md)。

---

## 🧱 技术栈

| 层次 | 当前方案 |
| :--- | :--- |
| Web | Django 5.1+ · Django Templates · 轻量 JavaScript |
| 数据 | SQLite（本地默认）· PostgreSQL 16（生产） |
| 缓存 | LocMem（本地默认）· Redis 7（生产，含 `cached_db` 会话） |
| 文件存储 | 本地 `media/`（开发）· S3 / MinIO（生产） |
| 图片 | Pillow · EXIF 方向 · WebP 重编码 · 缩略图与响应式候选 |
| 部署 | Docker Compose · Gunicorn · WhiteNoise · `/healthz/` 与 `/readyz/` |
| 日志 | 逐行 JSON · 请求关联 ID · 可选 Loki / Alloy / Grafana 叠加层 |
| 备份 | PostgreSQL custom dump · S3 对象镜像 · SHA-256 校验 · 增量恢复 · 可选异地镜像 |
| 质量 | GitHub Actions · PostgreSQL/Redis 集成任务 · Django check · 迁移检查 · 自动化测试 |

项目保持 Django 应用的自然边界：

```text
accounts/         注册、登录、个人资料与校园认证
chat_messages/    留言、私信、内容审核与申诉
observability/    告警翻转历史与可观测性数据
listings/         商品、订单、搜索、通知、运营与治理
templates/        页面模板
static/           CSS、JavaScript 与静态资源
docs/             功能、设计、架构和运营文档
```

<p align="center">
  <a href="docs/architecture.svg"><img src="docs/architecture.svg" alt="拾光校园系统架构，展示 Django 应用、业务模块、数据存储、通知中心和运营看板之间的关系" width="100%" /></a>
</p>

<p align="center"><sub>应用边界、共享数据事实与运营读取路径 · 点击查看架构图原图</sub></p>

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
| 我的心愿单 | <http://127.0.0.1:8000/listings/favorites/> |
| 关注的搜索 | <http://127.0.0.1:8000/listings/saved-searches/> |
| 消息工作台 | <http://127.0.0.1:8000/messages/inbox/> |
| 通知中心 | <http://127.0.0.1:8000/listings/notifications/> |
| 校园专题 | <http://127.0.0.1:8000/listings/campaigns/> |
| 我的专题足迹 | <http://127.0.0.1:8000/listings/campaigns/mine/> |
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
| `DJANGO_DB_PATH` | 本地 SQLite 数据库文件路径 | `db.sqlite3` |
| `DJANGO_DATABASE_URL` | 生产 PostgreSQL 连接串 | 空（使用 SQLite） |
| `DJANGO_CACHE_URL` | 生产 Redis 连接串 | 空（使用 LocMem） |
| `DJANGO_STORAGE_BACKEND` | `filesystem`、`s3` 或 `minio` | `filesystem` |
| `DJANGO_S3_*` | bucket、endpoint、凭据与浏览器侧签名端点 | 空 |
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
- `/readyz/`：就绪检查，验证 PostgreSQL、Redis 和 S3/MinIO，失败时返回 `503`。

容器 `HEALTHCHECK` 只走 `/healthz/`。`/readyz/` 连着三个外部依赖，拿它当存活探针的话，数据库抖一次编排系统就会把每个副本判成 unhealthy 并重启，一次存储故障被放大成全站故障。所以 `docker compose ps` 里的 `healthy` 只代表进程活着，能否接流量要看 `/readyz/`。

进程级指标暴露在 `/metrics/`：默认返回 404，配置 `DJANGO_METRICS_TOKEN` 后需带
`Authorization: Bearer` 令牌抓取。默认缓存是跨进程后端（生产即 Redis）时，每个 worker 每五秒把整份快照写进共享存储，一次抓取拿到的是整个舰队的合并视图；没启用共享时才退回到「只看到这一个副本」。启用共享后只能配一个抓取目标，否则总量会按副本数翻倍。八条盯住观测链路自己的规则在抓取那一刻评估——视图陈旧、读取失败上升、分桶不符、实例数下降、标签溢出、5xx 占比、p95 与共享未启用——结果以 `/metrics/` 末尾的 `campus_glimmer_alert_*` 序列和人读的 `/alerts/` JSON 给出；`/alerts/` 同时返回最近 100 条 `firing/resolved` 翻转（最新在前；默认写入数据库并保留最近 1000 条，数据库不可用时自动退回进程内窗口并标记降级），`campus_glimmer_alert_transitions_total` 按事件累计。配置 `DJANGO_ALERT_WEBHOOK_URL` 后，`pending → firing` 与 `firing → inactive` 会先写入数据库 outbox 再立即投递，持续 firing 不重复；失败按指数退避重试，`/alerts/` 的 `notification_queue` 显示队列状态；同一份摘要还会以固定五类状态的 `campus_glimmer_alert_notification_queue{status="..."}`、`_queue_due`、`_queue_enabled` 与 `_queue_degraded` 序列进入 `/metrics/`，数据库异常也不会让抓取失败，口径见[按指标告警设计](docs/metrics-alerting-design.md)。

本地 Compose 继续使用 SQLite 与本地媒体文件。需要 PostgreSQL、Redis、MinIO 和定时备份时，使用 [生产编排](docker-compose.prod.yml)：

```bash
cp .env.production.example .env.production
# 替换所有 CHANGE_ME，并保证数据库/Redis 密码与连接串一致
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
```

需要集中查看多副本日志时，再叠加 Loki、Alloy 与 Grafana。三者只监听宿主机
`127.0.0.1`，默认保留 7 天；启动前要在 `.env.production` 设置
`GRAFANA_ADMIN_PASSWORD`：

```bash
docker compose --env-file .env.production \
  -f docker-compose.prod.yml \
  -f docker-compose.observability.yml up -d --build
```

日志字段契约、查询示例、Docker socket 权限边界和回滚步骤见[集中日志设计](docs/log-aggregation-design.md)。

<details>
<summary><strong>生产部署检查清单</strong></summary>

- 替换 `DJANGO_SECRET_KEY`，关闭调试模式并配置允许域名；
- 配置 PostgreSQL、Redis、MinIO 或等价托管服务，生产启动会拒绝占位配置；
- `DJANGO_S3_PUBLIC_ENDPOINT_URL` 必须是浏览器可访问的域名或端口；
- 启用 HTTPS 后配置安全重定向、Session/CSRF Cookie 和可信来源；
- 域名与回滚窗口确认后再设置 `DJANGO_SECURE_HSTS_SECONDS`；
- 需要不随宿主机一起损失的副本时配置 `BACKUP_OFFSITE_*`（留空即关闭，只填其中几项会报错），并定期做恢复演练；
- 需要采集指标时再设置 `DJANGO_METRICS_TOKEN`，留空即保持 `/metrics/` 关闭；
- 需要接收告警状态翻转时配置 `DJANGO_ALERT_WEBHOOK_URL` 和可选的 Bearer 令牌；留空即关闭通知，配置后默认由数据库 outbox 做有界重试；
- 需要集中日志时设置 `GRAFANA_ADMIN_PASSWORD` 并叠加 `docker-compose.observability.yml`，只从受信任宿主机访问三个 loopback 管理端口；
- 应用日志使用逐行 JSON，访问日志不会记录查询字符串、Cookie 或 Authorization；
- 不提交生产数据库、上传文件、密钥或包含用户隐私的日志；
- 当前生产编排是单机、小规模基线，不提供自动故障转移或异地容灾。

详细步骤见[生产部署基线](docs/production-deployment.md)与[备份恢复](docs/backup-restore.md)。

</details>

---

<a id="engineering"></a>
## 🧪 工程与测试

提交前建议运行：

```bash
python -m compileall -q accounts campus_glimmer chat_messages listings observability
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
git diff --check
```

除完整测试集外，仓库里还有一批「页面不报错、只有截图才看得出来」的守卫测试：
标题层级与表单标签看 `campus_glimmer/test_page_semantics.py`，页内跳转落点是否让出吸顶导航
看 `campus_glimmer/test_anchor_scroll_margin.py`。改动模板、样式或表单时，这两个文件要一起跑。

GitHub Actions 在推送到 `main` 或提交 Pull Request 时执行语法检查、Django check、
迁移一致性检查和完整测试集。四个任务分工不同：`quality` 在 SQLite 下跑完整业务测试，
`production-deps` 用 PostgreSQL 16 与 Redis 7 再跑一遍，`object-storage` 在 runner 上启动
MinIO，对对象存储做真实上传、签名 URL 与备份恢复验证；`backup-restore` 把数据库 dump 灌进
一个全新的隔离库逐表比对，并用不匹配的客户端版本验证护栏会拦。目标不是只看“能启动”，
而是让每次迭代都能独立验证和回溯。

### 定时任务

周期任务统一封装成 Django management command，可接入 cron、系统计划任务或容器调度。

| 命令 | 用途 | 建议 |
| :--- | :--- | :--- |
| `process_order_timeouts` | 预约超时、归还提醒与逾期催收 | 周期性运行 |
| `send_meeting_response_reminders` | 提醒长时间未回应的交付安排 | 周期性运行 |
| `expire_items` | 下架到期商品并通知发布者 | 每日运行 |
| `send_operations_digest` | 生成管理员运营告警日报 | 按统计周期运行 |
| `flush_alert_notifications` | 重试到期告警通知并清理旧台账 | 每分钟运行，或由抓取与恢复流程触发 |
| `send_opportunity_digest` | 发送互助机会摘要 | 每日运行 |
| `send_saved_search_digest` | 发送关注搜索汇总 | 按日 / 周运行 |
| `send_campaign_update_digest` | 发送校园专题上新摘要 | 按日 / 周运行 |
| `send_lifecycle_reminders` | 发送商品生命周期提醒 | 每日运行 |
| `sync_academic_calendar` | 补齐学期阶段并刷新快照 | 开学前与每周运行 |
| `backfill_item_thumbnails` | 回填历史图片变体和宽高 | 上线辅助、分批运行 |

参数、幂等边界和运行台账见[后台与定时任务](docs/scheduled-tasks.md)。

### 权限与安全

- 普通用户只能编辑自己的商品，查看和操作自己参与的订单、私信、通知与收藏；
- 管理员可以处理治理事项，但不能绕过服务端权限检查代替用户完成交易操作；
- 项目默认启用 `nosniff`、`Referrer-Policy`、`Cross-Origin-Opener-Policy`、
  `X-Frame-Options` 和 HttpOnly Cookie；
- 生产环境的 400、权限不足、CSRF 校验失败、404 与 500 页面使用同一套品牌化错误体验：
  说明发生了什么、给出安全返回路径，不展示异常文本、堆栈或用户数据；500 页面使用独立静态外壳，
  避免数据库或会话异常时再次级联失败；
- 订单创建使用事务和锁，争议证据、通知读取、运营数据和订单操作均在服务端校验身份。

详细边界见[当前取舍与边界](docs/product-boundaries.md)。

### 可访问性

文字对比度按 WCAG AA 逐页量取，不靠肉眼估。做法是用无头浏览器把每个页面的文字节点
连同它真正合成出来的背景色一起读回来，再按 AA 公式复算：这一轮量完 53 个页面，
从 463 处不达标降到 0。容易漏的是三类位置——深绿渐变面板上的浅色文字（单看任何一个
色标都够深，合成之后的最亮处不够）、半透明前景（`rgba(255,255,255,.76)` 不是「白字」），
以及被全站几百处引用的令牌（`--muted` 动一次，所有次要文字一起动）。

结论落在 `campus_glimmer/test_contrast_contract.py` 里：它按与浏览器一致的分层合成规则
复算每个浅色文字位点的最坏背景，再断言达到 4.5:1。改渐变色标、高光透明度、文字透明度
或令牌值，这里会立刻变红，不必再为此起一轮浏览器审计。分页器也会把当前页渲染成不可点击的
`aria-current="page"` 指示器，并给普通数字页补“第 N 页”名称；商品、专题、失物招领和举报审核
的第二页渲染都有回归测试，不再只依赖模板源码检查。键盘焦点、窄屏布局与图片加载仍在逐页补齐，
见[后续路线图](docs/roadmap.md)。

---

## 🗓️ 后续路线图

- **供给侧与提醒**：完善刷新台账覆盖和生命周期提醒效果；
- **搜索、供需与机会流**：继续完善空结果后的需求承接、匹配解释和推荐解释；
- **交易、交付与借用**：交付安排提醒后的阅读与协商动作已进入观察，并用“提醒后待人工核对”清单承接超窗无行动与观察中样本；复核已支持结构化数据异常原因、到期复查、逾期分档、完整历史回看，以及按 `next_review_at` 统计的到期机会、覆盖率、期末待处理、提前重排和处理吞吐；已承接机会继续区分按时、逾期、继续排队与关闭复查，并观察中位处理延迟。下一步继续强化交付异常、归还质量、催收效果和安全确认；
- **治理与通知**：继续做治理分组、申诉质量和通知降噪；
- **校园场景与信任**：完善认证体验、可信档案和地点维护；
- **运营与数据质量**：加强导出一致性、样本审计与指标血缘；
- **基础设施与体验**：进程级 `/metrics/`、跨副本联合视图、按指标告警、带健康指标的 Webhook outbox、跨副本去重、最近 100 条数据库持久化翻转历史（表内保留 1000 条），以及 JSON 日志 + 可选 Loki / Alloy / Grafana 集中查询已经落地；接下来补默认启用的异机备份、恢复演练与外部升级路径。Web 侧的无状态依赖与多副本端口边界已经落地，继续优化移动端、无障碍和图片加载。

每个方向都拆成可独立验证、独立回滚的小迭代。详细待办、已落地范围和不做某项功能的理由见
[后续路线图](docs/roadmap.md)。

---

<a id="docs"></a>
## 📚 文档地图

| 想了解什么 | 从这里开始 |
| :--- | :--- |
| 完整项目说明、系统概览与本地运行 | [完整项目手册](docs/README.md) |
| 九条业务链路的完整规则、指标与边缘情况 | [功能手册](docs/feature-handbook.md) |
| 核心 ORM 模型、关键字段与约束 | [核心数据模型](docs/data-model.md) |
| 代码模块、测试与设计文档索引 | [模块与测试地图](docs/module-map.md) |
| 周期任务、幂等边界与运行台账 | [后台与定时任务](docs/scheduled-tasks.md) |
| PostgreSQL、Redis、对象存储与生产启动 | [生产部署基线](docs/production-deployment.md) |
| 备份内容、校验、保留与恢复演练 | [备份与恢复](docs/backup-restore.md) |
| 专题目标、严格漏斗与跨专题样本口径 | [专题目标与转化漏斗](docs/campaign-goals-funnel-design.md) |
| 求购确认后的订单转化、预约与交付阶段、免费领取和重新选择边界 | [求购响应转化设计](docs/demand-response-conversion-design.md) |
| 举报与争议队列的分页、排序与全量统计 | [治理队列分页设计](docs/governance-queue-pagination-design.md) |
| 预约异常的质量指标、处理动作链与旧数据边界 | [预约异常质量与处理链设计](docs/meeting-incident-quality-design.md) |
| 400 / 403 / 404 / 500 与 CSRF 错误页的恢复路径和隐私边界 | [品牌化错误体验设计](docs/error-experience-design.md) |
| 深绿面板浅色文字的合成规则与 AA 下限 | [文字对比度契约](docs/contrast-contract-design.md) |
| 浏览足迹、专题历史与免费领取申请队列的分页边界 | [个人队列分页设计](docs/personal-queue-pagination-design.md) |
| 学期阶段、统计快照与交易高峰 | [学期日历与交易高峰](docs/academic-calendar.md) |
| 当前取舍、隐私边界与实现纪律 | [当前取舍与边界](docs/product-boundaries.md) |
| 后续方向与拆分原则 | [后续路线图](docs/roadmap.md) |
| 系统界面与截图维护 | [系统界面预览](docs/screenshots/README.md) |
| 按源码目录了解模块 | [目录级 README 索引](docs/README.md#directory-readmes) |
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

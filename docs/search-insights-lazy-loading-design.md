# 搜索洞察分区懒加载与导出同构

状态：已落地
涉及模块：`listings/analytics.py`、`listings/search_insights_fragments.py`、`listings/views.py`、`listings/urls.py`
涉及页面：搜索需求洞察（`operations/search_insights.html`）与它的 CSV 导出
关联设计：[运营看板构建耗时](dashboard-build-performance-design.md)

## 1. 要回答的问题

搜索洞察这页原来和运营看板改造前一样：一次请求里视图顺序调用九个构建器，模板把十几个分区一次吐完。它有三个具体问题：

1. 首屏只有八张指标卡和一张搜索质量分，运营第一眼要看的东西不到整页的六分之一，剩下十几个分区要拖着一整页查询才轮到；
2. 页面和 CSV 导出各自维护了一份面板列表，两边漂移了七处——有的面板在导出里整节失踪，有的换了名字，运营下载回去读到的表和屏幕上看的对不上；
3. 模板单文件 433 行，改任何一个分区都要在几十个 section 里定位，而且没有任何机制保证新分区被登记。

本迭代做两件事：把这页照运营看板的模式改成**分区懒加载**，以及**把 CSV 导出补齐到与页面同构**。前者是性能，后者是正确性——后者其实更严重，因为性能问题用户能感觉到，导出缺节只有把两边放在一起比才能发现。

## 2. 为什么这页比运营看板多一列

运营看板的裁剪很简单：看板核心统计跑一次，其余每个分区点名自己要哪些面板构建器即可。搜索洞察不行——它的九个构建器里有八个各喂一个分区，剩下的全部统计都挤在 `build_search_insights` 这一个 builder 里。

所以 `listings/search_insights_fragments.py` 的登记表是三列而不是两列：

| 列 | 含义 |
| --- | --- |
| `partial` | 分区内容放在哪个片段文件里 |
| `parts` | 分区要跑 `build_search_insights` 内部的哪些统计块 |
| `panels` | 分区要跑哪些独立面板构建器 |

`parts` 的取值来自 `analytics.SEARCH_INSIGHTS_PARTS`：`terms`、`counts`、`clicks`、`metrics`、`quality`、`rhythm`、`comparison`、`facets`、`insights`。块与块之间还有依赖（指标卡要搜索词数量，于是 `metrics` 要 `terms`；质量分的点击率来自 `clicks`，于是 `quality` 要 `counts` 和 `clicks`；运营建议读缺口词，于是 `insights` 要 `terms`）。**依赖闭包放在 builder 一侧**（`SEARCH_INSIGHTS_PART_DEPENDENCIES` + `_close_search_insight_parts`），分区表只写直接依赖。这样直接调 `build_search_insights(parts=...)` 的人不必先手工摊平依赖，测试也不必重复一遍业务里的先后关系。

## 3. 需求与验收

### R1 分区表是唯一权威，且被反查

`SEARCH_INSIGHTS_SECTION_FRAGMENTS` 是「渲染这个分区需要什么」的唯一权威。`listings/test_search_insights_fragments.py` 拿一次真实渲染插桩，看每个分区到底读了哪些顶层键，再和这张表对：漏登记、多登记都会红。

验收：新增一个分区而不在表里登记，测试立刻失败，症状不会拖延到用户滚到那一块才发现它是空态。

### R2 不带 cookie 时逐字节等价

`build_search_insights(parts=None)` 与加这个参数之前的行为一致。`si_lazy` cookie 由 `base.html` 里一段内联脚本种下，所以关掉 JavaScript 的浏览器永远拿不到它，也就永远拿全量页面；Django 测试客户端同样不受影响。

验收：整页渲染与改造前逐字节一致（本轮已用对拍脚本验证过，脚本本身不入库）。

### R3 取回端点与整页同一套权限

`search_insights_fragment` 判定 staff，非 staff（含匿名）一律 403，不 302 到登录页——调用方是 fetch，不是用户点击，跳登录页对脚本没有意义。

验收：匿名与非 staff 访问分区端点返回 403。

### R4 分区取回不跑无关查询

取回「搜索结果位置质量」不会顺带把趋势、偏好迁移和词表聚合跑一遍——那几个正是这页最贵的查询。

验收：分区端点只构建表里登记的那一个分区所需的部分。

### R5 页面与导出同构

CSV 导出里的每个节标题，必须和页面上面板的 `h2` 标题同名。默认要求同名，只有确实不能同名的情况才在 `test_search_insights_export_coverage.py` 的 `PANEL_TO_SECTION` 里显式登记并写明理由。

验收：面板标题逐个出现在导出首格集合里；`PANEL_TO_SECTION` 与 `NOT_EXPORTED` 目前都是空表。

## 4. 分区与上下文的对应

表的顺序就是页面顺序。首屏只有两个分区：`si-metrics`（八张指标卡）和 `si-quality`（搜索质量分），它们合起来是这页的记分卡；其余十三个分区带 `data-si-deferred`，滚到附近再取。

| 锚点 | 片段 | parts | panels |
| --- | --- | --- | --- |
| `si-metrics` | `_metrics.html` | `metrics` | — |
| `si-quality` | `_quality.html` | `quality` | — |
| `si-trend` | `_trend.html` | — | `search_trend`、`combo_shift` |
| `si-period-comparison` | `_period_comparison.html` | `comparison` | — |
| `si-rhythm` | `_rhythm.html` | `rhythm` | — |
| `si-terms` | `_terms.html` | `terms` | — |
| `si-items` | `_items.html` | `metrics` | — |
| `si-facet-gaps` | `_facet_gaps.html` | `facets` | — |
| `si-rewrites` | `_rewrites.html` | — | `rewrite_candidates` |
| `si-synonym-effects` | `_synonym_effects.html` | — | `synonym_effects` |
| `si-rejection-stats` | `_rejection_stats.html` | — | `rejection_stats` |
| `si-rejection-recurrence` | `_rejection_recurrence.html` | — | `rejection_recurrence` |
| `si-threshold` | `_threshold.html` | — | `threshold_feedback` |
| `si-position-quality` | `_position_quality.html` | — | `position_quality` |
| `si-insights` | `_insights.html` | `insights` | — |

面板注册表 `SEARCH_INSIGHTS_PANEL_BUILDERS` 和运营看板那张一样是同构三元组（键、中文标签、构建器）。八个构建器里只有趋势和偏好迁移吃 `query`，剩下两个小包装 `_ignores_query` / `_takes_query` 把签名统一成 `(days, query)`。包装而不是改签名：这八个构建器各自还有别的调用方（定时任务、管理页），为了注册表整齐去改八个公开签名，会把同一次改动散到八个文件里。

否决原因的选项表（`rejection_reasons`）不查库，整页和分区取回永远给全量。否则同一个下拉框在两种入口下选项会不一样，而这种不一致在页面上看不出来。

## 5. 延后的两层，别混为一谈

CSS 那层是兜底：`html.js` 下面把延后分区的内容藏起来，滚近时由 JS 加 `is-revealed` 揭开。关掉脚本的浏览器没有 `html.js`，整页照常全量可见。

JS 那层才是懒加载：带 `si_lazy` cookie 的请求，服务端只渲染首屏两个分区，其余十三个只留一个空 `<section>` 加一条 `data-si-src` 取回地址。

于是「没有 JS 就没有数据」这个分支不存在：脚本关掉时连 cookie 都种不下，服务端只会给全量页面。

两页的揭开逻辑抽成了 `createSectionRevealer(panels, options)` 工厂，差别只有三处配置（分区选择器、取回地址属性、失败态属性）。复制一份出来的话，下次改揭开方向就得改两处，而那种 bug 在 Django 测试客户端里看不出来——那里干脆没有 JS。`test_operations_deferred_sections.py` 现在同时盯两页：工厂本体只允许一个 `innerHTML` 赋值点、必须有 `classList.add('is-revealed')` 和 `fetch(`，两个调用点都必须各自配齐三个属性。

两个必须走「连同前面分区一起取回」的场合：点 hash 直达时，目标分区上面的分区还是骨架，`scrollIntoView` 会按骨架高度算距离，落到半路就停；快速滚动时 IntersectionObserver 只在回调那一刻判断是否相交，被一跳而过的分区早就出了根矩形，会永远停在骨架态。这两种都是「已经被滚过去或要被滚过去」，一并取回不会让用户少看东西。

## 6. 导出同构：七处历史差异的消法

| 原来的导出节标题 | 面板标题 | 处理 |
| --- | --- | --- |
| 搜索词分析 | 搜索词表现 | 改名 |
| 同义词候选 | 搜索同义词候选 | 改名 |
| 候选否决记录 | 候选否决分析 | 改名（表头与数据行两处） |
| 搜索趋势与筛选偏好对比 | 搜索趋势与筛选偏好 | 改名 |
| 高需求缺口 | 运营建议 | 改名 |
| 分类供给缺口 / 地点供给缺口（两节） | 分类与地点缺口 | 合并为一节，两个子标题「分类需求」「地点需求」 |
| 周期对比 | 需求变化 | 改名 |
| （缺失） | 供给缺口 | 新增节：`gap_terms` |
| （缺失） | 曝光最多的商品 | 新增节：`exposed_items` |
| （缺失） | 被点击最多的商品 | 新增节：`clicked_items` |
| （缺失） | 有搜索但未点击 | 新增节：`no_click_terms` |

方向是统一的：**改导出，不改页面**。运营在屏幕上看到什么标题，下载回去就该看到什么标题；反过来为了让测试通过而改页面文案，是把展示逻辑按导出便利性扭曲。`test_search_insights_export_coverage.py` 现在把这条线钉死，`PANEL_TO_SECTION` 空表就是它的成绩。

## 7. 边界与隐私

- 懒加载只改「哪些查询在这一刻跑」，不改任何指标口径、分母、排序和阈值；
- 分区端点不引入新的权限模型：staff 判定和整页同一处，匿名与普通用户同样 403；
- 取回地址只带 `days` 和 `q`，不带用户身份、会话或签名参数；
- 失败态由 JS 挂属性并提示重试，不静默留空——静默的空分区没人会深究，用户只会以为这页本来就没数据；
- 页面与导出的隐私边界不变：只使用聚合后的业务记录，不展示用户身份或单次搜索详情。

## 8. 同名助手必须起别名

两个分区模块各有一对同名函数：`operations_fragments.section_panel_keys` /
`section_partial`，和 `search_insights_fragments` 里的那两个。名字相同、语义不同——各自查各自那张分区表。

本轮第一版在 `views.py` 里直接导入，第二个导入把第一个盖掉：运营看板的取回端点于是拿搜索洞察的表去查
`ops-demand-match` 这类锚点，一个键都匹配不上，**38 个分区里有 25 个静默变成空态**。页面其它部分完全
正常，因为不带 `ops_lazy` cookie 的全量渲染走的是另一条路；只有把取回结果和全量渲染逐字节比对才会发现，
那正是 `listings/test_operations_fragments.py` 的等价性测试抓到的。

约定由此定下：

* `views.py` 里搜索洞察那两个助手必须起别名导入（`search_insights_panel_keys` /
  `search_insights_partial`），并在导入处写明原因；
* `listings/test_search_insights_fragments.py` 的 `HelperNameCollisionTests` 把「views 里每个名字
  指向哪个模块」钉死，症状还没出现时就能定位到导入行，不必再从一片空分区倒查；
* 以后新增第三套分区表时，先给助手起别名再写视图。

这类 bug 值得单独记一条，是因为它同时踩中三个条件：两个模块名字相同、导入顺序有先有后、失败方式是
「静默少一块内容」而不是报错。三者去掉任何一个都更容易被发现。

# 首页推荐的反馈率，从一个下界变成一个测量

状态：已实现（只报告，不改推荐服务任何常数）
模块：`listings/recommendation_quality.py`
新增表：`RecommendationImpression`（迁移 `0061_recommendationimpression`）
写入点：`views.home`
面板：`recommendation-quality-panel`「首页推荐卡挑得准不准」

## 1. 来龙去脉

`recommendations.RecommendationService` 给每个登录用户算八件商品，按一个可解释的分数
排下来，首页「为你挑选」一栏渲染出来。用户点卡上的「想看看」或「不感兴趣」，落在
`RecommendationFeedback` 里，`(user, item)` 唯一。

`recommendation_quality` 上线时报了四组数：反馈率、正负比例、按理由拆、反馈之后用户
做了什么。前三组的分子是反馈，第四组的分母也是反馈——只有反馈率那一组，分母取的是
「窗口内全部被点开过的商品」，直接数 `BrowsingHistory` 去重后的 (用户, 商品) 对。

当时的设计文档把这个取舍写得很硬：**不加曝光表，首页不该为一个运营面板多一次
insert。** 于是反馈率报出来的是一个下界——一件从搜索点进去的商品也占了分母，却没有
推荐卡可点。

上一轮（`docs/opportunity-impression-design.md`）把同一个取舍在机会流上做成了结论：
值得。机会流每次渲染落一行 `OpportunityImpression`，忽略率从一个形状变成一个真实
的比例。本轮把那个结论搬到首页推荐：这是同一条路的第二次走，不是新造需求。

## 2. 这一轮和机会流那次是同一个取舍，但有一处关键的不同

相同的地方：都是「把被看见这件事落库」，都是不设唯一约束，都是分母只数窗口内第一
次被看见的 (用户, 目标) 对，都只报下界，都不改任何权重。

**不同的地方，也是本轮唯一值得单独论证的新东西：`RecommendationImpression` 存
`reason_key` 和 `score`，机会流那边刻意没存。**

机会流不存，是因为那边的分数和理由在忽略时刻要重算——`build_opportunity_feed`
的匹配是双向的（求购对在售商品、失物对活跃记录），同一条机会在不同时刻进池分数会
变，`OpportunityDismissal` 里没有当时的分数可查，`opportunity_dismissal_quality`
的 `_recompute_demand_score` 就是为此存在的。曝光表跟着存一份「当时的分数」只会得
到一份要和复算结果对齐、却永远对不齐的数据。

首页这边反过来：`Recommendation` 是一个 frozen dataclass，`reason` 和 `score` 在
渲染那一刻就已经算出来了，跟着 `item` 一起在内存里。落库是零成本的——`get()` 里
两行一起构造，`views.home` 里一次 `bulk_create`，不额外查任何库。

于是两件事第一次同时成立：

1. 「按理由拆的反馈率」有了**推荐当时的**分母。`recommendation_quality` 现有的
   按理由拆分仍然保留，它量的是**反馈时刻重算**的理由，回答「这句理由今天还成立
   吗」；新表量的是**曝光当时**的理由，回答「当时挂这句话的卡，用户点了吗」。两个
   都要，面板上必须说清差别，不能让运营以为其中一个是另一个的修正版。
2. 分数本身第一次可归因。`+10` 的反馈匹配、`+8` 的分类匹配、`+5` 的地点匹配各自
   把多少商品推上了首页，此前只能从理由反推（一个理由可能对应多个分数组合），现在
   可以直接读。

## 3. 事实链

写入点只有一处，`views.home`：

```python
recommendations = get_recommendations(request.user, limit=8)
_record_recommendation_impressions(request.user, recommendations)
return render(request, 'listings/home.html', context)
```

`get_recommendations` 已经返回 `Recommendation(item, reason, score)`，本轮给它加第四
个字段 `reason_key`（见第 4 节）。匿名访客也会走到这里——他们的 `recommendations`
不是空的，见 5.5。`_record_recommendation_impressions` 把这一屏转成
`RecommendationImpression` 行，一次 `bulk_create(rows, batch_size=32)`。一屏最多 8
件（`get_recommendations` 的 `limit=8`），所以一次渲染就是一次 round-trip，不随商品
总数增长。

字段：`user`（可空，见 5.5）、`item`、`reason_key`、`score`、`position`
（1–8，照 `SearchImpression`）、`created_at`（`auto_now_add`，照 `SearchImpression`
而不是 `OpportunityImpression` 的显式 `shown_at`——那边要 `bulk_create` 后回填时
间，这边不需要）。

## 4. `Recommendation` 加 `reason_key`：唯一的写入路径改动

`recommendations.py` 的 `Recommendation` 是 frozen dataclass，`reason` 存的是**中文
文案**。中文文案不能当分组键（运营改一句文案，历史行就全部归错组），所以新表要的是
`reason_key`。

两个选择：在新模块里反查 `REASON_LABELS`，或者让 `Recommendation` 多带一个字段。
选后者，理由：反查是 `{v: k for k, v in REASON_LABELS.items()}[rec.reason]`，把一
个渲染细节（文案）当成数据键来用，文案一改、同义词一多，反查就开始静默失败；而
`reason_key` 在 `get()` 里已经作为中间变量算出来了，多带一个字段是把它已经算出的
东西带出来，不重算、不改任何分数、不改任何文案。

这是本轮对业务代码的唯一改动。它不影响任何现有调用方（dataclass 加字段，用关键
字构造的地方照常工作），也不改变首页渲染的任何输出。

## 5. 口径（五条，逐条都是取舍）

### 5.1 不设 `(user, item)` 唯一约束

`RecommendationFeedback` 上有 `unique_recommendation_feedback`，因为表态该折叠：
同一个人第二次按按钮只是改口。**展示不该折叠。** 同一件商品在不同时刻被渲染给同一
个人是两次独立的展示，去重会让分母变小、反馈率虚高。

去重只发生在算「第一次被看见」的那一刻。

### 5.2 分母只数窗口内第一次被看见的 (用户, 商品) 对

一条商品在窗口外就被看见过、窗口内又被看见，它的分母会被计两次以上，而分子（反馈
行）只可能在窗口内产生一条。分母重复计、分子不重复计，率会虚低。

### 5.3 分子要求「这件商品在窗口内至少被展示过一次」

一条反馈若来自窗口外的展示（用户先看到、过了几天才按按钮），它没有窗口内的分母。
把它算进分子会让率虚高，所以分子规则是「这条反馈对应的商品在窗口内至少被展示过
一次」。

### 5.4 反馈率仍是下界，只低不高

三种偏低来源，方向都可接受：

1. `RecommendationFeedback.updated_at` 可能在窗口外（用户很早就表过态、窗口内又点
   了一次），按「反馈时刻归期」它进不了分子，分母照算；
2. 用户从别处点进商品、没有推荐卡可点的，**这一条本轮解决了**——它不再占分母；
3. 一件商品在窗口内被看见、用户在窗口内才按按钮，但按钮的动机来自窗口外那次看见。

**虚高的反馈率会让运营以为推荐在惹恼用户，虚低只会让人保守。**

空分母报 `None`，不报 0——一周一件都没展示过，反馈率不存在，不是零。分母门槛
`MIN_RECOMMENDATION_IMPRESSIONS = 24`，与 `opportunity_dismissal_quality` 的
`MIN_IMPRESSIONS` 同量级，与 `MIN_REASON_FEEDBACK = 12` 分开立（前者是曝光门槛，
后者是反馈门槛，复用一个会让「够不够样本」这件事在两张表上含义不同）。

### 5.5 匿名访客也写，`user` 存 null

**这一条是设计期间被代码纠正的，原样留在这里。** 最初写的是「只给登录用户写」，
依据是 `RecommendationService._load_preferences` 对匿名用户直接 return，于是推断匿名
用户拿到的推荐是空的。读了 `RecommendationService.get()` 才发现不是：`get()` 只把
`is_authenticated` 用在「排除自己卖的商品、排除已忽略的」那两个过滤上，匿名用户得
到的是**全站在售商品按分排下来的前八件**。模板 `home.html` 也证实了这一点——「为你
挑选」那一栏对没登录的人照常渲染，只是标题换成「校园里正在被关注」、两个反馈按钮
被 `{% if user.is_authenticated %}` 收起来，卡照常出。

所以匿名用户的看见必须落库，`user` 存 null。否则首页最大的那部分流量（没登录的
人）整块不在分母里，反馈率会变成一个只有登录用户才配被统计的数——那正是本轮要修
掉的那种偏。

`on_delete` 取 `SET_NULL` 而不是 `CASCADE`：账号注销之后那些看见仍然发生过，只是
再也归不到一个具体的人。这与 `OpportunityImpression.user` 的 `CASCADE` 不同，因为
机会流整页 `@login_required`，那边没有匿名行。

## 6. 看板回答什么

- 窗口内有多少件商品第一次被看见（真实分母），其中多少件收到了反馈，反馈率是多少；
- 按 `reason_key` 拆的反馈率：五句理由各自的曝光分母与反馈分子，这是本轮真正的新
  读数——「当时挂『你标记过想看，优先为你保留』这句话的卡，用户点了多少」；
- 按分数档拆的曝光规模：`+10`/`+8`/`+5` 三档各自把多少商品推上首页，回答「这套打
  分今天在推什么」；
- 按理由拆的正负比例、改口数、反馈之后做了什么——上一轮那四组数一个没少，其中按
  理由拆分仍是**反馈时刻重算**的理由，和新表的口径差别在面板上原样带到。

## 7. 口径汇总

- 渲染路径只多一次 `bulk_create`，不多任何一次 select；
- 没有推荐可渲染时（空平台）一次 insert 都不发；匿名访客照常写，`user` 存 null；
- 写不进去不致命：页面照常渲染，只是这一次展示进不了分母；
- 不回填历史数据，曝光表自上线起才开始积累，此前周期的反馈率仍是 `None`；
- 分子分母两个方向都只数窗口内产生的行，归期按 `created_at`；
- 理由的复算逻辑一行不改，`_recompute_reason` 原样保留；
- 不按用户排名，不判断反馈是否「正确」。

## 8. 不做什么

- **不给 `RecommendationImpression` 设 `(user, item)` 唯一约束**，见 5.1。
- **不改推荐权重。** +10、+8、+5、0.8、新鲜度 4 分，一个常数都不动。反馈率是给运
  营看的读数，不是给推荐服务看的训练信号——这条项目里所有推荐类模块一律如此。
- **不回填历史数据。** 回填需要伪造 `created_at`，伪造出来的分母比没有分母更糟。
- **不把 dismiss 回流成负样本。** `dismissed_item_ids` 的排除逻辑不动。
- **不改 `recommendation_quality` 现有的按理由拆分。** 它量的是反馈时刻重算的理
  由，新表量的是曝光当时的理由，两个都留，见第 2 节。
- **不做跨周期的曝光趋势。** 「这件商品这周被看见几次」是另一个模块的事。
- **不让首页推荐卡的数量随这个表变化。** 八张卡是产品决定，不是运营读数。

## 9. 先看板后改规则的理由

和机会流那次一样：分母自上线起才开始积累，第一个月的样本大概率够不到
`MIN_RECOMMENDATION_IMPRESSIONS`。而虚高的反馈率会推着人去动权重——这正是 5.4 把
方向钉成「只低不高」的原因。

先看板，是因为「当时挂『你标记过想看』这句话的卡，用户点了多少」这句话要等分母积
累够才说得出口。在那之前，看板上已经有的按理由正负比例、改口数、反馈后动作，足够
运营开始动手了。
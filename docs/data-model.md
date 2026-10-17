# 核心数据模型

[返回 README](../README.md) · [模块与测试地图](module-map.md) · [当前取舍与边界](product-boundaries.md)

> 本文档按业务领域整理项目的主要 ORM 模型与关键约束，便于理解数据边界和后续扩展点。

项目使用 Django ORM 管理业务数据，当前模型可以按以下几组理解：


<details>
<summary>模型分组（52 行，按业务领域归组）</summary>

| 领域 | 主要模型 | 作用 |
| --- | --- | --- |
| 商品 | `Category`、`CampusLocation`、`CampusLocationServiceWindow`、`CampusLocationServiceException`、`CampusLocationServiceExceptionWindow`、`Item`、`ItemImage`、`ItemPriceChange` | 商品分类、校园地点、每周服务时段与单日例外、商品主体、交易方式、图片、可选展示截止时间和出售价格轨迹 |
| 地点档案质量 | 不新增表，读 `CampusLocation` 与商品、求购、关注搜索、订单、交付安排的外键 | 报告地点档案形状、字段覆盖、当前引用、交付地点可解释性、近似重名和停用引用；只报告，不自动合并或改写任何地点与业务记录 |
| 地点维护队列 | 读 `CampusLocation` 与当前在售商品、活跃求购、启用关注搜索的地点引用 | 把停用引用、公共地点缺安全提示、近似重名和档案字段缺失排成互斥主优先级与可重叠信号清单；只提示人工核对，不自动修复 |
| 地点维护决策 | `CampusLocationMaintenanceDecision` | 保存同一地点同一组信号的人工结论、理由、复核人与时间；决策不隐藏当前信号，也不代替地点或业务记录修改 |
| 商品生命周期 | `Item.last_refreshed_at`、`Item.refresh_count`、`ItemRefreshEvent`、`Item.attention_reminder_sent_at`、`Item.price_drop_reminder_sent_at`、`LifecycleReminderEvent` | 商品最新刷新时间、累计次数与逐次真实刷新事件，两类提醒的冷却时间，以及逐商品、逐类型的真实发送事件台账；刷新和提醒台账都保留历史事实，历史缺口不回填 |
| 生命周期提醒效果 | `LifecycleReminderEvent`、`Item.attention_reminder_sent_at`、`Item.price_drop_reminder_sent_at`、`Item.category`、`Notification.read_at`、`OrderEvent.created_at`、`AcademicTerm`、`AcademicPhase` | 单期效果真实发送事件优先，旧数据回退商品冷却时间戳；通知是否被打开与订单终态时间共同决定「提醒之后有没有动作」。跨学期终态转化只读事件台账与订单终态，不用可覆盖的刷新时间判方向 |
| 分类调价参考 | 不新增表，只读 `Item.category` / `price` / `status` / `expires_at` / `created_at` | 分类级当前供给价格形状：优先近 180 天样本、每分类最多 500 件，当前在售单列低于 / 区间内 / 高于；样本不足、四分位退化与区间过宽都显式标记，只报告不自动改价 |
| 校园活动与报名 | `CampusCampaign`、`CampaignRegistration`、`CampaignUpdateMatch` | 有起止时间的专题活动、专题与在售商品的可选关联、用户逐次加入和退出的参与区间，以及主动订阅后的上新摘要命中缓冲；报名不创建订单或占用库存，命中也不预约商品 |
| 学期日历 | `AcademicTerm`、`AcademicPhase`、`AcademicTermSnapshot` | 学期起止日期、按偏移天数划分的阶段规则、阶段级统计快照（已结束阶段由 `build_academic_calendar` 读取，进行中阶段实时算） |
| 互动 | `Favorite`、`ItemAvailabilityWatch`、`Comment`、`PrivateMessage` | 收藏、有货提醒、公开留言和私信 |
| 交易 | `Order`、`OrderEvent`、`MeetingAppointment`、`DeliveryConfirmation`、`Rating` | 交易预约、交付时间协商、状态事件、双方确认和完成后的评价 |
| 交付开放时段风险 | 不新增表，读 `MeetingAppointment`、`CampusLocationServiceWindow`、`CampusLocationServiceException` | 把未来待确认或已确认的交付安排对照地点每周规则与单日例外，只提示人工核对，不改预约或地点配置 |
| 校园开放规则配置审计 | 不新增表，读 `CampusLocation`、`CampusLocationServiceWindow`、`CampusLocationServiceException`、`CampusLocationServiceExceptionWindow` | 解释每周时段、跨午夜、重叠或相邻、单日例外形状与生命周期；五类配置信号只提示人工确认，不自动修复规则 |
| 交易安全确认 | `OrderSafetyAcknowledgement` | 买卖双方对当前安全卡的版本化确认记录，按订单、角色和安全卡指纹唯一；历史记录保留用于审计 |
| 订单阶段链路 | `OrderEvent`（状态流转事件） | 每次状态变化都会留下带时间戳的事件记录，运营看板据此还原“下单 → 确认 → 交付 → 成交 / 借出 → 归还”的阶段耗时 |
| 搜索 | `SearchQuery`、`SearchClick`、`SearchImpression`、`SavedSearch`、`SearchSynonym` | 搜索需求、点击、曝光、保存搜索和联想词；无结果搜索会进入需求雷达 |
| 需求撮合 | `DemandPost`、`DemandResponse` | 求购发布、商品响应、确认匹配和需求转化 |
| 机会流曝光 | `OpportunityImpression`、`OpportunityDismissal` | 一次机会流渲染落一行的「被看见」，和一次「不再显示」落的「不看了」；两者口径对偶，忽略率的分母来自前者 |
| 通知 | `Notification`、`NotificationPreference` | 站内提醒、未读状态、阅读时间、通知偏好、求购关联、到期提醒和幂等键；通知类型包含商品生命周期提醒，可单独关闭。偏好开关先报形状；关闭后仍出现的历史通知再按发生时间与偏好行最后写入拆开，只给可疑下界，不把整行 `updated_at` 当成逐开关关闭时间 |
| 互助贡献 | `CommunityContribution`、`ContributionMilestoneEvent` | 可审计的贡献积分流水，以及等级与徽章首次达成的事件台账；台账有全量覆盖审计和同类学期同阶段节奏对照，里程碑只作运营事实，不触发额外通知、排名或规则修改 |
| 身份与信任 | `CampusDomain`、`CampusVerification`、`Rating` | 校园邮箱域名配置、身份认证和交易信誉摘要 |
| 治理 | `Report`、`ModerationEvent`、`OrderDispute`、`OrderDisputeEvidence` | 商品举报、交易争议和证据处理；内容审核保存命中规则、风险、复核结论，`receiver` 支持私信恢复，`released_at` 记录内容真正回到原渠道的时间 |
| 运营 | 订单、浏览、搜索、收藏、求购等行为记录的聚合 | 看板、漏斗、用户分层、回访率、需求雷达、跟进任务和运营告警 |
| 看板自身构建耗时 | 不新增表，请求内读共享构建注册表与 `perf_counter` 计时 | 核心统计 + 64 个共享面板；P50/P90 取最近秩，重复 key 单列；页面 Top 12、CSV 全量；不含渲染、序列化、筛选和下载，也不落库或跨请求平均 |
| 需求跟进闭环 | `DemandOpportunityTask.created_at`、`DemandOpportunityTask.radar_key`、`DemandOpportunityTask.available_supply`、`SearchQuery.result_count` | 任务创建时间是唯一稳定的分界线（雷达行是滚动聚合，内容每次刷新都变），创建时的供给数用于判断「动作是否真的发生」，搜索结果数用于判断「需求是否被满足」 |
| 同义词候选 | `SearchQuery.user_id`、`SearchQuery.query`、`SearchQuery.result_count`、`SearchQuery.created_at`、`SearchSynonym.keyword` / `synonym` / `is_active` | 同一次会话内相邻两次搜索的改口证据；已启用同义词的两个方向都参与去重，停用同义词不屏蔽候选 |
| 同义词效果回流 | 不新增表，读 `SearchSynonym.created_at` + `SearchQuery` | 确认时间是唯一稳定的分界线；搜索词记录的是学生原本输入的那一串（扩展只发生在查询内部），按扩展词统计会成为自己和自己比、永远报成功 |
| 搜索曝光与点击配对质量 | 不新增表，只读 `SearchQuery` + `SearchImpression` + `SearchClick` | 周期内点击可在曝光表上线以来寻找同一搜索、同一商品且不晚于点击的曝光；缺搜索记录、零位置、无法配对、点击早于曝光、未来时间各自单列，延迟从最早可配对曝光算起；不比较用户、不判成因、不输出搜索词、用户名或商品标题 |
| 新商品搜索曝光冷启动 | 不新增表，读 `Item` + `SearchImpression` + `SearchClick` | 以周期内发布且已满 48 小时观察期的商品为队列，看首曝延迟、仍未曝光、首曝后点击和分类差异；只读，不调排序、不置顶、不打商品分 |
| 筛选组合对比 | 不新增表，读 `SearchQuery.category_id` + `min_price` / `max_price` | 组合占比的分母是同一个分类内部的搜索次数，因此分类总量上涨不会把每个组合都推成上升；价格带判据与单维表同一套，闭带需要两个界都设、开区间顶带只需下限 |
| 候选门槛建议 | 不新增表，读 `SearchSynonym` + `SearchQuery` | 命中率衡量的是「候选池 + 人工确认」整条链而非候选池本身，因此只作为调整门槛的参考；词对大面积配错时判定为配对问题而非阈值问题，收紧门槛只会把错误藏起来 |
| 候选否决分析 | 新增表 `SearchSynonymRejection` + 读 `SearchQuery` | 否决记录本身落库（否决候选不落库），归一化词对与排查器同一套，因此「充电宝」和「充电宝 」落到同一行；出现次数由同一份改口日志反汇，否决按次数加权而不按否决条数 |
| 定时任务台账 | `TaskRun` | 每个管理命令每次运行的起止时间、耗时、状态、固定指标和失败摘要，用于回答「调度是否健康」 |
| 定时任务失败原因分类 | 不新增表，读 `TaskRun.note` | 把周期内失败的运行按“失败长什么样”归成配置、数据状态、外部依赖、未归类四类，并统计每类的次数、涉及命令与最近一次备注；只描述不下结论 |
| 定时任务健康度的跨周期趋势 | 不新增表，读 `TaskRun.started_at` + `duration_ms` + `status` | 把同一批运行切成本周期与上一个等长周期，比较成功率方向与耗时趋势。一次运行按它开始的时刻归期，否则卡在边界上的那次会算进下一个周期；耗时给中位数、最长一次和异常慢的次数，不给平均值（一次跑了两小时的运行会把平均值抬成一个从未发生过的数字）；只报告，不改 stale 阈值、不自动重试。 |
| 借用逾期催收 | `BorrowReturnEscalation` | 一笔借用订单当前的催收等级、累计升级次数、最近升级时间、说明和归还闭环时间，用于回答「这笔到底催到哪一步了」 |
| 借用逾期风险分层 | 不新增表，读 `Order` + `BorrowReturnEscalation` | 按分类与押金档位聚合的借用量、升级率、第三级率与闭环时长，用来回答「哪些品类天生更容易拖到第三级」；只读聚合，不新增写入路径 |
| 借用节奏与学期阶段交叉 | 不新增表，读 `Order` + `BorrowReturnEscalation` + `AcademicTerm` / `AcademicPhase` | 把风险分层的同一批借用按学期阶段再聚合一遍，给出阶段日均借用、日均第三级与阶段内分类明细，用来区分「考试周集中借出」和「这个品类本身难还」；只读聚合，不新增写入路径 |
| 借用催收跨周期时效对比 | 不新增表，读 `Order` + `BorrowReturnEscalation` | 当前周期与上一等长周期逐项比较；分子分母按同一笔借用归期，逾期天数不参与对比 |
| 机会流上被按「不再显示」的机会 | 新增 `OpportunityImpression`（机会流每次渲染写一行，一次 `bulk_create`），读它 + `OpportunityDismissal` + `Item` + `LostFoundPost` | 按机会类型、分数档和理由拆开忽略的规模与结构，并单独报重复忽略；忽略率的分母来自 `OpportunityImpression`，只数窗口内第一次被看见的（用户, 机会）对，分子要求该机会在窗口内至少被展示过一次，报出来的是下界、只低不高，空分母报 `None` 不报 0，分母不足 24 条只报数字不给方向；按分数档的忽略率带一行「今天已匹配不上」，避免看得见但算不出分数的展示从分母整个消失；重复忽略按 `expires_at` 与 `created_at` 的距离落在 (30, 35] 天判，是间接证据；分数在忽略时刻重算，走 `_match_demand` 与 `score_lost_found_posts` 自己；只报告不改任何一行的 `expires_at`、不改 30 天静默期 |
| 用户建的心愿单分组 | 不新增表，读 `FavoriteCollection` + `Favorite` + `Item` | 分组大小分布（分子分母都是分组）、空分组按 `created_at` 分「刚建」与「早就空着」（占空分组的分母是空分组自己）、收藏的分组渗透（已归组占全部收藏、有收藏且建过分组的人占有收藏用户——建分组不要求有收藏，取交集防占比破百）、分组名收敛（计数天然是用户数，只有 3 个以上用户用过的名字才进榜）；先收藏后归类按当场、事后、从未归类、归过类又取消四类分开报，宽限天数上面板，迁移前归组的老收藏单列不进分母（`Favorite.grouped_at` 由模型在 `collection_id` 变化时写，不用 `auto_now`，时间差在 Python 侧算以兼容 PostgreSQL）；不报分组改名频率（应用内没有改名路由）、不报删分组后收藏去向（只走 admin 且 `SET_NULL` 让删完的收藏和没归过类的一样）；「当前」是快照不是窗口，与窗口内新建规模分开报；只报告不改任何一条 `Favorite.collection` |
| 首页推荐卡挑得准不准 | 新增 `RecommendationImpression`（首页每次渲染写一行，一次 `bulk_create`），读它 + `RecommendationFeedback` + `Favorite` + `Order` | 按理由拆开统计反馈率、正负比例、改口数与反馈后动作；正负比例的分母只收有过反馈的推荐，反馈率的分母是「这张卡被看见过」（只低不高的下界，写入挂了只记日志）；另有两张按推荐当时的 `reason_key` 与 `score` 档拆开的表，这是首页这边独有的——frozen dataclass 在渲染那一刻就带着它们，落库零成本；理由的复算仍然读反馈时刻的输入，两个分母不要混；匿名访客的看见也计入分母（`user` 为 null，`on_delete=SET_NULL`）；只报告不改权重 |
| 推荐曝光日志质量 | 不新增表，只读 `RecommendationImpression` | 已落库行的字段形状、当前理由可解释性、零分、位置 1—8、未来时间、匿名或已注销访客、登录用户重复曝光和自然日覆盖；写失败只进日志所以数不出，没有渲染批次所以不倒推每屏缺几张；只报告，不改推荐和写入路径 |
| 机会流曝光日志质量 | 不新增表，只读 `OpportunityImpression` | 已落库曝光行的类型、目标一致性、合法重复、同刻重复、未来时间与自然日覆盖；页面与 CSV 同步保留最近 30 天逐日明细；只审计忽略率分母的形状，不重算忽略率，也不数不可见的写入失败 |
| 私信响应时效的跨周期对比 | 不新增表，读 `PrivateMessage.created_at` + `is_read` + `read_at` | 本周期与上一个等长周期逐项比较；时长按倍数判方向（中位达到上期 1.5 倍记为变慢），比例按百分点判；任一侧样本不足只报数字不猜方向，`read_at` 上线前就已读的老信计入打开数但不进时长样本 |
| 延后的通知，延了之后回来了吗 | 不新增表，读 `Notification.snoozed_until` + `read_at` + `created_at` | 把用户主动要求的等待从处理时长里拆出来：含延期、扣掉延期、用户主动要求三个数摆在一起不相加也不相减，第三个数是下界（`snoozed_until` 每次延后都被覆盖，前几次延掉的等待不在库里）；五种结局分开数，「还在延后中」不进阅读率分母，「到点之后仍未读」是右删失样本单独数不记 0 天；「此刻」两块不按统计周期过滤，周期开始之前就到期未读的那条恰恰是最不该被藏起来的一件；批量标记已读不清 `snoozed_until`，两个「读过」档位都混着一键清空的人，只报规模不读成「用户会提前回来」；按通知类型聚合，不列通知原文、不按用户排名；只报告，不加迁移、不加字段、不改写入路径、不自动取消延后、不自动标记已读 |
| 需求雷达命中率的跳周期趋势 | 整个 `DemandOpportunityTask` + `SearchQuery` | 同一个周期内的任务改变了多少，与上一周期比对命中率走向。取任务创建时刻作为归期，让刚刚建行的任务不会落入上一周期；只统计有证据的任务，只报告、不把命中率回流到任何字段。 |
| 需求雷达命中率的学期纵向对比 | 整个 `DemandOpportunityTask` 的 `created_at` + `AcademicTerm` / `AcademicPhase` + `SearchQuery` | 把同一个阶段在相邻两个学期里相比，而不是跟前三十天比。学期需求本身顺季节漂移，把上个月当成上个学期比会把季节性读成雷达在漂移；只在一个学期出现过的阶段不给方向，早于第一个学期配置的任务单独计数；只报告、不改加权。 |
| 学期阶段快照读路径 | 不新增表，读 `AcademicTermSnapshot` + `AcademicTerm` / `AcademicPhase` | 已结束阶段读匹配快照（四个 `created_at` 全是 `auto_now_add`，计数已封闭），进行中阶段实时算（快照最多是上周的）；实时行继续拆成“阶段未结束”和“已结束兜底”，后者给出缺行与区间失配告警，不自动补刷；不加缓存层、不改字段与约束、不加迁移 |
| 学期阶段快照质量 | 不新增表，读 `AcademicTermSnapshot` + `AcademicTerm` / `AcademicPhase` + `TaskRun` | 报这张长期只被写的表的规模、应有阶段覆盖率、孤儿行（匹配键失效，与学期停用残留分开）、过期快照（看阶段 `end_date` 不看 `refreshed_at`）和命令是否还在跑；读路径本身的缺行告警在学期日历面板上，质量面板不重算七个计数的偏差；只报告，不刷新、不删行、不改 `starts_on` |
| 跟进任务的接手等待与停留 | 不新增表，读 `DemandOpportunityTask` 的 `created_at` / `updated_at` / `status` / `assigned_to` / `due_at` / `note` | 一条状态机跑了这么久，而「动作是什么时候发生的」从来没被记下来：四个时间列里没有一个记的是「什么时候变成跟进中」。不加迁移、不加字段、不改写入路径，只在既有数据上把能说的和不能说的分开说。接手时长分两支：已接手的只报上界 `updated_at - created_at`，没人接手过的报 `now - created_at`（那一支中间没有任何一次写入，方向性错误就在这里——都用 `updated_at - created_at` 会把两组的可信度同时讲反）。停留时长只报当前快照 `now - updated_at`，这一条上 `updated_at` 恰好是正确的列。待处理清单只按「未闭环」筛，已接手但停住的同样在列；每行带 `has_handover` 把「没人管」和「接手后停住了」分开；最多列 20 条但总数照实报在 `row_total`，截断不等于「一共就这么多」。两个口径不能相加或相减；空平台报「—」不报 0 天；只报告，不自动派单、不改状态、不改截止时间、不按负责人排名。 |
| 需求雷达闭环的分层命中率 | 整个 `DemandOpportunityTask` 的 `category` / `location` + `SearchQuery` | 把同一周期的跟进任务按任务自己带的分类和地点切开，每个分层报自己的命中率。整体一个数字既可能是“每类都在 60% 左右”，也可能是“一类 95%、另一类 25%”，而这两者需要的回应相反。命中率只在层内读，不跟“该层占全部任务的比例”比；每层各自要过与整体相同的任务数门槛，门槛不为了表格好看而放开；任务数少的分层上的差值不算结论，这里不排序、不把命中率回流到机会分。 命中率只在层内读，不跟“该层占全部任务的比例”比；每层各自要过与整体相同的任务数门槛，门槛不为了表格好看而放开；任务数少的分层上的差值不算结论，这里不排序、不把命中率回流到机会分。 |

</details>

<details>
<summary>关键约束（14 条，含缩进子项）</summary>


- 收藏、商品有货提醒等重复关系通过数据库约束或幂等逻辑避免重复。

- 同一用户在同一校园专题下最多只有一条活跃报名；报名记录用 `joined_at` / `left_at` 保留参与区间，退出后重新加入不会覆盖旧历史，报名本身不预约商品、不占用库存。

- 订单创建在事务中校验并锁定商品，降低并发预约冲突。

- 通知读取、订单操作、评价、举报和运营数据访问均在服务端校验身份。

- 争议证据下载只允许订单买卖双方或管理员访问，不直接暴露原始 media 链接。

- 商品是否仍可预约由服务端的 `ItemQuerySet.available()` 和订单创建时的行级锁共同校验，不能只依赖前端按钮或定时任务是否及时运行。

- 搜索和运营数据尽量保存为结构化字段，便于重新计算和解释指标。

- 上传文件保存在 media 目录，数据库不直接保存二进制内容。

- 校园认证令牌只保存 Django 密码哈希，验证成功或重新发送后旧令牌不能继续使用。

- 校园邮箱域名由管理员显式配置，未配置或已停用的域名不能发起认证。

- 商品刷新按 24 小时最小间隔和每月 4 次上限控制，限流状态保存在商品上，刷新只改曝光排序，不改动商品内容和发布状态。

- 生命周期诊断在应用层计算，不回写商品状态，保证“待处理”只是一个可解释的运营口径而不是隐式过滤条件。

- 定时任务台账只保存可横向比较的标量指标，不原样保存命令返回的任意结构。
  - 失败记录写在独立短事务里，业务事务回滚也不会带走失败证据。

</details>

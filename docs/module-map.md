# 模块与测试地图

[返回 README](../README.md) · [功能手册](feature-handbook.md)

> 本索引由 README 的项目结构章节长期拆分而来，用于维护代码文件、测试模块和设计文档的对应关系。新增模块时先更新这里，再考虑是否需要在首页补充一句能力摘要。


顶层目录和根文件的分布如下。`listings/` 是业务主体，141 个模块全部列在后面的表里；最后一列标注这个模块是「读写」还是「只报告」——「只报告」是本项目的一条纪律：凡是统计与看板模块，都只读不写，也不把统计结果写回它观测的那张表。

```text
campus_glimmer/
├── accounts/                  # 注册、登录、个人资料和账户测试
├── chat_messages/             # 商品留言、私信、内容审核分级、误判放行与内容申诉闭环、私信响应时效、响应场景分层、响应时效跨周期对比和会话测试
├── campus_glimmer/            # Django 项目配置、根路由和健康检查
├── listings/                  # 商品、订单、搜索、通知、运营和治理业务（见下面的模块表）
│   ├── migrations/            # 数据库迁移
│   └── management/commands/   # 预约超时、商品到期、运营日报、机会摘要、生命周期提醒、学期日历等管理命令
├── templates/                 # Django 模板和页面布局
├── static/                    # CSS、JavaScript、图标等静态资源
├── media/                     # 本地开发时的上传文件目录
├── docker/                    # 容器相关辅助文件
├── docs/                      # 架构图、业务流程图和项目文档
├── Dockerfile
├── docker-compose.yml
├── init_data.py               # 本地演示数据初始化脚本
├── manage.py
├── requirements.txt
└── README.md
```

## 目录 README

| 目录 | README | 适合查看 |
| --- | --- | --- |
| `accounts/` | [账户与校园身份](../accounts/README.md) | 注册、资料和校园认证 |
| `chat_messages/` | [留言、私信与内容治理](../chat_messages/README.md) | 收件箱、审核和申诉 |
| `campus_glimmer/` | [Django 项目配置](../campus_glimmer/README.md) | 设置、根路由和健康检查 |
| `listings/` | [校园交易与运营核心](../listings/README.md) | 业务模块、定时任务和测试入口 |
| `templates/` | [服务端渲染页面](../templates/README.md) | 模板结构、公共布局和约定 |
| `static/` | [样式、脚本与基础图片](../static/README.md) | CSS、JavaScript 和静态资源 |
| `docker/` | [容器运行与健康检查](../docker/README.md) | 镜像启动、数据卷和探针 |
| `.github/` | [持续集成](../.github/CI.md) | GitHub Actions 与本地等价检查 |
| `docs/screenshots/` | [系统界面预览](screenshots/README.md) | 首页截图的来源与维护方式 |

## listings/ 的业务模块（141 个）


<details>
<summary>业务模块清单（141 个，按业务链路归组）</summary>

| 模块 | 做什么 | 读写 |
| --- | --- | --- |
| `admin.py` | 后台管理界面：商品、分类、地点、专题、订单、预约异常与举报的维护入口 | 读写 |
| `analytics.py` | 运营指标、漏斗、分层、回访和运营提醒 | 读写 |
| `dashboard_build_performance.py` | 运营看板共享构建器的请求内耗时、P50/P90、占比与重复 key 自检（不落库、不自动优化） | 只报告 |
| `availability.py` | 商品可用性与有货提醒 | 读写 |
| `first_viewed.py` | 商品第一次被看见之后的收藏与下单时延、来源与后续一步（只报告不改写） | 读写 |
| `bulk_listing_csv.py` | 毕业季批量发布 CSV 的 UTF-8 解码、固定表头、行级校验与预览草稿 | 只读 |
| `bulk_listing_xlsx.py` | 毕业季批量发布 XLSX 的第一工作表读取、安全单元格转换与共享行校验 | 只读 |
| `bulk_listing_template.py` | 毕业季批量发布 CSV / XLSX 固定表头模板的内存生成，含 XLSX 交易方式下拉与行内范围提示 | 只读 |
| `bulk_listing_import_summary.py` | 毕业季批量发布 CSV / XLSX 行级错误的重复问题分组与页面摘要 | 只读 |
| `forms.py` | 商品、搜索、通知偏好等表单 | 读写 |
| `image_processing.py` | 商品图片上传硬限制、降采样、EXIF 方向、WebP 重编码、列表缩略图与大图预览生成、内在宽高读取 | 读写 |
| `models.py` | 商品、地点、领取申请、订单、通知和机会忽略记录等核心模型 | 读写 |
| `notifications.py` | 通知生成和通知相关业务 | 读写 |
| `operations_digest.py` | 运营告警日报构建与幂等投递 | 读写 |
| `opportunity_digest.py` | 互助机会摘要通知构建与幂等投递 | 读写 |
| `order_maintenance.py` | 订单超时兜底与借用到期通知的批处理 | 读写 |
| `order_workflow.py` | 订单状态流转和交付相关逻辑 | 读写 |
| `meeting_scheduling.py` | 交付时间安排、冲突检测与开放时段推荐信号 | 读写 |
| `campus_location_service_hours.py` | 校园地点每周服务时段、单日开放例外、跨午夜截断与相邻窗口覆盖判断 | 只读 |
| `meeting_service_window_quality.py` | 未来交付安排对照每周开放时段与单日例外的只读风险审计、地点聚合与脱敏清单 | 只报告 |
| `campus_location_service_rule_quality.py` | 校园地点每周时段与单日例外的配置形状、例外生命周期、五类只读核对信号与脱敏清单 | 只报告 |
| `price_insights.py` | 同类价格参考与需求热度分析 | 读写 |
| `price_changes.py` | 出售价格轨迹与收藏商品降价提醒 | 读写 |
| `price_history.py` | 商品详情价格轨迹的连续性校验、最近窗口、走势、隐私粒度与卖家维护提示（只报告） | 只报告 |
| `price_response.py` | 价格调整后的搜索回响：调价前后各 72 小时的搜索曝光与点击、收藏、预约共同变化（只报告） | 只报告 |
| `category_price_reference.py` | 分类级调价参考聚合：样本范围、中位数与四分位区间、当前在售区间外规模和边界状态（只报告） | 只报告 |
| `price_change_quality.py` | 出售价格轨迹的方向、幅度、重复调价与分类视图（只报告不改写） | 只报告 |
| `campus_pulse.py` | 按校园地点聚合供需热力与运营压力 | 读写 |
| `location_registry_quality.py` | 校园地点档案形状、字段覆盖、当前引用、近似重名与停用引用（只读观测） | 只报告 |
| `location_maintenance_queue.py` | 校园地点维护信号的主优先级、重叠信号、行动提示与 20 条人工核对清单（只读分诊） | 只报告 |
| `location_maintenance_decision_review.py` | 校园地点维护决策的首次留痕时间分布、复核比例与暂缓停留清单（不计算历史处理率） | 只报告 |
| `campaign_analytics.py` | 校园专题周期、运营提示与严格用户转化漏斗；另含六类实时 CampaignGoal 目标的完成率、时间进度和节奏偏差 | 读写 |
| `campaign_phase_review.py` | 已结束专题按自身窗口均分三段，复盘供给、曝光、收藏、预约与完成交易的节奏；薄样本不判方向 | 只报告 |
| `campaign_registration.py` | 校园专题加入、退出、幂等状态与参与区间历史 | 读写 |
| `campaign_updates.py` | 主动订阅的专题上新摘要：幂等命中缓冲、发送前边界复核、每日 / 每周合并通知与批量取消审计 | 读写 |
| `demand_matching.py` | 新发布的商品与活跃求购的匹配撮合 | 读写 |
| `demand_radar.py` | 无结果搜索、求购与供给缺口的需求雷达 | 读写 |
| `demand_radar_hit_trend.py` | 需求雷达命中率的跨周期变化：按份额判方向、上一周期没有的类别标成新出现 | 读写 |
| `demand_radar_outcome.py` | 需求跟进任务创建前后的无结果率对比与闭环判定 | 读写 |
| `demand_radar_hit_layers.py` | 把闭环命中率按分类与地点切开，只报告不改加权 | 只报告 |
| `demand_radar_phase_trend.py` | 同一阶段跳学期对比命中率，只报告不改加权 | 只报告 |
| `demand_opportunity_followups.py` | 跟进任务截止时间与备注的填写率、逾期与完成率对比（只报告不改写） | 只报告 |
| `demand_task_aging.py` | 跟进任务的接手等待（上界/可读两支）与当前状态停留（只报告不改写） | 只报告 |
| `demand_match_quality.py` | 匹配打分与求购人真实决定的对照（只报告不改权重） | 只报告 |
| `mutual_aid_feedback_notes.py` | 互助反馈补充说明的填写情况与词频（只报告不改字段） | 只报告 |
| `search_rewrites.py` | 同一用户相邻搜索里的同义词候选挖掘（只提议不自动生效） | 只报告 |
| `search_synonym_effect.py` | 已确认同义词的无结果率回流与生效判定 | 读写 |
| `search_threshold_feedback.py` | 由命中率反推候选门槛建议（只建议不自动改） | 只报告 |
| `search_rejection_stats.py` | 否决记录的频率加权与门槛负向样本（只报告不改写） | 只报告 |
| `search_rejection_recurrence.py` | 否决的回头与被推翻：只报回头次数与覆盖，撤销读不到就不编数 | 只报告 |
| `search_impression_pairing_quality.py` | 搜索曝光与点击的配对质量：字段结构、可配对率、位置一致性、点击延迟与自然日覆盖（只报告，不改搜索写入与排序） | 只报告 |
| `search_exposure_cold_start.py` | 新商品从发布到首次搜索曝光的冷启动队列：48 小时观察缓冲、首曝延迟、异常时间形状与分类对照（只报告） | 只报告 |
| `search_position_quality.py` | 搜索结果位置分档点击率与排序效果（只报告不改权重） | 只报告 |
| `search_combo_shift.py` | 筛选组合的跳周期交叉对比（分类×价格带，只报告） | 只报告 |
| `notification_response.py` | 通知处理时效与触达质量统计 | 读写 |
| `notification_snooze_return.py` | 延后的通知到期后是否回来：把用户主动要求的等待从处理时长里拆出来 | 读写 |
| `notification_kind_coverage.py` | 通知类型的三张表对齐情况：KIND_CHOICES、偏好开关、表单字段现读对齐，上一轮的两个窟窿已补上，现在差集为空、由测试守住（只报告） | 只报告 |
| `notification_preference_bypass_diagnosis.py` | 通知偏好关闭后的异常链路诊断：与后关一致、偏好写入后仍出现、可疑行数下界与已知写入路径 | 只报告 |
| `notification_preference_usage.py` | 27 个通知类型开关各自的形状：开着、关掉、近期收到过，按「关掉它有没有交易后果」分四组（只报告，被挡掉的通知连一行都不建，所以不报开关的效果） | 只报告 |
| `notification_quiet_hours_shape.py` | 免打扰时段的形状：默认窗口采用率、同日 / 跨午夜、时长档、开始与结束小时、逐分钟叠加成小时曲线 | 只读 |
| `notification_aggregation_quality.py` | 通知聚合的质量：被聚合过的行 vs 从没被聚合过的行，阅读率与处理时长对照（只报告） | 只报告 |
| `report_priority.py` | 商品举报队列的服务端分页、SQL 优先排序、全量开放积压统计与具名解释信号，只排序不写回事实 | 只报告 |
| `report_resolution_quality.py` | 商品举报按原因的驳回率、审核意见填写情况、集中度、举报人分布与结果回访质量（只报告） | 只报告 |
| `meeting_attendance.py` | 交付预约的实际到场情况：响应分档、迟到区间与缺席分母（只报告不改写） | 读写 |
| `meeting_incident_quality.py` | 交付预约异常按类型的驳回率、处理意见填写情况、提交方向与提交人分布（只报告） | 只报告 |
| `saved_search_quiet_window.py` | 关注搜索临时静默的规模、缓冲区年龄与静默结束后的存活（只报告） | 只报告 |
| `saved_search_limit_shape.py` | 单次提醒条数的九档分布、截断规模与条数 × 节奏交叉（只报告） | 只报告 |
| `lost_found_matched_outcome.py` | 失物招领确认匹配之后的四种终局、截止时间已过的时长、匹配双向性与按终局拆的反馈提交（只报告） | 只报告 |
| `moderation_notice_preference.py` | 内容审核结果通知的开关状态、触达成因、已读率与关闭后的发言对照（只报告） | 只报告 |
| `order_stage_flow.py` | 订单全链路阶段时长与瓶颈分析 | 读写 |
| `order_presentation.py` | 订单详情页「下一步」卡片的标题与图标，按订单状态和查看者角色给出 | 只读 |
| `handoff_code_usage.py` | 当面交付取件码的生成与使用：签发到首次尝试的间隔、尝试次数与用不上的码（只报告不改写） | 读写 |
| `item_lifecycle.py` | 到期商品的过期处理：从公开商品流和推荐里隐藏并回写状态 | 读写 |
| `lifecycle_diagnostics.py` | 商品生命周期等级、刷新节奏与卖家侧诊断 | 读写 |
| `lifecycle_reminders.py` | 商品生命周期提醒的合并发送、事件台账写入与冷却回写；真实发送与 dry-run 边界由测试钉住 | 读写 |
| `lifecycle_reminder_effect.py` | 提醒后的触达、阅读、行动与分层统计；事件台账优先，旧数据回退冷却时间戳且不双计（只报告，不改发送策略） | 只报告 |
| `lifecycle_reminder_action_trend.py` | 提醒行动率的跨周期对比：只给行动率方向，打开率与商品数只报数 | 只报告 |
| `lifecycle_reminder_term_trend.py` | 生命周期提醒的跨学期终态转化：同一阶段对比相邻学期，事件台账分母、封闭观察窗口、订单终态动作与只读边界 | 只报告 |
| `supply_lifecycle.py` | 供给侧生命周期健康、卖家在管规模与运营建议 | 读写 |
| `refresh_quota_drift.py` | 刷新额度为什么是一次性的：累计计数器与「本月」文案的差量（只报告） | 只报告 |
| `item_refresh_event_ledger.py` | 商品刷新事件台账：逐次真实刷新、历史缺口、序列一致性与未来时间审计（只报告） | 只报告 |
| `refresh_response.py` | 商品刷新后的搜索回响：前后各 72 小时的搜索事件共同变化（只报告） | 只报告 |
| `relist_freshness.py` | 重新上架的商品为什么一回来就沉底：新鲜度欠账与刷新额度余量（只报告） | 只报告 |
| `task_runs.py` | 定时任务运行台账：每次运行的落库、失败记录与预演标记 | 读写 |
| `task_run_health.py` | 台账聚合：成功率、静默天数、连续失败与调度建议 | 读写 |
| `task_run_failure_causes.py` | 定时任务失败原因的分类聚合（配置/数据/外部依赖） | 读写 |
| `task_run_trend.py` | 台账的跨周期对比：成功率方向与耗时趋势 | 读写 |
| `task_run_duration_percentiles.py` | 按命令分组的耗时分位 P50/P90/P99 | 读写 |
| `task_run_hour_load.py` | 运行时长的按小时分布：本地小时分桶、拥塞判定与上一周期同一小时对比 | 读写 |
| `task_run_failure_cause_trend.py` | 失败原因的跨周期变化：按原因 × 两个等长周期按份额判方向 | 读写 |
| `task_run_overlap.py` | 命令的自我重叠：耗时对照运行间隔，重叠按台账真实对数 | 读写 |
| `task_run_ledger_integrity.py` | 定时任务台账的时间、状态、失败备注与指标形状完整性审计 | 只报告 |
| `governance_sla.py` | 五类治理队列的处理时长、超期清单与时限内完成率 | 读写 |
| `borrow_escalation.py` | 借用逾期催收三级阶梯、归还闭环与看板汇总 | 读写 |
| `borrow_risk.py` | 借用逾期风险分层：按分类与押金档位聚合升级率 | 读写 |
| `borrow_rhythm.py` | 借用节奏与学期阶段交叉：阶段日均口径与阶段内分类明细 | 读写 |
| `borrow_days_shape.py` | 默认借用天数的形状：期限档分布、配置与实际时长对照、借用天数 × 催收 | 只读 |
| `borrow_escalation_trend.py` | 借用催收的跨周期时效对比：与上一等长周期逐项比较 | 读写 |
| `lost_found_matching.py` | 失物招领规则匹配与可解释分数 | 读写 |
| `opportunity_feed.py` | 面向当前用户的互助机会聚合与去重 | 读写 |
| `circular_impact.py` | 隐私安全的校园循环影响力报告聚合 | 读写 |
| `contributions.py` | 校园互助贡献积分、等级与徽章的幂等记录、首次达成里程碑和历史回填（只累计自己的行为，不排名不公开） | 读写 |
| `contribution_tier_shape.py` | 积分等级与徽章的门槛形状：五级等级、七枚徽章、六种类型、幂等键业务域与重复分布（只报告，复用产品侧判定规则不本地复制） | 只报告 |
| `contribution_milestone_coverage.py` | 里程碑事实与贡献流水的全量覆盖审计：缺失、多余、规则快照差异与事实一致性分开报（只报告不修复） | 只报告 |
| `contribution_milestone_phase_rhythm.py` | 贡献里程碑在同类学期同一已结束阶段的达成节奏对照：先被覆盖审计阻塞，再用每百天强度比较，薄样本不判方向 | 只报告 |
| `recommendations.py` | 推荐结果和推荐反馈 | 读写 |
| `recommendation_impression_quality.py` | 首页推荐曝光日志的字段形状、重复曝光、匿名流量、位置与时间异常，以及自然日覆盖（只报告，不推断写入失败或每屏缺失） | 只报告 |
| `recommendation_quality.py` | 首页推荐卡的反馈率、正负比例、改口数与理由拆分，以及按推荐当时的理由和分数档拆开的反馈率（只报告不改权重） | 只报告 |
| `opportunity_dismissal_quality.py` | 机会流负反馈的规模、分数档与理由拆分、重复忽略，以及第一次可测的忽略率（只报告不改静默期） | 只报告 |
| `opportunity_impression_quality.py` | 机会流曝光日志的类型、目标一致性、重复展示、同刻重复、未来时间与自然日覆盖审计（只报告，不推断写入失败或每屏缺失） | 只报告 |
| `OpportunityImpression`（`listings/models.py`） | 机会流每次渲染落一行的「被看见」记录，是忽略率的分母 | 只追加 |
| `RecommendationImpression`（`listings/models.py`） | 首页每次渲染落一行的「被看见」记录，是反馈率真正的分母；同时存 `reason_key` 与 `score`，因为推荐那一刻它们就在内存里，落库零成本；刻意不设 (用户, 目标) 唯一约束——忽略该折叠，展示不该 | 只追加 |
| `favorite_collection_quality.py` | 心愿单分组的大小分布、空分组、渗透、命名收敛与先收藏后归类的四类分布（只报告不改任何分组） | 只报告 |
| `reputation.py` | 卖家交易信誉摘要与公开指标 | 读写 |
| `return_handshake.py` | 借用归还双方确认的顺序、间隔与只点一半：归还闭环的四条损失（只报告不改写） | 读写 |
| `search_trend.py` | 搜索词与筛选组合的学期窗口趋势、分面跳变与引导词对比（只报告） | 读写 |
| `academic_phase_supply_preparation.py` | 阶段切换前的分类供给预热：历史同阶段需求 × 当前 7 天供给（只报告） | 只报告 |
| `academic_term_snapshot_quality.py` | 学期阶段快照的规模、覆盖、孤儿行与过期（只报告不改写） | 只报告 |
| `academic_calendar.py` | 学期阶段划分、阶段统计与快照读路径（academic_calendar 的读路径见文件内 `_phase_row`） | 读写 |
| `transaction_safety.py` | 订单交易安全卡、可解释风险提示与安全卡信号指纹 | 读写 |
| `transaction_safety_acknowledgement_quality.py` | 交易安全卡当前确认覆盖、双方补齐时延、版本观察下界与待确认清单 | 只报告 |
| `saved_searches.py` | 保存搜索条件 | 读写 |
| `saved_search_digest.py` | 关注搜索汇总提醒的缓冲与补送 | 读写 |
| `saved_search_latency.py` | 关注搜索命中的等待时延统计 | 读写 |
| `campus_verification_friction.py` | 校园认证从发邮件到点开链接的等待时延统计 | 读写 |
| `gift_application_queue.py` | 免费领取申请队列的长度、处理时长与说明填写率统计 | 读写 |
| `moderation_rule_quality.py` | 内容审核四条规则的误判率、漏判率与复核时效统计 | 读写 |
| `moderation_appeal_quality.py` | 内容申诉的提交线、处理线、当前积压、恢复缺口与换人复核统计 | 只报告 |
| `lost_found_report_quality.py` | 失物招领举报的提交量、理由分布、处理时长分档、待处理积压与「举报人对发布者不可见」的边界自检（只报告） | 只报告 |
| `availability_watch_quality.py` | 有货提醒的等待时长、死信来源、白等规模与触达质量统计 | 读写 |
| `rating_quality.py` | 交易评价的覆盖率、提交时延、双向分布与文字填写率统计 | 读写 |
| `dispute_resolution_quality.py` | 交易争议按类型的驳回率、证据到达时刻、发起方向与处理意见填写率统计 | 读写 |
| `dispute_evidence_shape.py` | 争议证据的格式与大小分布：扩展名各档、固定大小档、贴上限与超过上限、每笔争议份数 | 只读 |
| `lost_found_lead_quality.py` | 失物招领线索的提交量、确认率、确认率指向分离与反馈填写率统计 | 读写 |
| `demand_response_conversion.py` | 确认求购响应之后的订单转化、下单时延、订单结局与卡点统计 | 读写 |
| `item_image_coverage.py` | 在架商品的图片覆盖度与无图商品清单 | 读写 |
| `delivery_handshake.py` | 当面交付两次确认的顺序、间隔、只点一半与挂起清单（只报告不改写） | 只报告 |
| `order_deadline_reminders.py` | 订单确认截止与归还截止的等待分档、催收四种结局与距截止余量（只报告不改写） | 只报告 |
| `meeting_reschedule.py` | 一次交付安排被改了几次才定下来：改期次数只认「更新了」备注的条数（下界，只报告不改写） | 只报告 |
| `urls.py` | 应用路由：商品、订单、搜索、通知、运营看板与后台入口 | 只读 |
| `views.py` | 商品、订单、搜索、通知和运营页面 | 读写 |

</details>

## 业务链路的测试模块（158 个）

<details>
<summary>业务链路测试清单（158 个，按主题就近排列）</summary>

清单按主题就近排列，包含 `listings/tests.py`（早期总装）、各业务模块测试，以及落在 `chat_messages/` 但守住运营看板契约的六条链路测试。

- **`test_academic_calendar.py`**：学期阶段划分、阶段统计与快照写路径

- **`test_academic_phase_supply_preparation.py`**：阶段临近、同学期同阶段基准、分类样本门槛、7 天节奏、隐私与导出契约

- **`test_academic_term_snapshot_quality.py`**：快照规模、覆盖分母、孤儿行与过期残留分开，命令状态与看板导出

- **`test_academic_term_snapshot_read_path.py`**：已结束读快照、进行中实时算、区间不匹配不读、缺行标 live 不伪装

- **`test_availability_watch_quality.py`**：只数仍挂着的关注、死信与白等分开、分母是商品数、不自动清理

- **`test_borrow_escalation.py`**：三级阶梯、跳级补发、归还闭环、命令台账与看板测试

- **`test_borrow_escalation_trend.py`**：归期口径、逾期天数不参与对比、门槛与方向词

- **`test_borrow_rhythm.py`**：阶段归属、日均口径、窗口截断、合成桶与门槛测试

- **`test_borrow_risk.py`**：风险分层：升级率按分类与押金档位聚合、小样本不指方向

- **`test_borrow_days_shape.py`**：期限档固定且全列、从未升级的借用进分母、右删失样本单独数、逾期按已归还的笔算

- **`test_borrow_risk_panel.py`**：风险分层上板、小样本提示与两种导出行

- **`test_borrowing.py`**：限期借用的下单、交付确认、到期提醒与归还闭环

- **`test_bulk_campaign.py`**：商品批量加入和移出专题测试
- **`test_bulk_listing_renewal.py`**：毕业季批量续期的登录边界、整批校验、状态边界、成功续期与刷新 / 通知隔离测试
- **`test_semester_listing_template.py`**：学期批量发布模板的私有保存、同名覆盖、数量与时间边界、停用配置跳过、套用回填与删除测试

- **`test_bulk_listing_create.py`**：毕业季批量发布的空行忽略、共同设置、交易方式默认值、20 行边界、事务一致性与匹配通知测试

- **`test_bulk_listing_csv_import.py`**：批量发布 CSV 的 UTF-8 与表头校验、行级错误、预览不写入、token 失效与确认发布闭环
- **`test_bulk_listing_xlsx_import.py`**：批量发布 XLSX 的数字转换、公式 / 日期 / 布尔拒绝、第一工作表边界、文件上限与确认发布闭环
- **`test_bulk_listing_import_summary.py`**：批量发布导入错误摘要的空态、重复问题聚合、行号展示、排序稳定与前六组截断
- **`test_bulk_listing_import_template.py`**：批量发布 CSV / XLSX 模板的固定表头、XLSX 输入约束与冻结行、空模板回读、格式白名单与登录边界
- **`test_bulk_listing_image_queue.py`**：批量发布后的短期补图队列、本人与展示状态过滤、完成进度、行内补图上传、整批校验失败不写入、session 覆盖与失效边界

- **`test_campaign_analytics.py`**：专题周期内的新增、在售与转化漏斗
- **`test_campaign_goals.py`**：六类专题目标的窗口继承、实时进度、节奏状态、严格漏斗分母、跨专题汇总限制与 CSV 口径

- **`test_campaign_phase_review.py`**：已结束专题的阶段边界、业务时间归段、薄样本门槛、跨专题总结、隐私与只读页面 / CSV 契约

- **`test_campaign_registration.py`**：专题报名的幂等加入与退出、参与区间、数据库约束、页面权限、返回地址、历史参与分页、安全锚点回跳和后台入口

- **`test_campaign_updates.py`**：加入不自动订阅、命中幂等入队、关闭订阅与退出取消、发送前状态复核、日 / 周独立去重、多专题合并、免打扰、任务台账、权限和运营导出

- **`test_campaigns.py`**：校园专题、时间窗口和专题商品测试

- **`test_campus_pulse.py`**：按校园地点聚合的供需热力与运营压力
- **`test_location_registry_quality.py`**：档案开关、字段覆盖、当前引用、交付来源、近似重名、停用引用、只读边界与看板 / CSV 导出

- **`test_location_maintenance_queue.py`**：地点维护队列的入队规则、互斥优先级、重叠信号、排序、截断、只读边界与看板 / CSV 导出
- **`test_location_maintenance_decision_review.py`**：校园地点维护决策回看的周期归属、暂缓停留、只读边界、页面与 CSV 导出契约

- **`test_campus_verification_friction.py`**：只收认证成功、按分钟与小时分档、窗口按 verified_at、慢的清单按耗时倒序

- **`test_circular_impact.py`**：循环影响力统计、趋势和隐私边界测试

- **`test_contributions.py`**：贡献积分只累计自己的行为、不排名不公开、徽章分级

- **`test_contribution_milestones.py`**：一次跨多级、类型徽章、总事件徽章、幂等写入、业务时间、历史回填、回滚与唯一约束

- **`test_contribution_tier_shape.py`**：五级全列、四级跨度全列、空样本报 None 不报 0、复用产品侧判定规则、组合数不数用户数、只读性与周期回退

- **`test_contribution_milestone_coverage.py`**：稳定重放、缺失与多余分开、快照差异不进入事实不一致、触发贡献指向、空分母与页面 / CSV 只读契约
- **`test_contribution_milestone_phase_rhythm.py`**：同类学期同阶段对照、每百天强度、覆盖审计阻塞、快照差异放行、薄样本、进行中与无学期事件、页面 / CSV 只读契约

- **`test_dashboard_build_performance.py`**：请求内构建耗时的空态、单样本、最近秩分位、重复 key、共享组装契约与页面 / CSV 导出一致性
- **`test_dashboard_export_columns.py`**：面板与导出的层级与表头：子标题归节、表头可判别、摘要不冒充表头

- **`test_dashboard_export_coverage.py`**：每个运营面板都有一条导出行，屏幕与下载同一份口径

- **`test_delivery_handshake.py`**：顺序四档、双方同秒单列、挂起不按周期切、cancelled 回问事件表、空分母 None 与看板导出测试

- **`test_demand_match_quality.py`**：分档采纳率、单调性、高分被拒、维度效果与看板导出

- **`test_demand_opportunity_followups.py`**：填写率排在逾期前面、没给期限的单独报、两组完成率守住噪声线

- **`test_demand_radar.py`**：需求雷达聚合、供给判断和看板导出测试

- **`test_demand_radar_hit_layers.py`**：分层命中率：门槛、未分织任务与导出行

- **`test_demand_radar_hit_trend.py`**：任务按创建时间归期、稀疏周期不指方向、只看报告不改权重

- **`test_demand_radar_outcome.py`**：闭环窗口口径、样本不足处理、命中率与告警测试

- **`test_demand_radar_phase_trend.py`**：学期纵向对比：归期、跳学期、薄格子与导出行

- **`test_demand_response_conversion.py`**：只认「商品相同 + 买家是响应人 + 下单不早于确认」，响应说明只报写没写

- **`test_demand_responses.py`**：求购响应的提交、确认与撤回流转

- **`test_demand_task_aging.py`**：两支两个式子、updated_at 不能当状态变更时刻、空平台不报 0 天、看板导出

- **`test_dispute_resolution_quality.py`**：驳回率按 reason 拆、已完结才进分母、证据只报到达时刻、发起方向不按用户聚合

- **`test_dispute_evidence_shape.py`**：扩展名全列含 0 样本档、大小档取规则原值、读不到大小单独数、已完结组才给方向

- **`test_favorite_collection_quality.py`**：心愿单分组的大小分布、空分组、渗透、命名收敛与先收藏后归类的四类分开报

- **`test_first_viewed.py`**：第一次被看见之后的收藏与下单时延、来源拆解与后续一步

- **`test_free_gifting.py`**：免费赠送不伪装成 0 元商品、领取申请队列与结果通知

- **`test_gift_application_queue.py`**：队列长度分商品聚合、处理时长、说明填写率对照与看板导出测试
- **`test_personal_queue_pagination.py`**：浏览足迹固定 24 件分页、领取申请状态优先级、发布者全量待处理数、权限隔离与安全原页回跳

- **`test_governance_borrow_queue.py`**：治理工作台真的能读到三级催收案件，不只是举报与争议

- **`test_governance_sla.py`**：去重计数、周期前积压、五类时限与治理工作台测试

- **`test_handoff_code_usage.py`**：取件码的签发间隔、尝试次数分档、重复签发与用不上的码

- **`test_item_image_coverage.py`**：商品图片覆盖度，只报告不改写

- **`test_item_image_processing.py`**：上传硬限制、等比降采样、WebP 重编码、EXIF 方向、表单错误与页面提示

- **`test_item_thumbnails.py`**：列表缩略图、大图预览、响应式候选与展示宽高生成、替换重建、失败回退、历史回填、只补元数据不重建文件、分批限制与任务台账

- **`test_item_lifecycle.py`**：到期商品从公开流与推荐里隐藏并回写状态

- **`test_item_price_changes.py`**：出售价格轨迹、收藏商品降价提醒、偏好开关、重新上架边界与通知失败隔离

- **`test_lifecycle_diagnostics.py`**：商品生命周期等级、刷新限流和卖家面板测试

- **`test_my_items_workspace.py`**：卖家工作台五类服务端页签、状态隔离、全量计数、交易方式展示与空态测试

- **`test_my_items_pagination.py`**：每页 12 件、非法与越界页码、页签分页保留、批量工具可见性、原页回跳与开放重定向防护

- **`test_lifecycle_reminder_effect.py`**：提醒之后的触达、阅读与行动对照，含事件优先、混合来源、通知删除后事件保留、分类分层、本周期提醒负担、样本门槛、隐私边界与看板导出

- **`test_lifecycle_reminder_action_trend.py`**：跨周期对比的窗口切分、样本门槛、`both` 桶不判方向、无提醒不报零
- **`test_lifecycle_reminder_term_trend.py`**：跨学期对比的事件台账分母、阶段封闭窗口、终态动作边界、覆盖缺口、样本门槛、页面与导出契约

- **`test_lifecycle_reminders.py`**：生命周期提醒的合并、冷却、偏好、免打扰、事件台账同事务写入与 dry-run 边界测试

- **`test_lost_found.py`**：失物招领、匹配和线索流转测试

- **`test_lost_found_image.py`**：一条记录一张可选照片的模型约束、变体生成与回退、表单字段显隐、发布 / 编辑 / 移除三条路径与页面渲染测试

- **`test_lost_found_lead_quality.py`**：线索量分档、确认率指向分离、确认率按 post_type 拆与看板导出测试

- **`test_lost_found_matched_outcome.py`**：确认匹配之后的四种终局照实报、双向性只数认账那一侧、清单不带标题与用户名
- **`test_lost_found_report.py`**：失物招领举报的唯一约束与禁止自举报、四条路径的权限边界、队列筛选分页、审核落库与只通知举报人测试
- **`test_lost_found_report_quality.py`**：三条时间线分离、五个理由一栏不少、小样本只报数、边界集合差集与「既发布又举报的人不计进泄漏」、处理完没通知的举报人测试

- **`test_meeting_attendance.py`**：到场口径：响应分档、迟到区间与缺席分母分开

- **`test_meeting_incident_quality.py`**：驳回率按 reason 拆、意见只读长度与空否、提交方向取不到身份归未知、右删失样本与看板导出测试

- **`test_meeting_recommendations.py`**：交付时间建议的生成与冲突规避

- **`test_campus_location_service_hours.py`**：每周时段模型约束、跨午夜与相邻窗口判断、交付推荐解释和时段外提交提醒
- **`test_campus_location_service_exceptions.py`**：单日关闭与替换例外、跨午夜截断、推荐解释、原因提醒与约束测试
- **`test_meeting_service_window_quality.py`**：未来安排开放状态分类、例外识别、未登记与无地点边界、分页、页面与 CSV 同构和只读测试
- **`test_campus_location_service_rule_quality.py`**：开放规则配置信号、例外生命周期、跨午夜截断、页面与 CSV 同构和只读测试

- **`test_meeting_reschedule.py`**：下界只认「更新了」备注、首次提议不算改期、没约上的不进分母、响应分档与 attendance 一致、地点只和当前版比、清单只列改过两次以上、只报告不改写与空态导出测试

- **`test_moderation_notice_preference.py`**：触达四种成因不合成、免打扰只压徽章通知照格落库、已读率带同期全部通知作照局分母

- **`test_moderation_appeal_quality.py`**：提交线与处理线分窗、申诉率下界、成立但未恢复、积压清单脱敏与换人复核口径
- **`test_moderation_rule_quality.py`**：按规则拆的误判率与漏判率、右删失样本、权重只报分布与看板导出测试

- **`test_mutual_aid_feedback.py`**：求购响应与失物线索的完成反馈闭环测试

- **`test_mutual_aid_feedback_notes.py`**：补充说明只数字数与空否、不切词不聚类不读原文

- **`test_notification_aggregation_quality.py`**：被聚合过的行与从没被聚合过的行，阅读率与处理时长对照

- **`test_notification_kind_gap_fix.py`**：三个来源此刻对齐且必须继续对齐、曾经的两种窟窿补上之后真的筛得出也真的关得掉、两个修法都没顺手改掉偏好闸门

- **`test_notification_kind_coverage.py`**：三张表对齐：KIND_CHOICES、偏好开关、表单字段现读对齐，两个曾经的窟窿已从差集里消失

- **`test_notification_preference_bypass_diagnosis.py`**：偏好写入后仍出现的下界、与后关一致、窗口过滤、`last_occurred_at` 回退、无偏好行、权限与导出隐私

- **`test_notification_preference_usage.py`**：开关名单从模型现读、归组无遗漏无重复、开着与关掉分开计数、无偏好行单列

- **`test_notification_preference_usage_smoke.py`**：三个场景的看板与导出渲染——有偏好行、空平台、有通知却零偏好行

- **`test_notification_quiet_hours.py`**：免打扰窗口、跨午夜和未读静默测试

- **`test_notification_quiet_hours_shape.py`**：空分母不报 0、相等时间单独数、总闸关着的配置不叠加、跨午夜按分钟覆盖、看板与导出同名

- **`test_notification_response.py`**：通知处理时效与触达质量，阅读时刻可登记

- **`test_notification_snooze.py`**：单条与批量延后/恢复、状态边界、未读统计和权限测试

- **`test_notification_snooze_return.py`**：延后的通知到期后是否回来：把主动等待从处理时长里拆出来

- **`test_operations_digest.py`**：告警日报只投活跃管理员、尊重偏好、幂等键不重复刷屏

- **`test_opportunity_digest.py`**：互助机会摘要通知、偏好和命令测试

- **`test_opportunity_dismissal_quality.py`**：忽略规模、分数档与理由拆分、重复忽略，第一次可测的忽略率

- **`test_opportunity_feed.py`**：互助机会匹配、过期过滤和去重测试

- **`test_opportunity_impression_quality.py`**：机会流曝光日志的周期边界、类型与目标一致性、重复与同刻重复、未来容忍、逐日截断与页面 / CSV 同构

- **`test_opportunity_impression_write.py`**：机会流渲染一次落一行「被看见」，写入失败不影响页面

- **`test_order_deadline_reminders.py`**：确认时长只认 confirmed 事件、四种结局分开、自动释放与取消分开、借用侧单独取数、台账拆开、只报告不改写与空态导出测试

- **`test_order_stage_flow.py`**：订单全链路阶段时长与瓶颈定位

- **`test_order_detail_presentation.py`**：订单详情「下一步」卡片的分支标题、渲染出的侧栏 h2 与模板拆分形状

- **`test_price_insights.py`**：同类价格分位数与参考可信度，样本不足时降低提示强度

- **`test_price_change_quality.py`**：出售价格轨迹的方向、幅度、零元基线、重复调价、当前分类视图、隐私边界与看板导出
- **`test_price_response.py`**：调价前后窗口归因、相邻调价排除、方向与幅度分组、隐私边界与看板导出
- **`test_category_price_reference.py`**：分类样本边界、当前在售区间外计数、历史回退、小样本降级、排序与看板导出隐私边界
- **`test_price_history.py`**：商品详情价格轨迹的展示门槛、连续性、最近窗口、零元基线、隐私粒度、卖家维护提示与只读边界

- **`test_rating_quality.py`**：覆盖率分母只收已走完、完成时刻取 OrderEvent、双向按方向拆与看板导出测试

- **`test_recommendation_dashboard_smoke.py`**：看板面板与 CSV 在新字段下都能渲染，不报 KeyError

- **`test_recommendation_impression_write.py`**：首页渲染一次落一行「被看见」，reason_key 不改变渲染输出

- **`test_recommendation_impression_quality.py`**：推荐曝光日志的周期边界、字段形状、匿名合法性、登录用户重复、时间覆盖与看板导出隐私边界
- **`test_recommendation_quality.py`**：反馈率分母只收点开过的、正负比例分母只收有反馈的、复算理由与服务同函数、改口带 60 秒容差、样本不足不给方向与看板导出测试

- **`test_refresh_quota_drift.py`**：刷新额度累计口径与升级限制测试，沉底天数和同一行的距上次刷新同源，归零扫描按 AST 判

- **`test_item_refresh_event_ledger.py`**：刷新事件同事务写入、拒绝不落库、覆盖缺口、未来时间、页面与导出契约

- **`test_refresh_response.py`**：刷新前后窗口、成熟样本、次序分组、样本门槛、隐私边界与看板导出同构测试

- **`test_relist_freshness.py`**：重新上架新鲜度欠账的判据、分母、额度与无互动测试

- **`test_governance_queue_pagination.py`**：举报与争议服务端分页、SQL 优先级一致性、稳定队列顺序、全量待办统计与越界页码
- **`test_report_priority.py`**：举报优先级的具名信号、开放队列排序、管理页解释与治理工作台联动
- **`test_report_resolution_quality.py`**：驳回率按 reason 拆、意见只读长度与空否、不把驳回率和处理人挂钩

- **`test_report_outcome_feedback.py`**：我的举报、一次性三档回访、管理员只读、统计分母与页面 / 导出契约

- **`test_return_handshake.py`**：归还双方确认的顺序、间隔与只点一半，只报告不改写

- **`test_saved_search_cadence.py`**：三种提醒节奏切换无需重建关注、汇总单条列出条数上限

- **`test_saved_search_entry.py`**：管理入口可见、保存成功带链接、「当前命中」点过去必须对得上

- **`test_saved_search_latency.py`**：命中时延口径、交叉与看板导出测试

- **`test_saved_search_quiet_window.py`**：三档静默状态分开数、占比分母是全部关注、缓冲命中不计入已送达

- **`test_saved_search_limit_shape.py`**：九档分布与「不是 1 到 9」单列、截断规模只报此刻、条数 × 节奏交叉与页面导出测试
- **`test_search_combo_shift.py`**：组合占比口径、稀疏门槛、单周期组合与面板导出测试

- **`test_search_impression_pairing_quality.py`**：周期边界、历史曝光可配对、字段缺失、零位置、延迟分档、自然日覆盖与看板导出隐私边界

- **`test_search_exposure_cold_start.py`**：观察缓冲、首曝延迟边界、早于发布曝光、早于首曝点击、有点击无曝光、分类聚合、页面 20 条与 CSV 完整清单

- **`test_search_position_quality.py`**：位置分档点击率、排序效果与深度诊断，含页面与导出呈现

- **`test_search_rejection_stats.py`**：否决生效、双向、可撤销、否决失效与面板导出测试

- **`test_search_rejection_recurrence.py`**：回头区间不跨失效累计、回头与挖掘同规则、撤销不编数、样本不足不报推翻率

- **`test_search_rewrites.py`**：改口配对、时间窗口、已配同义词排除与一键确认入口测试

- **`test_search_synonym_effect.py`**：已确认同义词的无结果率回流与生效判定

- **`test_search_threshold_feedback.py`**：由命中率反推候选门槛，只建议不自动改

- **`test_search_trend.py`**：搜索词与筛选组合的学期窗口趋势、分面跳变与引导词对比

- **`test_supply_lifecycle.py`**：供给侧覆盖率、在管规模分组和建议阈值测试

- **`test_task_run_duration_percentiles.py`**：P50/P90/P99 取最近秩、样本撑不起就留空

- **`test_task_run_failure_cause_trend.py`**：失败原因按 × 两个等长周期逐类比较，新类别不记成从 0 涨上来

- **`test_task_run_failure_causes.py`**：失败备注分成配置/数据/外部依赖/未归类四类

- **`test_task_run_hour_load.py`**：小时按本地时钟切、基线用命令自己的全周期中位数、样本少不判拥塞

- **`test_task_run_overlap.py`**：自我重叠：节奏从台账反推、重叠须有代价、只报告不改写

- **`test_task_run_ledger_integrity.py`**：台账完整性审计的时间边界、指标形状、未知命令、只读边界与页面 / CSV 导出契约

- **`test_task_run_trend.py`**：跨周期对比的归期口径、耗时中位数与样本门槛测试

- **`test_task_runs.py`**：台账落库、失败仍记录、预演排除与健康聚合测试

- **`test_transaction_safety.py`**：交易安全卡、可解释风险提示、买卖双方版本化确认、信号变化后的重新确认与幂等边界
- **`test_transaction_safety_acknowledgement_quality.py`**：当前覆盖分母、完整链路时延、版本下界、待确认清单隐私、看板与导出权限
</details>

## chat_messages/ 的关键测试

<details>
<summary>消息、私信与审核测试（8 组）</summary>

- **`tests.py`**：留言、私信、会话、审核队列和通知路径的基础流程测试。

- **`test_moderation_release.py`**：误判放行后内容真的回到原渠道、接收方收到通知、历史缺材料不伪称放行、重复复核不重复发布。

- **`test_moderation_appeal.py`**：作者单次申诉、队列优先与筛选、原复核快照、改判恢复、缺材料终态与后台只读边界。

- **`test_moderation_risk_tier.py`**：规则最高档、拦截持久化、历史标签回填、复核节奏、队列排序筛选和后台只读测试。

- **`test_moderation_queue_pagination.py`**：每页 12 条、非法与越界页码收敛、组合筛选保留、空态、单页隐藏分页和操作回跳测试。

- **`test_chat_response.py`**：私信第一次被打开的响应时效、静默样本、商品分类与交易方式分层、未关联商品边界、样本门槛、看板与 CSV 同构测试。

- **`test_chat_response_trend.py`**：私信响应时效的跨周期对比、样本门槛与方向判断测试。

- **`test_chat_trade_correlation.py`**：私信响应与成交的共同变化、成熟窗口、订单匹配、样本门槛、看板与 CSV 同构测试。

</details>

## docs/ 下与本项目运营口径相关的设计文档

<details>
<summary>设计文档清单（119 篇，持续更新）</summary>

- **`academic-phase-supply-preparation-design.md`**：阶段切换前的分类供给预热，只读历史同阶段需求与当前 7 天供给

- **`academic-term-snapshot-quality-design.md`**：阶段快照的规模、覆盖、孤儿行与过期，照实报不伪装

- **`academic-term-snapshot-read-path-design.md`**：已结束读快照、进行中实时算，判据是计数是否封闭而不是 `refreshed_at`
- **`academic-snapshot-miss-alert-design.md`**：把实时行拆成进行中与已结束兜底，缺匹配快照不再藏在 live 里

- **`availability-watch-design.md`**：等有货的人等了多久，只数仍挂着的关注，死信与白等分开

- **`borrow-days-shape-design.md`**：默认借用天数的形状，borrow_days 第一次被数字读到
- **`graduation-bulk-listing-design.md`**：毕业季批量发布第一刀，20 行全成或全不写，图片与导入另做
- **`graduation-bulk-listing-csv-import-design.md`**：批量发布 CSV 导入预览，UTF-8 固定表头、行级错误与不直接写入边界
- **`graduation-bulk-listing-xlsx-import-design.md`**：批量发布 XLSX 导入预览，只读第一工作表、安全单元格转换与共用预览发布边界
- **`graduation-bulk-listing-image-followup-design.md`**：批量发布后的卖家补图队列，session 短期草稿、逐件编辑与不做批量上传边界
- **`graduation-bulk-listing-image-upload-design.md`**：本批次补图上传，商品行与文件一一映射、整批校验失败不写入与复用图片治理
- **`graduation-bulk-listing-import-template-design.md`**：批量发布 CSV / XLSX 导入模板，固定表头单一来源、受控格式生成与不落库边界
- **`graduation-bulk-listing-template-validation-design.md`**：批量发布 XLSX 模板输入约束，行内范围提示、空行容忍与服务端最终校验分层
- **`graduation-bulk-listing-import-error-summary-design.md`**：批量发布导入错误摘要，重复问题分组、完整清单保留与只做阅读层的边界
- **`graduation-bulk-listing-renewal-design.md`**：毕业季批量续期，统一新截止时间、可续期状态边界、整批失败与不触碰刷新额度
- **`my-items-workspace-pagination-design.md`**：卖家工作台五类状态页签、服务端分页、全量统计与安全原页回跳
- **`personal-queue-pagination-design.md`**：浏览足迹 24 件分页、专题历史 12 段分页、领取申请状态排序、全量待处理统计与安全原页回跳
- **`semester-bulk-listing-template-design.md`**：学期批量发布模板，共同设置私有保存、相对展示天数重算与不保存商品行边界

- **`dispute-evidence-shape-design.md`**：证据文件的形状，attachment.name 与 size 第一次被数字读到
- **`meeting-service-window-quality-design.md`**：未来交付安排对照开放时段与单日例外的只读风险审计
- **`campus-location-service-rule-quality-design.md`**：校园地点每周时段与单日例外的配置形状、生命周期与只读核对清单
- **`search-exposure-cold-start-design.md`**：新商品从发布到首次搜索曝光的冷启动队列、延迟分桶与分类对照

- **`campus-verification-friction-design.md`**：校园认证最后一公里的等待时延，只收认证成功，慢的清单按耗时倒序

- **`chat_message_response_design.md`**：一条私信隔了多久被打开，右删失样本单独数规模

- **`chat-response-context-design.md`**：私信响应按商品分类与交易方式分层、未关联商品边界和只读隐私口径

- **`chat-trade-correlation-design.md`**：私信响应与成交的共同变化、7 天观察窗口与只读隐私边界

- **`chat_response_trend_design.md`**：这个月的私信响应比上个月好还是坏，按份额判方向

- **`contribution-tier-shape-design.md`**：积分等级与徽章的三张规则表第一次被数字读到，按全量不按周期切
- **`contribution-milestone-ledger-design.md`**：等级与徽章首次达成的事实台账，在线幂等写入、历史稳定回填且不触发庆祝
- **`contribution-milestone-coverage-design.md`**：里程碑事实与贡献流水的全量对账，缺失、多余、快照差异和事实不一致分开报

- **`meeting-reschedule-design.md`**：改期计数的四条损失与「一次谈成是主档」的口径
- **`campus-location-service-hours-design.md`**：校园地点每周开放时段、跨午夜合并与推荐提醒边界

- **`campus-location-service-exceptions-design.md`**：单日临时关闭、替换时段与跨午夜截断口径


- **`governance-queue-pagination-design.md`**：举报与争议服务端分页、SQL 优先级排序、全量待办统计与只读边界
- **`report-outcome-feedback-design.md`**：举报人侧的结果回访：三档结构化反馈、一次性提交与运营回访质量口径
- **`report-priority-design.md`**：商品举报的可解释优先级：风险原因、在售曝光与多人交叉信号只影响人工查看顺序
- **`report-resolution-quality-design.md`**：商品举报按 reason 拆驳回率，意见只读长度，不把驳回率和处理人挂钩

- **`rejection-recurrence-design.md`**：被否决词对在更长周期里的回头率，撤销和覆盖方向相反因此分开报

- **`search-impression-pairing-quality-design.md`**：搜索分子分母的可配对口径、历史上线顺序、位置一致性与只读隐私边界

- **`refresh-quota-drift-design.md`**：刷新额度累计计数器的四条证据与「先看板后改口径」的取舍

- **`refresh-quota-drift-fixups-design.md`**：两个挂了很久的测试失败，一个同一行两个口径，一个扫描把查询参数当归零

- **`refresh-response-design.md`**：商品刷新后的搜索回响：最新一次刷新、前后各 72 小时和只读隐私边界
- **`price-response-design.md`**：价格调整后的搜索回响：调价事件、前后各 72 小时、方向与幅度档和只读隐私边界
- **`category-price-reference-design.md`**：分类级调价参考聚合：当前供给的区间外形状、样本可信度与只读边界

- **`relist-freshness-design.md`**：新鲜度欠账的四条证据与「先看板后改口径」的三条路

- **`demand-response-conversion-design.md`**：确认求购响应之后交易真的发生了吗，分母是已确认响应数

- **`demand-task-aging-design.md`**：跟进任务派出去之后有人接手吗，两支两个式子

- **`dispute-resolution-quality-design.md`**：交易争议按 reason 拆驳回率，已完结才进分母，处理意见只读长度

- **`favorite-collection-quality-design.md`**：心愿单分组的大小分布、空分组与命名收敛，不改任何分组

- **`favorite-grouping-latency-design.md`**：先收藏后归类第一次可测，`grouped_at`写在哪、四类为什么不是三类、老数据为什么单列

- **`first-viewed-design.md`**：第一次被看见之后的收藏与下单时延，三个问题三个分母

- **`gift-application-queue-design.md`**：免费领取申请队列的长度、处理时长与说明填写率

- **`item-image-coverage-design.md`**：在架商品的图片覆盖度与无图商品清单
- **`item-image-display-metadata-design.md`**：商品图内在宽高、列表懒加载、详情立即加载与历史元数据补齐
- **`item-price-drop-notifications-design.md`**：出售价格轨迹、收藏商品降价提醒、偏好开关与历史边界
- **`price-change-quality-design.md`**：出售价格轨迹的方向、幅度、重复调价与分类视图，只读运营口径
- **`item-price-history-design.md`**：商品详情价格轨迹的展示门槛、日期粒度、连续性校验与隐私边界
- **`price-history-maintenance-design.md`**：卖家侧价格轨迹维护提示的可见性、五种状态与只读边界
- **`item-responsive-image-variants-design.md`**：960px 预览档、列表与详情响应式候选及历史回填边界
- **`item-upload-image-optimization-design.md`**：新上传商品图的硬限制、降采样、方向与隐私边界
- **`item-thumbnail-backfill-design.md`**：列表缩略图规格、历史图片分批回填、失败回退与任务台账
- **`readme-information-architecture-design.md`**：README 短首页、阅读路径和深文档分层口径

- **`lifecycle-reminder-action-trend-design.md`**：行动率的跨周期对比，为什么只有行动率带方向、为什么先不做按卖家的分层
- **`lifecycle-reminder-event-ledger-design.md`**：生命周期提醒从“最新时间戳”升级为逐商品逐类型事件台账，为跨学期对比保留稳定分母
- **`item-refresh-event-ledger-design.md`**：商品刷新从累计计数与最新时间戳升级为逐次事件台账，先守住事实，不急着判效果
- **`lifecycle-reminder-term-trend-design.md`**：同一学期阶段的跨学期终态转化对比，只用事件台账分母，并把可覆盖的刷新动作排除在纵向方向外

- **`lifecycle-reminder-segment-effect-design.md`**：分类与本周期提醒负担的行动率分层、12 件样本门槛和只读隐私边界

- **`lost-found-lead-quality-design.md`**：失物线索的提交量、确认率指向分离与反馈填写率

- **`lost-found-image-assist-design.md`**：一条记录为什么只放一张照片、变体管线为什么复用商品那一套，以及哪些信息必须留在镜头外
- **`lost-found-matched-outcome-design.md`**：确认匹配之后的四种终局照实报，双向性只数认账那一侧

- **`meeting-incident-quality-design.md`**：预约异常的驳回率按类型拆，提交方向取不到身份归未知

- **`moderation-notice-preference-design.md`**：审核结果通知的触达四种成因不合成，免打扰只压徽章

- **`moderation-release-closure-design.md`**：误判放行必须真正回到原渠道；拦截时保存材料，历史缺材料不伪装成已放行

- **`moderation-rule-quality-design.md`**：四条审核规则的误判率、漏判率与复核时效

- **`moderation-risk-tier-design.md`**：内容审核规则按严重风险、脱离平台联系与低权重线索给出不同复核节奏

- **`moderation-queue-pagination-design.md`**：审核队列每页 12 条、筛选保留、越界页码收敛与操作回跳边界

- **`moderation-appeal-design.md`**：确认违规后的单次申诉、改判留痕与恢复闭环
- **`moderation-appeal-quality-design.md`**：内容申诉的提交线与处理线分窗、积压等待、成立与恢复缺口

- **`narrow-grid-first-cell-contract-design.md`**：固定宽度第一轨为什么只能放图标和时间戳，以及懒加载分区为什么会让整轮溢出审计失效
- **`notification-aggregation-quality-design.md`**：被聚合过的行与从没被聚合过的行，阅读率与时长对照

- **`notification-kind-coverage-design.md`**：通知类型、偏好开关与表单字段三张表的对齐情况

- **`notification-preference-bypass-diagnosis-design.md`**：`updated_at` 的限制、可疑下界、正常残留与只读隐私边界

- **`notification-quiet-hours-shape-design.md`**：免打扰时段的形状，quiet_hours 两个时间字段第一次被数字读到

- **`notification-kind-gap-fix-design.md`**：两个曾经被诊断出的窟窿怎么补：一个补数据层的名字，一个补表单层的暴露，两处都不需迁移

- **`notification-snooze-return-design.md`**：延后把处理慢拆成两半，到期后到底回不回来

- **`opportunity-dismissal-quality-design.md`**：按过「不再显示」的机会的规模、理由与重复忽略

- **`opportunity-impression-design.md`**：忽略率的分母为什么值得落库、曝光表的四条口径与「仍然报下界」的理由
- **`opportunity-impression-quality-design.md`**：机会流曝光行自身的类型、目标一致性、重复与自然日覆盖审计

- **`order-deadline-reminders-design.md`**：确认与归还截止的等待分档、催收四种结局与距截止余量

- **`order-safety-acknowledgement-design.md`**：交易安全卡双方确认；确认绑定当前安全行动指纹，历史确认保留但不冒充当前版本

- **`rating-quality-design.md`**：交易评价的覆盖率分母只收已走完、完成时刻取 `OrderEvent`

- **`recommendation-impression-design.md`**：首页推荐反馈率的分母为什么值得落库、曝光表口径、`reason_key` 与 `score` 的取舍，以及匿名访客也写入的纠正
- **`recommendation-impression-quality-design.md`**：已落库推荐曝光行的字段形状、重复曝光、匿名流量、时间覆盖，以及不能数写入失败的边界
- **`recommendation-quality-design.md`**：首页那八张推荐卡的反馈率、正负比例与理由拆分

- **`return-handshake-design.md`**：归还双方确认的顺序、间隔与只点一半的四条损失

- **`saved-search-entry-design.md`**：关注搜索的管理入口与「当前命中」点过去必须对得上

- **`saved-search-latency-design.md`**：关注搜索命中的等待时延，交叉口径与看板导出

- **`saved-search-limit-shape-design.md`**：用户自己配的截断，为什么只报此刻的规模、为什么空字符串提交是「不改」
- **`saved-search-quiet-window-design.md`**：三档静默状态分开数，缓冲命中不计入已送达

- **`transaction-safety-acknowledgement-quality-design.md`**：安全卡确认质量的当前覆盖、右删失时延、版本下界与待确认清单

- **`dashboard-build-performance-design.md`**：运营看板共享构建器的请求内耗时口径、页面与 CSV 共用注册表、最近秩分位和只观测不自动优化边界
- **`location-registry-quality-design.md`**：校园地点档案的开关语义、覆盖口径、交付地点解释优先级、近似重名和停用引用边界
- **`location-maintenance-queue-design.md`**：校园地点维护队列的入队信号、主优先级、重叠信号、排序与只读边界
- **`location-maintenance-decision-review-design.md`**：校园地点维护决策回看的首次留痕周期、复核比例、暂缓停留与不伪造历史处理率口径
- **`location-maintenance-decision-design.md`**：校园地点维护决策的信号指纹、三类结果、后台留痕与队列复核口径
- **`dashboard-export-subhead-alignment-design.md`**：面板与下载件的标题必须是同一句话，以及第一版为什么选错了数——`field_total` 恒为 25、`rows|length` 空平台是 0，两者问的不是同一个问题
- **`task-run-ledger-integrity-design.md`**：定时任务台账的时间、状态、失败备注与指标形状完整性审计口径
- **`deployment-health-endpoints-design.md`**：存活检查与数据库就绪检查拆分，失败不泄露部署细节
- **`campaign-goals-funnel-design.md`**：专题目标的周期口径、实时进度、节奏偏差、严格漏斗与跨专题汇总边界
- **`production-deployment.md`**：PostgreSQL、Redis、MinIO / S3、WhiteNoise、单机 Compose 拓扑与上线边界
- **`backup-restore.md`**：数据库 custom dump、对象镜像、SHA-256 校验、保留期、恢复顺序与演练边界
- **ackup-script-boundary-design.md**：备份脚本 30 项离线测试钉住的失败分支、mock 选在脚本与外部世界的接缝上、以及为什么 dump 的恢复仍要人工演练

- **`delivery-handshake-design.md`**：当面交付两次确认的顺序四档、双方同秒单列、挂起不按周期切

- **`notification-preference-usage-design.md`**：25 个通知类型开关第一次被逐个读到，被挡掉的通知一行都不建所以只报形状不报效果、四格不是两格、按交易后果分四组、没有偏好行单列，
  以及三处被代码纠正的地方（时间窗留 60 秒余量、`filter` 的 key 合并、空平台的「-1 个」）

- **`security-response-baseline-design.md`**：默认安全响应头的基线、HSTS 配置边界，以及为什么先不直接上 CSP
- **`contribution-milestone-phase-rhythm-design.md`**：同类学期同一已结束阶段的达成节奏、每百天强度与覆盖审计阻塞
- **`campaign-phase-review-design.md`**：校园专题按自身窗口复盘前中后段节奏，每百天强度、样本门槛与只读边界
- **`campaign-registration-design.md`**：专题报名、幂等参与区间、个人足迹、聚合人数和运营 CSV 同口径
- **`campaign-update-digest-design.md`**：专题上新摘要的主动订阅、命中缓冲、发送前复核、每日 / 每周去重、页面和运营口径
</details>

# 按指标告警设计

## 为什么先做需求

上一轮把 `/metrics/` 做成了跨副本的合并视图，路线图里「按指标告警」那一行并没有跟着结掉。原因很直接：数字凑齐了不等于有人看。聚合解决的是「看到哪个副本的数字」，告警要解决的是「越界的时候谁知道」。

在动手之前先认下几件本项目已经存在的事实，因为它们直接决定了这一轮的形状：

- 单机小规模部署，没有 Prometheus/Alertmanager 这条链路，也没有值班轮换。
- 监控数据只有一份暴露端点，人和外层系统都从同一个地方读。
- 生产可用性建立在「出问题时能查到」上，而不是「出问题时有人被叫醒」。

所以这一轮做的不是一套值班系统，而是把「现在有没有越界」这个判断从人的眼睛搬到代码里，并且让它和自己暴露出去的数字共用同一份输入。

## 目标

- 八条覆盖观测链路自身的规则，全部在进程内评估，不引入新的外部依赖。
- 告警判断的依据和看板上看到的数不可能对不上。
- 没有数据时不下结论：序列缺失、样本不足、取不到基线，都退回「不判断」，绝不读成 0。
- 告警自己坏掉时，`/metrics/` 仍然返回 200。
- 人有一个不用学 PromQL 的入口：`/alerts/` 返回同一批状态的 JSON。
- 数据库保留最近 1000 条真实状态翻转，`/alerts/` 展示最近 100 条；数据库不可用时自动退回进程内窗口。

## 五个设计取舍

### 一、状态翻转先入 outbox，再投递与重试

评估不修改业务事实；除 `/alerts/` 的 JSON 和 `/metrics/` 末尾的 `campus_glimmer_alert_*` 序列外，配置 `DJANGO_ALERT_WEBHOOK_URL` 后，`pending → firing` 和 `firing → inactive` 会先写入 `observability_alertnotificationdelivery`，随后立即尝试一次 Webhook。持续 firing 不重复发送，`pending → inactive` 也不发送；URL 留空即完全关闭通知。

这是有界重试的 Webhook outbox，不是一套值班系统：没有邮件、呼叫、升级路径或值班轮换。投递失败会按指数退避留在表中，Prometheus 下一次抓取或 `flush_alert_notifications` 会继续尝试；达到上限后标记 `exhausted`。`/alerts/` 的 `notification_queue` 暴露状态计数与到期数量，数据库故障时端点仍返回 200，并退回一次直接投递。做了值班系统的项目还要回答「谁在什么时候必须响应」「静默多久」，这些问题仍然留给部署方的外层监控。

### 二、状态翻转写入有界数据库历史，失败时退回进程内尾部

`AlertEngine` 只把真正的 `firing` 与 `resolved` 翻转交给 `DatabaseAlertHistory`，`pending`、持续 firing 和 `pending → inactive` 都不写。数据库表保留最近 1000 条，`/alerts/` 按最新在前返回最近 100 条，并显式给出 `history_limit`、`history_scope`、`history_persistent`、`history_degraded` 与 `history_retention`。内存 `deque(maxlen=100)` 始终保留，既承接关闭持久化的部署，也用于数据库故障降级。

这是有界排障记录，不是可靠审计：数据库写入或读取失败只把 `campus_glimmer_alert_history_errors_total{operation="write|read"}` 加一，端点仍返回 200 并标记降级；多 worker 仍可能重复记录。Webhook 重试台账单独保存在 outbox 表中，不与这条历史混用。事件里不包含 Webhook URL、Bearer Token 或其他凭据。要可靠追查跨部署历史，仍应接外层监控系统。

### 三、在抓取时评估，不起第二个定时线程

评估节奏等于抓取节奏。再起一个线程只是多一条要维护的生命周期，而两份节奏一旦漂移，看板上的告警就说不清是哪一次评估得出的结论——同一条曲线旁边挂着一个对不上时间的告警，比没有告警更费时间。

代价是没人抓取时没有评估。但没人抓取的时候，也没人在看这些数字。

### 四、阈值是模块常量，不是环境变量

`VIEW_STALE_SECONDS`、`ERROR_RATE_THRESHOLD` 这些值写在 `alerts.py` 顶部，每个都带一条注释说明为什么是这个数。改阈值等于改「什么算故障」的定义，那应该走一次提交和一次测试，而不是某个下午在部署面板上改一个数字。

环境变量适合放「随部署而变」的值（密钥、地址、副本数）。「p95 超过两秒算故障」不是随部署而变的，它是这个服务的性质。

### 五、规则的输入就是 `/metrics/` 正要暴露的那批样本

`render_alerts` 的入参是 `iter_samples(view)` 摊出来的样本，`/alerts/` 走的也是同一条路。这是本模块唯一比「算法正确」更要紧的不变式：告警判断的依据和看板上看到的数因此不可能对不上。

上一轮已经踩过一次同类问题的坑——`_families` 重构时漏了直方图的后缀，Prometheus 认不出那是直方图，而本项目的行格式校验照样全绿。这一轮的对应风险是「规则读的名字和端点暴露的名字分裂」，症状同样是某条规则永远不触发，而「永远不触发」和「没有这条规则」在看板上长得一模一样。所以序列名全部从 `metrics.PREFIX` 拼出来，不在这里重写字面量。

## 八条规则

| 规则 | 级别 | for | 判断依据 |
| --- | --- | --- | --- |
| `MetricsFleetViewStale` | warning | 60s | 视图年龄超过 15 秒 |
| `MetricsSharedReadErrorsRising` | warning | 0s | 十分钟内读取失败增加超过 3 次 |
| `MetricsBucketMismatch` | warning | 120s | 有实例的分桶与本地不同 |
| `MetricsFleetShrunk` | warning | 120s | 实例数低于一小时内峰值 |
| `MetricsLabelOverflow` | warning | 0s | 标签组合触及序列上限 |
| `MetricsServerErrorRateHigh` | critical | 300s | 5xx 占比超过 5%，且样本不少于 100 |
| `MetricsLatencyP95High` | warning | 300s | p95 超过 2 秒，且样本不少于 50 |
| `MetricsSharedDisabled` | info | 600s | 未启用跨副本共享 |

`for` 时长按「这个现象自己恢复的概率有多大」取，不按感觉取：

- **0 秒（立刻）**：标签溢出和读取失败上升都是「指标自己已经在撒谎」。前者意味着分桶已经不全，后者意味着舰队视图正在退化。这两种情况下多等一分钟只是多损失一分钟的真实数据，没有等下去的理由。
- **60-120 秒**：视图陈旧、分桶不符、实例数下降。滚动发布期间这三条必然亮着，等一次发布走完比立刻喊人有用。取两分钟是为了让一次 GC 停顿或重启不至于把值班的人叫起来。
- **300 秒**：5xx 占比和 p95。持续五分钟才算故障，一次部署抖动或一次冷启动不该触发。
- **600 秒**：共享未启用。info 级，本地开发会一直亮着——那不是噪声，那是一句正确的提醒。

## pending → firing 的语义

沿用 Prometheus 的 `for`：条件连续成立熬过 `for` 时长才升级为 firing，熬不过就退回 inactive 并把计时器清零。

一次抖动不该喊人，一次持续故障必须喊。`for` 写短了天天狼来了，写长了故障已经半小时看板上还是 pending。所以两条规则各自的 `for` 是上面那张表，不是同一个全局值。

恢复时清掉 `pending_since` 和 `firing_since`，并记下 `resolved_at`：`active_seconds` 报的是「条件连续成立了多久」，firing 时即故障已持续的时长。

## 冷启动的基线回退

错误率和延迟两条规则用的是「窗口内两个增量的比值」。严格按窗口起点取基线的话，进程刚起来的那十分钟里，窗口内每一条样本都比起点新，基线取不到，两条规则整整十分钟不判断——而这两条规则用的窗口都是十分钟，等于说刚部署完的那段时间正好是监控最可能想要它工作的时段。

退一步：取不到基线时退回窗口内最旧的样本。窗口短一点也不影响比值，读取失败那条是计数，窗口短了只会少算，不会误报。

仍然保留「一条样本算不出增量」：只有一条样本时返回 `None`。「没有历史」和「增量为 0」必须分开，否则一次重启之后的第一次抓取会报告「十分钟内零个 5xx」——那是一句没法证明的话。

## 没有数据时不许下结论

`RuleContext.gauge` 在没有对应序列时返回 `None` 而不是 `0`。把「没有这个序列」读成「这个序列是 0」，会在监控自己坏掉的时候给出一条一切正常的结论——这是所有失败模式里最难排查的一种，因为它不报错。

同理，样本不足（5xx 少于 100 次、p95 少于 50 个样本）也不判断。凌晨的一次低流量抖动足以把比例推过阈值。

## 降级与自保

评估整体包在 `try` 里。探针抛异常时 `/metrics/` 仍然 200，只是沿用上一次的结论，并把 `campus_glimmer_alert_evaluation_errors_total` 加一。一个 500 的监控端点比它要报告的故障更难排查：Prometheus 会记 `up == 0`，而看板上所有曲线一起消失。

Webhook 投递还有一层独立保护：即使通知实现意外抛出异常，也只计为 `failed` 并写一条不含 URL 的告警日志，不改变评估结果。请求使用短超时；outbox 对失败行做有界指数退避，下一次抓取或管理命令继续尝试，不会把一次网络故障放大成无限重试。没有 outbox 的本地调试模式仍保留一次性直投。

`_advance` 按需建状态，不按当次的规则集合一次性建齐。两份集合一旦漂移就是一次 KeyError，而它只在运行期显形，启动时一点声音都没有。

`SampleWindow` 是有界的：序列数由指标侧的封顶决定，时间长度由 `WINDOW_MAX_AGE`（3900 秒，不小于任何规则的窗口）决定，都不会跟着进程一起无限长大。

## 覆盖范围

`campus_glimmer/test_alerts.py` 共 74 项，分十一组：

- **HistogramQuantileTests（6 项）**：没有分桶、没有 +Inf 桶、空档、零样本时的取值，以及 p95 的插值结果。
- **AlertStateMachineTests（6 项）**：pending 不喊人、熬满 for 才 firing、中途恢复要重启计时、恢复要清掉 active_seconds、状态按副本交出、每条规则各自评估。
- **AlertHistoryTests（9 项）**：初始为空、firing/resolved 写入、持续 firing 与 pending 恢复不重复写入、最新在前与同轮反向规则顺序、100 条淘汰、返回值副本、翻转计数与 Prometheus 标签逐条一致。
- **ViewAndSharedProbeTests（8 项）**：视图陈旧、读取失败上升、分桶不符、标签溢出四条规则在「序列缺失」和「刚过阈值」两侧的行为。
- **TrafficAndLatencyProbeTests（6 项）**：5xx 占比与 p95 的样本下限、低值安静、高值报警，以及分母是含 5xx 的请求总数而不是 2xx。
- **SampleWindowTests（5 项）**：单条样本没有基线、窗口内增量、计数器回退按 0 计、峰值只算窗口内、过期样本被剪掉。
- **MetricsExposureTests（6 项）**：八个告警家族的 TYPE 行唯一、规则读的名字与端点暴露的名字逐条相同、firing/pending/active_seconds 三族的成员与状态机完全一致、评估计数随抓取推进、告警拼在业务指标之后。
- **AlertsEndpointTests（7 项）**：未配令牌 404、只允许 GET/HEAD、载荷带服务身份与计数、静默规则也在载荷里、有界历史与来源声明、历史不含 Webhook 凭据、JSON 与 `/metrics/` 给出同一个 firing 集合。
- **AlertSelfPreservationTests（4 项）**：探针抛异常时端点仍 200 且计入评估错误、失败时沿用上一次结论、抓 `/alerts/` 不喂业务指标、单例可重置。
- **AlertNotificationTests（10 项）**：pending 不发、首次 firing 只发一次、持续 firing 不重复、pending 恢复不发、firing 恢复发 resolved、多规则合并批次、载荷/请求头/Bearer 令牌、未配置时跳过、网络与非 2xx 失败不外溢、通知计数进入 Prometheus。
- **AlertHistoryPersistenceTests（7 项）**：数据库写入与读回、1000 条上限、写入失败与读取失败降级、端点默认使用数据库历史、数据库故障不拖垮两个监控端点、显式关闭持久化。

投递队列另有 `observability/tests.py` 的 8 项测试覆盖：立即投递、跨副本去重、退避后重试、重试耗尽、台账清理、队列摘要、数据库故障直投降级、管理命令重试与旧记录清理。两者合计 82 项。

「规则读的名字与端点暴露的名字逐条相同」和「三族成员与状态机完全一致」是这里最值得留意的两条。前者防的是名字分裂（症状是规则永远不触发），后者防的是把 pending 也画成 firing（症状是看板把每条规则都显示成正在响，而所有数值本身毫无异常）。这两条在写完之后都用变异测试验证过：把对应判断改坏，有用例会红。

## 边界

这一轮**没有**做的部分，需要如实说明：

- **通知是可选且有界重试的**。数据库 outbox 保存状态、尝试次数和下次重试时间，失败可继续收敛；但它不是消息队列或值班系统，没有无限重试、人工确认、升级路径和优先级保证。多个 worker 仍各自维护状态，同一时间窗口内的翻转通过幂等键合并。
- **持久化历史不是可靠审计**。数据库只保留最近 1000 条状态翻转，`/alerts/` 只展示最近 100 条；数据库失败时降级为进程内窗口，多 worker 仍可能重复记录。投递台账只回答通知是否送达，不替代事故时间线。
- **没有抑制与静默**。没有「维护窗口内不喊」、没有「同一故障合并成一条」、没有升级路径。这些是值班系统的功能，不在本模块范围内。
- **规则是硬编码的八条**。不提供用户自定义规则、不提供配置文件里的阈值、不提供 PromQL。加一条规则就是加一个 `_probe_*` 函数和一行 `RULES`。
- **`for` 的计时依赖抓取节奏**。两次抓取间隔超过 `for` 时长的规则（300 秒那两条）在抓取间隔过长时会晚一个周期才 firing。这是「评估节奏等于抓取节奏」的代价，写在这里以免以后有人以为是 bug。

## Webhook 配置与运维边界

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `DJANGO_ALERT_WEBHOOK_URL` | 空 | 完整的 `http://` 或 `https://` 地址；留空即关闭通知 |
| `DJANGO_ALERT_WEBHOOK_BEARER_TOKEN` | 空 | 可选，作为 `Authorization: Bearer` 发送 |
| `DJANGO_ALERT_WEBHOOK_TIMEOUT` | `2.0` | 单次 POST 超时秒数，必须为正数 |
| `DJANGO_ALERT_HISTORY_DB_ENABLED` | `true` | 翻转历史是否写入数据库；关闭后只保留进程内窗口 |
| `DJANGO_ALERT_NOTIFICATION_QUEUE_ENABLED` | `true` | 是否在配置 Webhook 后启用数据库 outbox；关闭时退回一次性直投 |
| `DJANGO_ALERT_NOTIFICATION_MAX_ATTEMPTS` | `5` | 单条通知的最大尝试次数 |
| `DJANGO_ALERT_NOTIFICATION_RETRY_BASE_SECONDS` | `30` | 指数退避的基础秒数 |
| `DJANGO_ALERT_NOTIFICATION_RETRY_MAX_SECONDS` | `3600` | 单次退避上限 |
| `DJANGO_ALERT_NOTIFICATION_DEDUPE_SECONDS` | `60` | 跨副本合并同一翻转的时间窗口 |
| `DJANGO_ALERT_NOTIFICATION_LEASE_SECONDS` | `120` | 投递认领租约，进程崩溃后由命令接手 |

Webhook 地址在启动时校验。配置 URL 后默认启用 outbox；Prometheus 抓取会首次投递并继续处理到期重试，`python manage.py flush_alert_notifications` 则用于没有抓取流量时主动补投并清理默认 30 天前的终态记录。多 worker 仍各自维护状态，但同一规则、事件、状态和 60 秒窗口会生成相同幂等键；需要更严格顺序、优先队列或升级响应时，应在外部监控层完成。

## 运维上必须知道的两件事

**一、告警只覆盖观测链路自己。** 八条规则里没有一条盯着业务表的行数、没有一条盯着「今天有没有成交」。它们盯着的是「你看到的这些数字还值不值得信」。业务指标的超标仍然写在外层监控系统里——那需要知道什么算正常，而那只有运营知道。

**二、`/alerts/` 和 `/metrics/` 用同一把令牌。** 两个端点回答的是同一批问题的不同侧面，权限不该分成两套。`METRICS_TOKEN` 未设置时两个端点都返回 404，不是 401：没有令牌的部署本来就不该暴露观测端点，404 比 401 更诚实。

# 通知偏好开关：25 个开关各自的形状，第一次被运营读到

状态：已实现（只报告，不改任何一处写入路径）
模块：`listings/notification_preference_usage.py`
新增表：无（不新增表、不加迁移）
写入点：无（纯读）
面板：`notification-preference-usage-panel`「通知偏好开关：25 个开关各自的形状」

## 1. 来龙去脉

`NotificationPreference` 上有 26 个布尔字段，其中 25 个是通知类型的开关，1 个是
免打扰总闸（`quiet_hours_enabled`）。每个开关都有真实的读取路径：
`notifications.py:66` 的 `create_notification` 第一件事就是
`if not getattr(preference, kind, True): return None`——**开关关掉的通知，在写入
那一刻就被静默丢弃，库里连一行都没有。**

这个字段的读取路径此前散落在六处写入侧（`notifications.py` 的 `quiet_hours_*`、
`operations_digest.py:52`、`opportunity_digest.py:46`、`saved_search_digest.py:124`、
`lifecycle_reminder_effect.py:392`）和一处展示侧（`forms.py:40` 的表单字段）。
**运营侧只有 `moderation_notice_preference` 读过一行值，而它只读 `moderation_update`
一个开关。** 其余 24 个开关关没关、谁关的、关了之后通知还来不来，一处都答不上来。

选题的过程本身值得记一笔。最初想找的是「只写在 models.py 里、其他文件零引用」的
孤立模型——这条路走不通，45 个模型里没有一个是孤立的，这个项目的写入路径一直很密。
于是换成另一个问法：**哪些表被写、有面板，但某个字段维度从未被拆过。** 把每个候选
模型在 `listings/*.py` 与 `templates/listings/*.html` 里的出现次数和看板现有的面板
清单对照，`NotificationPreference` 是 23 个文件里都出现、却几乎全是写入侧和展示侧的
那一个。上一轮 `notification_kind_coverage` 已经报了「三个名单对不齐」，但报的是
**有哪些名字**，不是**这些名字各自的值被读了多少次**。本轮补的是后一半。

## 2. 最大的口径难题：关掉之后通知变少了，从 `Notification` 表里读不出来

被关掉的通知根本不存在于表中，所以「这个开关有没有生效」不能拿 `Notification` 的
条数当证据。本模块因此**只报偏好的形状，不报偏好的效果**：

- 报「多少人关掉了这个开关」「关掉的人里有多少近期还在活跃」；
- **不报**「关掉之后这一类通知少了多少」——分子在库里，分母不在。拿得到的那个比是
  现存通知数除以用户数，它随窗口长短和用户活跃度漂移，不是开关的效果。

这一条和 `notification_snooze_return` 的右删失样本是同一类问题：缺的那一半不在库里。
报一个能被误读成「量过」的数，比没有数字更糟。

## 3. 四格，不是两格

「关掉的人」和「开着的人」不能直接比，因为他们本来就是两群不同的人——一个天天收
订单通知的人和一个早就把订单通知关掉的人，比出来的差异和开关没有因果关系。本模块
按**近期有没有真的收到过这一类通知**分四格：

1. 近期收到过、开关开着；
2. 近期收到过、开关关着（**这一格是观测对象**：它只可能是窗口前收到的，或者汇总类
   通知——`operations_digest` / `opportunity_digest` / `saved_search_digest` 三类走的
   是偏好闸门、但通知本身在另一个时刻生成）；
3. 近期没收到过、开关开着；
4. 近期没收到过、开关关着。

第 2 格的规模回答「有多少人是关着开关、却仍然在收这一类的东西」——如果这一格很大，
说明这个开关要么没接上、要么接的是一条用户没意识到的路径。

## 4. 按「关掉它有没有交易后果」分四组，不合成一个关闭率

25 个开关不是一类东西。关掉 `order_created`（有人预约你的商品）有真实的交易后果：
用户收不到通知，可能错过一笔成交。关掉 `moderation_update`（你违规了）没有任何交易
后果。本模块按这个性质分四组报：

- **交易类**（9 个）：`order_created`、`order_status`、`order_dispute`、
  `meeting_incident`、`order_expiring`、`order_expired`、`gift_application`、
  `gift_application_status`、`demand_response`；
- **互动类**（3 个）：`message_received`、`comment_received`、`rating_received`；
- **汇总类**（3 个）：`operations_digest`、`opportunity_digest`、`saved_search_digest`；
- **其余**（10 个）：按原样列出，不强行归组。

合成一个全站关闭率会把「用户关掉了运营告警日报」和「用户关掉了新订单通知」读成
同一件事。

## 5. 偏好行不存在是一格，不是零

`NotificationPreference` 是 `get_or_create` 出来的（`create_notification` 里、
`quiet_hours_active` 里各一处），默认全开。一个从没创建过偏好行的用户，行为上和
「全部开着」完全一致，但他和「显式保存过一次设置」的用户不是同一群人。本模块把
「没有偏好行」单列，**不并入「开着」**——否则「多少人碰过设置页」这个问题就永远
问不出来。

## 6. 设计期间被代码纠正的三处

**这三处原样留在这里，因为它们是本轮真正的工作量，也比结论更值得留。**

### 6.1 时间窗上界差 1.2 秒，`off_but_seen` 恒为 0

现象：冒烟测试里 `notified_users` 是 1，`off_but_seen` 是 0。根因是视图先取 `now`、
通知后写，`Notification.created_at` 是 `auto_now_add`，它比传进来的 `now` **晚了
1.2 秒**（测试建库慢）。最初只留 `+1 秒` 余量，不够。修法是把上界放宽到
`end + timedelta(seconds=60)`：统计窗口是 7/30/90/365 天，多收 1 分钟内的行不改变
任何一个比例的方向，而丢掉刚刚发生的行会。**这个坑对所有读 `Notification` 的模块都
成立**，本项目的模块以后一律直接留 60 秒。

### 6.2 `filter(**{field: False, 'user_id__in': seen})` 是错的

两个 key 混在一个 dict 里解包给 `filter`，Django 不会按预期把它们合并。修法是
`filter(**{field: False}, user_id__in=seen)`。这个错在测试里表现为第 2 格恒为 0，
和 6.1 的现象一模一样，所以两处是一起定位的。

### 6.3 模板 `{{ rows|length|add:"-1" }}` 在空平台渲染出「其余 -1 个」

空平台 `rows` 是 `[]`，`|length` 给 0，`add:"-1"` 给 -1。页面照样 200，只是文案变成
一句错话——**冒烟测试抓不到这种，因为它只断言状态码。** 修法是模块在两条返回路径
都加 `field_total`（`len(fields)`），模板改用
`{{ notification_preference_usage.field_total }}` 和
`{{ notification_preference_usage.field_total|add:"-1" }}`，**不再有 `{% if %}` 分支，
也没有硬编码 25**——硬编码会在将来加开关时说错。

## 7. 看板回答什么

- 25 个开关逐个：开着多少行、关掉多少行、近期收到过的用户、关着却仍收到过、开着却
  没收到过；
- 按四组分的开关规模与关闭占比（占比要过 `MIN_PREFERENCE_ROWS` 才给）；
- 免打扰总闸单独一组，它和通知类型不是一类东西，但摆在同一张表单里；
- 覆盖：多少用户有偏好行、多少用户收到过通知却还没有偏好行。

## 8. 口径汇总

- 开关名单从 `NotificationPreference._meta.get_fields()` 现读布尔字段，排除免打扰
  总闸；加了新开关自动进面板，不硬编码；
- 门槛按行算（`MIN_PREFERENCE_ROWS = 12`），和按条数算的样本门槛
  （`MIN_BUCKET_SAMPLE = 12`）分开立——偏好是逐用户一行，按人算和按条数算不是
  一回事，取同一个数量级；
- 样本不足的组照实报数字、只把字色压淡，不隐藏；
- 空平台早退：`rows=[]`、`by_group=[]`、`has_data=False`，不报 25 行全 0。空行和
  「没有人用这个开关」是两件不同的事，前者是空白，后者才是答案；
- 只报形状，不排名：不列谁关了什么、不按用户排名、不输出用户名。

## 9. 不做什么

- 不加迁移、不加字段、不改 `KIND_CHOICES`、不改表单字段列表——那三个数的缺口
  `notification_kind_coverage` 已经报了，修不修是另一个决定；
- 不改 `create_notification` 的静默 `return None`，不把它改成记日志——那是本模块要
  观测的对象；
- 不自动打开任何开关、不提醒用户「你关掉了交易通知」；
- 不报「关掉之后通知少了多少」，见第 2 节；
- 不按用户排名、不输出用户名。

## 10. 先看板后改规则的理由

「用户把某一类通知关掉之后发生了什么」这个问题此前没有任何数字支撑，而它是通知
体验改动的唯一依据：改文案、改频率、改分组，都要先知道用户在用开关做什么。但
**形状本身不指向任何一处改动**——25 行数字里没有一行能自动变成一条产品决定，所以
这一轮只做读，不做任何自动调整。真正值得等的是第 3 节第 2 格的积累：如果某一天
「关着却仍收到过」在某个交易类开关上变得很大，那是一个具体的 bug，不是一条产品
建议。
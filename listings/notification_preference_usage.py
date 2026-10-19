# -*- coding: utf-8 -*-
"""通知偏好开关：每种通知类型各自的形状，第一次被运营读到。

`NotificationPreference` 上为每种通知类型各保留一个布尔开关，另有 1 个免打扰总闸
（`quiet_hours_enabled`）。每个开关都有真实的读取路径：
`notifications.py:66` 的 `create_notification` 第一件事就是
`if not getattr(preference, kind, True): return None`——**开关关掉的通知，在写入那一刻
就被静默丢弃，库里连一行都没有。**

这个字段的读取路径此前散落在六处写入侧（`notifications.py` 的 `quiet_hours_*`、
`operations_digest.py:52`、`opportunity_digest.py:46`、`saved_search_digest.py:124`、
`lifecycle_reminder_effect.py:392`）和一处展示侧（`forms.py:40` 的表单字段），
**运营侧只有 `moderation_notice_preference` 读过一行值，而它只读 `moderation_update` 一个
开关。** 其余开关关没关、谁关的、关了之后通知还来不来，一处都答不上来。

本模块把它们一次读完，只报告：不加迁移、不加字段、不改 `create_notification`、
不改表单、不给任何人自动打开开关。`create_notification` 那句静默的 `return None`
正是本模块要观测的对象——改掉它，观测对象就没了。

## 一、最大的口径难题：关掉之后通知变少了，从 `Notification` 表里读不出来

被关掉的通知根本不存在于表中，所以「这个开关有没有生效」不能拿 `Notification` 的条数
当证据。本模块因此**只报偏好的形状，不报偏好的效果**：

- 报「多少人关掉了这个开关」「关掉的人里有多少近期还在活跃」；
- **不报**「关掉之后这一类通知少了多少」——分子在库里，分母不在，拿得到的那个比
  是拿现存通知数除以用户数，而它随窗口长短和用户活跃度漂移，不是开关的效果。

这一条和 `notification_snooze_return` 的右删失样本是同一类问题：缺的那一半不在库里，
报一个能被误读成「量过」的数，比没有数字更糟。

## 二、四格，不是两格

「关掉的人」和「开着的人」不能直接比，因为他们本来就是两群不同的人——一个天天收
订单通知的人和一个早就把订单通知关掉的人，比出来的差异和开关没有因果关系。本模块
按**近期有没有真的收到过这一类通知**分四格：

1. 近期收到过、开关开着；
2. 近期收到过、开关关着（**这一格是观测对象**：它只可能是窗口前收到的、或者汇总
   类通知——`operations_digest` / `opportunity_digest` / `saved_search_digest` 三类
   走的是偏好闸门但通知本身在另一个时刻生成）；
3. 近期没收到过、开关开着；
4. 近期没收到过、开关关着。

第 2 格的规模回答「有多少人是关着开关、却仍然在收这一类的东西」——如果这一格很大，
说明这个开关要么没接上、要么接的是一条用户没意识到的路径。

## 三、关掉的是哪一类，按「有没有交易后果」分

这些开关不是一类东西。关掉 `order_created`（有人预约你的商品）有真实的交易后果：
用户收不到通知，可能错过一笔成交。关掉 `moderation_update`（你违规了）没有任何交易
后果。本模块按这个性质分四组报，不合成一个「关闭率」：

- **交易类**：`order_created`、`order_status`、`order_dispute`、`meeting_incident`、
  `order_expiring`、
  `order_expired`、`gift_application`、`gift_application_status`、`demand_response`；
- **互动类**：`message_received`、`comment_received`、`rating_received`；
- **汇总类**：`operations_digest`、`opportunity_digest`、`saved_search_digest`；
- **其余**：剩下的按原样列出，不强行归组。

合成一个关闭率会把「用户关掉了运营告警日报」和「用户关掉了新订单通知」读成同一件事。

## 四、偏好行不存在是一格，不是零

`NotificationPreference` 是 `get_or_create` 出来的（`create_notification` 里、
`quiet_hours_active` 里各一处），默认全开。一个从没创建过偏好行的用户，行为上和
「全部开着」完全一致，但他和「显式保存过一次设置」的用户不是同一群人。本模块把
「没有偏好行」单列，**不并入「开着」**——否则「多少人碰过设置页」这个问题就永远
问不出来。

## 五、只报形状，不排名

关掉通知偏好是用户的私事。本模块不列「谁关了什么」、不按用户排名、不按用户名输出，
只输出聚合数字。四格之间只报规模，比值只给方向（哪一格更大），不给精确百分比差。

## 六、不做什么

- 不加迁移、不加字段、不改 `KIND_CHOICES`、不改表单字段列表——`notification_kind_coverage`
  已经报了那三个数的缺口，修不修是另一个决定；
- 不改 `create_notification` 的静默 `return None`，不把它改成记日志；
- 不自动打开任何开关、不提醒用户「你关掉了交易通知」；
- 不报「关掉之后通知少了多少」——见第一节；
- 不按用户排名、不输出用户名。
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import Notification, NotificationPreference

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 给出方向前至少要有这么多条样本。全站统一的「12 条」。
MIN_BUCKET_SAMPLE = 12

# 一个类型的偏好行至少要这么多行，才允许对它下「这一类关的人偏多」的判断。
# 偏好是逐用户一行，门槛按人算，和按条数算的样本门槛不是一回事，但取同一个
# 数量级：低于这个数，一个人就是一个百分点。
MIN_PREFERENCE_ROWS = 12

# 通知类型的分组。按「关掉它有没有交易后果」分，不合成一个关闭率。
PREFERENCE_GROUPS = (
    ('transaction', '交易类', (
        'order_created', 'order_status', 'order_dispute', 'meeting_incident',
        'order_expiring', 'order_expired',
        'gift_application', 'gift_application_status', 'demand_response',
    )),
    ('interaction', '互动类', (
        'message_received', 'comment_received', 'rating_received',
    )),
    ('digest', '汇总类', (
          'operations_digest', 'opportunity_digest', 'saved_search_digest',
          'campaign_update',
    )),
    ('other', '其余', (
        'saved_search_match', 'item_available', 'item_expired',
        'report_update', 'moderation_update', 'demand_match',
        'lost_found_match', 'lost_found_lead', 'mutual_aid_feedback', 'item_price_drop',
        'lifecycle_reminder',
    )),
)

# 免打扰总闸单独一组——它不是通知类型，但和通知类型开关摆在同一个模型上、
# 同一张表单里，用户关掉它的频率是最能说明「这一页好不好用」的信号。
QUIET_HOURS_FIELD = 'quiet_hours_enabled'


def _group_label(group_key):
    for key, label, _ in PREFERENCE_GROUPS:
        if key == group_key:
            return label
    return group_key


def _group_of(field):
    for key, _, members in PREFERENCE_GROUPS:
        if field in members:
            return key
    return 'other'


def _preference_fields():
    """从模型现读布尔开关名单，不硬编码。

    硬编码的名单会和代码走散，而那正是 `notification_kind_coverage` 要报的问题。
    这里只做一件事：把 `NotificationPreference` 上的布尔字段按定义顺序取出来，
    排除免打扰总闸。
    """
    return [
        field.name for field in NotificationPreference._meta.get_fields()
        if getattr(field, 'concrete', False)
        and field.get_internal_type() == 'BooleanField'
        and field.name != QUIET_HOURS_FIELD
    ]


def _recently_notified_users_by_kind(kinds, start, end):
    """窗口内真的收到过各类通知的用户集合，按类型各一份。

    注意这是「通知存在」的下界，不是「事件发生」的下界——被开关挡掉的通知
    一行都没有，见模块文档字符串第一节。这里不试图还原事件数。

    上界取 `end + 60 秒`：`Notification.created_at` 是 `auto_now_add`，而传进来的 `now` 总可能
    早于刚刚写入的那一行——视图先取 `now`、通知后写，两者间隔可以是一秒以上。
    上界严严地等于 `now` 会把刚写入的行整批排除，而那看起来就像一个计算错误。
    余量取 60 秒而不是 1 秒：统计窗口是 7/30/90/365 天，多收 1 分钟内的行不改变任何一个比例的方向，
    而丢掉刚刚发生的行会。

    一次查询拿回全部类型。**这不是把 N 条查询 UNION 起来**：UNION 只是把往返次数
    塞进一条 SQL，执行计划不会变好，而一张运营页面上的多层 UNION 会让优化器放弃索
    引、退成全表扫描。这里是一次 `kind IN (...)` 的范围扫描，命中
    `notification_recipient_kind_idx`，数据库要读的行数和逐类型查「去重收件人」完全
    一样——两边都是同一批 (类型, 收件人) 对——只是从 27 次往返变成 1 次。

    `.order_by()` 是显式清空模型默认的 `-created_at`：留着它，`DISTINCT` 会把
    `created_at` 一起选出来，同一对 (类型, 收件人) 会按通知条数重复出现。Python
    那边照样按集合去重，但没必要让数据库白搬这些行。
    """
    # 每个要问的类型都先给一份空集合：调用方直接 `notified[field]`，缺键会
    # KeyError，而「没收过」和「没这一格」是两件不同的事——前者是 0，后者是报错。
    notified = {kind: set() for kind in kinds}
    rows = Notification.objects.filter(
        kind__in=kinds, created_at__gte=start,
        created_at__lt=end + timedelta(seconds=60),
    ).order_by().values_list('kind', 'recipient_id').distinct()
    for kind, recipient_id in rows:
        notified[kind].add(recipient_id)
    return notified


def build_notification_preference_usage(days=30, now=None):
    """把每种通知类型开关各自的四格形状读出来。"""
    now = now or timezone.now()
    start = now - timedelta(days=days)
    fields = _preference_fields()

    # 一次查询拿回全部类型的「窗口内收到过」集合，见
    # `_recently_notified_users_by_kind`：数据库读的行数和逐类型查一模一样，往返从
    # 27 次变成 1 次。
    notified = _recently_notified_users_by_kind(fields, start, now)

    # 偏好行按用户逐行读。没有偏好行的用户单列，不并入「开着」。
    # 空平台不报 25 行全 0：那会让面板出现一堆空行，而空行和「没有人用这个开关」
    # 是两件不同的事——前者是空白，后者才是答案。
    if not NotificationPreference.objects.exists():
        return {
            'days': days,
            'field_total': len(fields),
            'summary': (
                f'{len(fields)} 个开关逐个报，按「关掉它有没有交易后果」分四组，不合成一个全站关闭率。'
                '当前平台还没有任何偏好行。'
            ),
            'rows': [],
            'by_group': [],
            'quiet_hours': {'on': 0, 'off': 0, 'rows_total': 0},
            'coverage': {
                'rows_total': 0,
                'users_with_notification': Notification.objects.values('recipient').distinct().count(),
                'users_without_row': Notification.objects.values('recipient').distinct().count(),
            },
            'min_preference_rows': MIN_PREFERENCE_ROWS,
            'min_bucket_sample': MIN_BUCKET_SAMPLE,
            'has_data': False,
        }

    # 每个开关的四格原本是四次 COUNT：27 个开关就是一百多次往返，而它们报的都是同一批
    # 偏好行的不同切面。合成一次 aggregate 让数据库只扫一遍表——查询数从一百次降到
    # 一次，四格的口径一个都没换：开的还是开、关的还是关，「近期收到过」仍然只认
    # 上面那份逐类型用户集合，不重新定义。
    switch_aggregates = {}
    for field in fields:
        switch_aggregates['on_%s' % field] = Count('id', filter=Q(**{field: True}))
        switch_aggregates['off_%s' % field] = Count('id', filter=Q(**{field: False}))
        seen = notified[field]
        if not seen:
            # 空集合走原来的早退分支：第 2 格是 0，第 4 格就是「开着」的全部。
            continue
        switch_aggregates['off_seen_%s' % field] = Count(
            'id', filter=Q(**{field: False, 'user_id__in': seen}),
        )
        switch_aggregates['on_seen_%s' % field] = Count(
            'id', filter=Q(**{field: True, 'user_id__in': seen}),
        )
    switch_aggregates['quiet_on'] = Count('id', filter=Q(**{QUIET_HOURS_FIELD: True}))
    switch_aggregates['quiet_off'] = Count('id', filter=Q(**{QUIET_HOURS_FIELD: False}))
    switch_aggregates['rows_total'] = Count('id')
    preference_counts = NotificationPreference.objects.aggregate(**switch_aggregates)

    rows = []
    for field in fields:
        on_with_row = preference_counts['on_%s' % field]
        off_with_row = preference_counts['off_%s' % field]
        seen = notified[field]
        # 第 2 格：近期收到过、开关却关着。只可能是窗口前收到的，或者汇总类
        # 通知在另一个时刻生成——两种都记，不拆，因为拆不开。
        off_but_seen = preference_counts['off_seen_%s' % field] if seen else 0
        on_but_unseen = (
            on_with_row - preference_counts['on_seen_%s' % field] if seen else on_with_row
        )
        rows.append({
            'field': field,
            'group': _group_of(field),
            'group_label': _group_label(_group_of(field)),
            'on': on_with_row,
            'off': off_with_row,
            'rows_total': on_with_row + off_with_row,
            'off_but_seen': off_but_seen,
            'on_but_unseen': on_but_unseen,
            'notified_users': len(seen),
            'has_enough_rows': (on_with_row + off_with_row) >= MIN_PREFERENCE_ROWS,
        })

    # 免打扰总闸单独一行。它和通知类型不是一类东西，但摆在同一张表单里。
    # 这两个计数已经并进上面那一次 aggregate，不再各发一次 COUNT。
    quiet_on = preference_counts['quiet_on']
    quiet_off = preference_counts['quiet_off']

    # 没有偏好行的用户数：收到过通知却还没建过偏好行的。
    users_with_notification = Notification.objects.values('recipient').distinct().count()
    rows_total = preference_counts['rows_total']
    users_without_row = max(0, users_with_notification - rows_total)

    by_group = []
    for key, label, members in PREFERENCE_GROUPS:
        group_rows = [r for r in rows if r['group'] == key]
        if not group_rows:
            continue
        on = sum(r['on'] for r in group_rows)
        off = sum(r['off'] for r in group_rows)
        by_group.append({
            'key': key,
            'label': label,
            'fields': [r['field'] for r in group_rows],
            'on': on,
            'off': off,
            'share': (off / (on + off)) if (on + off) else None,
            'has_enough_rows': (on + off) >= MIN_PREFERENCE_ROWS,
        })

    summary = (
        f'{len(fields)} 个开关逐个报，按「关掉它有没有交易后果」分四组，不合成一个全站关闭率。'
        '被开关挡掉的通知连一行都不建，所以「关掉之后通知少了多少」不在本模块口径里——'
        '分子在库里，分母不在。没有偏好行的用户单列一格，不并入「开着」。'
    )
    return {
        'days': days,
        'field_total': len(fields),
        'summary': summary,
        'rows': rows,
        'by_group': by_group,
        'quiet_hours': {
            'on': quiet_on,
            'off': quiet_off,
            'rows_total': quiet_on + quiet_off,
        },
        'coverage': {
            'rows_total': rows_total,
            'users_with_notification': users_with_notification,
            'users_without_row': users_without_row,
        },
        'min_preference_rows': MIN_PREFERENCE_ROWS,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'has_data': bool(rows_total),
    }

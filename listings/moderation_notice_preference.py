# -*- coding: utf-8 -*-
"""内容审核结果通知：开关、触达与沉默，第一次被运营读到。

`moderation_update`（内容审核结果）是站内 27 种通知里唯一一条**平台自己说话**的通知：
其余 26 种讲的都是人和人之间的事——有人预约了你的商品、有人申请了你的免费商品、
有人给你发了私信。只有这一种，判你违规、然后通知你的，是平台自己。它走的是
`chat_messages/views.py:442` 的 `create_notification(..., kind='moderation_update')`，
而站内信之外没有第二条路径。

这个字段的读取路径此前只有两处：`forms.py:40` 的表单字段，和 `notifications.py:66` 的
`getattr(preference, kind, True)`。**一个开关被表单收集、被写入路径读取，从来没被统计过。**

`notification_response` 报了 24 类的处理时效，`notification_aggregation_quality` 报了聚合
质量，`notification_snooze_return` 报了延后回归，`notification_kind_coverage` 报了版图缺口
——四个模块都在读 `Notification` 表，**没有一个读过 `NotificationPreference` 的一行值**。
于是「用户把这一类通知关掉之后发生了什么」整类问题，一处都答不上来。

`moderation_rule_quality` 已经回答了「规则判得准不准」，但它的终点是
`ModerationEvent.status`。**复核结论发出去之后，接收端发生了什么，五个模块一处都没接上。**

本模块把它们接上，只报告：不加迁移、不加字段、不改 `create_notification`、不改复核写入
路径、不给任何人自动打开开关。`create_notification` 那句静默的 `return None` 正是本模块
要观测的对象，改掉它，观测对象就没了。

四节，每一节的口径都比数字本身更要紧：

**一、触达要拆成因，不能合成一个未送达率。** 「复核完了，作者没收到通知」有四种完全
不同的成因：开关关着（`notifications.py:66` 静默返回）、免打扰时段（`quiet_hours_active`）、
自己是自己内容的作者（`create_notification` 第一行的 `actor.pk == recipient.pk` 短路）、
偏好行不存在（`get_or_create` 默认全开，正常路径下不可能）。合成一个数，运营看到的
未送达率会随「复核人是不是内容作者」这种人极少的情况抖动，而那和用户有没有关开关
毫无关系。

**二、偏好四格只报人数，不报任何两格之间的比值。** 关掉一个通知偏好是用户的私事，
做成排行榜就是把设置页变成公开表态。而四格里后两格（没被复核过内容的人）关不关这个
开关，和「收到过审核结果」没有因果关系——直接拿第二格比第四格，比出来的是两群不同的人，
不是同一群人的两种选择。

**三、关掉之后人还在不在社区里说话，只报形状，不作因果。** 关掉通知不会让人不说话，
收到一条「你违规了」也不会。两群人在发言率上的差，可能只是「常发言的人更在意审核结果」。
方向只允许三档（关组更高 / 开组更高 / 两组相同），不给百分比差——一个 3 个百分点和
一个 30 个百分点在看板上长一样，给的指引却完全相反。**不按任何用户维度排名。**

**四、已读率按复核结论拆。** 确认违规和误判放行是两条情绪上完全不同的消息：一条说你
违规了，一条说我们搞错了。它们的已读率没有理由相同。同时报同期全部通知的整体已读率
作对照——少了这个分母，单看一类的已读率读不出「这一类是不是特别没人看」。

**不重复别人的结论。** 不重复 `notification_kind_coverage` 的版图（那个报选择项与表单
字段的差集，这个报值本身）、不重复 `notification_response` 的时效（只报已读率这一项）、
不重复 `moderation_rule_quality` 的规则质量（那个的终点是 `status`，这个从 `status`
往后接一步）、不重复 `notification_snooze_return` 的延后结论。

**一个字符的自由文本都不读。** `ModerationEvent.content` 是被拦截的原文，
`Notification.title` / `message` 是发给用户的话，三处都可能写着电话、微信、楼栋号。
"""

from collections import namedtuple
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from chat_messages.models import ModerationEvent

from .models import Notification, NotificationPreference
from .notifications import quiet_hours_active


# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
)

# 给出方向前至少要有这么多条样本。全站统一的「12 条」。
MIN_BUCKET_SAMPLE = 12

# 偏好四格要报差值，两群各自至少这么多人。偏好是逐用户一行，门槛按人算，
# 和按条数算的样本门槛不是一回事，但取同一个数量级：低于这个数，
# 一个人就是一个百分点。
MIN_PREFERENCE_USERS = 12

# 这一类的偏好字段名。只读这一个开关——它是站内唯一一条平台自己说话的通知，
# 也是唯一一条关掉它没有任何交易后果的通知。
PREFERENCE_FIELD = 'moderation_update'

# 复核结论。`confirmed` 是确认违规，`dismissed` 是误判放行。
# 注意 `dismissed` 在这里是「放行」，和 `MeetingIncident` 的 `dismissed`（驳回）
# 是相反的意思——同一批运维看两张表，两个 `dismissed` 摆在一起，靠字段名分不开，
# 所以这里显式给中文标签，模板和导出一律用标签，不出现英文原值。
DECISION_CHOICES = (
    ('confirmed', '确认违规'),
    ('dismissed', '误判放行'),
)

# 已复核状态：只有这两种算「复核完了」。还挂在 `pending` 的是右删失样本——
# 复核还没发生，谈不上通知发没发。
REVIEWED_STATUSES = ('confirmed', 'dismissed')

# 触达结果。四种成因分开数，见模块文档字符串。
REACH_BANDS = (
    ('delivered', '收到了通知'),
    ('switch_off', '开关关着'),
    ('quiet_hours', '免打扰时段'),
    ('self_authored', '复核人是作者本人'),
    ('no_preference_row', '取不到偏好行'),
)

# 发言行为的三档形状。只报「有没有」，不报条数，不报内容。
ACTIVITY_KINDS = (
    ('comment', '新的商品留言'),
    ('message', '新的私信'),
    ('item', '新发布的商品'),
)

# 方向只允许三档。不给百分比差。
DIRECTION_BANDS = (
    ('switch_off_higher', '关掉的一组更高'),
    ('switch_on_higher', '开着的一组更高'),
    ('same', '两组相同'),
)

# 模块需要读的列。保持显式：90 天视图上审核记录可能不少，只取用得上的。
# 不读 content——被拦截的原文是自由文本。
PERIOD_FIELDS = (
    'id',
    'channel',
    'status',
    'author_id',
    'item_id',
    'reviewed_by_id',
    'reviewed_at',
    'created_at',
)


# 一条审核记录，连它发出去的通知一起取回来。一次查完固定下来：触达、偏好四格、
# 发言行为、已读率各读一遍，按记录现算会让每条记录打好几遍库，而看板是一次打开全算的。
EventRecord = namedtuple('EventRecord', [
    'event', 'notice', 'preference', 'reach', 'author_activities',
])


def _percent_or_none(part, whole):
    """拿不到的分母报 None 不报 0。

    0 会被读成「发了，占比很小」，而这里的事实是「分母不存在」。看板模板把 None
    显示成「—」，和 0 区分开。
    """
    return round(part / whole * 100, 1) if whole else None


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    """
    from datetime import time as dt_time
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _decision_labels():
    return dict(DECISION_CHOICES)


def _reach_labels():
    return dict(REACH_BANDS)


def _fetch_events(days, now):
    """周期内复核完的审核记录，按复核时间取。

    按复核时间而不是创建时间：这个模块问的是「复核结论发出去之后发生了什么」，
    窗口要圈住的是被发出去的那批结论。

    按复核时间与主键显式排序：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    """
    start = _period_start(days, timezone.localtime(now).date())
    return list(
        ModerationEvent.objects.filter(
            status__in=REVIEWED_STATUSES,
            reviewed_at__gte=start,
            reviewed_at__lte=now,
        )
        .values(*PERIOD_FIELDS)
        .order_by('reviewed_at', 'id')
    )


def _fetch_notices(author_ids):
    """这批作者的 `moderation_update` 通知，按作者归拢。

    一条通知只可能由一次复核产生，而一次复核只产生一条——`create_notification`
    这里没传 `dedupe_key`，走的是每次都新建的路径。所以按作者取最近的一条即可，
    不按事件对。真要按事件对得上，得给通知加外键，那是加字段，本模块不做。
    """
    if not author_ids:
        return {}
    rows = Notification.objects.filter(
        recipient_id__in=author_ids,
        kind='moderation_update',
    ).values('id', 'recipient_id', 'is_read', 'read_at', 'created_at').order_by(
        'recipient_id', '-created_at', 'id',
    )
    latest = {}
    for row in rows:
        # 同一接收人只留最新的一条：要回答的是「这一类通知最近有没有被看见」，
        # 而不是把历史通知全翻出来。
        latest.setdefault(row['recipient_id'], row)
    return latest


def _fetch_preferences(author_ids):
    """这批用户的偏好行。取不到的记 None，不当成开着。

    `get_or_create` 会现建一行默认全开，所以正常路径下每个人都有；取不到只可能来自
    并发或手工删行。当成「开着」会让触达那一节的分母虚高，所以单独归一档。
    """
    if not author_ids:
        return {}
    rows = NotificationPreference.objects.filter(
        user_id__in=author_ids,
    ).values('user_id', PREFERENCE_FIELD)
    return {row['user_id']: row for row in rows}


def _classify_reach(event, notice, preference, quiet_authors, now):
    """这条复核结论到底有没有送到作者眼前，以及没送到的话是哪种没送到。

    顺序即优先级：先看通知建没建出来。建出来了就是送到了——`create_notification` 返回
    None 的三种情况都发生在建行之前，建出来了就说明三种都没发生。建不出来的三种里，
    开关关着是用户主动选的，免打扰是有时限的，作者本人是结构上的极小概率，
    偏好行不存在是数据异常。**混成一个「未送达」，这个数就会随复核人是不是作者抖动。**
    """
    if notice:
        return 'delivered'
    if preference is None:
        return 'no_preference_row'
    if not preference[PREFERENCE_FIELD]:
        return 'switch_off'
    # 免打扰只压制徽章式提醒，通知照样落库。也就是说免打扰时段的作者其实**收到了**
    # 通知，只是没被提醒。把它算进「没收到」是错的——这里只在没有通知行、
    # 而开关明明开着的时候才回头看免打扰，用来解释这条本该发出的通知为什么没发。
    if event['author_id'] in quiet_authors:
        return 'quiet_hours'
    if event['reviewed_by_id'] and event['reviewed_by_id'] == event['author_id']:
        return 'self_authored'
    return 'switch_off'


def _fetch_quiet_authors(author_ids, now):
    """当前正落在免打扰时段里的作者。

    免打扰是**时刻相关**的：一条 40 天前复核完的记录，当时在不在免打扰时段里，
    本模块答不上来——`NotificationPreference` 上没有免打扰的历史，`quiet_hours_enabled`
    一改，过去的时段就找不回来了。所以这里只报「当下正在免打扰的人」，
    并且只把它用来解释**当前**还没发出通知的那批，不去追认历史。

    这一条是模块里最弱的一节，所以门槛卡得最死：样本不足时只报人数，不给方向。
    """
    quiet = set()
    for user_id in author_ids:
        # 免打扰逐用户查一次：它是纯 Python 判断，没有 ORM 表达方式，
        # 而作者数是审核记录数的子集，通常不大。
        if quiet_hours_active(User(id=user_id), now=now):
            quiet.add(user_id)
    return quiet


def _fetch_author_activities(author_ids, start, now):
    """这批作者在窗口内有没有三种发言行为。只报「有没有」，不报条数。

    **不报内容。** 留言和私信都是收发双方的私域内容，商品标题也不该出现在
    一张偏好看板上。三档各自独立，一个人可以同时中三档。
    """
    from chat_messages.models import Comment, PrivateMessage
    from .models import Item

    activities = {user_id: set() for user_id in author_ids}
    if not author_ids:
        return activities

    commenters = set(Comment.objects.filter(
        author_id__in=author_ids, created_at__gte=start, created_at__lte=now,
    ).values_list('author_id', flat=True))
    for user_id in commenters:
        activities.setdefault(user_id, set()).add('comment')

    messengers = set(PrivateMessage.objects.filter(
        sender_id__in=author_ids, created_at__gte=start, created_at__lte=now,
    ).values_list('sender_id', flat=True))
    for user_id in messengers:
        activities.setdefault(user_id, set()).add('message')

    publishers = set(Item.objects.filter(
        seller_id__in=author_ids, created_at__gte=start, created_at__lte=now,
    ).values_list('seller_id', flat=True))
    for user_id in publishers:
        activities.setdefault(user_id, set()).add('item')

    return activities


def _build_records(days, now):
    """周期内复核完的记录，连通知、偏好、发言行为一次取齐。"""
    events = _fetch_events(days, now)
    author_ids = {event['author_id'] for event in events if event['author_id']}
    notices = _fetch_notices(author_ids)
    preferences = _fetch_preferences(author_ids)
    quiet_authors = _fetch_quiet_authors(author_ids, now)
    start = _period_start(days, timezone.localtime(now).date())
    activities = _fetch_author_activities(author_ids, start, now)

    records = []
    for event in events:
        author_id = event['author_id']
        notice = notices.get(author_id)
        preference = preferences.get(author_id)
        records.append(EventRecord(
            event=event,
            notice=notice,
            preference=preference,
            reach=_classify_reach(event, notice, preference, quiet_authors, now),
            author_activities=activities.get(author_id, set()),
        ))
    return records


def _build_reach_rows(records):
    """触达：这条结论到底送到作者眼前了没有，没送到的话是哪种没送到。

    行按复核结论拆。确认违规和误判放行是两条情绪上完全不同的消息，用户对它们的
    打开意愿没有理由相同，把它们平均成一个送达率，波动来了还是要去查处理人。
    """
    labels = _reach_labels()
    decision_labels = _decision_labels()
    grouped = {}
    for record in records:
        decision = record.event['status']
        grouped.setdefault(decision, {band: 0 for band, _ in REACH_BANDS})
        grouped[decision][record.reach] += 1

    rows = []
    for decision in [value for value, _ in DECISION_CHOICES] + [
        key for key in grouped if key not in dict(DECISION_CHOICES)
    ]:
        counts = grouped.get(decision)
        if counts is None:
            continue
        total = sum(counts.values())
        # 作者不存在的记录（注销用户）不进任何一档：`author` 是 SET_NULL，
        # 没有作者的复核结论谈不上送到谁眼前。它只在作者缺失行里单独数。
        rows.append({
            'decision': decision,
            'label': decision_labels.get(decision, decision),
            'total_count': total,
            'delivered': counts['delivered'],
            'switch_off': counts['switch_off'],
            'quiet_hours': counts['quiet_hours'],
            'self_authored': counts['self_authored'],
            'no_preference_row': counts['no_preference_row'],
            'delivered_rate': _percent_or_none(counts['delivered'], total),
            'switch_off_rate': _percent_or_none(counts['switch_off'], total),
            'quiet_hours_rate': _percent_or_none(counts['quiet_hours'], total),
            'self_authored_rate': _percent_or_none(counts['self_authored'], total),
            'no_preference_row_rate': _percent_or_none(
                counts['no_preference_row'], total,
            ),
            'is_small_sample': 0 < total < MIN_BUCKET_SAMPLE,
        })
    rows.sort(key=lambda row: (-row['total_count'], row['decision']))
    return rows


def _build_outcome_stats(records):
    """总结局：周期内复核完多少条、其中多少条送到了作者眼前。"""
    total = len(records)
    delivered = sum(1 for record in records if record.reach == 'delivered')
    switch_off = sum(1 for record in records if record.reach == 'switch_off')
    missing_author = sum(1 for record in records if not record.event['author_id'])
    return {
        'reviewed_count': total,
        'delivered_count': delivered,
        'switch_off_count': switch_off,
        'missing_author_count': missing_author,
        'delivered_rate': _percent_or_none(delivered, total),
        'switch_off_rate': _percent_or_none(switch_off, total),
        'has_data': bool(total),
    }


def _build_preference_grid(records):
    """偏好四格：有内容被复核且开着、有内容被复核但关着、两边都没内容。

    **只报四格的人数，不报用户名、不排名，也不报任何两格之间的比值。** 关掉一个通知
    偏好是用户的私事，做成排行榜就是把设置页变成公开表态。而四格的后两格（没被复核过
    内容的人）关不关这个开关，和「收到过审核结果」没有因果关系——直接拿第二格比第四格，
    比出来的是两群不同的人，不是同一群人的两种选择。
    """
    reviewed_ids = {
        record.event['author_id'] for record in records if record.event['author_id']
    }
    grid = {key: 0 for key in (
        'reviewed_switch_on', 'reviewed_switch_off',
        'unreviewed_switch_on', 'unreviewed_switch_off',
    )}
    # 全站用户逐个看开关状态。
    #
    # **没有偏好行的用户算「开着」。** `NotificationPreference` 不是注册时建的，是用户
    # 第一次走进设置页、或者第一次收到通知时由 `get_or_create` 现建的。一个从没进过
    # 设置页的用户库里没有这一行，而 `create_notification` 对这种人照样发通知——
    # `get_or_create` 建出默认全开的一行，`getattr(preference, kind, True)` 取到 True。
    # 也就是说「没有行」和「行上写着开着」在行为上完全等价。只数库里的行，会把这群人
    # 整群漏掉，而他们恰恰是全站的大多数——漏掉之后「开关开着」两格会小到没有意义。
    switch_off_ids = set(NotificationPreference.objects.filter(
        **{PREFERENCE_FIELD: False},
    ).values_list('user_id', flat=True))
    all_ids = set(User.objects.values_list('id', flat=True))
    for user_id in all_ids:
        switch_on = user_id not in switch_off_ids
        if user_id in reviewed_ids:
            grid['reviewed_switch_on' if switch_on else 'reviewed_switch_off'] += 1
        else:
            grid['unreviewed_switch_on' if switch_on else 'unreviewed_switch_off'] += 1
    return grid


def _build_activity_contrast(records, days, now):
    """关掉的人和开着的人，各自还有没有在社区里说话。只报形状。

    这一节是全模块最容易被读歪的一节，所以口径卡得最死：报的是**同期人群的对照形状**，
    不是因果。关掉通知不会让人不说话，收到一条「你违规了」也不会。两群人在发言率上的差，
    可能只是「常发言的人更在意审核结果」。两群人都过 `MIN_PREFERENCE_USERS` 才报差值和
    方向，不过就只报两群各自的人数。

    方向只允许三档，不给百分比差——一个 3 个百分点和一个 30 个百分点在看板上长一样，
    给的指引却完全相反。
    """
    # 只有「有内容被复核」的人才进这一节：没被复核过内容的人关不关这个开关，
    # 和审核结果通知没有因果关系，混进来只会把两群都稀释掉。
    reviewed_ids = {
        record.event['author_id'] for record in records if record.event['author_id']
    }
    if not reviewed_ids:
        return {'has_data': False, 'rows': [], 'summary': '周期内没有被复核过的内容作者。'}

    # 和偏好四格同一处理：没有偏好行的用户算「开着」。他们没进过设置页，
    # 而 `create_notification` 对这种人照样发通知。只数库里的行会漏掉全站大多数。
    switch_off_ids = set(NotificationPreference.objects.filter(
        user_id__in=reviewed_ids, **{PREFERENCE_FIELD: False},
    ).values_list('user_id', flat=True))
    switch_on_ids = reviewed_ids - switch_off_ids

    start = _period_start(days, timezone.localtime(now).date())
    off_activities = _fetch_author_activities(switch_off_ids, start, now)
    on_activities = _fetch_author_activities(switch_on_ids, start, now)

    result_rows = []
    for kind, label in ACTIVITY_KINDS:
        off_count = sum(
            1 for acts in off_activities.values() if kind in acts
        )
        on_count = sum(
            1 for acts in on_activities.values() if kind in acts
        )
        off_total = len(off_activities)
        on_total = len(on_activities)
        comparable = (
            off_total >= MIN_PREFERENCE_USERS and on_total >= MIN_PREFERENCE_USERS
        )
        if comparable:
            if off_count > on_count:
                direction = 'switch_off_higher'
            elif on_count > off_count:
                direction = 'switch_on_higher'
            else:
                direction = 'same'
        else:
            # 样本不够就只报人数，不给方向：几条样本上的百分比不是方向。
            direction = None
        result_rows.append({
            'kind': kind,
            'label': label,
            'off_count': off_count,
            'off_total': off_total,
            'on_count': on_count,
            'on_total': on_total,
            'off_rate': _percent_or_none(off_count, off_total),
            'on_rate': _percent_or_none(on_count, on_total),
            'direction': direction,
            'is_comparable': comparable,
        })
    return {
        'has_data': bool(switch_off_ids and switch_on_ids),
        'rows': result_rows,
        'summary': (
            f'周期内有 {len(reviewed_ids)} 位内容作者被复核过，其中 '
            f'{len(switch_off_ids)} 位关掉了内容审核结果通知。'
        ),
        'off_total': len(switch_off_ids),
        'on_total': len(switch_on_ids),
    }


def _build_read_rows(records):
    """通知发出去之后，读没读。按复核结论拆。

    确认违规和误判放行是两条情绪上完全不同的消息：一条说你违规了，一条说我们搞错了。
    它们的已读率没有理由相同。同时报同期全部通知的整体已读率作对照——少了这个分母，
    单看一类的已读率读不出「这一类是不是特别没人看」。

    只报聚合已读率，不报任何一条通知的阅读时长分布：`notification_response` 已经报了
    全部 24 类的时效，这里重复一遍是把看板撑大，不是把看板变好。
    """
    decision_labels = _decision_labels()
    grouped = {}
    for record in records:
        if record.reach != 'delivered':
            continue
        decision = record.event['status']
        grouped.setdefault(decision, {'sent': 0, 'read': 0})
        grouped[decision]['sent'] += 1
        if record.notice and record.notice['is_read']:
            grouped[decision]['read'] += 1

    rows = []
    for decision in [value for value, _ in DECISION_CHOICES] + [
        key for key in grouped if key not in dict(DECISION_CHOICES)
    ]:
        counts = grouped.get(decision)
        if counts is None:
            continue
        rows.append({
            'decision': decision,
            'label': decision_labels.get(decision, decision),
            'sent_count': counts['sent'],
            'read_count': counts['read'],
            'unread_count': counts['sent'] - counts['read'],
            'read_rate': _percent_or_none(counts['read'], counts['sent']),
            'is_small_sample': 0 < counts['sent'] < MIN_BUCKET_SAMPLE,
        })
    rows.sort(key=lambda row: (-row['sent_count'], row['decision']))

    return rows


def _build_baseline_read_rate(days, now):
    """同期全部通知的整体已读率。作对照分母，不单独下结论。"""
    start = _period_start(days, timezone.localtime(now).date())
    sent = Notification.objects.filter(created_at__gte=start, created_at__lte=now).count()
    read = Notification.objects.filter(
        created_at__gte=start, created_at__lte=now, is_read=True,
    ).count()
    return {
        'sent_count': sent,
        'read_count': read,
        'read_rate': _percent_or_none(read, sent),
    }


def _summary(outcome, reach_rows, grid, activity, baseline):
    """一句话说清这个模块在读什么。空数据时报原因，不报 0。"""
    if not outcome['has_data']:
        return '周期内没有被复核完的内容审核记录，这一类通知的触达无从读起。'
    return (
        f"周期内复核完 {outcome['reviewed_count']} 条内容审核记录，其中 "
        f"{outcome['delivered_count']} 条的通知送到了作者眼前"
        f"（{outcome['delivered_rate']}%），{outcome['switch_off_count']} 条的作者"
        f"关掉了内容审核结果通知。"
    )


def build_moderation_notice_preference(days=30, now=None):
    """内容审核结果通知的开关、触达与沉默。只报告，不改任何一处写入路径。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    records = _build_records(days, now)
    outcome = _build_outcome_stats(records)
    reach_rows = _build_reach_rows(records)
    grid = _build_preference_grid(records)
    activity = _build_activity_contrast(records, days, now)
    read_rows = _build_read_rows(records)
    baseline = _build_baseline_read_rate(days, now)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(records),
        'summary': _summary(outcome, reach_rows, grid, activity, baseline),
        'outcome': outcome,
        'reach_rows': reach_rows,
        'preference_grid': grid,
        'activity': activity,
        'read_rows': read_rows,
        'baseline_read_rate': baseline,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'min_preference_users': MIN_PREFERENCE_USERS,
        'noise_points': 8,
    }

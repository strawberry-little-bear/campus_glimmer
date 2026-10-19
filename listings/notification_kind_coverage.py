# -*- coding: utf-8 -*-
"""通知类型、偏好开关与用户手上的那一栏——版图第一次被运营读到。

``Notification.KIND_CHOICES``、``NotificationPreference`` 上的通知类型开关和
``NotificationPreferenceForm.Meta.fields`` 是三个独立来源。它们本应对齐，或者只差
免打扰那一组；而每个表各自都是自洽的，所以差额很容易被「看起来像」吃掉。

两个方向各有一个窟窿：

**``mutual_aid_feedback`` 是一个正在被发送、却没有名字的类型。** 它在 ``views.py:7436``
（求购侧）和 ``views.py:7838``（失物招领侧）被真正发送，偏好表上有它的开关，而
``KIND_CHOICES`` 里没有它。后果不止是看板上显示英文：
``notification_list`` 的类型筛选器拿 ``KIND_CHOICES`` 当白名单——**这一类通知，用户在自己
的通知中心里筛不出来。** 它照样进来、照样进角标、照样能被点开，但类型下拉里没有它。

**``meeting_incident`` 是一个用户关不掉的开关。** 模型上有、默认开着、``views.py:1213``
和 ``views.py:1224`` 真的在发，而表单字段里没有它。模板是
``{% for field in form.visible_fields %}``，表单里没有的字段页面上就没有那一行。其余
类型的开关全在偏好页面上，用户形成的心智是「这一页能管我的通知」——一个 Silent 的例外
破坏的是这个心智本身。

本模块只报告：把这三个数并排放，让对不上的那几行自己浮出来。**不加迁移、不改
``KIND_CHOICES``、不改 ``NotificationPreference``、不改表单字段列表**——那两个窟窿的修法
各是一行代码，但那是修复，不是度量。修不修、什么时候修，是另一个决定。

三个来源（模型、``KIND_CHOICES``、表单）全部从代码现读，不硬编码名单：硬编码的名单会和
代码走散，而那正是本模块要报的问题。
"""

from datetime import timedelta

from django.utils import timezone

from .models import Notification, NotificationPreference

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一个类型的规模至少要这么多条，才允许对它下「这一类触达不足」的判断。和搜索位置质量、
# 求购匹配质量、跟进任务接手等待、延后通知回归同一个量级：几十条样本上几个百分点的差距
# 是噪声。口径与门槛全站统一，读者在一处学会「12 条」的含义后，不该在另一处重新学一个数。
MIN_BUCKET_SAMPLE = 12

# 偏好表上不属于任何通知类型的字段：免打扰开关本身，以及它配的两个时刻。它们不是「多出来
# 的窟窿」，是另一套机制——用户级免打扰时段，由 notification_snooze_return 与
# lifecycle_reminder_effect 各自覆盖，本模块不重复报。
NON_KIND_PREFERENCE_FIELDS = (
    'quiet_hours_enabled',
    'quiet_hours_start',
    'quiet_hours_end',
)

# 模块需要读的列。保持显式：365 天视图上 Notification 可能很大，只取用得上的。
PERIOD_FIELDS = (
    'kind',
    'is_read',
    'read_at',
    'created_at',
)


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    这条沿用延后通知回归模块的同名函数：两份私有工具各自可读，好过一个共享的隐藏依赖。
    """
    from datetime import time as dt_time
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _preference_switch_fields():
    """``NotificationPreference`` 上所有布尔开关的字段名。

    从模型元数据现读，不硬编码一份名单：硬编码的名单会和模型走散，而那正是本模块要报的
    问题本身。这样「模型上有哪些开关」永远对得上此刻的代码。
    """
    names = []
    for field in NotificationPreference._meta.get_fields():
        if field.is_relation:
            continue
        if field.name in NON_KIND_PREFERENCE_FIELDS:
            continue
        if field.name in ('id', 'user', 'updated_at'):
            continue
        if field.get_internal_type() != 'BooleanField':
            continue
        names.append(field.name)
    return names


def _form_switch_fields():
    """``NotificationPreferenceForm`` 实际摆出来的字段名。

    「用户手上能不能改」看的是表单，不是模型：模型上有而表单里没有的开关，用户根本看不到
    那一行，也就无从关起。这正是 ``meeting_incident`` 的处境。
    """
    from .forms import NotificationPreferenceForm
    return set(NotificationPreferenceForm.Meta.fields)


def _fetch_period_rows(days, now):
    """周期内创建的通知，按创建时间与主键显式排序。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    规模与阅读情况是回顾性的，按 ``created_at`` 落在周期内筛——和延后通知回归同一个口径。
    """
    start = _period_start(days, timezone.localtime(now).date())
    return list(
        Notification.objects.filter(created_at__gte=start, created_at__lte=now)
        .values(*PERIOD_FIELDS)
        .order_by('created_at', 'id')
    )


def _count_by_kind(rows):
    """周期内每个类型发了几条、被读了几条。

    「被读」要求 ``read_at`` 有值——``is_read=True`` 而 ``read_at IS NULL`` 的是阅读时间
    上线之前标记的老数据（延后通知回归模块已经如实报出过这一档），本模块不把它算进已读，
    也不并进未读，如实漏过去。
    """
    sent = {}
    read = {}
    for row in rows:
        kind = row['kind']
        sent[kind] = sent.get(kind, 0) + 1
        if row['is_read'] and row['read_at']:
            read[kind] = read.get(kind, 0) + 1
    return sent, read


def _build_landscape(sent_by_kind, read_by_kind):
    """类型版图：每行一个通知类型，四列二值，加上周期内的规模。

    行取自 ``KIND_CHOICES`` 与偏好开关的并集——只在一边出现而另一边没有的类型也要露出来。
    只报其中一边，就等于假设另一边是齐的，而本模块存在的理由正是这个假设不成立。
    """
    kind_labels = dict(Notification.KIND_CHOICES)
    switch_fields = set(_preference_switch_fields())
    form_fields = _form_switch_fields()

    rows = []
    for kind in sorted(set(kind_labels) | switch_fields):
        sent = sent_by_kind.get(kind, 0)
        rows.append({
            'kind': kind,
            'label': kind_labels.get(kind, kind),
            'has_choice_label': kind in kind_labels,
            'has_preference_switch': kind in switch_fields,
            'switch_in_form': kind in form_fields,
            'sent_in_period': bool(sent),
            'sent_count': sent,
            'read_count': read_by_kind.get(kind, 0),
            # 三列全是代码事实、周期内一条都没发过的类型，说不出它触达如何。
            'is_small_sample': 0 < sent < MIN_BUCKET_SAMPLE,
        })
    return rows


def _build_gap_rows(rows, *, key, sent_by_kind, read_by_kind, total_sent):
    """把版图里某一列为假的行挑出来，附上规模与阅读情况。

    两个方向共用一个函数：``key='has_choice_label'`` 挑出「没有名字」的类型，
    ``key='switch_in_form'`` 挑出「用户关不掉」的类型。两者问的不是同一件事——前者问
    「用户收到的东西在看板上叫什么」，后者问「用户能不能对自己的通知说不」——但取行的
    形状一样，报的形状也一样：规模、阅读数、占全部通知的比例。
    """
    gap = [row for row in rows if not row[key]]
    result = []
    for row in gap:
        sent = sent_by_kind.get(row['kind'], 0)
        result.append({
            'kind': row['kind'],
            'label': row['label'],
            'sent_count': sent,
            'read_count': read_by_kind.get(row['kind'], 0),
            # 空分母报 0 会变成「占比 0%」，而 0% 说的是「发了，占比很小」。这里报 None，
            # 让模板显示「—」：一条通知都没有的时候，这些数都不存在，不是零。
            'share': _percent(sent, total_sent) if total_sent else None,
            'sent_in_period': bool(sent),
            'is_small_sample': 0 < sent < MIN_BUCKET_SAMPLE,
        })
    return result


def _build_preference_rows(rows, sent_by_kind, read_by_kind):
    """周期内真的发过的类型上，偏好的实际状态。

    **只统计发过的类型。** 一个从没被发送过的类型，它的开关开着或关着都不产生任何后果，
    报出来是一堆没有行为支撑的数字。

    开关状态按**发过通知的接收人**统计，不按全站用户：一个从没收过这一类通知的用户，他的
    开关开着或关着都没有行为意义。**不按用户报、不排名、不报任何人的用户名**——「谁关了
    哪一类」是一份考勤表，不是一份看板。
    """
    recipients_by_kind = _recipients_by_kind(sent_by_kind)
    # 偏好行原来按类型逐个 `user_id__in=...` 取：周期内有几种发过的类型就查几次，
    # 而类型之间取的是同一批用户——一个人会收到好几种通知。先取并集、只查一次，
    # 再在内存里按 user_id 索引复用。查询数从「周期内有几种类型」降到 1，
    # 逐类型数出来的开关状态一个都没变。
    preference_index = {
        preference.user_id: preference
        for preference in _preferences_for(
            {user_id for ids in recipients_by_kind.values() for user_id in ids}
        )
    }
    result = []
    for row in rows:
        kind = row['kind']
        sent = sent_by_kind.get(kind, 0)
        if not sent:
            continue
        recipients = recipients_by_kind.get(kind, [])
        if not recipients:
            continue
        off = 0
        for user_id in recipients:
            preference = preference_index.get(user_id)
            if preference is not None and not getattr(preference, kind, True):
                off += 1
        result.append({
            'kind': kind,
            'label': row['label'],
            'recipient_count': len(recipients),
            'sent_count': sent,
            'read_count': read_by_kind.get(kind, 0),
            'switched_off': off,
            'switched_off_rate': _percent(off, len(recipients)),
            # 一个接收人身上几个百分点是噪声；这里只报状态，不下「这一类没人要」的结论，
            # 除非规模过门槛——而下结论的部分交给 notification_response 的 kind 榜单。
            'is_small_sample': len(recipients) < MIN_BUCKET_SAMPLE,
        })
    result.sort(key=lambda row: (-row['switched_off_rate'], row['kind']))
    return result


def _recipients_by_kind(sent_by_kind):
    """周期内每个类型的接收人是谁（只要 id，不加载用户对象）。

    偏好是用户私域。这里只取 id 用来查开关，最终面板上出现的是「多少个接收人关掉了」，
    不是「谁关掉了」。
    """
    result = {kind: set() for kind in sent_by_kind}
    if not sent_by_kind:
        return result
    # 原本一个类型一次查询：周期里有 19 种发过的类型就是 19 次往返，每次拉的都只有
    # 「kind + recipient_id」两列。改成一次分组查询把同样的行一次拉完，内存里的形状
    # 不变（还是按类型分组的 id 集合），往返从 N 次降到 1 次。
    for kind, recipient_id in Notification.objects.filter(
        kind__in=list(sent_by_kind),
    ).values_list('kind', 'recipient_id').distinct():
        result.setdefault(kind, set()).add(recipient_id)
    return result


def _preferences_for(recipient_ids):
    """一批接收人的通知偏好，按 user_id 建索引。

    没有偏好行的用户走默认值（全开）——``create_notification`` 第一件事就是
    ``get_or_create``，所以「没有行」等价于「没碰过，全开」。
    """
    if not recipient_ids:
        return []
    return list(
        NotificationPreference.objects.filter(user_id__in=recipient_ids)
    )


def _build_current(rows, sent_by_kind, read_by_kind):
    """此刻的三类合计，加上周期总规模。

    版图对齐情况是关于代码的，不按周期过滤；这里报的规模是回顾性的，按 ``created_at``
    落在周期内筛——和延后通知回归同一个口径。
    """
    total_sent = sum(sent_by_kind.values())
    unnamed = sum(1 for row in rows if not row['has_choice_label'])
    unreachable = sum(1 for row in rows if not row['switch_in_form'])
    return {
        'kind_total': len(rows),
        'total_sent': total_sent,
        'total_read': sum(read_by_kind.values()),
        'unnamed_count': unnamed,
        'unreachable_count': unreachable,
        'has_unnamed': bool(unnamed),
        'has_unreachable': bool(unreachable),
    }


def _summary(current, unnamed_rows, unreachable_rows):
    """一段人话，说明版图此刻齐不齐。"""
    if not current['total_sent']:
        return (
            '当前周期还没有通知数据。类型版图是代码事实，随时可查；'
            '但「哪一类触达不足」需要先有通知发生。'
        )
    parts = [
        f"周期内共 {current['total_sent']} 条通知，覆盖 {current['kind_total']} 种已登记的类型。"
    ]
    if current['has_unnamed']:
        labels = '、'.join(row['label'] for row in unnamed_rows)
        parts.append(
            f"有 {current['unnamed_count']} 种正在发送的类型没有中文名（{labels}）——"
            '用户在自己的通知中心里按类型筛不出它们。'
        )
    if current['has_unreachable']:
        labels = '、'.join(row['label'] for row in unreachable_rows)
        parts.append(
            f"有 {current['unreachable_count']} 种类型的开关不在偏好页面上（{labels}）——"
            '用户收到了，但没有一个开关能关掉它。'
        )
    if not current['has_unnamed'] and not current['has_unreachable']:
        parts.append('三种表此刻是对齐的：每个类型都有名字、有开关、开关都在用户手上。')
    return ''.join(parts)


def build_notification_kind_coverage(days=30, now=None):
    """统计通知类型版图的三张表对齐情况，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    rows = _fetch_period_rows(days, now)
    sent_by_kind, read_by_kind = _count_by_kind(rows)

    landscape = _build_landscape(sent_by_kind, read_by_kind)
    unnamed_rows = _build_gap_rows(
        landscape, key='has_choice_label',
        sent_by_kind=sent_by_kind, read_by_kind=read_by_kind,
        total_sent=sum(sent_by_kind.values()),
    )
    unreachable_rows = _build_gap_rows(
        landscape, key='switch_in_form',
        sent_by_kind=sent_by_kind, read_by_kind=read_by_kind,
        total_sent=sum(sent_by_kind.values()),
    )
    preference_rows = _build_preference_rows(landscape, sent_by_kind, read_by_kind)
    current = _build_current(landscape, sent_by_kind, read_by_kind)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(rows),
        'summary': _summary(current, unnamed_rows, unreachable_rows),
        'landscape': landscape,
        'unnamed_rows': unnamed_rows,
        'unreachable_rows': unreachable_rows,
        'preference_rows': preference_rows,
        'current': current,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
    }

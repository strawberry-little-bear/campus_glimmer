# -*- coding: utf-8 -*-
"""延后的通知，延了之后回来了吗——「现在不方便」第一次被运营读到。

``Notification.snoozed_until`` 从通知中心上线那天起就在表上：用户点一下「延后 2 小时 /
24 小时 / 3 天」，这条通知就从顶部未读提醒里消失，到期后自己回来。这条链路上已经有度量——
``notification_response.py`` 读 ``read_at``，报每类通知的已读率和处理时长；但它的
``PERIOD_FIELDS`` 里没有 ``snoozed_until``，它的 ``response_seconds`` 从上线到现在都是同一个
式子：``read_at - created_at``。

于是一条 3 月 1 日创建、当天被延后 3 天、3 月 4 日到期当天被读的通知，处理时长读出来是
3 天——而用户实际的动作是「到期当天就处理了」。这 3 天不是平台慢，是用户自己说「现在不方便」。
把它记在处理时长的账上，会让一类通知显得难处理，而真相可能只是这类通知总在不方便的时刻到来，
延后功能工作得很好。

**延后把「处理慢」拆成了两半。** 拆开之后是一个等式：::

    read_at - snoozed_until == (read_at - created_at) - (snoozed_until - created_at)
       扣掉延期的处理时长        含延期的处理时长            用户主动要求的等待

左边那一项正是「这条通知到期之后，隔了多久被读」。它不是 ``notification_response`` 的竞争口径，
是它的一个分量：中间那一段是用户自己安排的等待，剩下的才是平台该负责的部分。

**延后和未读是两种完全不同的沉默。** 未读是提醒没被看见；延后是提醒被看见了、被明确地推到
以后。把延后并进未读，未读率会虚高——一批用户已经明确处置过的事项挂在角标上；把延后当成已
处理，又会漏掉一批真的被搁置的事：到期之后它回来了，而它可能永远不再被读。所以本轮的核心问题
是**延后到点之后回来了吗**：``actionable_unread_q`` 的条件是
``snoozed_until__isnull=True OR snoozed_until__lte=now``，到点的通知重新计入未读提醒，它后来
被读了吗？从到点算起隔了多久？至今没读的有多少？

只报告。不加迁移、不加字段、不改写入路径、不自动取消延后、不自动标记已读、不按用户排名。
"""

import math
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import Notification

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一组至少要这么多条，才允许对它下「这一组读得更少」的判断。和搜索位置质量、求购匹配质量、
# 跟进任务接手等待同一个量级：几十条样本上几个百分点的差距是噪声。
MIN_BUCKET_SAMPLE = 12

# 两组的阅读率相差小于这么多百分点视为噪声。百分点尺度的噪声线，和搜索位置质量、免费领取
# 申请队列同一个数：一张看板上多个面板并排，读者在一处学会「8 个百分点」的含义后，不该在另一处
# 重新学一个数。
NOISE_POINTS = 8

# 时长分档的噪声线。时长的尺度是天，报「中位相差 1.4 天」是假装精确。
NOISE_DAYS = 3

# 从到期到被读的分档。按天切而不是按小时：延后的档位最短是 2 小时、最长是 3 天，
# 回来之后隔多久被读的时间尺度是天，报「隔了 37 小时」对一个按周看板的运营没有意义。
RETURN_LAG_BUCKETS = (
    (1, '当天'),
    (4, '1-3 天'),
    (8, '4-7 天'),
    (31, '8-30 天'),
    (None, '30 天以上'),
)

# 延后档位本身。写入路径只给三档，这里报的是「用户实际选了几档里的哪一档」。
SNOOZE_DURATION_BUCKETS = (
    (timedelta(hours=6), '2 小时档'),
    (timedelta(hours=30), '24 小时档'),
    (None, '3 天档'),
)

# 五种结局，加上「已读但没有阅读时刻」这一行数据窟窿。顺序就是看板上的顺序。
OUTCOMES = (
    ('still_snoozed', '还在延后中'),
    ('read_while_snoozed', '延后期间就被读了'),
    ('read_after_return', '到点之后才被读'),
    ('unread_after_return', '到点之后仍未读'),
    ('read_without_timestamp', '已读但没有阅读时刻'),
)

OUTCOME_LABELS = dict(OUTCOMES)

# 模块需要读的列。保持显式：365 天视图上 Notification 可能很大，只取用得上的。
PERIOD_FIELDS = (
    'kind',
    'is_read',
    'read_at',
    'snoozed_until',
    'created_at',
)


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _bucket_label(days):
    """把一段天数归进分档。当天记 0 天，负数（时钟回拨）也落进「当天」。

    档位的上限是排它的：隔了 0.4 天仍然是当天，隔满 1 天才进下一档。第一档写成
    `days < 1` 而不是 `days <= 0`，否则「当天」这一档只收得下整 0 天，一条上午到期、
    下午被读的通知会被算成「1-3 天」——而它明明是同一天回来的。
    """
    if days is None:
        return None
    for limit, label in RETURN_LAG_BUCKETS:
        if limit is None or days < limit:
            return label
    return RETURN_LAG_BUCKETS[-1][1]


def _duration_label(delta):
    """延后了多久：按写入路径实际给的三档归，不按连续值切。"""
    if delta is None:
        return None
    for limit, label in SNOOZE_DURATION_BUCKETS:
        if limit is None or delta < limit:
            return label
    return SNOOZE_DURATION_BUCKETS[-1][1]


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


def _format_duration(days):
    """把天数渲染成最粗的可读单位。None 报「—」，不报 0 天。"""
    if days is None:
        return '—'
    if days < 1:
        hours = round(days * 24)
        return f'{hours} 小时'
    return f'{days} 天'


def _period_start(days, today):
    '''统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    '''
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _snoozed_delta(row):
    """用户主动要求的等待：``snoozed_until - created_at``。

    只对被延后过的行有意义。返回 ``None`` 表示这一行从来没有被延后，或者延后时刻早于创建
    时刻（时钟回拨），那不是「负的等待」，是数据问题。
    """
    snoozed_until = row['snoozed_until']
    created_at = row['created_at']
    if not snoozed_until or not created_at:
        return None
    delta = snoozed_until - created_at
    if delta.total_seconds() < 0:
        return None
    return delta


def _outcome_of(row, now):
    """这一行被延后过的通知，现在落在五种结局里的哪一种。

    顺序有讲究：先看有没有阅读时刻，再看它和 ``snoozed_until`` 的先后。

    **「延后期间就被读了」这一组混着两种来源**，本模块如实报它的规模但不对它下结论。一种来源
    是用户点单条「标记已读」（``mark_read``，语义干净）；另一种是一键全部已读 / 批量勾选已读——
    ``Notification.objects.filter(...).update(is_read=True, read_at=now)``，**它不清
    ``snoozed_until``**。于是一条还在延后中的通知被一键已读，``read_at`` 有了值而到期时刻还在
    未来，它落进的是这一档，而用户根本没有提前回来处理，他只是按了「全部已读」。反过来，
    到期之后被一键已读的那条，落进的是「到点之后才被读」。所以批量标记会让两档都变浑：这一档
    里混着一键清空的人，那一档里也混着没真读的人。把两种来源分开需要一条写入路径，本轮不加。
    """
    if row['is_read']:
        read_at = row['read_at']
        if not read_at:
            return 'read_without_timestamp'
        if row['snoozed_until'] and read_at <= row['snoozed_until']:
            return 'read_while_snoozed'
        return 'read_after_return'
    if row['snoozed_until'] and row['snoozed_until'] > now:
        return 'still_snoozed'
    return 'unread_after_return'


def _return_lag_days(row):
    """从到期到被读隔了几天：``read_at - snoozed_until``。

    只对「到点之后才被读」的行有意义。延后期间就被读的行，到期时刻还没到，谈不上「回来」。
    """
    read_at = row['read_at']
    snoozed_until = row['snoozed_until']
    if not read_at or not snoozed_until:
        return None
    return (read_at - snoozed_until).total_seconds() / 86400.0


def _fetch_period_rows(days, now):
    '''周期内创建的通知，按创建时间与主键显式排序。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    '''
    start = _period_start(days, timezone.localtime(now).date())
    return list(
        Notification.objects.filter(created_at__gte=start, created_at__lte=now)
        .values(*PERIOD_FIELDS)
        .order_by('created_at', 'id')
    )


def _fetch_current_rows(now):
    '''当前这一刻被延后过的未读通知，不按统计周期过滤。

    「此刻还有多少通知被延后着」「此刻有多少到点未读」是关于现在的。一个三个月前到期、至今
    没人读的通知，恰恰是最不该被藏起来的一件——这条口径沿用跟进任务停留时长和治理队列待处理
    清单的既有做法。
    '''
    return list(
        Notification.objects.filter(is_read=False, snoozed_until__isnull=False)
        .values(*PERIOD_FIELDS)
        .order_by('snoozed_until', 'id')
    )


def _build_totals(rows, now):
    """规模与六种结局：有多少通知被延后过，它们现在都在哪里。"""
    snoozed = [row for row in rows if row['snoozed_until']]
    counts = {key: 0 for key, _label in OUTCOMES}
    for row in snoozed:
        counts[_outcome_of(row, now)] += 1

    duration_counts = {}
    for row in snoozed:
        label = _duration_label(_snoozed_delta(row))
        if label is not None:
            duration_counts[label] = duration_counts.get(label, 0) + 1
    duration_order = [label for _limit, label in SNOOZE_DURATION_BUCKETS]

    outcome_rows = []
    for key, label in OUTCOMES:
        outcome_rows.append({
            'key': key,
            'label': label,
            'count': counts[key],
            'share': _percent(counts[key], len(snoozed)),
        })

    return {
        'sent_total': len(rows),
        # 空周期报 None 不报 0：0 说的是「通知来了，一条都没被延后」，None 说的是「没有通知」。
        # 两者在看板上长得一样，意思正相反。snoozed_rate 同理，空分母报 0 会变成「延后率 0%」。
        'snoozed_total': len(snoozed) if rows else None,
        'snoozed_rate': _percent(len(snoozed), len(rows)) if rows else None,
        'outcome_rows': outcome_rows,
        'outcome_counts': counts,
        'still_snoozed_count': counts['still_snoozed'],
        'read_while_snoozed_count': counts['read_while_snoozed'],
        'read_after_return_count': counts['read_after_return'],
        'unread_after_return_count': counts['unread_after_return'],
        'read_without_timestamp_count': counts['read_without_timestamp'],
        'duration_rows': [
            {'label': label, 'count': duration_counts.get(label, 0)}
            for label in duration_order
        ],
    }


def _build_return_lag(rows, now):
    """回来之后多久被读：分档、中位数与 P90，至今没读的是右删失样本。

    右删失样本单独数、不丢不混——和会面出席、交接码使用、归还握手三个模块守的是同一条线。
    一条到期未读的通知，它隔多久会被读还没发生，把它记成「0 天」会让回来之后显得立刻被处理。
    """
    snoozed = [row for row in rows if row['snoozed_until']]
    read_after = [
        row for row in snoozed if _outcome_of(row, now) == 'read_after_return'
    ]
    lags = []
    for row in read_after:
        days = _return_lag_days(row)
        if days is not None and days >= 0:
            lags.append(round(days, 1))

    buckets = {label: {'label': label, 'count': 0} for _limit, label in RETURN_LAG_BUCKETS}
    for days in lags:
        label = _bucket_label(days)
        if label is not None:
            buckets[label]['count'] += 1
    order = [label for _limit, label in RETURN_LAG_BUCKETS]

    median_days = _median(lags)
    return {
        'read_after_return_count': len(read_after),
        'unread_after_return_count': sum(
            1 for row in snoozed if _outcome_of(row, now) == 'unread_after_return'
        ),
        'median_days': median_days,
        'median_label': _format_duration(median_days),
        'p90_days': _percentile(lags, 0.9),
        'p90_label': _format_duration(_percentile(lags, 0.9)),
        'fastest_days': min(lags) if lags else None,
        'slowest_days': max(lags) if lags else None,
        'bucket_rows': [buckets[label] for label in order if label in buckets],
    }


def _percentile(values, fraction):
    """最近秩分位数：取某次真实发生过的值，不取插值。

    插值 P90 会落在第九次和第十次之间，那是一次从未发生过的等待。
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(math.ceil(fraction * len(ordered))) - 1, len(ordered) - 1)
    index = max(index, 0)
    return round(ordered[index], 1)


def _build_decomposed(rows, now):
    """把延后期从处理时长里拆出来：三个数摆在一起，谁也不加谁。

    含延期的是 ``notification_response`` 的口径，扣掉延期的是本模块的口径，差是用户主动要求的
    等待。**差是下界**：``snoozed_until`` 每次延后都被覆盖，一条被延后三次的通知库里只留得下
    第三次的时刻，前两次延掉的等待不在里面。三个数各有各的可信度，摆在一起看，不相加、不相减。
    """
    snoozed = [row for row in rows if row['snoozed_until']]
    read_rows = [
        row for row in snoozed
        if row['is_read'] and row['read_at'] and row['created_at']
    ]

    with_snooze = []
    without_snooze = []
    requested = []
    for row in read_rows:
        raw = (row['read_at'] - row['created_at']).total_seconds() / 86400.0
        with_snooze.append(round(max(raw, 0.0), 1))
        lag = _return_lag_days(row)
        if lag is not None:
            without_snooze.append(round(max(lag, 0.0), 1))
        delta = _snoozed_delta(row)
        if delta is not None:
            requested.append(round(delta.total_seconds() / 86400.0, 1))

    with_median = _median(with_snooze)
    without_median = _median(without_snooze)
    requested_median = _median(requested)
    gap = None
    if with_median is not None and without_median is not None:
        gap = round(with_median - without_median, 1)
    return {
        'read_total': len(read_rows),
        'with_snooze_median_days': with_median,
        'with_snooze_label': _format_duration(with_median),
        'without_snooze_median_days': without_median,
        'without_snooze_label': _format_duration(without_median),
        'requested_median_days': requested_median,
        'requested_label': _format_duration(requested_median),
        'gap_days': gap,
        'gap_label': _format_duration(gap),
        'has_both_sides': with_median is not None and without_median is not None,
    }


def _build_contrast(rows, now):
    """延后过 vs 从没延后过的阅读率对照。只做对照，不给因果。

    延后过的一组只收「已经有结论」的：已读，或到期未读。还在延后中的不计入分母——一条还没到
    期的通知说不出它会不会被读。从没延后过的一组全部计入。

    **只做对照不给因果**：愿意延后的用户本来就是更谨慎的那批，这个差值描述的是人，不是通知。
    两组都要过样本门槛，差值在噪声带内不给方向。
    """
    snoozed = [row for row in rows if row['snoozed_until']]
    plain = [row for row in rows if not row['snoozed_until']]

    snoozed_concluded = [
        row for row in snoozed if _outcome_of(row, now) != 'still_snoozed'
    ]
    snoozed_read = sum(1 for row in snoozed_concluded if row['is_read'])
    plain_read = sum(1 for row in plain if row['is_read'])

    snoozed_rate = _percent(snoozed_read, len(snoozed_concluded))
    plain_rate = _percent(plain_read, len(plain))
    gap = None
    if snoozed_concluded and plain:
        gap = round(snoozed_rate - plain_rate, 1)

    both_big_enough = bool(
        len(snoozed_concluded) >= MIN_BUCKET_SAMPLE and len(plain) >= MIN_BUCKET_SAMPLE
    )
    return {
        'snoozed_total': len(snoozed_concluded),
        'snoozed_read': snoozed_read,
        'snoozed_rate': snoozed_rate,
        'plain_total': len(plain),
        'plain_read': plain_read,
        'plain_rate': plain_rate,
        'gap_points': gap,
        'is_measurable': both_big_enough and gap is not None,
        'gap_is_meaningful': bool(
            both_big_enough and gap is not None and abs(gap) >= NOISE_POINTS
        ),
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'noise_points': NOISE_POINTS,
    }


def _build_kind_rows(rows, now):
    """按通知类型拆：延后规模、到点未读、扣掉延期的中位时长。

    通知的标题和内容是收发双方的私域内容，运营看板是全站运营读的——把一条通知的标题搬上看板，
    等于把两个用户之间的一句话搬到第三方眼前。所以按类型聚合，不列清单。
    """
    kind_labels = dict(Notification.KIND_CHOICES)
    grouped = {}
    for row in rows:
        grouped.setdefault(row['kind'], []).append(row)

    result = []
    for kind, kind_rows in grouped.items():
        snoozed = [row for row in kind_rows if row['snoozed_until']]
        if not snoozed:
            continue
        outcomes = {}
        for row in snoozed:
            key = _outcome_of(row, now)
            outcomes[key] = outcomes.get(key, 0) + 1
        lags = []
        for row in snoozed:
            if _outcome_of(row, now) != 'read_after_return':
                continue
            days = _return_lag_days(row)
            if days is not None and days >= 0:
                lags.append(round(days, 1))
        median_days = _median(lags)
        result.append({
            'kind': kind,
            'label': kind_labels.get(kind, kind),
            'sent': len(kind_rows),
            'snoozed': len(snoozed),
            'snoozed_rate': _percent(len(snoozed), len(kind_rows)),
            'unread_after_return': outcomes.get('unread_after_return', 0),
            'still_snoozed': outcomes.get('still_snoozed', 0),
            'median_days': median_days,
            'median_label': _format_duration(median_days),
            'is_small_sample': len(snoozed) < MIN_BUCKET_SAMPLE,
        })
    # 到点未读多的排在前面，名次相同按通知类型，不靠数据库返回顺序。
    result.sort(key=lambda row: (-row['unread_after_return'], row['kind']))
    return result


def _build_current(rows, now):
    """此刻：还有多少通知被延后着、多少到点未读、最久的等了多久。

    不按统计周期过滤——一个三个月前到期、至今没人读的通知，恰恰是最不该被藏起来的一件。
    只报聚合，不列清单：同上，通知内容是收发双方的私域内容。
    """
    still_snoozed = [row for row in rows if row['snoozed_until'] > now]
    overdue = [row for row in rows if row['snoozed_until'] <= now]

    def _age_days(row):
        return max((now - row['snoozed_until']).total_seconds() / 86400.0, 0.0)

    overdue_ages = [round(_age_days(row), 1) for row in overdue]
    kind_counts = {}
    for row in overdue:
        kind_counts[row['kind']] = kind_counts.get(row['kind'], 0) + 1
    kind_labels = dict(Notification.KIND_CHOICES)
    top_kinds = sorted(kind_counts.items(), key=lambda pair: (-pair[1], pair[0]))

    return {
        'still_snoozed_count': len(still_snoozed),
        'overdue_count': len(overdue),
        'overdue_median_days': _median(overdue_ages),
        'overdue_median_label': _format_duration(_median(overdue_ages)),
        'overdue_max_days': max(overdue_ages) if overdue_ages else None,
        'overdue_kind_rows': [
            {'label': kind_labels.get(kind, kind), 'count': count}
            for kind, count in top_kinds
        ],
    }


def _summary(totals, return_lag, decomposed, contrast, current):
    """一段人话，说明这半边通知现在在哪里。"""
    if not totals['sent_total']:
        return '当前周期还没有通知数据，延后与回来需要先有通知存在。'
    if not totals['snoozed_total']:
        return (
            f"周期内共 {totals['sent_total']} 条通知，没有一条被延后过——"
            '用户暂时没有需要「现在不方便」的事项。'
        )
    parts = [
        f"周期内 {totals['sent_total']} 条通知里有 {totals['snoozed_total']} 条被延后过"
        f"（{totals['snoozed_rate']}%）。"
    ]
    if return_lag['read_after_return_count']:
        parts.append(
            f"到点之后有 {return_lag['read_after_return_count']} 条被读了，"
            f"从到期算起中位隔了 {return_lag['median_label']}。"
        )
    if return_lag['unread_after_return_count']:
        parts.append(
            f"到点之后仍未读的有 {return_lag['unread_after_return_count']} 条。"
        )
    if current['overdue_count']:
        parts.append(
            f"此刻全站有 {current['overdue_count']} 条到期未读，"
            f"最久的已经等了 {current['overdue_max_days']} 天。"
        )
    if decomposed['has_both_sides'] and decomposed['gap_days'] is not None:
        parts.append(
            f"延后过的通知，含延期的处理时长中位数 {decomposed['with_snooze_label']}，"
            f"扣掉延期只剩 {decomposed['without_snooze_label']}。"
        )
    if contrast['gap_is_meaningful']:
        direction = '更低' if contrast['gap_points'] < 0 else '更高'
        parts.append(
            f"延后过的一组阅读率比从没延后过的一组{direction} "
            f"{abs(contrast['gap_points'])} 个百分点——这是人的差别，不是通知的差别。"
        )
    elif contrast['is_measurable']:
        parts.append('两组的阅读率相差在噪声范围内，读不出方向。')
    if totals['read_without_timestamp_count']:
        parts.append(
            f"另有 {totals['read_without_timestamp_count']} 条已读但没有阅读时刻，"
            '是阅读时间上线之前标记的老数据。'
        )
    return ''.join(parts)


def build_notification_snooze_return(days=30, now=None):
    '''统计延后过的通知去了哪里、回来之后多久被读，只报告不改任何字段。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    rows = _fetch_period_rows(days, now)
    totals = _build_totals(rows, now)
    return_lag = _build_return_lag(rows, now)
    # 当前状态在整个表上算，不按统计周期过滤：一个周期开始之前就到期未读的通知，
    # 恰恰是最不该被藏起来的一件。规模与结局仍然只统计期内创建的通知——它们是回顾性的，
    # 混进更早的通知会让周期失真。
    current_rows = _fetch_current_rows(now)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(rows),
        'summary': _summary(
            totals, return_lag, _build_decomposed(rows, now),
            _build_contrast(rows, now), _build_current(current_rows, now),
        ),
        'totals': totals,
        'return_lag': return_lag,
        'decomposed': _build_decomposed(rows, now),
        'contrast': _build_contrast(rows, now),
        'kind_rows': _build_kind_rows(rows, now),
        'current': _build_current(current_rows, now),
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'noise_points': NOISE_POINTS,
        'noise_days': NOISE_DAYS,
    }

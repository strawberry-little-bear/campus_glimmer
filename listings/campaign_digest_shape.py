# -*- coding: utf-8 -*-
"""专题摘要的形状：从加入到订阅、缓冲池的三种结局，和送达的相对节奏。

``CampaignRegistration.notify_updates`` 与 ``update_frequency`` 是用户在专题页
亲手拨的两个开关；``CampaignUpdateMatch`` 上的 ``matched_at`` / ``notified_at`` /
``cancelled_at`` 是这两个开关驱动出来的缓冲池。写入路径全在 ``campaign_updates.py``
里：``queue_campaign_update_matches`` 入队，``send_campaign_update_digest`` 送达或
取消，``set_campaign_update_preference`` 在关闭订阅时批量取消。

而读者只有 ``campaign_analytics`` 汇总里的三个原始计数：当前摘要订阅、待发送命中、
周期摘要。三个数回答不了运营真正要问的事：

- **有多少人真的开着摘要。** 加入专题和订阅摘要是两次独立同意，报名数永远大于
  订阅数。这两个数之间的落差、每日与每周各自的落差，此前没有任何一张表读过。
- **缓冲池的条目都去了哪里。** 入队之后只有三种结局：送达、取消、还挂着。取消
  一栏此前完全没有读者——退出订阅、专题结束、商品不可投递都会写 ``cancelled_at``，
  这些条目一条都没被数过。
- **摘要到底快不快。** 每日摘要的周期是一天，每周是七天。缓冲三天的条目在每周
  那一档是提前送达，在每日那一档是拖了三倍。绝对等待时长分不清这件事，必须除以
  各自频率的周期。

本模块只报告。不加迁移、不改频率选项、不改发送边界、不取消任何命中、不补发任何
摘要、不按频率做任何自动处置。摘要阅读率已由 ``campaign_analytics`` 报，本模块
不重复：那边报的是触达，这里报的是节奏。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .borrow_risk import _rate
from .campaign_updates import _eligible_item
from .models import CampaignRegistration, CampaignUpdateMatch, NotificationPreference
from .notifications import quiet_hours_for_users


# 本模块按周期过滤，取值集合与看板下拉一致。
DEFAULT_DAYS = 30
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 频率的展示顺序与标签。顺序固定为「先每日后每周」：读表时从上到下就是周期从短
# 到长，和第三节的档位方向一致。
FREQUENCY_LABELS = {'daily': '每日摘要', 'weekly': '每周摘要'}
FREQUENCY_ORDER = ('daily', 'weekly')

# 各自频率的周期长度，单位天。每日摘要的周期是本地日期，每周摘要的周期是 ISO 年
# 与周，见 ``campaign_updates._digest_period``。这里只借它的长度，不重新定义周期键。
DIGEST_CYCLE_DAYS = {'daily': 1, 'weekly': 7}

# 节奏分档：等待时长 ÷ 该频率自己的周期。互斥、左闭右开、全列含 0 样本档。
# (上限倍数, 标签, 这一档说明什么)
RHYTHM_BANDS = (
    (1.0, '周期内', '在自己那一档的周期里送达'),
    (2.0, '一到两倍', '超过一个周期，但还在两个周期以内'),
    (4.0, '两到四倍', '已经拖到两个周期以上'),
    (None, '超过四倍', '拖过四个周期，这一档的合并已经没有节奏可言'),
)

# 频率取值不在两个选项之内（历史数据或直接写库）时的统一标签。正常写入只会落在
# 已知取值上；表外值单独成行，不静默并进任何一档。
OTHER_FREQUENCY_LABEL = '其它'

# 一档的样本下限。和其余模块同一个量级：几十条上几个百分点的差距是噪声。
MIN_SAMPLE = 12

# 百分点差值不满此值视为噪声，不给方向。全站同一套。
NOISE_POINTS = 8


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime``
    类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_seconds(seconds):
    """等待时长按秒、分钟、小时、天说话。

    这个模块报的是一天到一周尺度的量。报「平均等待 259200 秒」会让运营以为这是
    秒级流程，而真正要动作的对象是「这一档的合并节奏还在不在」。
    """
    if seconds is None:
        return '—'
    seconds = int(round(seconds))
    if seconds < 60:
        return '%d 秒' % seconds
    if seconds < 3600:
        return '%d 分钟' % (seconds // 60)
    if seconds < 86400:
        return '%d 小时' % (seconds // 3600)
    return '%d 天' % (seconds // 86400)


def _format_wait(seconds):
    """等待时长按最贴近的那个单位说话，比 ``_format_seconds`` 多留一位小数。

    第三节报的是中位和 P90，一个 2.5 天和一个 3 天在运营眼里是两件事：四舍五入到
    整数会把「这一档的中位数已经贴着一个周期」这件事磨掉。一天的尺度上多一位小数
   不会让表变难读，少一位小数会让两个不同的档位看起来一样。
    """
    if seconds is None:
        return '—'
    seconds = float(seconds)
    if seconds < 60:
        return '%d 秒' % int(round(seconds))
    if seconds < 3600:
        minutes = seconds / 60
        return ('%d 分钟' if minutes >= 10 else '%.1f 分钟') % (
            round(minutes) if minutes >= 10 else minutes,
        )
    if seconds < 86400:
        hours = seconds / 3600
        return ('%d 小时' if hours >= 10 else '%.1f 小时') % (
            round(hours) if hours >= 10 else hours,
        )
    days = seconds / 86400
    return ('%d 天' if days >= 10 else '%.1f 天') % (
        round(days) if days >= 10 else days,
    )


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _band_index(ratio, bands):
    """倍数落在哪一档。闭区间右端：恰好等于一个周期算「周期内」。

    踩着周期线送达的那一条是准点的，把它推到下一档会让「周期内」这一列系统性
    少算一条，而那一列正是第三节唯一要报的数。
    """
    for index, (limit, _label, _hint) in enumerate(bands):
        if limit is None or ratio <= limit:
            return index
    return len(bands) - 1


def _frequency_label(frequency):
    return FREQUENCY_LABELS.get(frequency, OTHER_FREQUENCY_LABEL)


def _frequency_keys(grouped):
    """已知频率按固定顺序在前，表外取值按字典序在后。

    顺序固定是有意义的：两张表隔着一屏，频率行的次序变了就等于换了一张表。
    """
    known = [key for key in FREQUENCY_ORDER if key in grouped]
    outside = sorted(key for key in grouped if key not in FREQUENCY_ORDER)
    return known + outside


def _fetch_registrations():
    """全部报名行，只要用得到的几列。

    一次查完固定下来：四张表各读一遍、按报名现算会让每条报名打好几遍库，而看板
    是一次打开全算的。已退出的报名也取——第一节要把它们单列在分母之外。
    """
    return list(CampaignRegistration.objects.values(
        'id', 'user_id', 'is_active', 'notify_updates', 'update_frequency',
    ))


def _fetch_window_matches(*, after, before):
    """窗口内入队的命中。

    窗口按 ``matched_at`` 切，不按 ``notified_at`` 切：要数的是「这段时间里入队
    的条目分别去了哪里」，而「什么时候送到」是第三节的事。

    频率取报名行**当前**的值，不是入队那一刻的值：``CampaignUpdateMatch`` 自己
    没有频率字段，一个把每日改成每周的用户会把老条目带到每周那一行去。这是数据
    模型决定的，面板上必须写明，不能假装它是入队时的频率。
    """
    return list(CampaignUpdateMatch.objects.filter(
        matched_at__gte=after, matched_at__lte=before,
    ).values(
        'matched_at', 'notified_at', 'cancelled_at',
        'registration__update_frequency',
    ))


def _fetch_pending_matches():
    """此刻仍未送达、也未取消的全部命中，带上判定可投递需要的关系。"""
    return list(CampaignUpdateMatch.objects.filter(
        notified_at__isnull=True, cancelled_at__isnull=True,
    ).select_related('registration', 'registration__campaign', 'item'))


def _is_deliverable(match, *, now):
    """这条挂着的命中此刻还能不能进下一轮摘要。

    复用发送端自己的边界函数，不另写一份条件：两份条件一旦漂移，这个数就会从
    「还能送」悄悄变成「送不出去」。
    """
    registration = match.registration
    return bool(
        registration.is_active
        and registration.notify_updates
        and registration.campaign.is_live(now)
        and _eligible_item(match.item, registration.campaign, now=now)
    )


def _subscription_rows(registrations, *, now):
    """第一节：从加入到订阅，只看此刻的快照，不按窗口切。"""
    active = [row for row in registrations if row['is_active']]
    left = [row for row in registrations if not row['is_active']]
    subscribed = [row for row in active if row['notify_updates']]
    subscribed_user_ids = {row['user_id'] for row in subscribed}

    # 订阅开着但全局通知偏好关着：发送端第一道门就在这里拦下，命中会一直挂着。
    preference_enabled = {}
    if subscribed_user_ids:
        preference_enabled = dict(NotificationPreference.objects.filter(
            user_id__in=subscribed_user_ids,
        ).values_list('user_id', 'campaign_update'))
    preference_off_ids = {
        user_id for user_id, enabled in preference_enabled.items() if not enabled
    }
    # 没有偏好行的用户发送端会 get_or_create 出一行默认开启的，所以照样送达；
    # 单独数出来，不并进「偏好关着」。
    no_preference_ids = subscribed_user_ids - set(preference_enabled)
    quiet_ids = quiet_hours_for_users(subscribed_user_ids, now=now)

    grouped = {}
    for row in active:
        grouped.setdefault(row['update_frequency'], []).append(row)

    frequency_rows = []
    for key in _frequency_keys(grouped):
        group = grouped[key]
        group_subscribed = [row for row in group if row['notify_updates']]
        frequency_rows.append({
            'key': key,
            'label': _frequency_label(key),
            'cycle_days': DIGEST_CYCLE_DAYS.get(key),
            'registration_count': len(group),
            'subscribed_count': len(group_subscribed),
            'subscribe_rate': _rate(len(group_subscribed), len(group)),
            'share_of_active': _rate(len(group), len(active)),
            'preference_off_count': sum(
                1 for row in group_subscribed
                if row['user_id'] in preference_off_ids
            ),
            'no_preference_count': sum(
                1 for row in group_subscribed
                if row['user_id'] in no_preference_ids
            ),
            'quiet_count': sum(
                1 for row in group_subscribed if row['user_id'] in quiet_ids
            ),
            'is_small_sample': len(group) < MIN_SAMPLE,
            'is_other': key not in FREQUENCY_ORDER,
        })

    return {
        'registration_count': len(registrations),
        'active_count': len(active),
        'left_count': len(left),
        'subscribed_count': len(subscribed),
        'subscribe_rate': _rate(len(subscribed), len(active)),
        'subscribed_preference_off_count': sum(
            1 for row in subscribed if row['user_id'] in preference_off_ids
        ),
        'subscribed_no_preference_count': sum(
            1 for row in subscribed if row['user_id'] in no_preference_ids
        ),
        'subscribed_quiet_count': sum(
            1 for row in subscribed if row['user_id'] in quiet_ids
        ),
        'frequency_rows': frequency_rows,
    }


def _outcome_rows(matches):
    """第二节：窗口内入队的条目，三种结局加一种还没有结局。"""
    grouped = {}
    for match in matches:
        grouped.setdefault(match['registration__update_frequency'], []).append(match)

    rows = []
    for key in _frequency_keys(grouped):
        group = grouped[key]
        delivered = sum(1 for row in group if row['notified_at'] is not None)
        cancelled = sum(
            1 for row in group
            if row['notified_at'] is None and row['cancelled_at'] is not None
        )
        pending = len(group) - delivered - cancelled
        rows.append({
            'key': key,
            'label': _frequency_label(key),
            'cycle_days': DIGEST_CYCLE_DAYS.get(key),
            'queued_count': len(group),
            'delivered_count': delivered,
            'cancelled_count': cancelled,
            'pending_count': pending,
            'delivered_rate': _rate(delivered, len(group)),
            'cancelled_rate': _rate(cancelled, len(group)),
            'is_small_sample': len(group) < MIN_SAMPLE,
            'is_other': key not in FREQUENCY_ORDER,
        })

    return {
        'rows': rows,
        'queued_count': len(matches),
        'delivered_count': sum(row['delivered_count'] for row in rows),
        'cancelled_count': sum(row['cancelled_count'] for row in rows),
        'pending_count': sum(row['pending_count'] for row in rows),
    }


def _rhythm_rows(matches):
    """第三节：已送达的那些，等待时长相对各自频率的周期。"""
    grouped = {}
    for match in matches:
        if match['notified_at'] is None:
            continue
        grouped.setdefault(match['registration__update_frequency'], []).append(match)

    rows = []
    for key in _frequency_keys(grouped):
        group = grouped[key]
        cycle_seconds = DIGEST_CYCLE_DAYS.get(key)
        seconds = []
        ratios = []
        band_counts = [0] * len(RHYTHM_BANDS)
        for row in group:
            wait = max(
                (row['notified_at'] - row['matched_at']).total_seconds(), 0.0,
            )
            seconds.append(wait)
            if cycle_seconds:
                ratio = wait / (cycle_seconds * 86400)
                ratios.append(ratio)
                band_counts[_band_index(ratio, RHYTHM_BANDS)] += 1
        rows.append({
            'key': key,
            'label': _frequency_label(key),
            'cycle_days': cycle_seconds,
            'delivered_count': len(group),
            'bands': [
                {
                    'label': label,
                    'hint': hint,
                    'count': count,
                    'share': _rate(count, len(group)),
                }
                for (_limit, label, hint), count in zip(RHYTHM_BANDS, band_counts)
            ],
            'within_share': _rate(band_counts[0], len(group)) if cycle_seconds else None,
            'mean_seconds': round(_mean(seconds), 1) if seconds else None,
            'median_seconds': _percentile(seconds, 0.5),
            'p90_seconds': _percentile(seconds, 0.9),
            # 导出按秒报，页面按最贴近的单位报：同一行两种口径，说明行里写明。
            'mean_wait_label': _format_wait(round(_mean(seconds), 1) if seconds else None),
            'median_wait_label': _format_wait(_percentile(seconds, 0.5)),
            'p90_wait_label': _format_wait(_percentile(seconds, 0.9)),
            'is_small_sample': len(group) < MIN_SAMPLE,
            'is_other': key not in FREQUENCY_ORDER,
        })

    return rows


def _pending_rows(matches, *, now):
    """第四节：此刻仍挂着的，右删失样本，单独一节。"""
    grouped = {}
    for match in matches:
        grouped.setdefault(match.registration.update_frequency, []).append(match)

    rows = []
    for key in _frequency_keys(grouped):
        group = grouped[key]
        cycle_days = DIGEST_CYCLE_DAYS.get(key)
        cycle_seconds = cycle_days * 86400 if cycle_days else None
        waits = [max((now - row.matched_at).total_seconds(), 0.0) for row in group]
        if cycle_seconds:
            within = sum(1 for wait in waits if wait <= cycle_seconds)
            overdue = len(group) - within
        else:
            # 表外频率没有周期，除不出一个倍数来。两个档位都报 None，不把整组
            # 扫进「已超周期」——那是拿一个不存在的分母下结论。
            within = None
            overdue = None
        deliverable = sum(1 for row in group if _is_deliverable(row, now=now))
        rows.append({
            'key': key,
            'label': _frequency_label(key),
            'cycle_days': cycle_days,
            'pending_count': len(group),
            'deliverable_count': deliverable,
            'within_cycle_count': within,
            'overdue_count': overdue,
            'oldest_wait_seconds': max(waits) if waits else None,
            'oldest_wait_label': _format_seconds(max(waits) if waits else None),
            'is_small_sample': len(group) < MIN_SAMPLE,
            'is_other': key not in FREQUENCY_ORDER,
        })

    return {
        'rows': rows,
        'pending_count': len(matches),
        'deliverable_count': sum(row['deliverable_count'] for row in rows),
        'oldest_wait_seconds': max(
            (row['oldest_wait_seconds'] for row in rows
             if row['oldest_wait_seconds'] is not None),
            default=None,
        ),
    }


def _summary(subscription, outcome, rhythm, pending):
    """一句话说清这条摘要链路此刻的形状。"""
    if not subscription['registration_count']:
        return '平台上还没有任何专题报名，专题摘要链路一次都没有被触发过。'

    parts = [
        '当前 %d 段参与中的报名里有 %d 段开着专题摘要'
        % (subscription['active_count'], subscription['subscribed_count'])
    ]
    if subscription['subscribe_rate'] is not None:
        parts.append('，订阅率 %s%%' % subscription['subscribe_rate'])
    if subscription['left_count']:
        parts.append('，另有 %d 段已退出、不计入分母' % subscription['left_count'])

    if outcome['queued_count']:
        parts.append(
            '；窗口内入队 %d 条，已送达 %d、已取消 %d、仍未送达 %d'
            % (
                outcome['queued_count'], outcome['delivered_count'],
                outcome['cancelled_count'], outcome['pending_count'],
            )
        )
    else:
        parts.append('；窗口内没有任何新入队的命中')

    measurable = [
        row for row in rhythm
        if row['delivered_count'] >= MIN_SAMPLE and row['within_share'] is not None
    ]
    if measurable:
        worst = min(measurable, key=lambda row: row['within_share'])
        parts.append(
            '；%s的周期是 %d 天，已送达的 %d 条里只有 %s%% 在自己的周期内'
            % (worst['label'], worst['cycle_days'],
               worst['delivered_count'], worst['within_share'])
        )
    elif any(row['delivered_count'] for row in rhythm):
        parts.append('；两个频率的已送达样本都不足 %d 条，节奏占比只作参考' % MIN_SAMPLE)

    if pending['pending_count']:
        parts.append('；此刻仍挂着 %d 条' % pending['pending_count'])
        if pending['oldest_wait_seconds'] is not None:
            parts.append(
                '，最老一条已等 %s' % _format_seconds(pending['oldest_wait_seconds'])
            )
    return ''.join(parts) + '。'


def build_campaign_digest_shape(days=DEFAULT_DAYS, now=None):
    """把专题摘要的订阅、缓冲与送达节奏读成运营可读的形状。"""
    allowed_days = {value for value, _label in PERIOD_CHOICES}
    if days not in allowed_days:
        days = DEFAULT_DAYS
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    registrations = _fetch_registrations()
    matches = _fetch_window_matches(after=start, before=now)
    pending = _fetch_pending_matches()

    subscription = _subscription_rows(registrations, now=now)
    outcome = _outcome_rows(matches)
    rhythm = _rhythm_rows(matches)
    still_pending = _pending_rows(pending, now=now)

    return {
        'days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(registrations),
        'summary': _summary(subscription, outcome, rhythm, still_pending),
        'subscription': subscription,
        'active_count': subscription['active_count'],
        'left_count': subscription['left_count'],
        'subscribed_count': subscription['subscribed_count'],
        'subscribe_rate': subscription['subscribe_rate'],
        'subscribed_preference_off_count': subscription['subscribed_preference_off_count'],
        'subscribed_no_preference_count': subscription['subscribed_no_preference_count'],
        'subscribed_quiet_count': subscription['subscribed_quiet_count'],
        'frequency_rows': subscription['frequency_rows'],
        'outcome_rows': outcome['rows'],
        'outcome_queued_count': outcome['queued_count'],
        'outcome_delivered_count': outcome['delivered_count'],
        'outcome_cancelled_count': outcome['cancelled_count'],
        'outcome_pending_count': outcome['pending_count'],
        'rhythm_rows': rhythm,
        'pending_rows': still_pending['rows'],
        'pending_total': still_pending['pending_count'],
        'pending_deliverable_count': still_pending['deliverable_count'],
        'oldest_wait_seconds': still_pending['oldest_wait_seconds'],
        'oldest_wait_label': _format_seconds(still_pending['oldest_wait_seconds']),
        'rhythm_bands': [label for _limit, label, _hint in RHYTHM_BANDS],
        'min_sample': MIN_SAMPLE,
        'noise_points': NOISE_POINTS,
    }
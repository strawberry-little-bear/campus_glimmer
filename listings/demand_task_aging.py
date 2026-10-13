# -*- coding: utf-8 -*-
"""跟进任务派出去之后，有人接手了吗——没有时刻表的状态机，第一次被读。

``DemandOpportunityTask`` 的状态机跑了这么久，而「动作是什么时候发生的」这件事从来没被
记下来。表上有四个时间列：``due_at`` 是运营自己填的期限，``note`` 是自由文本，
``created_at`` 是建任务的时刻，``updated_at`` 是最后一次写任何字段的时刻。**没有任何一个
列记的是「什么时候变成 in_progress」或「什么时候变成 completed」。**

``update_demand_opportunity_task`` 说明了后果：点「开始跟进」的时候顺手把负责人补上，
那是全站唯一一处自动派单，而它只改 ``status`` 和 ``assigned_to`` 两个字段。
于是「什么时候开始有人管的」和「什么时候改了一句备注」在库里是同一个数字——
``updated_at``。

拿 ``updated_at`` 冒充状态变更时刻有两个方向的错。反方向：一个建了三个月没人碰过的
任务，``updated_at`` 停在 ``created_at``，读出来是「刚刚动过」，和一条已经结案的任务
一样新；拿它做任何时间排序，都会把最久没人管的那批排到最后。正方向：一个 3 月 1 日建
好、3 月 5 日被接手、4 月 2 日结案的任务，``updated_at`` 是 4 月 2 日，
``updated_at - created_at`` 读出来是 32 天，而真实情况是「等了 4 天才有人接手，接手之后
28 天结案」。这两个数字说的是两件不同的事，而运营会按后者排期、按前者问责。

所以本模块不加迁移、不加字段、不动写入路径，只在既有数据上把能说的和不能说的分开说。
两个口径，各自说清楚自己是什么：

**口径一，接手时长。** 已经有人接手的任务，等待取 ``updated_at - created_at`` 这个
上界——真实等待只会比它更长：每一次改备注、改期限都会把 ``updated_at`` 往后推，而等待
在第一次点「开始跟进」那一刻就已经结束了。

没接手过的任务走另一个式子：``now - created_at``。没人碰过的任务 ``updated_at`` 停在
``created_at``，中间没有任何一次写入，所以「等了多久」就是「建出来到现在」，这一段是干净
的、可以直接读的。两组分开报，正是因为它们的可信度不一样。分档而不是逐个报，因为上界本身
就带噪声，在一个带噪声的数上做精确对比是假装精确。

**口径二，停留时长，只报当前快照。** ``now - updated_at`` 衡量「这个任务最后一次被写之后
又过去了多久」。它同样是间接的，但方向和上一个口径相反：这里要的是「最近一次活动发生在
多久以前」，而 ``updated_at`` 记的正是「最近一次写入」。**这一条上，``updated_at`` 恰好
就是那个正确的列。** 所以第二个口径报的是结构而不是等待：每个状态上有多少任务、它们的
中位停留时长、以及一张最久没有活动的任务清单。

只报告。不改任务状态、不自动派单、不改截止时间、不新增任何提醒，也不按负责人排名——
那是一份考勤表，不是一份看板。负责人只在待处理清单里作为一列出现，用来找到人。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import DemandOpportunityTask

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一组任务至少要这么多条，才允许对它下「这一组等得更久」的判断。和搜索位置质量、
# 求购匹配质量同一个量级：几十条样本上几个百分点的差距是噪声。
MIN_BUCKET_SAMPLE = 12

# 两组的等待时长中位数相差小于这个天数视为噪声。等待的尺度是天，报「中位相差 1.4 天」
# 是假装精确；三天是运营能据以重新排期的最小可见差距。
NOISE_DAYS = 3

# 接手等待的分档。按天切而不是按小时：跟进任务的时间尺度是天，报「等了 37 小时」对
# 一个按周排期的负责人没有意义。
HANDOVER_BUCKETS = (
    (0, '当天'),
    (3, '1-3 天'),
    (7, '4-7 天'),
    (30, '8-30 天'),
    (None, '30 天以上'),
)

# 停留时长的分档。和接手等待共用一把尺子，两个口径才能摆在同一张表里比。
RESIDENCY_BUCKETS = HANDOVER_BUCKETS

# 待处理清单最多列这么多条。清单是给「现在去处理」用的，不是给归档用的。
STALE_TASK_LIMIT = 20

STATUS_LABELS = dict(DemandOpportunityTask.STATUS_CHOICES)

# 已经不需要再跟的状态。它们从待处理清单里排除，但照常参与停留时长统计。
CLOSED_STATUSES = ('completed', 'ignored')

OPEN_STATUSES = ('todo', 'in_progress')


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _bucket_label(days):
    """把一段天数归进分档。当天接手记 0 天，负数（时钟回拨）也落进「当天」。"""
    if days is None:
        return None
    if days <= 0:
        return HANDOVER_BUCKETS[0][1]
    for limit, label in HANDOVER_BUCKETS[1:]:
        if limit is None or days < limit:
            return label
    return HANDOVER_BUCKETS[-1][1]


def _is_open(task):
    return task.status in OPEN_STATUSES


def _is_closed(task):
    return task.status in CLOSED_STATUSES


def _has_handover(task):
    """这个任务有没有被人接手过。

    两个信号取或：状态已经不是 ``todo``，或者 ``assigned_to`` 非空。第二个信号覆盖
    「在后台直接派了人、状态还是 todo」这种手动操作——自动派单只发生在点「开始跟进」
    的时候，但后台可以只改负责人。
    """
    return task.status != 'todo' or task.assigned_to_id is not None


def _handover_wait_days(task, now):
    """接手等待。没人接手的任务等的是「建出来到现在」，已接手的只报上界。

    两条分支用的是两个不同的式子，因为它们回答的不是同一个问题，可信度也不一样。

    没人接手过（状态还是 ``todo`` 且没有负责人）的任务，``updated_at`` 停在
    ``created_at``，中间没有任何一次写入。所以「等了多久」就是 ``now - created_at``，
    这一段是干净的、可以直接读的。

    已经有人接手的任务，``updated_at`` 是「最后一次写任何字段」的时刻，里面混着改备注和
    改期限，``updated_at - created_at`` 因此只能当上界：真实等待只会更长。时钟回拨
    （``updated_at`` 早于 ``created_at``）记 0 而不是负数——一个负数天数是数据问题，
    不是「提前被人接手」。
    """
    if _has_handover(task):
        return max((task.updated_at - task.created_at).days, 0)
    return max((now - task.created_at).days, 0)


def _residency_days(task, now):
    """最后一次写入之后又过去了多久，即当前状态的停留时长。"""
    delta = now - task.updated_at
    return max(delta.days, 0)


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


def _period_start(days, today):
    '''统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    '''
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _fetch_tasks(days, now):
    '''周期内创建的任务，按创建时间与主键显式排序。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    '''
    start = _period_start(days, timezone.localtime(now).date())
    return list(
        DemandOpportunityTask.objects.filter(created_at__gte=start, created_at__lte=now)
        .select_related('assigned_to', 'category', 'location')
        .order_by('created_at', 'id')
    )


def _fetch_all_tasks():
    '''全部跟进任务，供停留时长与待处理清单使用。

    停留时长描述的是「现在」：一个周期开始之前就没人管的任务，恰恰是最不该被藏起来的
    一件。这一点沿用治理队列和逾期清单的既有口径。
    '''
    return list(
        DemandOpportunityTask.objects.all()
        .select_related('assigned_to', 'category', 'location')
        .order_by('created_at', 'id')
    )


def _build_handover_stats(tasks, now):
    """接手等待：有没有人接手、按分档拆、两组的等待中位数对比。

    两组用的是两个不同的式子，理由写在 ``_handover_wait_days`` 里：已经接手的任务
    等待取 ``updated_at - created_at``，是上界；没接手的任务中间没有任何写入，等待取
    ``now - created_at``，是可读的。按「有没有接手过」拆成两组单独报，正是因为它们的
    可信度不一样——混在一组里，可读的那半会被不可读的那半拖成一句废话。
    """
    handed = [task for task in tasks if _has_handover(task)]
    waiting = [task for task in tasks if not _has_handover(task)]

    def bucket_rows(group):
        buckets = {label: {'label': label, 'count': 0} for _limit, label in HANDOVER_BUCKETS}
        for task in group:
            label = _bucket_label(_handover_wait_days(task, now))
            if label is not None:
                buckets[label]['count'] += 1
        order = [label for _limit, label in HANDOVER_BUCKETS]
        return [buckets[label] for label in order if label in buckets]

    handed_waits = [_handover_wait_days(task, now) for task in handed]
    waiting_waits = [_handover_wait_days(task, now) for task in waiting]
    handed_median = _median(handed_waits)
    waiting_median = _median(waiting_waits)
    gap = None
    if handed_median is not None and waiting_median is not None:
        gap = round(handed_median - waiting_median, 1)
    measurable = bool(
        len(handed) >= MIN_BUCKET_SAMPLE and len(waiting) >= MIN_BUCKET_SAMPLE
        and gap is not None
    )
    return {
        'total': len(tasks),
        'handed_count': len(handed),
        'handed_rate': _percent(len(handed), len(tasks)),
        'waiting_count': len(waiting),
        'waiting_rate': _percent(len(waiting), len(tasks)),
        'handed_median_days': handed_median,
        'waiting_median_days': waiting_median,
        'handed_bucket_rows': bucket_rows(handed),
        'waiting_bucket_rows': bucket_rows(waiting),
        'gap_days': gap,
        'is_measurable': measurable,
        # 两组都过门槛、差值也出了噪声区间，才给方向。
        'gap_is_meaningful': measurable and abs(gap) >= NOISE_DAYS,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'noise_days': NOISE_DAYS,
    }


def _build_residency_stats(tasks, now):
    """停留时长：每个当前状态停了多久，按状态拆。

    停留取 ``now - updated_at``，这一条上 ``updated_at`` 恰好就是那个正确的列——要的正是
    「最近一次写入发生在多久以前」。所以这里报的是结构而不是等待：每个状态上有多少任务、
    中位停了多久。已闭环的任务照常参与，它们的停留时长回答的是「结案之后还留着多久」。
    """
    rows = []
    for key, label in DemandOpportunityTask.STATUS_CHOICES:
        group = [task for task in tasks if task.status == key]
        residencies = [_residency_days(task, now) for task in group]
        buckets = {name: {'label': name, 'count': 0} for _limit, name in RESIDENCY_BUCKETS}
        for days in residencies:
            name = _bucket_label(days)
            if name is not None:
                buckets[name]['count'] += 1
        order = [name for _limit, name in RESIDENCY_BUCKETS]
        rows.append({
            'key': key,
            'label': label,
            'task_count': len(group),
            'median_days': _median(residencies),
            'max_days': max(residencies) if residencies else 0,
            'is_small_sample': len(group) < MIN_BUCKET_SAMPLE,
            'bucket_rows': [buckets[name] for name in order if name in buckets],
        })
    open_tasks = [task for task in tasks if _is_open(task)]
    never_handed = [
        task for task in tasks
        if not _is_closed(task) and not _has_handover(task)
    ]
    return {
        'rows': rows,
        'open_count': len(open_tasks),
        'never_handed_count': len(never_handed),
        'never_handed_rate': _percent(len(never_handed), len(open_tasks)) if open_tasks else 0,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
    }


def _build_stale_rows(tasks, now):
    """最久没有活动的未闭环任务。

    清单只按「未闭环」筛：已结案的任务已经不是待办。已经接手但停住的任务同样在列——
    「接手之后 40 天没有任何写入」正是最该被点开看看的一件，把它藏起来等于把最可操作的
    一条藏起来。每行带 ``has_handover``，看板上能把「没人管」和「接手后停住了」分开。

    清单不按统计周期过滤，理由同逾期清单：周期开始之前就没人管的任务，恰恰是最不该被
    藏起来的一件。按停留时长排，最久的在最前面；名次相同按创建时间与主键，不靠数据库
    返回顺序。负责人只在清单里作为一列出现，用来找到人，不用来比较人。

    清单最多列 ``STALE_TASK_LIMIT`` 条：它是给「现在去处理」用的，不是给归档用的。
    总数照实报在 ``row_total`` 里——截断不等于「一共就这么多」，把 24 条报成 20 条会让
    人以为已经看全了。
    """
    stale = []
    for task in tasks:
        if _is_closed(task):
            continue
        stale.append({
            'id': task.id,
            'title': task.title,
            'status_label': STATUS_LABELS.get(task.status, task.status),
            'assigned_to': task.assigned_to.username if task.assigned_to else '',
            'has_handover': _has_handover(task),
            'has_due': task.due_at is not None,
            'due_at': task.due_at,
            'note_length': len((task.note or '').strip()),
            'residency_days': _residency_days(task, now),
            'created_at': task.created_at,
        })
    stale.sort(key=lambda row: (-row['residency_days'], row['created_at'], row['id']))
    return {
        'rows': stale[:STALE_TASK_LIMIT],
        'row_total': len(stale),
        'row_limit': STALE_TASK_LIMIT,
        'max_residency_days': max((row['residency_days'] for row in stale), default=0),
        'never_handed_max_days': max(
            (row['residency_days'] for row in stale if not row['has_handover']), default=0
        ),
    }


def _summary(handover, residency, stale):
    if not handover['total']:
        return '当前周期还没有建立需求跟进任务，等待与停留需要先有任务存在。'
    parts = [
        f"共 {handover['total']} 个跟进任务，{handover['handed_count']} 个有人接手"
        f"（{handover['handed_rate']}%），{handover['waiting_count']} 个还停在待跟进。"
    ]
    if handover['handed_median_days'] is not None:
        parts.append(
            f"接手过的任务，从建任务到库里最后一次写它平均隔了 "
            f"{handover['handed_median_days']} 天——这是上界，真实等待只会更长。"
        )
    if handover['waiting_count']:
        parts.append(
            f"其中 {handover['waiting_count']} 个从建出来就没被人碰过，"
            f"最久的已经 {stale['never_handed_max_days']} 天没有任何写入。"
        )
    if handover['gap_is_meaningful']:
        direction = '更短' if handover['gap_days'] < 0 else '更长'
        parts.append(
            f"有人接手的任务等待{direction}（相差 {abs(handover['gap_days'])} 天）。"
        )
    elif handover['is_measurable']:
        parts.append('两组的等待相差在噪声范围内，读不出方向。')
    if residency['never_handed_count']:
        parts.append(
            f"未闭环的任务里有 {residency['never_handed_count']} 个从没被接手过。"
        )
    return ''.join(parts)


def build_demand_task_aging(days=30, now=None):
    '''统计跟进任务的接手等待与当前停留，只报告不改任何字段。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    tasks = _fetch_tasks(days, now)
    handover = _build_handover_stats(tasks, now)
    # 停留时长与待处理清单在整个台账上算，不按统计周期过滤：周期开始之前就没人管的
    # 任务，恰恰是最不该被藏起来的一件。接手等待仍然只统计期内创建的任务——它是回顾
    # 性的，混进更早的任务会让周期失真。
    all_tasks = _fetch_all_tasks()
    residency = _build_residency_stats(all_tasks, now)
    stale = _build_stale_rows(all_tasks, now)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(tasks),
        'summary': _summary(handover, residency, stale),
        'handover': handover,
        'residency': residency,
        'stale_rows': stale,
        'status_labels': STATUS_LABELS,
    }

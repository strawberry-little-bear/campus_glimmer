# -*- coding: utf-8 -*-
'''需求跟进任务的截止时间与备注，第一次被写、被读、被统计。

``DemandOpportunityTask`` 从需求雷达上线那天起就带着两个字段：``due_at`` 记这次
跟进该在什么时候之前有结论，``note`` 是一段留给跟进人自己的自由文本。雷达旁边那三个
按钮——建立跟进任务、开始跟进、标记完成——只改状态和负责人，从来不碰这两个字段；
创建任务的视图不传它们，看板模板里也找不到它们的名字。全库唯一的引用是后台的
``search_fields``，也就是说，除了有人在后台列表页手填过，没有任何一条业务路径写进
过它们。一个建了台账却没有时间表的跟进流程，和没有台账是两件事：前者会让"正在跟进"
这句话无限期地有效。

所以这个模块先问填写率，再问逾期，最后问一句最有信息量的：设了截止时间的任务，
是不是真的更容易被做完。

第一件事是填写率。一个没人填的字段不该被分析，该被撤掉；而"任务建了但没人给期限"
本身比逾期更能说明跟进流程有没有真的在跑，所以它被单独报出来，而不是混在逾期里。
第二件是逾期与临期，按逾期时长分档。待处理清单刻意不按统计周期过滤——周期开始之前
就超期的那个任务，恰恰是最不该被藏起来的一件，这一点沿用治理队列的既有口径。第三件
是有期限与无期限两组的完成率对比：如果设了期限的那组并没有更容易做完，说明截止时间
只是被填上去了，没有在约束任何东西；如果两组的差距落在噪声区间内，就只报数字、不给
方向，因为几十条任务上几个百分点的距离和抛硬币差不多。

它只报告。不改任务状态、不自动派单、不改截止时间、不新增任何提醒。``note`` 是跟进人
写给自己的内部备忘，不是用户声音，所以这里只数字数、只报有多少人写，不做切词、不聚类、
不推断语义，也不公开任何一条原文——给内部记录做词典，会把备忘讲成用户反馈。
'''

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import DemandOpportunityTask

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一条备注短于这个长度就不算"写过"：一两个字的占位和空着没有区别，
# 把它算成填写率会把一个死字段讲成还在用。
MIN_NOTE_LENGTH = 4

# 截止时间给多长的合理上限。超过这个长度的期限不是期限，是"以后再说"，
# 报出来是为了让运营看见它，不是为了给它改判。
MAX_REASONABLE_DUE_DAYS = 180

# 逾期时长分档。按天切而不是按小时：跟进任务的时间尺度是天，
# 报"超了 37 小时"对一个按周排期的负责人没有意义。
OVERDUE_BUCKETS = (
    (1, '当天'),
    (3, '1-3 天'),
    (None, '3 天以上'),
)

# 临期窗口：截止时间落在这个天数之内、且还没到，算"快到期限了"。
DUE_SOON_DAYS = 3

# 一组任务至少要这么多条已闭环，完成率的对比才给方向。和匹配质量模块同一个量级：
# 几十条样本上几个百分点的差距是噪声。
MIN_COMPARISON_TASKS = 12

# 两组的完成率相差小于这个百分点视为噪声，同互助反馈补充说明的既有口径。
NOISE_POINTS = 8

# 待处理清单最多列这么多条。清单是给"现在去处理"用的，不是给归档用的。
OPEN_TASK_LIMIT = 20

STATUS_LABELS = dict(DemandOpportunityTask.STATUS_CHOICES)

# 已经不需要再跟的状态。它们从逾期和临期里排除，但照常参与完成率统计。
CLOSED_STATUSES = ('completed', 'ignored')

OPEN_STATUSES = ('todo', 'in_progress')


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _note_text(task):
    return (task.note or '').strip()


def _has_due(task):
    return task.due_at is not None


def _has_note(task):
    return len(_note_text(task)) >= MIN_NOTE_LENGTH


def _is_open(task):
    return task.status in OPEN_STATUSES


def _is_closed(task):
    return task.status in CLOSED_STATUSES


def _overdue_days(task, now):
    '''已经过去几天，没过期返回 None；当天就到了期限算 0 天。

    按本地时间的日期差算，不按 24 小时的倍数算：一个今晚 23:00 到期的任务，
    在第二天早上 08:00 已经是"逾期一天"，而两者的间隔只有九个小时。跟进是按
    天排期的，期限也该按天读。同一个道理，今天早上八点就到期的任务，下午再来
    读它已经是"当天逾期"——没凑满一整天，但期限确实过了；把它算成"还没到期"
    会让逾期清单凭空少一截，而"当天"这一档也就永远数是零。
    '''
    if not _has_due(task):
        return None
    due_date = timezone.localtime(task.due_at).date()
    today = timezone.localtime(now).date()
    delta = (today - due_date).days
    if delta > 0:
        return delta
    if delta == 0 and task.due_at < now:
        return 0
    return None


def _overdue_bucket_label(days):
    for limit, label in OVERDUE_BUCKETS:
        if limit is None or days < limit:
            return label
    return OVERDUE_BUCKETS[-1][1]


def _days_until_due(task, now):
    '''距离截止还有几天，负数表示已过期，没有期限返回 None。'''
    if not _has_due(task):
        return None
    due_date = timezone.localtime(task.due_at).date()
    return (due_date - timezone.localtime(now).date()).days


def _due_span_days(task):
    '''从建任务到截止给了多少天，用来识别"以后再说"式的期限。'''
    if not _has_due(task):
        return None
    created = timezone.localtime(task.created_at).date()
    due = timezone.localtime(task.due_at).date()
    return (due - created).days


def _period_start(days, today):
    '''统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    '''
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _fetch_tasks(days, now):
    '''周期内创建的任务，按创建时间与主键显式排序。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何"第一名"都会随机漂移。
    '''
    start = _period_start(days, timezone.localtime(now).date())
    return list(
        DemandOpportunityTask.objects.filter(created_at__gte=start, created_at__lte=now)
        .select_related('assigned_to', 'category', 'location')
        .order_by('created_at', 'id')
    )


def _fetch_all_tasks():
    '''全部跟进任务，供逾期与临期清单使用。

    显式排序，同一个理由：待处理清单里名次的先后不能取决于数据库这一次
    返回行的顺序。
    '''
    return list(
        DemandOpportunityTask.objects.all()
        .select_related('assigned_to', 'category', 'location')
        .order_by('created_at', 'id')
    )


def _build_fill_stats(tasks):
    '''填写率、长度分布，以及写了备注的那些人平均写多长。'''
    total = len(tasks)
    with_due = [task for task in tasks if _has_due(task)]
    with_note = [task for task in tasks if _has_note(task)]
    lengths = [len(_note_text(task)) for task in with_note]
    # 期限过长的那一批单独数出来：超过 MAX_REASONABLE_DUE_DAYS 的期限读起来
    # 像"填了个日子好让字段不为空"，它和没填一样不约束任何东西。
    spans = [span for span in (_due_span_days(task) for task in with_due) if span is not None]
    distant = [span for span in spans if span > MAX_REASONABLE_DUE_DAYS]
    return {
        'total': total,
        'with_due_count': len(with_due),
        'due_fill_rate': _percent(len(with_due), total),
        'without_due_count': total - len(with_due),
        'with_note_count': len(with_note),
        'note_fill_rate': _percent(len(with_note), total),
        'average_note_length': round(sum(lengths) / len(lengths), 1) if lengths else 0,
        'max_note_length': max(lengths) if lengths else 0,
        'distant_due_count': len(distant),
        'distant_due_rate': _percent(len(distant), len(with_due)),
        'min_note_length': MIN_NOTE_LENGTH,
        'max_reasonable_due_days': MAX_REASONABLE_DUE_DAYS,
    }


def _build_due_contrast(tasks):
    '''有期限与无期限两组，各自的完成率。

    完成率的分母是已闭环的任务，不是全部任务：一个还没做完的任务说不出它会不会
    被做完，把它算进分母会让刚建的任务显得拉低完成率。样本不足的组只报数字。
    '''
    rows = []
    for key, label in (('with_due', '设了截止时间'), ('without_due', '没有截止时间')):
        group = [task for task in tasks if _has_due(task) == (key == 'with_due')]
        closed = [task for task in group if _is_closed(task)]
        completed = [task for task in group if task.status == 'completed']
        rows.append({
            'key': key,
            'label': label,
            'task_count': len(group),
            'closed_count': len(closed),
            'completed_count': len(completed),
            'completion_rate': _percent(len(completed), len(closed)),
            'is_small_sample': len(closed) < MIN_COMPARISON_TASKS,
        })
    with_due = next((row for row in rows if row['key'] == 'with_due'), None)
    without_due = next((row for row in rows if row['key'] == 'without_due'), None)
    gap = None
    if with_due and without_due and (with_due['closed_count'] or without_due['closed_count']):
        gap = round(with_due['completion_rate'] - without_due['completion_rate'], 1)
    measurable = bool(
        with_due and without_due
        and not with_due['is_small_sample'] and not without_due['is_small_sample']
        and gap is not None
    )
    return {
        'rows': rows,
        'gap_points': gap,
        # 只有两组都过门槛、差值也出了噪声区间，才给方向。
        'is_measurable': measurable,
        'gap_is_meaningful': measurable and abs(gap) >= NOISE_POINTS,
        'min_comparison_tasks': MIN_COMPARISON_TASKS,
        'noise_points': NOISE_POINTS,
    }


def _build_overdue_stats(tasks, now):
    '''逾期与临期，按逾期时长分档，每档各自过门槛。

    待处理清单不按统计周期过滤：周期开始之前就超期的那个任务，恰恰是最不该被
    藏起来的一件。分档只数未闭环的任务——已完成和已忽略的逾期是历史，不是待办。
    '''
    open_tasks = [task for task in tasks if _is_open(task)]
    overdue = []
    approaching = []
    for task in open_tasks:
        days = _overdue_days(task, now)
        if days is not None:
            overdue.append((task, days))
            continue
        remaining = _days_until_due(task, now)
        if remaining is not None and remaining <= DUE_SOON_DAYS:
            approaching.append((task, remaining))

    buckets = {label: {'label': label, 'count': 0} for _limit, label in OVERDUE_BUCKETS}
    for _task, days in overdue:
        buckets[_overdue_bucket_label(days)]['count'] += 1
    order = [label for _limit, label in OVERDUE_BUCKETS]
    bucket_rows = [buckets[label] for label in order if label in buckets]

    # 逾期清单按超期天数排，超得最久的在最前面。名次相同按创建时间与主键，
    # 不靠数据库返回顺序。
    overdue_rows = [
        {
            'id': task.id,
            'title': task.title,
            'status_label': STATUS_LABELS.get(task.status, task.status),
            'assigned_to': task.assigned_to.username if task.assigned_to else '',
            'due_at': task.due_at,
            'overdue_days': days,
            'note_length': len(_note_text(task)),
            'created_at': task.created_at,
        }
        for task, days in sorted(
            overdue, key=lambda pair: (-pair[1], pair[0].created_at, pair[0].id),
        )
    ]
    approaching_rows = [
        {
            'id': task.id,
            'title': task.title,
            'status_label': STATUS_LABELS.get(task.status, task.status),
            'assigned_to': task.assigned_to.username if task.assigned_to else '',
            'due_at': task.due_at,
            'days_until_due': remaining,
            'note_length': len(_note_text(task)),
        }
        for task, remaining in sorted(approaching, key=lambda pair: (pair[1], pair[0].id))
    ]
    return {
        'open_count': len(open_tasks),
        'with_due_open_count': sum(1 for task in open_tasks if _has_due(task)),
        'overdue_count': len(overdue),
        'overdue_rate': _percent(len(overdue), sum(1 for task in open_tasks if _has_due(task))),
        'max_overdue_days': max((days for _task, days in overdue), default=0),
        'approaching_count': len(approaching),
        'due_soon_days': DUE_SOON_DAYS,
        'bucket_rows': bucket_rows,
        'overdue_rows': overdue_rows,
        'approaching_rows': approaching_rows,
        'open_task_limit': OPEN_TASK_LIMIT,
    }


def _build_status_rows(tasks):
    '''按任务状态的分布，用于把完成率和状态台账放在同一张表里。'''
    rows = []
    for key, label in DemandOpportunityTask.STATUS_CHOICES:
        group = [task for task in tasks if task.status == key]
        rows.append({
            'key': key,
            'label': label,
            'task_count': len(group),
            'with_due_count': sum(1 for task in group if _has_due(task)),
            'with_note_count': sum(1 for task in group if _has_note(task)),
        })
    return rows


def _summary(fill_stats, contrast, overdue_stats):
    if not fill_stats['total']:
        return '当前周期还没有建立需求跟进任务，截止时间和备注需要先有任务存在。'
    parts = [
        f"共 {fill_stats['total']} 个跟进任务，{fill_stats['with_due_count']} 个设了截止时间"
        f"（{fill_stats['due_fill_rate']}%），{fill_stats['with_note_count']} 个写了备注"
        f"（{fill_stats['note_fill_rate']}%）。"
    ]
    if fill_stats['distant_due_count']:
        parts.append(
            f"其中 {fill_stats['distant_due_count']} 个期限距建任务超过 "
            f"{fill_stats['max_reasonable_due_days']} 天，那种期限读起来像"
            f"「填个日子好让字段不为空」。"
        )
    else:
        parts.append('已填的期限都在合理范围内。')
    if overdue_stats['overdue_count']:
        parts.append(
            f"未闭环的任务里有 {overdue_stats['overdue_count']} 个已过期限，"
            f"最久的一个超了 {overdue_stats['max_overdue_days']} 天。"
        )
    elif overdue_stats['with_due_open_count']:
        parts.append('未闭环且设了期限的任务都还在期限内。')
    else:
        parts.append('未闭环的任务都没有设期限，逾期也就无从谈起。')
    if contrast['gap_is_meaningful']:
        direction = '更容易' if contrast['gap_points'] > 0 else '并没有更容易'
        parts.append(
            f"设了截止时间的任务{direction}被做完"
            f"（相差 {abs(contrast['gap_points'])} 个百分点）。"
        )
    elif contrast['is_measurable']:
        parts.append('两组的完成率相差在噪声范围内，读不出方向。')
    return ''.join(parts)


def build_demand_opportunity_followups(days=30, now=None):
    '''统计跟进任务的截止时间与备注填写情况，只报告不改任何字段。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    tasks = _fetch_tasks(days, now)
    fill_stats = _build_fill_stats(tasks)
    contrast = _build_due_contrast(tasks)
    # 逾期与临期在整个台账上算，不按统计周期过滤：周期开始之前就超期的那个任务，
    # 恰恰是最不该被藏起来的一件。而填写率、完成率对比和状态分布仍然只统计期内
    # 创建的任务——它们是回顾性的，混进更早的任务会让周期失真。
    overdue_stats = _build_overdue_stats(_fetch_all_tasks(), now)
    status_rows = _build_status_rows(tasks)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(tasks),
        'summary': _summary(fill_stats, contrast, overdue_stats),
        'fill_stats': fill_stats,
        'due_contrast': contrast,
        'overdue_stats': overdue_stats,
        'status_rows': status_rows,
        'status_labels': STATUS_LABELS,
    }

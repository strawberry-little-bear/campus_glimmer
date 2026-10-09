# -*- coding: utf-8 -*-
"""Close the loop between a demand-radar opportunity and what actually changed.

The radar already finds gaps and lets staff raise a follow-up task, which leaves
an obvious question unanswered: after a task is created, does the gap close? A
radar that only raises alarms cannot prove it is worth acting on, so staff stop
believing it. This module supplies the missing feedback by measuring what the
search log says about the same topic before and after the task existed.

The comparison is deliberately built around DemandOpportunityTask.created_at
rather than around the radar snapshot. A radar row is a rolling aggregate whose
contents change every time the dashboard is opened, so it cannot serve as a
stable boundary; a task is a fixed, dated decision. Every task row is therefore
re-derived from its own radar_key, which is what keeps the measured topic
identical to the topic that was acted upon.

Each task is split into a baseline window ending at creation and an observation
window starting at it, and the zero-result search rate is compared across the
two. A falling rate means the supply-side action is working. A flat rate means
either nobody acted or the action missed the actual demand, and those two cases
are told apart by the change in available supply rather than by guesswork.

The module refuses to score thin evidence. A window holding one search produces
a zero-result rate of 0% or 100% that carries no information, and reporting such
a task as "converged" would be the kind of self-congratulation that makes an
operational metric untrustworthy. Tasks without enough data are separated out
as insufficient-sample cases instead of being silently folded into the
successful ones.

Only aggregate counts are ever read from the search log, and no user identity is
returned. This matches the radar's existing position that demand should inform
replenishment without exposing who asked for what.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import DemandOpportunityTask, Item, SearchQuery

# How far either side of the task creation moment to look when comparing the
# search log. The same span is used on both sides so a single seasonal shift
# cannot masquerade as convergence; 14 days is long enough to collect a few
# searches on a quiet topic and short enough that a task still reflects what
# the market looked like when it was raised.
DEFAULT_WINDOW_DAYS = 14

# A window needs this many searches before its zero-result rate means anything.
# Below this the rate is a rounding error, so the task is held back rather than
# being counted as either a success or a failure.
MIN_WINDOW_SEARCHES = 2

# Classification thresholds for the observed change. A drop of at least this
# many percentage points counts as convergence, and a rise of the same size
# counts as divergence. The band in between is "flat" on purpose: a topic that
# swings a few points is noise, and treating noise as a result would be worse
# than admitting nothing happened.
CONVERGENCE_DELTA_POINTS = 10

# How the outcome is labelled to staff. Kept as a flat mapping because the same
# labels are reused by the dashboard, the export and the alert builder.
OUTCOME_LABELS = {
    'converged': '缺口收敛',
    'flat': '基本持平',
    'diverged': '缺口扩大',
    'insufficient': '样本不足',
    'pending': '观察期未结束',
}

# How long after a task is created it is still considered too early to judge.
# Before this the observation window is mostly empty, so the rate would be
# computed from almost nothing and would look artificially good.
MATURITY_DAYS = DEFAULT_WINDOW_DAYS


def parse_radar_key(radar_key):
    """Split a radar key back into the filters that produced it.

    The key is assembled as 'term|category:<id>|location:<id>' but the term is
    free text that may itself contain a pipe, so parsing starts from the right
    where the two trailing segments have a known shape.
    """
    parts = (radar_key or '').rsplit('|', 2)
    if len(parts) != 3:
        return None
    term, category_part, location_part = parts
    if not category_part.startswith('category:') or not location_part.startswith('location:'):
        return None
    try:
        category_id = int(category_part.split(':', 1)[1] or 0)
        location_id = int(location_part.split(':', 1)[1] or 0)
    except (TypeError, ValueError):
        return None
    return {
        'term': term.strip(),
        'category_id': category_id or None,
        'location_id': location_id or None,
    }


def _rate(zero_count, total):
    return round(zero_count / total * 100, 1) if total else None


def _classify_delta(delta_points):
    """Turn a rate change into one of the three verdicts.

    The boundaries are shared by the row classification and the summary so a
    task can never be counted as converged in one place and flat in another.
    """
    if delta_points is None:
        return 'insufficient'
    if delta_points >= CONVERGENCE_DELTA_POINTS:
        return 'converged'
    if delta_points <= -CONVERGENCE_DELTA_POINTS:
        return 'diverged'
    return 'flat'


def _search_window_stats(filters, start, end):
    """Count searches and their zero-result subset inside one window."""
    queryset = SearchQuery.objects.filter(created_at__gte=start, created_at__lt=end)
    term = filters.get('term')
    if term:
        queryset = queryset.filter(query__iexact=term)
    if filters.get('category_id'):
        queryset = queryset.filter(category_id=filters['category_id'])
    if filters.get('location_id'):
        queryset = queryset.filter(location_id=filters['location_id'])
    totals = queryset.aggregate(
        total=Count('id'),
        zero=Count('id', filter=Q(result_count=0)),
    )
    return totals['total'] or 0, totals['zero'] or 0


def _current_supply(filters, now):
    """Reuse the radar's own supply rule so both sides agree on what counts."""
    items = Item.objects.available().filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    if filters.get('category_id'):
        items = items.filter(category_id=filters['category_id'])
    if filters.get('location_id'):
        items = items.filter(location_id=filters['location_id'])
    if filters.get('term'):
        items = items.filter(
            Q(title__icontains=filters['term']) | Q(description__icontains=filters['term']),
        )
    return items.count()


def build_task_outcome(task, *, now=None, window_days=DEFAULT_WINDOW_DAYS):
    """Measure one follow-up task against the search log it was meant to fix."""
    now = now or timezone.now()
    window = timedelta(days=window_days)
    created_at = task.created_at
    filters = parse_radar_key(task.radar_key)

    base = {
        'task_id': task.id,
        'title': task.title,
        'radar_key': task.radar_key,
        'status': task.status,
        'status_label': task.get_status_display(),
        'level': task.level,
        'created_at': created_at,
        'window_days': window_days,
        'filters': filters or {},
        'baseline_searches': 0,
        'baseline_zero_searches': 0,
        'observation_searches': 0,
        'observation_zero_searches': 0,
        'baseline_zero_rate': None,
        'observation_zero_rate': None,
        'delta_points': None,
        'outcome': 'pending',
        'outcome_label': OUTCOME_LABELS['pending'],
        'supply_at_creation': task.available_supply,
        'supply_now': task.available_supply,
        'supply_delta': 0,
        'signal_total': 0,
        'is_mature': (now - created_at) >= timedelta(days=MATURITY_DAYS),
        'verdict': None,
    }
    if filters is None:
        base['verdict'] = '该任务的雷达标识无法解析，暂不参与统计。'
        return base

    base_start = created_at - window
    base_end = created_at
    obs_start = created_at
    obs_end = created_at + window

    baseline_total, baseline_zero = _search_window_stats(filters, base_start, base_end)
    observation_total, observation_zero = _search_window_stats(filters, obs_start, obs_end)

    base['baseline_searches'] = baseline_total
    base['baseline_zero_searches'] = baseline_zero
    base['observation_searches'] = observation_total
    base['observation_zero_searches'] = observation_zero
    # 需求信号总量：任务前后两次窗口的搜索合计，用来在收敛幅度相同时区分
    # "本来就是冷门话题" 和 "热门话题的缺口被真的补上了"。
    base['signal_total'] = baseline_total + observation_total
    base['baseline_zero_rate'] = _rate(baseline_zero, baseline_total)
    base['observation_zero_rate'] = _rate(observation_zero, observation_total)

    supply_now = _current_supply(filters, now)
    base['supply_now'] = supply_now
    base['supply_delta'] = supply_now - task.available_supply

    # The observation window is clipped at "now" because a rate computed partly
    # from searches that have not happened yet would understate failure.
    if now < obs_end:
        base['outcome'] = 'pending'
        base['outcome_label'] = OUTCOME_LABELS['pending']
        base['verdict'] = '观察期尚未结束，暂不判定效果。'
        return base

    if (
        baseline_total < MIN_WINDOW_SEARCHES
        or observation_total < MIN_WINDOW_SEARCHES
    ):
        base['outcome'] = 'insufficient'
        base['outcome_label'] = OUTCOME_LABELS['insufficient']
        base['verdict'] = '基线期或观察期搜索量不足，结论不可靠。'
        return base

    baseline_rate = base['baseline_zero_rate']
    observation_rate = base['observation_zero_rate']
    delta = round(baseline_rate - observation_rate, 1)
    base['delta_points'] = delta
    outcome = _classify_delta(delta)
    base['outcome'] = outcome
    base['outcome_label'] = OUTCOME_LABELS[outcome]

    if outcome == 'converged':
        if base['supply_delta'] > 0:
            base['verdict'] = '无结果搜索下降，且可用供给增加，可判定为供给动作生效。'
        else:
            base['verdict'] = '无结果搜索下降，但可用供给没有增加，需排除季节性或偶然因素。'
    elif outcome == 'diverged':
        if base['supply_delta'] > 0:
            base['verdict'] = '无结果搜索反而上升，尽管供给增加了，需核对是否补错了主题。'
        else:
            base['verdict'] = '无结果搜索上升且供给未增加，任务未见效果。'
    else:
        base['verdict'] = '无结果搜索基本持平，需人工判断是否值得继续投入。'
    return base


def build_demand_radar_outcomes(
    days=DEFAULT_WINDOW_DAYS, *, now=None, limit=20, window_days=DEFAULT_WINDOW_DAYS,
    statuses=None,
):
    """Summarise whether demand follow-up tasks actually closed their gaps.

    "days" selects the tasks being reported (how far back they were created)
    while "window_days" controls the measurement span on either side of each
    task. They are separate on purpose: an operator asking about a 90-day
    quarter should not be forced into a 90-day comparison window, which would
    swamp every task's signal with unrelated market drift.
    """
    now = now or timezone.now()
    task_queryset = DemandOpportunityTask.objects.filter(
        created_at__gte=now - timedelta(days=days), created_at__lte=now,
    ).select_related('assigned_to', 'created_by')
    if statuses:
        task_queryset = task_queryset.filter(status__in=statuses)
    tasks = list(task_queryset.order_by('-created_at')[:max(limit, 1)])

    rows = [build_task_outcome(task, now=now, window_days=window_days) for task in tasks]
    rows.sort(key=lambda row: (
        0 if row['outcome'] == 'pending' else 1,
        -(row['delta_points'] or 0),
        -row['signal_total'],
        row['title'],
    ))

    scored = [row for row in rows if row['outcome'] != 'pending']
    outcome_counts = {
        key: sum(row['outcome'] == key for row in scored)
        for key in ('converged', 'flat', 'diverged', 'insufficient')
    }
    converged = outcome_counts['converged']
    judged = sum(count for key, count in outcome_counts.items() if key != 'insufficient')
    # The hit rate is reported only over tasks with usable evidence. Counting
    # thin-sample tasks as failures would punish the metric for a topic nobody
    # searched for, and counting them as successes would flatter it.
    hit_rate = round(converged / judged * 100, 1) if judged else None
    supply_added = sum(max(0, row['supply_delta']) for row in scored)
    total_delta = [
        row['delta_points'] for row in scored if row['delta_points'] is not None
    ]
    median_delta = None
    if total_delta:
        ordered = sorted(total_delta)
        middle = len(ordered) // 2
        median_delta = (
            ordered[middle]
            if len(ordered) % 2
            else round((ordered[middle - 1] + ordered[middle]) / 2, 1)
        )

    return {
        'rows': rows,
        'period_days': days,
        'window_days': window_days,
        'has_data': bool(rows),
        'summary': {
            'task_count': len(rows),
            'pending_count': sum(row['outcome'] == 'pending' for row in rows),
            'converged_count': converged,
            'flat_count': outcome_counts['flat'],
            'diverged_count': outcome_counts['diverged'],
            'insufficient_count': outcome_counts['insufficient'],
            'judged_count': judged,
            'hit_rate': hit_rate,
            'median_delta_points': median_delta,
            'supply_added_total': supply_added,
        },
    }

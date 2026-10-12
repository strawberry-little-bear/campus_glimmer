"""Academic-calendar helpers shared by the dashboard, digests and pages."""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import (
    AcademicPhase, AcademicTerm, AcademicTermSnapshot, DemandPost, Item, Order, SearchQuery,
)


# Phases are resolved by day offset, so a term only needs its start and end date.
DEFAULT_PHASE_PLAN = (
    ('registration', 0, 13, '开学两周，教材和宿舍用品需求集中'),
    ('regular', 14, 97, '常规教学周，以日常兴趣和小件为主'),
    ('exam', 98, 111, '考试周，复习资料和陈旧资料需求上升'),
    ('graduation', 112, 125, '毕业季，清仓供给集中释放'),
)

PHASE_LABELS = dict(AcademicPhase.PHASE_CHOICES)

SUPPLY_TREND_LABELS = {
    'baseline': '基准阶段',
    'rising': '供给上升',
    'falling': '供给回落',
    'flat': '与上一阶段持平',
}


# 阶段计数在阶段结束之后就封闭了：Item / DemandPost / Order / SearchQuery 的
# created_at 全是 auto_now_add，仓库里没有任何写入路径会回头改它们。所以一个结束
# 日期已经过去的阶段，它的七个计数是定值，可以安全地读快照而不用重算。下面这组
# 常量描述的是「读哪一份」的切分，以及面板上怎么标。
SOURCE_LABELS = {
    'snapshot': '读快照',
    'live': '实时算',
}


def ensure_default_phases(term):
    """Fill a term with the default phase plan, keeping existing rows."""
    existing_offsets = set(term.phases.values_list('start_offset', flat=True))
    created = 0
    for phase_key, start_offset, end_offset, note in DEFAULT_PHASE_PLAN:
        if start_offset in existing_offsets:
            continue
        AcademicPhase.objects.create(
            term=term,
            phase=phase_key,
            start_offset=start_offset,
            end_offset=end_offset,
            note=note,
        )
        created += 1
    return created


def _term_for_day(day, terms=None):
    """Return the active term containing the day, preferring the latest start."""
    terms = terms if terms is not None else list(
        AcademicTerm.objects.filter(is_active=True).prefetch_related('phases')
    )
    matches = [term for term in terms if term.contains(day)]
    if not matches:
        return None, terms
    matches.sort(key=lambda term: (term.starts_on, term.name))
    return matches[-1], terms


def resolve_academic_phase(day=None, terms=None):
    """Return (term, phase) for the given date; either element may be None."""
    day = day or timezone.localdate()
    term, terms = _term_for_day(day, terms)
    if term is None:
        return None, None
    for phase in term.phases.all():
        if phase.contains(day):
            return term, phase
    return term, None


def academic_phase_context(day=None):
    """Return a serialisable summary of the phase covering today."""
    day = day or timezone.localdate()
    term, phase = resolve_academic_phase(day)
    if term is None:
        return {
            'has_term': False,
            'term_name': '',
            'phase_key': '',
            'phase_label': '',
            'phase_note': '',
            'progress_percent': 0,
            'days_left': None,
        }
    if phase is None:
        return {
            'has_term': True,
            'term_name': term.name,
            'phase_key': '',
            'phase_label': '',
            'phase_note': '',
            'progress_percent': 0,
            'days_left': max((term.ends_on - day).days, 0),
        }
    return {
        'has_term': True,
        'term_name': term.name,
        'phase_key': phase.phase,
        'phase_label': phase.get_phase_display(),
        'phase_note': phase.note,
        'progress_percent': phase.progress_percent(day),
        'days_left': max((phase.end_date - day).days, 0),
    }


def phase_label(phase_key):
    """Human-readable label for a stored phase key."""
    return PHASE_LABELS.get(phase_key, phase_key)


def _phase_metric(queryset, date_field, start, end):
    """Count rows whose date field falls inside [start, end]."""
    return queryset.filter(
        **{f'{date_field}__gte': start, f'{date_field}__lte': end}
    ).count()


def _annotate_phase_trends(phases):
    """Attach per-phase comparisons against the previous phase of the term.

    A single phase count says little on its own: the interesting question for
    operations is whether supply is accelerating as the term moves on. Each row
    therefore carries the previous phase's numbers and a plain-language trend
    so the template does not have to do arithmetic.
    """
    for index, row in enumerate(phases):
        previous = phases[index - 1] if index > 0 else None
        row['previous_phase_label'] = previous['phase_label'] if previous else ''
        row['previous_new_items'] = previous['new_items'] if previous else 0
        row['previous_new_demands'] = previous['new_demands'] if previous else 0
        row['previous_completed_orders'] = (
            previous['completed_orders'] if previous else 0
        )
        row['delta_new_items'] = row['new_items'] - row['previous_new_items']
        row['delta_new_demands'] = row['new_demands'] - row['previous_new_demands']
        row['delta_completed_orders'] = (
            row['completed_orders'] - row['previous_completed_orders']
        )
        if previous is None or not previous['new_items']:
            row['supply_trend'] = 'baseline'
        elif row['new_items'] > previous['new_items']:
            row['supply_trend'] = 'rising'
        elif row['new_items'] < previous['new_items']:
            row['supply_trend'] = 'falling'
        else:
            row['supply_trend'] = 'flat'
        row['supply_trend_label'] = SUPPLY_TREND_LABELS[row['supply_trend']]
        if not previous or not previous['new_demands']:
            row['demand_trend'] = 'baseline'
        elif row['new_demands'] > previous['new_demands']:
            row['demand_trend'] = 'rising'
        elif row['new_demands'] < previous['new_demands']:
            row['demand_trend'] = 'falling'
        else:
            row['demand_trend'] = 'flat'
        row['demand_trend_label'] = SUPPLY_TREND_LABELS[row['demand_trend']]
        row['zero_result_rate'] = (
            round(row['zero_result_searches'] / row['new_searches'] * 100, 1)
            if row['new_searches'] else 0
        )


def _phase_stat_payload(term, phase, *, now):
    """Aggregate the counters for one phase of one term."""
    start = phase.start_date
    end = phase.end_date
    start_at = timezone.make_aware(timezone.datetime.combine(start, timezone.datetime.min.time()))
    end_at = timezone.make_aware(timezone.datetime.combine(end, timezone.datetime.max.time()))
    window = (start_at, end_at)

    items = Item.objects.all()
    demands = DemandPost.objects.all()
    orders = Order.objects.all()
    searches = SearchQuery.objects.all()

    return {
        'term_id': term.pk,
        'term_name': term.name,
        'phase_key': phase.phase,
        'phase_label': phase.get_phase_display(),
        'phase_note': phase.note,
        'start_date': start,
        'end_date': end,
        'day_count': (end - start).days + 1,
        'is_current': start <= now.date() <= end,
        'new_items': _phase_metric(items, 'created_at', *window),
        'new_demands': _phase_metric(demands, 'created_at', *window),
        'new_orders': _phase_metric(orders, 'created_at', *window),
        'completed_orders': _phase_metric(
            orders.filter(status='completed'), 'created_at', *window,
        ),
        'new_searches': _phase_metric(searches, 'created_at', *window),
        'zero_result_searches': _phase_metric(
            searches.filter(result_count=0), 'created_at', *window,
        ),
    }


def _load_phase_snapshots(terms):
    """按学期取回快照行，键是 (term_id, phase_key, start_date, end_date)。

    键里带上 end_date 是有意的：改一次 `AcademicTerm.starts_on`，四个阶段的
    start_date 会整体平移，旧行不会被更新而是被旁置成孤儿行，新键下另插一批新
    行。用四元组当键，只有区间和当前 `AcademicPhase` 算出来完全一致的那一行
    才会被读走——孤儿行的 start_date 对不上，自然落不到这个键上。
    """
    if not terms:
        return {}
    term_ids = [term.pk for term in terms]
    rows = AcademicTermSnapshot.objects.filter(term_id__in=term_ids)
    return {
        (row.term_id, row.phase_key, row.start_date, row.end_date): row
        for row in rows
    }


def _snapshot_payload(snapshot, *, term, phase, today):
    """把一行快照摊成和 `_phase_stat_payload` 同形状的字典。

    两份数据的形状必须一致，模板和 CSV 导出才能不分辨来源地读同一套键。任何
    一处只有实时路径才算的派生量（`is_current`、`zero_result_rate`、趋势）都在
    这里补上，而不是留给调用方分支处理——包括 `source_label` 和 `source_note`：
    少一个键，模板就得为快照行单独写一条分支，而那正是这条读路径要避免的。
    """
    start = snapshot.start_date
    end = snapshot.end_date
    return {
        'term_id': term.pk,
        'term_name': term.name,
        'phase_key': snapshot.phase_key,
        'phase_label': snapshot.phase_label or phase.get_phase_display(),
        'phase_note': phase.note,
        'start_date': start,
        'end_date': end,
        'day_count': snapshot.day_count,
        'is_current': start <= today <= end,
        'new_items': snapshot.new_items,
        'new_demands': snapshot.new_demands,
        'new_orders': snapshot.new_orders,
        'completed_orders': snapshot.completed_orders,
        'new_searches': snapshot.new_searches,
        'zero_result_searches': snapshot.zero_result_searches,
        'source': 'snapshot',
        'source_label': SOURCE_LABELS['snapshot'],
        'source_note': (
            '已结束阶段，计数已封闭，读每周刷新的快照'
            if start < today
            else '阶段已结束，读快照'
        ),
    }


def _phase_row(term, phase, *, snapshots, now):
    """一条阶段的统计行：能读快照就读，否则实时算。

    判据不是 `refreshed_at`。上一轮已经查证 `update_or_create` 会把 `auto_now`
    字段无条件补进 `update_fields`，所以每次刷新都推进它，十个计数一个都没变也
    照推——拿它判断「这一行还新鲜吗」，会把一条两个月前就结束的阶段读成刚刚更
    新。真正的判据是这一行的计数是否已经封闭，那由两条保证：阶段的结束日期已经
    过去，且快照行的区间与当前 `AcademicPhase` 算出来的完全一致。

    进行中的阶段永远实时算。它每小时都在长出新商品和新搜索，快照最多是上周的，
    读出来会让运营看着一个正在变化的数字以为是新鲜的——而当前阶段恰好是看板上
    唯一有人盯着的那一行。
    """
    today = now.date()
    key = (term.pk, phase.phase, phase.start_date, phase.end_date)
    snapshot = snapshots.get(key)
    if snapshot is not None and phase.end_date < today:
        payload = _snapshot_payload(snapshot, term=term, phase=phase, today=today)
    else:
        payload = _phase_stat_payload(term, phase, now=now)
        payload['source'] = 'live'
        payload['source_label'] = SOURCE_LABELS['live']
        payload['source_note'] = (
            '实时算' if snapshot is None else '阶段进行中，快照最多是上周的'
        )
    return payload


def build_academic_calendar(*, day=None, limit_terms=3):
    """Build the phase breakdown for the current and nearby terms."""
    day = day or timezone.localdate()
    now = timezone.now()
    terms = list(
        AcademicTerm.objects.filter(is_active=True)
        .prefetch_related('phases')
        .order_by('-starts_on')[:limit_terms]
    )
    if not terms:
        return {
            'has_terms': False,
            'current': None,
            'context': academic_phase_context(day),
            'terms': [],
            'source': {
                'snapshot_row_total': 0,
                'live_row_total': 0,
                'row_total': 0,
                'snapshot_share': None,
            },
        }

    snapshots = _load_phase_snapshots(terms)

    term_rows = []
    snapshot_row_total = 0
    live_row_total = 0
    for term in terms:
        phases = []
        for phase in term.phases.all():
            phases.append(_phase_row(term, phase, snapshots=snapshots, now=now))
        phases.sort(key=lambda row: row['start_date'])
        _annotate_phase_trends(phases)
        if phases:
            for row in phases:
                if row['source'] == 'snapshot':
                    snapshot_row_total += 1
                else:
                    live_row_total += 1
            term_rows.append({
                'term': term,
                'term_name': term.name,
                'kind_label': term.get_kind_display(),
                'starts_on': term.starts_on,
                'ends_on': term.ends_on,
                'is_current': term.contains(day),
                'phases': phases,
            })

    current_term, current_phase = resolve_academic_phase(day)
    row_total = snapshot_row_total + live_row_total
    return {
        'has_terms': True,
        'current': {
            'term': current_term,
            'phase': current_phase,
            'context': academic_phase_context(day),
        },
        'context': academic_phase_context(day),
        'terms': term_rows,
        'source': {
            'snapshot_row_total': snapshot_row_total,
            'live_row_total': live_row_total,
            'row_total': row_total,
            'snapshot_share': (
                round(snapshot_row_total / row_total * 100, 1)
                if snapshot_row_total else None
            ),
        },
    }


def refresh_academic_term_snapshots(*, term=None, now=None):
    """Persist one snapshot row per phase so the dashboard can read cache."""
    now = now or timezone.now()
    day = timezone.localdate(now)
    terms = [term] if term is not None else list(
        AcademicTerm.objects.filter(is_active=True).prefetch_related('phases')
    )
    updated = 0
    for target_term in terms:
        for phase in target_term.phases.all():
            payload = _phase_stat_payload(target_term, phase, now=now)
            AcademicTermSnapshot.objects.update_or_create(
                term=target_term,
                phase_key=payload['phase_key'],
                start_date=payload['start_date'],
                defaults={
                    'phase_label': payload['phase_label'],
                    'end_date': payload['end_date'],
                    'day_count': payload['day_count'],
                    'new_items': payload['new_items'],
                    'new_demands': payload['new_demands'],
                    'new_orders': payload['new_orders'],
                    'completed_orders': payload['completed_orders'],
                    'new_searches': payload['new_searches'],
                    'zero_result_searches': payload['zero_result_searches'],
                },
            )
            updated += 1
    return updated


def academic_phase_summary_line(*, day=None):
    """One-line phase description used by digest notifications."""
    context = academic_phase_context(day)
    if not context['has_term'] or not context['phase_key']:
        return ''
    note = context['phase_note']
    if note:
        return f'当前处于{context["term_name"]}{context["phase_label"]}：{note}。'
    return f'当前处于{context["term_name"]}{context["phase_label"]}。'

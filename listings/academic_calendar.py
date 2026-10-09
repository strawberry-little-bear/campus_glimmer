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
        }

    term_rows = []
    for term in terms:
        phases = []
        for phase in term.phases.all():
            phases.append(_phase_stat_payload(term, phase, now=now))
        phases.sort(key=lambda row: row['start_date'])
        _annotate_phase_trends(phases)
        if phases:
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
    return {
        'has_terms': True,
        'current': {
            'term': current_term,
            'phase': current_phase,
            'context': academic_phase_context(day),
        },
        'context': academic_phase_context(day),
        'terms': term_rows,
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

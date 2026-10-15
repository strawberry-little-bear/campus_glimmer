# -*- coding: utf-8 -*-
"""Read-only supply preparation cues before an academic phase changes.

The academic calendar already explains the current phase and demand radar can
compare the same phase across terms. Neither answers the forward-looking
question: which categories should be prepared before the next phase starts?

This module keeps that question narrow and observable. When the next phase
starts within fourteen days it reads the same phase from the most recent closed
term (preferring the same term kind), groups explicit demand signals by
category, and compares the current seven-day listing flow. It never writes an
operations task, notifies anybody, or turns history into a target.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import AcademicPhase, AcademicTerm, Category, DemandPost, Item, SearchQuery


PHASE_LOOKAHEAD_DAYS = 14
PREPARATION_WINDOW_DAYS = 7
MIN_GUIDANCE_SEARCHES = 3
MIN_GUIDANCE_ZERO_RESULTS = 2
PACE_THRESHOLD_PERCENT = 80
PAGE_ROW_LIMIT = 10

UNCLASSIFIED_LABEL = '未设置分类搜索'
GUIDANCE_LABELS = {
    True: '值得预热',
    False: '继续观察',
}
PACE_LABELS = {
    'on_pace': '接近基准',
    'slow': '预热偏慢',
    'no_new_items': '尚未新增',
    'no_baseline': '无供给基准',
    'observe': '不判节奏',
}


def _day_bounds(start, end):
    """Return inclusive local-day bounds for a date interval."""
    return (
        timezone.make_aware(timezone.datetime.combine(start, timezone.datetime.min.time())),
        timezone.make_aware(timezone.datetime.combine(end, timezone.datetime.max.time())),
    )


def _upcoming_phase(day):
    """Find the earliest enabled phase starting after today within the horizon."""
    terms = list(
        AcademicTerm.objects.filter(is_active=True).prefetch_related('phases')
    )
    candidates = []
    for term in terms:
        for phase in term.phases.all():
            days_until = (phase.start_date - day).days
            if 0 < days_until <= PHASE_LOOKAHEAD_DAYS:
                candidates.append((phase.start_date, term.starts_on, term.name, term, phase))
    if not candidates:
        return None, terms
    candidates.sort(key=lambda row: row[:3])
    return candidates[0][3:], terms


def _benchmark_phase(upcoming, *, day):
    """Find the latest closed phase with the same key in another active term."""
    candidates = [
        phase for phase in AcademicPhase.objects.select_related('term').filter(
            phase=upcoming.phase,
            term__is_active=True,
        ).exclude(term_id=upcoming.term_id)
        if phase.end_date < day
    ]
    same_kind = [phase for phase in candidates if phase.term.kind == upcoming.term.kind]
    pool, kind_matched = (same_kind, True) if same_kind else (candidates, False)
    if not pool:
        return None, kind_matched, len(candidates)
    pool.sort(key=lambda phase: (phase.term.starts_on, phase.start_date), reverse=True)
    return pool[0], kind_matched, len(candidates)


def _search_groups(start_at, end_at):
    rows = SearchQuery.objects.filter(
        created_at__gte=start_at, created_at__lte=end_at,
    ).values('category_id').annotate(
        search_count=Count('id'),
        zero_result_count=Count('id', filter=Q(result_count=0)),
    )
    return {row['category_id']: row for row in rows}


def _simple_groups(queryset, date_field, start_at, end_at):
    rows = queryset.filter(
        **{f'{date_field}__gte': start_at, f'{date_field}__lte': end_at},
    ).values('category_id').annotate(total=Count('id'))
    return {row['category_id']: row['total'] for row in rows}


def _category_names(category_ids):
    names = dict(
        Category.objects.filter(id__in=category_ids).values_list('id', 'name')
    )
    return {
        category_id: names.get(category_id, f'已删除分类 #{category_id}')
        if category_id is not None else UNCLASSIFIED_LABEL
        for category_id in category_ids
    }


def _share(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _base_payload(day):
    return {
        'has_upcoming_phase': False,
        'has_benchmark': False,
        'today': day,
        'lookahead_days': PHASE_LOOKAHEAD_DAYS,
        'preparation_days': PREPARATION_WINDOW_DAYS,
        'min_guidance_searches': MIN_GUIDANCE_SEARCHES,
        'min_guidance_zero_results': MIN_GUIDANCE_ZERO_RESULTS,
        'pace_threshold_percent': PACE_THRESHOLD_PERCENT,
        'page_row_limit': PAGE_ROW_LIMIT,
        'empty_reason': f'{PHASE_LOOKAHEAD_DAYS} 天内没有即将开始的启用阶段。',
        'summary': '当前不在阶段切换预热窗口内。',
    }


def build_academic_phase_supply_preparation(*, days=30, now=None):
    """Build the phase-transition supply cue panel; ``days`` is accepted only for API parity."""
    now = now or timezone.now()
    day = timezone.localdate(now)
    payload = _base_payload(day)
    payload['period_days'] = days

    selected, terms = _upcoming_phase(day)
    if selected is None:
        payload['enabled_term_total'] = len(terms)
        return payload
    upcoming_term, upcoming_phase = selected
    days_until = (upcoming_phase.start_date - day).days
    payload.update({
        'has_upcoming_phase': True,
        'upcoming_term': upcoming_term,
        'upcoming_phase': upcoming_phase,
        'upcoming_term_name': upcoming_term.name,
        'upcoming_phase_label': upcoming_phase.get_phase_display(),
        'upcoming_phase_note': upcoming_phase.note,
        'upcoming_start_date': upcoming_phase.start_date,
        'upcoming_end_date': upcoming_phase.end_date,
        'days_until': days_until,
        'empty_reason': '',
    })

    benchmark, kind_matched, benchmark_candidate_total = _benchmark_phase(
        upcoming_phase, day=day,
    )
    if benchmark is None:
        payload.update({
            'has_benchmark': False,
            'benchmark_candidate_total': benchmark_candidate_total,
            'empty_reason': '没有找到已结束且同阶段的其它启用学期，先不造基线。',
            'summary': '下一阶段已进入预热窗口，但缺少可比较的历史阶段。',
        })
        return payload

    benchmark_start_at, benchmark_end_at = _day_bounds(
        benchmark.start_date, benchmark.end_date,
    )
    lead_start_at = timezone.make_aware(
        timezone.datetime.combine(
            day - timedelta(days=PREPARATION_WINDOW_DAYS - 1),
            timezone.datetime.min.time(),
        )
    )

    benchmark_searches = _search_groups(benchmark_start_at, benchmark_end_at)
    benchmark_demands = _simple_groups(
        DemandPost.objects.all(), 'created_at', benchmark_start_at, benchmark_end_at,
    )
    benchmark_items = _simple_groups(
        Item.objects.all(), 'created_at', benchmark_start_at, benchmark_end_at,
    )
    lead_demands = _simple_groups(
        DemandPost.objects.all(), 'created_at', lead_start_at, now,
    )
    lead_items = _simple_groups(
        Item.objects.all(), 'created_at', lead_start_at, now,
    )
    available_items = dict(
        Item.objects.available(now=now).values('category_id').annotate(
            total=Count('id'),
        ).values_list('category_id', 'total')
    )

    category_ids = set(
        benchmark_searches | benchmark_demands | benchmark_items
    )
    category_names = _category_names(category_ids)
    phase_days = (benchmark.end_date - benchmark.start_date).days + 1
    rows = []
    for category_id in category_ids:
        search = benchmark_searches.get(category_id, {})
        search_count = search.get('search_count', 0)
        zero_count = search.get('zero_result_count', 0)
        benchmark_item_count = benchmark_items.get(category_id, 0)
        current_item_count = lead_items.get(category_id, 0)
        lead_baseline = (
            round(benchmark_item_count / phase_days * PREPARATION_WINDOW_DAYS, 1)
            if phase_days else None
        )
        is_candidate = (
            search_count >= MIN_GUIDANCE_SEARCHES
            and zero_count >= MIN_GUIDANCE_ZERO_RESULTS
        )
        pace_percent = (
            round(current_item_count / lead_baseline * 100, 1)
            if is_candidate and lead_baseline else None
        )
        if not is_candidate:
            pace_key = 'observe'
        elif not lead_baseline:
            pace_key = 'no_baseline'
        elif current_item_count >= lead_baseline * PACE_THRESHOLD_PERCENT / 100:
            pace_key = 'on_pace'
        elif current_item_count:
            pace_key = 'slow'
        else:
            pace_key = 'no_new_items'
        rows.append({
            'category_id': category_id,
            'category_name': category_names[category_id],
            'search_count': search_count,
            'zero_result_count': zero_count,
            'zero_result_share': _share(zero_count, search_count),
            'benchmark_demand_count': benchmark_demands.get(category_id, 0),
            'benchmark_item_count': benchmark_item_count,
            'lead_baseline_items': lead_baseline,
            'lead_demand_count': lead_demands.get(category_id, 0),
            'lead_item_count': current_item_count,
            'available_item_count': available_items.get(category_id, 0),
            'is_guidance_candidate': is_candidate,
            'guidance_label': GUIDANCE_LABELS[is_candidate],
            'pace_key': pace_key,
            'pace_label': PACE_LABELS[pace_key],
            'pace_percent': pace_percent,
        })

    rows.sort(key=lambda row: (
        -row['zero_result_count'], -row['search_count'], row['category_name'],
    ))
    guidance_rows = [row for row in rows if row['is_guidance_candidate']]
    slow_rows = [
        row for row in guidance_rows
        if row['pace_key'] in {'slow', 'no_new_items'}
    ]
    payload.update({
        'has_benchmark': True,
        'benchmark_term': benchmark.term,
        'benchmark_phase': benchmark,
        'benchmark_term_name': benchmark.term.name,
        'benchmark_phase_label': benchmark.get_phase_display(),
        'benchmark_start_date': benchmark.start_date,
        'benchmark_end_date': benchmark.end_date,
        'benchmark_day_count': phase_days,
        'benchmark_kind_matched': kind_matched,
        'benchmark_candidate_total': benchmark_candidate_total,
        'lead_start_date': day - timedelta(days=PREPARATION_WINDOW_DAYS - 1),
        'lead_end_date': day,
        'benchmark_search_total': sum(row['search_count'] for row in rows),
        'benchmark_zero_result_total': sum(row['zero_result_count'] for row in rows),
        'category_total': len(rows),
        'guidance_total': len(guidance_rows),
        'slow_total': len(slow_rows),
        'visible_stock_total': sum(row['available_item_count'] for row in rows),
        'rows': rows,
        'page_rows': rows[:PAGE_ROW_LIMIT],
        'empty_reason': '',
        'summary': (
            f'{upcoming_phase.get_phase_display()}还有 {days_until} 天开始，'
            f'先看{benchmark.term.name}同阶段的分类需求形状。'
            if guidance_rows else
            f'{upcoming_phase.get_phase_display()}还有 {days_until} 天开始，'
            '历史同阶段暂无足够强的分类预热信号。'
        ),
    })
    return payload

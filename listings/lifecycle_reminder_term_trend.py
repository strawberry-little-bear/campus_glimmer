# -*- coding: utf-8 -*-
"""Compare lifecycle reminders across the same academic phase, term over term.

The adjacent-period trend answers a tuning question: is this month different from
the month before it? A campus marketplace also has a calendar question that this
cannot answer. Registration, regular teaching, examination, and graduation weeks
have different listing mixes and seller behaviour, so comparing two neighbouring
months can mistake the calendar for a change in reminder effectiveness.

This module therefore groups event-ledger reminders by academic phase and
compares the newest closed cell with the same phase in the previous term that
has one. The comparison is deliberately narrower than the single-period action
panel: only terminal order events carry a cross-term direction. Refreshes are
reported by the current action panel but are not preserved as an event stream;
``Item.last_refreshed_at`` can be overwritten later, which would make old terms
look progressively worse for reasons that have nothing to do with reminders.
"""

from datetime import timedelta

from django.utils import timezone

from .academic_calendar import _term_for_day
from .demand_radar_phase_trend import DEFAULT_LOOKBACK_DAYS, PHASE_ORDER
from .lifecycle_reminder_effect import EFFECT_WINDOW_DAYS, TERMINAL_ORDER_STATUSES
from .models import AcademicTerm, LifecycleReminderEvent, OrderEvent


# A direction needs this many event-backed items in both phase cells. Smaller
# cells still report every volume figure, but a percentage that moves in whole
# large steps is not evidence of a term-over-term change.
MIN_PHASE_ITEMS = 12

# A gap of this many percentage points between the same phase in neighbouring
# terms is worth a human look. It is not a ranking and triggers no action.
PHASE_GAP_POINTS = 15

DIRECTION_LABELS = {
    'rising': '较上学期上升',
    'falling': '较上学期下降',
    'flat': '较上学期持平',
}

SOURCE_NOTE = (
    '分母只读生命周期提醒事件台账；旧冷却时间戳只保存最新一次，不能还原跨学期历史。'
)

REFRESH_NOTE = (
    '跨学期方向只看订单终态动作。刷新时间会被下一次刷新覆盖，在没有刷新流水前，'
    '把它放进纵向对比会把数据留存问题误读成效果下降。'
)


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _phase_slot(term, day):
    """Resolve one local date to a configured academic phase."""
    if term is None:
        return None
    for phase in term.phases.all():
        if phase.contains(day):
            return phase
    return None


def _file_events_by_phase(events, *, terms, now):
    """File event-ledger rows into phase cells and explicit coverage gaps.

    Events outside every active term and events inside a term but outside every
    configured phase are not errors in the reminder ledger. They are counted
    separately rather than dropped: hiding them would quietly shrink the panel's
    coverage story.
    """
    cells = {}
    no_term_events = 0
    unassigned_events = 0
    open_cell_events = 0
    today = timezone.localdate(now)

    for event in events:
        day = timezone.localdate(event.sent_at)
        term, _terms = _term_for_day(day, terms)
        phase = _phase_slot(term, day)
        if term is None:
            no_term_events += 1
            continue
        if phase is None:
            unassigned_events += 1
            continue

        slot = (term.pk, phase.phase)
        cell = cells.setdefault(slot, {
            'term': term,
            'phase': phase,
            'events': [],
        })
        cell['events'].append(event)
        # The whole phase must be closed, not merely the first reminder in it.
        # Otherwise the newest cell can have a half-empty observation window
        # while the older one has had years to accumulate terminal events.
        if today <= phase.end_date + timedelta(days=EFFECT_WINDOW_DAYS):
            open_cell_events += 1

    return cells, no_term_events, unassigned_events, open_cell_events


def _observations(events):
    """Collapse repeated reminder kinds for one item inside one phase cell.

    Every event remains visible in ``event_count``. For the outcome denominator
    the latest reminder is the observation start, because an earlier nudge does
    not have a separate stable denominator once the same item was reminded again.
    """
    grouped = {}
    for event in events:
        row = grouped.setdefault(event.item_id, {
            'item_id': event.item_id,
            'seller_ids': set(),
            'reminded_at': event.sent_at,
        })
        row['seller_ids'].add(event.seller_id)
        if event.sent_at > row['reminded_at']:
            row['reminded_at'] = event.sent_at
    return list(grouped.values())


def _terminal_times(item_ids, floor):
    """Terminal order events keyed by item, from a common lower bound."""
    if not item_ids:
        return {}
    rows = OrderEvent.objects.filter(
        order__item_id__in=item_ids,
        to_status__in=TERMINAL_ORDER_STATUSES,
        created_at__gte=floor,
    ).values('order__item_id', 'created_at')
    times = {}
    for row in rows:
        times.setdefault(row['order__item_id'], []).append(row['created_at'])
    return times


def _build_cell(cell, *, terminal_times):
    """Build one reportable phase-term cell from event-ledger observations."""
    observations = _observations(cell['events'])
    item_ids = [row['item_id'] for row in observations]
    terminal_count = 0
    for row in observations:
        reminded_at = row['reminded_at']
        window_end = reminded_at + timedelta(days=EFFECT_WINDOW_DAYS)
        terminal_count += any(
            reminded_at < moment <= window_end
            for moment in terminal_times.get(row['item_id'], ())
        )

    event_count = len(cell['events'])
    item_count = len(observations)
    repeated_events = max(event_count - item_count, 0)
    phase = cell['phase']
    term = cell['term']
    return {
        'term_name': term.name,
        'term_starts_on': term.starts_on,
        'phase_key': phase.phase,
        'phase_label': phase.get_phase_display(),
        'start_date': phase.start_date,
        'end_date': phase.end_date,
        'event_count': event_count,
        'item_count': item_count,
        'seller_count': len({
            event.seller_id for event in cell['events']
        }),
        'repeat_reminder_rate': _rate(repeated_events, event_count),
        'terminal_action_count': terminal_count,
        'terminal_action_rate': _rate(terminal_count, item_count),
        'has_sample': item_count >= MIN_PHASE_ITEMS,
    }


def _cell_sort_key(cell):
    return (cell['term_starts_on'], cell['start_date'], cell['phase_key'])


def _build_phase_groups(cells):
    """Pair the newest closed cell of each phase with its previous term."""
    by_phase = {}
    for cell in cells:
        by_phase.setdefault(cell['phase_key'], []).append(cell)

    groups = []
    for phase_key in PHASE_ORDER:
        phase_cells = by_phase.get(phase_key)
        if not phase_cells:
            continue
        phase_cells = sorted(phase_cells, key=_cell_sort_key, reverse=True)
        current = phase_cells[0]
        previous = phase_cells[1] if len(phase_cells) > 1 else None
        has_sample = bool(
            current['has_sample'] and previous is not None and previous['has_sample']
        )
        gap_points = None
        direction = None
        if previous is not None:
            gap_points = round(
                current['terminal_action_rate'] - previous['terminal_action_rate'], 1,
            )
            if has_sample:
                if gap_points >= PHASE_GAP_POINTS:
                    direction = 'rising'
                elif gap_points <= -PHASE_GAP_POINTS:
                    direction = 'falling'
                else:
                    direction = 'flat'

        groups.append({
            'key': phase_key,
            'label': current['phase_label'],
            'current': current,
            'previous': previous,
            'cells': phase_cells,
            'gap_points': gap_points,
            'direction': direction,
            'direction_label': DIRECTION_LABELS.get(direction, '') if direction else '',
            'has_sample': has_sample,
            'has_baseline': previous is not None,
            'is_flagged': bool(
                has_sample
                and gap_points is not None
                and abs(gap_points) >= PHASE_GAP_POINTS
            ),
        })
    return groups


def _coverage_sentence(*, no_term_events, unassigned_events, open_cell_events):
    parts = []
    if no_term_events:
        parts.append(f'{no_term_events} 条事件落在无学期日历')
    if unassigned_events:
        parts.append(f'{unassigned_events} 条事件落在学期内未划分阶段')
    if open_cell_events:
        parts.append(f'{open_cell_events} 条事件所在阶段尚未满观察窗口')
    if not parts:
        return ''
    return '另有' + '、'.join(parts) + '，已单独计数，不参与同学期对比。'


def _summary(*, event_count, closed_cell_count, groups, coverage):
    if not event_count:
        return '回看期内没有生命周期提醒事件，跨学期对比需要先有事件台账。'
    if not closed_cell_count:
        return (
            f'回看期内有 {event_count} 条提醒事件，但还没有同学期阶段满 '
            f'{EFFECT_WINDOW_DAYS} 天观察窗口。' + coverage
        )

    comparable = [group for group in groups if group['has_baseline']]
    if not comparable:
        return (
            '事件已经能按同学期阶段归组，但每个阶段都只有最新一个格子，'
            '暂不虚构上一学期基线。' + coverage
        )
    if not any(group['has_sample'] for group in comparable):
        return (
            f'相邻同学期阶段中至少一侧事件口径商品数不足 {MIN_PHASE_ITEMS} 件，'
            '数字照报，方向扣下。' + coverage
        )

    flagged = [group for group in comparable if group['is_flagged']]
    if not flagged:
        return (
            f'相邻同学期阶段的终态转化率相差都在 {PHASE_GAP_POINTS} 个百分点以内，'
            '暂看不出需要回看的跨学期漂移。' + coverage
        )
    parts = []
    for group in flagged[:3]:
        word = '高于' if group['gap_points'] > 0 else '低于'
        parts.append(
            f"{group['label']}同学期上一学期 {abs(group['gap_points'])} 个百分点"
        )
    return (
        f"有 {len(flagged)} 个同学期阶段与上一学期相差达到 {PHASE_GAP_POINTS} 个百分点："
        + '，'.join(parts)
        + '。标记只代表值得回看，不是排名或因果结论。' + coverage
    )


def build_lifecycle_reminder_term_trend(days=DEFAULT_LOOKBACK_DAYS, *, now=None):
    """Build the same-phase, previous-term comparison over a fixed lookback.

    ``days`` is the panel's own lookback rather than the dashboard period
    selector. A seven- or thirty-day selector cannot contain two examination
    weeks from neighbouring terms, so allowing it to shrink this comparison would
    turn a calendar panel back into a neighbouring-period panel by accident.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start = now - timedelta(days=days)
    terms = list(AcademicTerm.objects.filter(is_active=True).prefetch_related('phases'))
    events = list(
        LifecycleReminderEvent.objects.filter(
            sent_at__gte=start, sent_at__lte=now,
        ).select_related('item', 'seller')
    )

    cells_by_slot, no_term_events, unassigned_events, open_cell_events = (
        _file_events_by_phase(events, terms=terms, now=now)
    )
    closed_cells = []
    today = timezone.localdate(now)
    for cell in cells_by_slot.values():
        phase = cell['phase']
        if today > phase.end_date + timedelta(days=EFFECT_WINDOW_DAYS):
            closed_cells.append(cell)

    floor = min((event.sent_at for event in events), default=now)
    terminal_times = _terminal_times(
        [event.item_id for event in events], floor,
    )
    cells = [
        _build_cell(cell, terminal_times=terminal_times)
        for cell in closed_cells
    ]
    phase_groups = _build_phase_groups(cells)
    coverage = _coverage_sentence(
        no_term_events=no_term_events,
        unassigned_events=unassigned_events,
        open_cell_events=open_cell_events,
    )

    return {
        'days': days,
        'window_days': EFFECT_WINDOW_DAYS,
        'min_items': MIN_PHASE_ITEMS,
        'gap_points': PHASE_GAP_POINTS,
        'phase_groups': phase_groups,
        'no_term_events': no_term_events,
        'unassigned_events': unassigned_events,
        'open_cell_events': open_cell_events,
        'closed_cell_count': len(cells),
        'event_count': len(events),
        'has_data': bool(events),
        'has_sample': any(
            group['has_sample'] for group in phase_groups if group['has_baseline']
        ),
        'summary': _summary(
            event_count=len(events),
            closed_cell_count=len(cells),
            groups=phase_groups,
            coverage=coverage,
        ),
        'source_note': SOURCE_NOTE,
        'refresh_note': REFRESH_NOTE,
    }


def build_lifecycle_reminder_term_trend_dashboard(*, days):
    """Registry adapter: keep this panel on its own cross-term lookback."""
    return build_lifecycle_reminder_term_trend(days=DEFAULT_LOOKBACK_DAYS)

# -*- coding: utf-8 -*-
"""Cross the borrow risk stratification with the academic calendar.

`borrow_risk` answers "which categories of item end up at escalation level
three more often" and `academic_calendar` answers "what does the term look like
around each phase". Neither answers the question those two answers make
possible. A category that reaches level three on a fifth of its borrows is a
very different fact if those borrows all landed in the two weeks of exam phase
than if they are spread across a fourteen-week term. In the first case the item
may be perfectly ordinary and the timing unlucky; in the second the category is
genuinely harder to get back. Read as a single number the two look identical.

So this module reads the same borrows `borrow_risk` reads - from the order side,
never-escalated borrows included, so the denominator is the whole population -
and resolves each one into a term phase. The cross it reports is phase x
category: per phase, how many borrows there were, how many reached level three
and how long the closed ones took, with a per-category breakdown inside the
phase.

Four boundaries do the real work here.

Phases are not the same length. The default plan gives registration fourteen
days, regular eighty-four, exam fourteen and graduation fourteen. Comparing raw
counts across them means the eighty-four-day phase always wins on volume, which
tells the reader nothing about risk. Every phase row therefore also carries a
per-day figure, and the summary sentence leads with intensity per day rather
than with a total. A share is still computed against the phase's own borrows,
because that is the question "of what happened during exam week, how much went
wrong"; the per-day figure exists so a long phase is not mistaken for a busy
one.

The per-day figure divides by the days the phase actually produced borrows on,
not by the phase's nominal length. Dividing by nominal length would punish a
reporting window that clips a phase: a fourteen-day exam phase sampled for its
last five days would show nearly three times the real daily rate, which is the
kind of number that looks like a finding and is an artefact of the window.

The cross is sparse and may not pretend otherwise. A phase crossed with a
category produces far more cells than either single view, and campus borrow
volumes put only a handful of borrows in most of them. The floor is
`borrow_risk.MIN_SAMPLE_SIZE` and it is not lowered for the cross: a cell of
this table has thinner evidence than any single row of either parent view, and
a number too thin to conclude from on the category table cannot become sturdy
just because it was crossed with a phase. Cells below the floor are listed,
marked, and excluded from every direction.

Borrows the calendar cannot place are counted, never dropped. A borrow created
before any term was configured, or in a gap between a term's phases, is still a
borrow that happened. Silently discarding it would shrink the denominator and
make every phase's share quietly larger, so the two synthetic buckets are
reported as rows in their own right and their total is stated in the summary.

Nothing here is a policy. The module reads Order, BorrowReturnEscalation,
AcademicTerm and AcademicPhase, adds no table, writes no constant and sends no
message. In particular it never touches the escalation ladder: if exam week
looks risky that is a fact for a human to reason about, not a licence to chase
borrowers sooner. Escalation timing is a decision about how long a classmate's
item may be missing, and it must not move because one phase had a bad
fortnight.
"""

from datetime import timedelta

from django.utils import timezone

from .academic_calendar import _term_for_day
from .borrow_risk import MIN_SAMPLE_SIZE, _fetch_borrows, _rate
from .models import AcademicTerm, Order


# Phases in the order the term runs, so a reader scans the table in calendar
# order rather than in whatever order the database happened to return. The
# labels come from the academic calendar's own choices rather than being
# duplicated here: the same phase must not be called one thing on the calendar
# panel and another thing on this one.
PHASE_ORDER = ('registration', 'regular', 'exam', 'graduation', 'holiday')

# A borrow that falls inside a term but in a gap between its phases. That is a
# configuration state rather than a data problem - an operator may deliberately
# leave a stretch of term unlabelled - so it gets a row instead of a warning.
UNASSIGNED_PHASE_KEY = 'unassigned'
UNASSIGNED_PHASE_LABEL = '未划分阶段'

# A borrow created before any term was configured, or after the last one ends.
NO_TERM_KEY = 'no_term'
NO_TERM_LABEL = '无学期日历'

# Keys that describe the calendar's coverage rather than a rhythm. They are
# reported but never compete for the "busiest phase" sentence.
SYNTHETIC_PHASE_KEYS = (NO_TERM_KEY, UNASSIGNED_PHASE_KEY)

DEFAULT_LOOKBACK_DAYS = 120


def _borrow_dates(borrows):
    """Map each borrow id to the local date it was created on.

    ``borrow_risk._fetch_borrows`` selects the fields its own stratification
    needs, and the creation date is not one of them - a category table has no
    use for it. Reading the dates in one extra query keeps that module
    untouched: widening its ``values()`` to serve a second consumer would mean
    every future change to either table has to be checked against the other.

    One extra query against the same window is cheaper than that coupling, and
    the window is already bounded by the same start and end instants.
    """
    if not borrows:
        return {}
    ids = [borrow['id'] for borrow in borrows]
    dates = {}
    for row in (
        Order.objects.filter(id__in=ids)
        .values_list('id', 'created_at')
    ):
        dates[row[0]] = timezone.localdate(row[1])
    return dates


def _phase_key_for_day(day, terms):
    """Resolve a date into (phase_key, phase_label, term_name).

    Resolution goes through the academic calendar's own term lookup so the two
    modules cannot disagree about which term contains a date. A synthetic key is
    returned rather than None when the day is outside every term or in a gap,
    because a borrow that cannot be placed still happened and has to be counted
    somewhere the reader can see.
    """
    term, terms = _term_for_day(day, terms)
    if term is None:
        return NO_TERM_KEY, NO_TERM_LABEL, ''
    for phase in term.phases.all():
        if phase.contains(day):
            return phase.phase, phase.get_phase_display(), term.name
    return UNASSIGNED_PHASE_KEY, UNASSIGNED_PHASE_LABEL, term.name


def _phase_sort_key(row):
    """Calendar order, with the synthetic buckets at the end."""
    if row['phase_key'] in PHASE_ORDER:
        return (0, PHASE_ORDER.index(row['phase_key']), row['phase_key'])
    return (1, 0, row['phase_key'])


def _new_group(key, label, term_name):
    return {
        'key': key,
        'phase_key': key,
        'phase_label': label,
        'term_name': term_name,
        'borrow_count': 0,
        'escalated_count': 0,
        'level_three_count': 0,
        'resolved_count': 0,
        'open_count': 0,
        'closed_durations': [],
        'overdue_days': [],
        'category_counts': {},
        'category_level_three': {},
    }


def _phase_rows(borrows, escalations, *, terms, now, dates):
    """Aggregate the borrows once per phase, with a per-category inner count.

    One pass rather than one pass per phase, so the phase totals and the
    category breakdown inside each phase cannot drift apart. They are read side
    by side, and a reader who notices the inner counts not adding up to the
    total will stop trusting both.
    """
    groups = {}
    for borrow in borrows:
        day = dates[borrow['id']]
        phase_key, phase_label, term_name = _phase_key_for_day(day, terms)
        group = groups.get(phase_key)
        if group is None:
            group = groups[phase_key] = _new_group(phase_key, phase_label, term_name)
        group['borrow_count'] += 1

        category = borrow['item__category__name'] or '未分类'
        group['category_counts'][category] = group['category_counts'].get(category, 0) + 1

        escalation = escalations.get(borrow['id'])
        if not escalation:
            continue
        group['escalated_count'] += 1
        if escalation['escalation_level'] >= 3:
            group['level_three_count'] += 1
            group['category_level_three'][category] = (
                group['category_level_three'].get(category, 0) + 1
            )
        resolved_at = escalation['resolved_at']
        if resolved_at:
            group['resolved_count'] += 1
            # Measured from the rung that actually happened, the same way
            # borrow_risk does it: an order that sat at level one for a week is
            # not an order the operator spent a week on.
            started_at = escalation['last_escalated_at'] or borrow['return_due_at']
            if started_at:
                group['closed_durations'].append(
                    max((resolved_at - started_at).total_seconds(), 0.0) / 86400
                )
        else:
            group['open_count'] += 1
            if borrow['return_due_at']:
                group['overdue_days'].append(
                    max((now - borrow['return_due_at']).total_seconds(), 0.0) / 86400
                )

    rows = [_finalise_group(group) for group in groups.values()]
    rows.sort(key=_phase_sort_key)
    return rows


def _finalise_group(group):
    """Turn one accumulating group into the row shape the template reads."""
    borrow_count = group['borrow_count']
    durations = group['closed_durations']

    category_rows = []
    for name, count in group['category_counts'].items():
        level_three = group['category_level_three'].get(name, 0)
        category_rows.append({
            'key': name,
            'category_label': name,
            'borrow_count': count,
            'level_three_count': level_three,
            # Share inside the phase, not inside the category. The reader is
            # looking at one phase and asking which of its categories went
            # wrong; computing it the other way would need a per-category
            # denominator this table does not show, and would answer a question
            # about the category rather than about the phase.
            'share_in_phase': _rate(count, borrow_count),
            'level_three_rate': _rate(level_three, count),
            'is_small_sample': count < MIN_SAMPLE_SIZE,
        })
    category_rows.sort(key=lambda row: (
        -(row['level_three_rate'] or 0),
        -row['level_three_count'],
        -row['borrow_count'],
        row['key'],
    ))

    return {
        'key': group['key'],
        'phase_key': group['phase_key'],
        'phase_label': group['phase_label'],
        'term_name': group['term_name'],
        'borrow_count': borrow_count,
        'escalated_count': group['escalated_count'],
        'level_three_count': group['level_three_count'],
        'resolved_count': group['resolved_count'],
        'open_count': group['open_count'],
        'escalation_rate': _rate(group['escalated_count'], borrow_count),
        'level_three_rate': _rate(group['level_three_count'], borrow_count),
        'active_days': 0,
        'share_in_window': None,
        'borrow_per_day': None,
        'level_three_per_day': None,
        'average_close_days': (
            round(sum(durations) / len(durations), 1) if durations else None
        ),
        'max_overdue_days': (
            round(max(group['overdue_days']), 1) if group['overdue_days'] else None
        ),
        'is_small_sample': borrow_count < MIN_SAMPLE_SIZE,
        'category_rows': category_rows,
    }


def _phase_key_of_borrow(borrow, dates, terms):
    """The phase key one borrow landed in, resolved once more for the day count.

    Deliberately recomputed instead of threaded through: this helper stays a
    pure function of the borrow list it is handed, which keeps the intensity
    pass independent of the aggregation pass and testable on its own.
    """
    return _phase_key_for_day(dates[borrow['id']], terms)[0]


def _active_day_counts(borrows, dates, terms, phase_keys):
    """How many distinct days each phase actually produced borrows on.

    A phase that ran for two weeks but only saw borrows on three of them is not
    comparable to one that saw borrows on all fourteen, and neither is
    comparable to the eighty-four-day regular phase. Dividing by the days the
    phase actually produced borrows is the only way to compare intensity without
    pretending a phase that had not started yet was quiet, or punishing a
    reporting window that clipped a phase.
    """
    days = {key: set() for key in phase_keys}
    for borrow in borrows:
        key = _phase_key_of_borrow(borrow, dates, terms)
        if key in days:
            days[key].add(dates[borrow['id']])
    return {key: len(value) for key, value in days.items()}


def _attach_intensity(rows, borrows, dates, terms):
    """Give each phase its per-day figures and its share of the whole window."""
    total = len(borrows)
    day_counts = _active_day_counts(borrows, dates, terms, [row['phase_key'] for row in rows])
    for row in rows:
        active_days = day_counts.get(row['phase_key']) or 0
        row['active_days'] = active_days
        row['share_in_window'] = _rate(row['borrow_count'], total)
        if active_days:
            row['borrow_per_day'] = round(row['borrow_count'] / active_days, 2)
            row['level_three_per_day'] = round(row['level_three_count'] / active_days, 2)


def _usable_phases(rows):
    """Phases large enough to say anything about, synthetic buckets excluded.

    The two synthetic buckets describe the calendar's coverage rather than a
    rhythm, so they are never candidates for the "busiest phase" sentence. They
    are still counted, still shown in the table, and still reported in the
    summary as a coverage number.
    """
    return [
        row for row in rows
        if not row['is_small_sample']
        and row['phase_key'] in PHASE_ORDER
        and row['borrow_count']
    ]


def _conclusion(rows, *, has_data, has_sample, unplaced_count):
    """One sentence naming the phase that actually stands out.

    The sentence leads with the per-day figure rather than the total, because a
    total comparison would only rank the phases by length. When no phase clears
    the floor it says so instead of picking a winner: naming the busiest phase
    out of three borrows is a coin flip described as a finding.
    """
    if not has_data:
        return '周期内没有借用订单，学期节律交叉需要先有借用发生。'
    if not has_sample:
        return '周期内借用单量不足，暂时无法把逾期风险与学期阶段交叉比较。'

    usable = _usable_phases(rows)
    parts = []
    if usable:
        worst = max(
            usable,
            key=lambda row: (row['level_three_rate'] or 0, row['level_three_count']),
        )
        if worst['level_three_rate']:
            parts.append(
                f'{worst["phase_label"]}日均借用 {worst["borrow_per_day"]} 笔，'
                f'第三级催收率 {worst["level_three_rate"]}%'
            )
        else:
            parts.append(
                f'{worst["phase_label"]}日均借用 {worst["borrow_per_day"]} 笔，'
                f'周期内没有升级到第三级'
            )
    else:
        parts.append('各阶段的借用量都偏少，暂不足以支撑节奏结论')

    if unplaced_count:
        parts.append(
            f'另有 {unplaced_count} 笔借用落在学期日历覆盖范围之外，未计入任何阶段'
        )
    return '；'.join(parts) + '。'


def build_borrow_rhythm(*, days=DEFAULT_LOOKBACK_DAYS, now=None):
    """Cross the borrow risk stratification with the academic calendar."""
    now = now or timezone.now()
    borrows, escalations = _fetch_borrows(days=days, now=now)
    terms = list(
        AcademicTerm.objects.filter(is_active=True).prefetch_related('phases')
    )

    dates = _borrow_dates(borrows)
    rows = _phase_rows(borrows, escalations, terms=terms, now=now, dates=dates)
    _attach_intensity(rows, borrows, dates, terms)

    total = len(borrows)
    total_level_three = sum(1 for borrow in borrows if borrow['id'] in escalations)
    unplaced = sum(
        row['borrow_count'] for row in rows if row['phase_key'] in SYNTHETIC_PHASE_KEYS
    )

    return {
        'days': days,
        'borrow_count': total,
        'level_three_count': total_level_three,
        'level_three_rate': _rate(total_level_three, total),
        'rows': rows,
        'unplaced_count': unplaced,
        'covered_count': total - unplaced,
        'covered_share': _rate(total - unplaced, total),
        'min_sample_size': MIN_SAMPLE_SIZE,
        'phase_order': PHASE_ORDER,
        'has_data': bool(borrows),
        'has_sample': total >= MIN_SAMPLE_SIZE,
        'has_calendar': bool(terms),
        'summary': _conclusion(
            rows,
            has_data=bool(borrows),
            has_sample=total >= MIN_SAMPLE_SIZE,
            unplaced_count=unplaced,
        ),
    }

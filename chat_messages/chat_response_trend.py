# -*- coding: utf-8 -*-
"""Say whether private-message responsiveness is getting better or worse.

`build_chat_response_insights` answers "how is messaging doing right now": how
long a message waited before it was opened, how many were never opened at all,
and what shape the conversations took. Every one of those is a question about a
single window, and a single window cannot answer the one that comes after it -
is this month better or worse than last month. A median that sits at six hours
in both windows and a median that climbed from two hours to six look identical
when only one of them is on screen, and the reader cannot tell "always been like
this" from "just broke" from "only this month, because the term started". Those
three need opposite responses, and a snapshot renders all of them as the same
number.

The comparison is the one the search, schedule and borrowing panels already
make: the current window against the equal-length window immediately before it,
both cut by local midnight at the send time, so a message counted here falls in
the same period everywhere else on the dashboard. Five figures are placed side
by side, three of them carrying a direction and two reported without one.

Three boundaries keep the comparison honest.

The opening time is compared as a multiple, never in percentage points. The
ten-point threshold the search panel uses is a percentage-point threshold and
only means something for a figure that is already a percentage; a duration in
seconds has no such scale, since 3600 to 7200 and 60 to 120 are the same
hundred-percent move and only one of them can cross an absolute difference of
3600. The multiple is also deliberately wider than the quarter the schedule
panel uses for its own duration median, because a message that waited is a
person waiting, and the tail is heavy enough that a quarter is noise.

Each window needs its own sample before either is given a direction. `read_at`
was added recently, so the previous window is full of messages that were opened
before the column existed: they count as opened and carry no timestamp at all.
That is not a slow previous period, it is a previous period whose opening times
were never recorded, and reporting it as one would be inventing evidence. When a
window has too few timed samples the figures are still shown and the direction
is withheld, which is the rule every other cross-period panel follows.

Nothing here adjusts anything. The comparison does not move `SILENT_AFTER`, does
not change the latency buckets, does not reorder the base panel, and does not
write back to a single message. A trend module that moved a threshold would turn
the record into something shaped by what reports on it.
"""

from django.utils import timezone

# Imported rather than reimplemented, on purpose. Two panels that cut the
# calendar differently would put the same message in different periods, and the
# reader would have no way to tell which one is lying. The threshold is shared
# for the same reason: a reader who learned that ten points means something on
# the search panel should not have to learn a second number here.
from listings.search_trend import TREND_DELTA_POINTS, _period_bounds

from .chat_response import (
    MIN_LATENCY_SAMPLE,
    PERIOD_FIELDS,
    SILENT_AFTER,
    _build_conversation_health,
    _format_duration,
    _percentile,
    response_seconds_of,
)
from .models import PrivateMessage


# How far the opening-time median must move, as a multiple of the previous
# window's own median, before it is called slower or faster. Wider than the
# schedule panel's 0.25 on purpose: that panel compares commands on a machine,
# where a quarter is a real signal, and this one compares people, where a
# quarter is a bad week.
LATENCY_DRIFT_FACTOR = 1.5

# How many messages a window needs before its shares mean anything. A share
# taken over three messages moves in steps of a third, so a difference between
# two such windows is arithmetic rather than evidence. The same floor the base
# panel uses for its role breakdown, kept identical so both pages refuse to read
# a share off the same handful of messages.
MIN_SENT_SAMPLE = 10

# The figures compared, in the order the panel reads them. The opening time
# leads because it is the figure the base panel headlines; the shares follow it
# in the same order that panel uses, so a reader moving from one to the other
# does not have to re-learn which number comes first.
COMPARED_METRICS = (
    ('median_seconds', '中位打开时长', ''),
    ('open_rate', '打开率（%）', '%'),
    ('unopened_share', '超期未打开占比（%）', '%'),
    ('p90_seconds', 'P90 打开时长', ''),
    ('two_way_rate', '双向会话占比（%）', '%'),
)

# The label a metric carries in the table, and the one it carries in a sentence.
# A table has a column to hold "超期未打开占比（%）"; a sentence does not, and
# reading that back with the unit still attached sounds like a spreadsheet
# talking. Both come from the same table so they cannot drift apart.
SENTENCE_LABELS = {
    'median_seconds': '中位打开时长',
    'open_rate': '打开率',
    'unopened_share': '超期未打开占比',
    'p90_seconds': 'P90 打开时长',
    'two_way_rate': '双向会话占比',
}

# Only these carry a direction. A longer opening time, a lower open rate and a
# larger share of messages nobody ever opened are the three things an operator
# can act on. The P90 is reported without one because a tail is not a trend: a
# single message that sat for a month moves the P90 of a thirty-message window
# by a third, and calling that rising would teach the reader to distrust the
# column. The two-way rate is reported without one because a conversation
# becoming more two-way is not automatically better - it can equally mean two
# people going back and forth about an item neither of them buys.
DIRECTION_METRICS = ('median_seconds', 'open_rate', 'unopened_share')

DURATION_KEYS = ('median_seconds', 'p90_seconds')

# What a direction means for each metric that carries one. The same word means
# opposite things depending on what moved, so the panel has to be told which way
# is good: a falling open rate is bad news and a falling opening time is not.
DIRECTION_TONES = {
    ('median_seconds', 'rising'): 'bad',
    ('median_seconds', 'falling'): 'good',
    ('open_rate', 'rising'): 'good',
    ('open_rate', 'falling'): 'bad',
    ('unopened_share', 'rising'): 'bad',
    ('unopened_share', 'falling'): 'good',
}

TREND_DIRECTIONS = ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient')

TREND_LABELS = {
    'rising': '较上期上升',
    'falling': '较上期下降',
    'flat': '较上期持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

# The order the conclusion names moved figures in, which is the order they need
# a response in rather than the order the table reads them. A rising share of
# messages nobody ever opened is the one that costs trades; a median that
# drifted up is the one that costs patience; a falling open rate is usually the
# same story told from the other end and gets fixed by the same action.
DRIVER_ORDER = ('unopened_share', 'median_seconds', 'open_rate')


def _rate(part, whole):
    """A percentage, or None when there is nothing to divide by.

    None rather than 0 on an empty window: a share computed from no messages is
    unknown, not zero, and reporting it as 0% would make a silent month look
    like a perfect one.
    """
    return round(part / whole * 100, 1) if whole else None


def _window_figures(rows, *, silent_cutoff):
    """Figures for one window, measured at that window's own end.

    The silence cutoff is the window's end minus SILENT_AFTER, not now minus
    SILENT_AFTER. The current window is measured at now, which is what the base
    panel does; the previous window is measured at its own boundary. Using now
    for both would empty the previous window's unopened count on any period
    shorter than the silence window, and comparing a real share against zero
    would report every short period as a dramatic improvement that never
    happened.

    The conversation shape is taken from the base panel's own helper rather than
    recomputed here, so the two panels cannot disagree about what "two-way"
    means.
    """
    sent = len(rows)
    opened = sum(1 for row in rows if row['is_read'])
    latencies = [
        row['response_seconds'] for row in rows
        if row['is_read'] and row['response_seconds'] is not None
    ]
    unopened = sum(
        1 for row in rows if not row['is_read'] and row['created_at'] <= silent_cutoff
    )
    health = _build_conversation_health(rows)
    return {
        'sent': sent,
        'opened': opened,
        'opened_untimed': opened - len(latencies),
        'latency_sample_size': len(latencies),
        'median_seconds': _percentile(latencies, 0.5),
        'median_label': _format_duration(_percentile(latencies, 0.5)),
        'p90_seconds': _percentile(latencies, 0.9),
        'p90_label': _format_duration(_percentile(latencies, 0.9)),
        'open_rate': _rate(opened, sent),
        'unopened': unopened,
        'unopened_share': _rate(unopened, sent),
        'conversations': health['total'],
        'two_way': health['two_way'],
        'two_way_rate': health['two_way_rate'],
    }


def _classify_latency_direction(current, previous, current_sample, previous_sample):
    """Direction of an opening-time figure, compared as a multiple.

    Both windows need their own sample first. A previous window whose messages
    were all opened before `read_at` existed has no opening times at all, and a
    direction read off it would be a direction read off nothing.
    """
    if current_sample < MIN_LATENCY_SAMPLE or previous_sample < MIN_LATENCY_SAMPLE:
        return 'insufficient'
    if current is None:
        return 'gone'
    if previous is None:
        return 'new'
    if not previous:
        # A previous median of zero carries no multiple to drift against.
        return 'insufficient'
    ratio = current / previous
    if ratio >= LATENCY_DRIFT_FACTOR:
        return 'rising'
    if ratio <= 1 / LATENCY_DRIFT_FACTOR:
        return 'falling'
    return 'flat'


def _classify_share_direction(current, previous, current_denominator, previous_denominator):
    """Direction of a share, compared in percentage points.

    Both sides of a share are already percentages, so the search panel's
    ten-point threshold applies without translation. The denominator floor is
    what makes it applicable at all: a rate over three messages moves in steps
    of a third, and two such rates differ by arithmetic rather than by anything
    that happened.
    """
    if current is None and previous is None:
        return 'insufficient'
    if previous is None:
        return 'new'
    if current is None:
        return 'gone'
    if current_denominator < MIN_SENT_SAMPLE or previous_denominator < MIN_SENT_SAMPLE:
        return 'insufficient'
    delta = round(current - previous, 1)
    if delta >= TREND_DELTA_POINTS:
        return 'rising'
    if delta <= -TREND_DELTA_POINTS:
        return 'falling'
    return 'flat'


def _display(key, value):
    """Render one figure the way the panel reads it.

    A share drops the trailing zero, because 
ound(10.0, 1) is a float and
    f-stringing it prints 10.0% for what a reader counts as ten percent. The
    value itself keeps the precision; only the label is tidied.
    """
    if value is None:
        return '—'
    if key in DURATION_KEYS:
        return _format_duration(value)
    return f'{value:g}%'


def _delta_display(key, current, previous):
    """The move between the two windows, on the scale the figure lives on.

    A duration gets a multiple, because a multiple is the only comparison a
    duration has; a share gets percentage points, because that is what it
    already is. Neither is ever shown when one side is missing, since a delta
    computed against a number nobody measured is arithmetic on nothing.
    """
    if current is None or previous is None:
        return '—'
    if key in DURATION_KEYS:
        if not previous:
            return '—'
        return f'{current / previous:.2f} 倍'
    return f'{current - previous:+g}'


def _metric_rows(current, previous):
    """One row per compared figure, with its delta, direction and tone.

    A figure present in only one of the two windows is reported without a delta
    and marked new or gone, because there is no earlier figure to subtract from
    and inventing one would be arithmetic on a number nobody measured.
    """
    rows = []
    for key, label, unit in COMPARED_METRICS:
        current_value = current.get(key)
        previous_value = previous.get(key)
        if key not in DIRECTION_METRICS:
            # The P90 and the two-way rate are reported as figures with no
            # direction. A tail moves on one message, and a conversation
            # becoming more two-way is not automatically better, so neither
            # gets a word a reader would act on.
            direction = None
        elif key in DURATION_KEYS:
            direction = _classify_latency_direction(
                current_value, previous_value,
                current['latency_sample_size'], previous['latency_sample_size'],
            )
        else:
            direction = _classify_share_direction(
                current_value, previous_value, current['sent'], previous['sent'],
            )
        tone = DIRECTION_TONES.get((key, direction), 'flat') if direction else 'flat'
        rows.append({
            'key': key,
            'label': label,
            'unit': unit,
            'current': current_value,
            'previous': previous_value,
            'current_label': _display(key, current_value),
            'previous_label': _display(key, previous_value),
            'delta': (
                round(current_value - previous_value, 1)
                if current_value is not None and previous_value is not None else None
            ),
            'delta_display': _delta_display(key, current_value, previous_value),
            'direction': direction,
            'direction_label': TREND_LABELS[direction] if direction else '',
            'tone': tone,
            'has_direction': key in DIRECTION_METRICS,
        })
    return rows


def _conclusion(metric_rows, *, current, previous):
    """One sentence naming what actually moved.

    The order the moved figures are named in is the order they need a response
    in, which is not the order the table reads them: a growing share of messages
    nobody ever opened is the one that costs trades, and a median that drifted
    up is the one that costs patience. When a window is too thin to compare, the
    sentence says so instead of picking a winner - a delta computed from four
    messages is a coin flip described as a trend.
    """
    if not current['sent'] and not previous['sent']:
        return '两个周期内都没有私信记录，跨周期对比需要先有对话发生。'
    if not previous['sent']:
        return '上一周期没有私信记录，本周期是第一批样本，暂时无法与过去比较。'
    if not current['sent']:
        return '本周期一条私信都没有，上一周期的数字仍在，但没有可比的这一边。'

    moved = [
        row for row in metric_rows
        if row['direction'] in ('rising', 'falling') and row['tone'] != 'flat'
    ]
    thin = [row for row in metric_rows if row['direction'] == 'insufficient']
    if not moved:
        if thin:
            return (
                '与上一周期相比，'
                + '、'.join(SENTENCE_LABELS[row['key']] for row in thin)
                + '的样本还不足，判不出方向，其余指标没有明显变化。'
            )
        return '与上一周期相比，打开时长、打开率与超期未打开占比都没有明显变化。'

    moved.sort(key=lambda row: DRIVER_ORDER.index(row['key']))
    parts = [
        f"{SENTENCE_LABELS[row['key']]}{row['direction_label']}（{row['delta_display']}）"
        for row in moved[:3]
    ]
    sentence = '与上一周期相比，' + '、'.join(parts) + '。'
    if thin:
        sentence += (
            '、'.join(SENTENCE_LABELS[row['key']] for row in thin)
            + '样本不足，未判方向。'
        )
    return sentence


def build_chat_response_trend(days=30, *, now=None, silent_after=None):
    """Compare this period's private-message responsiveness with the one before.

    `days` selects the messages being reported, the same way
    `build_chat_response_insights` uses it. Both windows are cut by the send
    time using the same local-midnight bounds the search comparison uses, so
    this panel cannot disagree with the search panel about which period a
    message belongs to, and it cannot disagree with the base responsiveness
    panel either, since both read the same rows.

    The unopened share is measured at each window's own end rather than at now,
    so a message sent on the last day of the previous window and still unopened
    is not counted as silently ignored in a window that had barely finished.
    """
    now = now or timezone.now()
    silent_after = silent_after or SILENT_AFTER
    days = max(1, int(days))
    start, previous_start = _period_bounds(now, days)

    rows = [
        dict(row, response_seconds=None)
        for row in PrivateMessage.objects.filter(
            created_at__gte=previous_start, created_at__lte=now,
        ).values(*PERIOD_FIELDS)
    ]
    for row in rows:
        row['response_seconds'] = response_seconds_of(row)

    current_rows = [row for row in rows if row['created_at'] >= start]
    previous_rows = [row for row in rows if row['created_at'] < start]

    current = _window_figures(current_rows, silent_cutoff=now - silent_after)
    previous = _window_figures(previous_rows, silent_cutoff=start - silent_after)
    metric_rows = _metric_rows(current, previous)

    return {
        'period_days': days,
        'silent_after_days': silent_after.days,
        'latency_drift_factor': LATENCY_DRIFT_FACTOR,
        'latency_drift_percent': int(round((LATENCY_DRIFT_FACTOR - 1) * 100)),
        'trend_delta_points': TREND_DELTA_POINTS,
        'min_latency_sample': MIN_LATENCY_SAMPLE,
        'min_sent_sample': MIN_SENT_SAMPLE,
        'current_period_start': start,
        'previous_period_start': previous_start,
        'current_period_end': now,
        'current': current,
        'previous': previous,
        'metric_rows': metric_rows,
        'drivers': [
            row for row in metric_rows
            if row['direction'] in ('rising', 'falling') and row['tone'] != 'flat'
        ],
        'thin_rows': [row for row in metric_rows if row['direction'] == 'insufficient'],
        'comparable': bool(current['sent'] and previous['sent']),
        'has_data': bool(rows),
        'summary': _conclusion(metric_rows, current=current, previous=previous),
    }

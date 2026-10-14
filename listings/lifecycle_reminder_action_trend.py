# -*- coding: utf-8 -*-
"""Say whether lifecycle reminders are working better or worse than they were.

`build_lifecycle_reminder_effect` answers every question it answers about a
single period: how many listings were reminded, how many notices were opened,
how many sellers acted. That is the right first question, and the roadmap
entry for reminder effectiveness already promised the second one - "a
cross-period comparison of the action rate, so an operator can tell whether a
reminder should be sent more often or worded differently". A single 42% action
rate is at least three different realities - stable, falling, or rising from a
low base - and the three call for opposite responses. Sending more often on a
metric that is deteriorating only delivers more noise to the sellers who
already ignore the reminders.

This module therefore reports the same figures for the current period and the
equal-length period immediately before it, cut by the same local-midnight
bounds every other cross-period panel on the dashboard uses, so a reminder
counted in this window here is counted in the same window everywhere else.

Four boundaries keep the comparison honest, and the first three are inherited
from the single-period panel rather than invented here:

Correlation is still not causation. A seller may have been tidying their
listings before the reminder arrived; all this module can confirm is that the
action happened after the nudge. Comparing two periods does not change that,
so every sentence here says the *rate changed*, never that the reminder became
more persuasive.

Only the action rate carries a direction. The read rate and the action rate
answer different questions: a seller can open a notice and do nothing, which
raises the read rate while leaving the action rate flat. That is better copy
against unchanged listings, not a more useful reminder. Only the action rate
corresponds to the one question the panel exists to answer, so only it is
given a direction word; the rest are reported as figures with their delta.

The `both` bucket is reported without a direction. A listing reached by both a
generic nudge and a price-evidence reminder cannot attribute its outcome to
either, and a direction computed over that bucket would be noise read as
signal.

Nothing here adjusts anything. The comparison does not change send frequency,
does not touch the cool-down fields, does not reword the reminder, and does
not write back to the ledger. An operator deciding whether to send more often
or word differently is the entire point; a module that made that decision for
them would make it with nobody looking.
"""

from django.utils import timezone

from .lifecycle_reminder_effect import (
    _notice_for,
    _terminal_event_times,
    _reminded_items,
    _notice_index,
)
from .search_trend import TREND_DELTA_POINTS, _period_bounds as _trend_period_bounds

# A window needs this many reminded listings before its action rate means
# anything. Below it the rate moves in steps of a whole percentage point, so a
# difference between two thin windows is arithmetic rather than evidence. The
# value matches the bucket floor `demand_task_aging` uses, because a panel
# sitting on the same dashboard must refuse to score thin evidence at the same
# volume as its neighbours.
MIN_PERIOD_ITEMS = 12

# Direction words, taken verbatim from the search trend comparison so a reader
# who learned that "rising" means ten points on one panel does not have to
# learn a second number for this one.
TREND_DIRECTIONS = ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient')

TREND_LABELS = {
    'rising': '较上期上升',
    'falling': '较上期下降',
    'flat': '较上期持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

# The overall metrics worth comparing, in the order the panel reads them. The
# action rate leads because it is the figure that answers whether sellers are
# acting; the read rate and the reminded count follow because two periods can
# share an action rate while one of them stopped opening the notices at all.
COMPARED_METRICS = (
    ('reminded_items', '被提醒商品', ''),
    ('notices', '提醒通知数', ''),
    ('read_rate', '通知打开率（%）', '%'),
    ('action_rate', '行动率（%）', '%'),
)

# Only the action rate is given a direction. The read rate is a different
# question, and the reminded count is a volume figure: a busier period is not
# a better one, and labelling it rising would teach the reader to read a
# quantity change as a quality change.
DIRECTION_METRICS = ('action_rate',)

# Which way is good for the one metric that carries a direction. Rising here
# is the good outcome, unlike the duration median on the schedule trend panel.
DIRECTION_TONES = {
    ('action_rate', 'rising'): 'good',
    ('action_rate', 'falling'): 'bad',
    ('action_rate', 'flat'): 'flat',
}

# The reminder types, in the order the single-period panel reports them. The
# `both` bucket is reported but never given a direction.
KIND_ORDER = ('attention', 'price', 'both')


def _rate(part, whole):
    """A percentage rounded to one decimal, or None when there is no whole.

    Returning None rather than 0 matters here more than anywhere else on the
    dashboard: a period with no reminders has no action rate, and reporting 0
    would be read as "sellers stopped acting" when the truth is "nobody was
    asked".
    """
    if not whole:
        return None
    return round(part / whole * 100, 1)


def _kind_keys(attention_at, price_at):
    keys = []
    if attention_at is not None:
        keys.append('attention')
    if price_at is not None:
        keys.append('price')
    return keys


def _window_figures(start, now):
    """The single-period figures for one window, computed the same way twice.

    The arithmetic deliberately repeats `build_lifecycle_reminder_effect`
    rather than importing it, because that function also builds the untouched
    listing rows and the blocked-seller rows, neither of which has a
    cross-period meaning. Importing it would mean paying for rows nobody
    reads here and, worse, would couple this module to every future change in
    the panel it is supposed to be independent of.
    """
    rows = _reminded_items(start, now)
    item_ids = [row['item'].pk for row in rows]
    seller_ids = {row['item'].seller_id for row in rows}
    notice_index = _notice_index(seller_ids, start, now)
    terminal_times = _terminal_event_times(item_ids, start)

    notices = sum(len(value) for value in notice_index.values())
    read_notices = sum(
        1 for value in notice_index.values()
        for row in value if row['is_read']
    )

    reminded_items = 0
    acted = 0
    read = 0
    kind_totals = {key: {'items': 0, 'read': 0, 'acted': 0} for key in KIND_ORDER}
    for row in rows:
        item = row['item']
        refreshed_after = bool(
            item.last_refreshed_at is not None and item.last_refreshed_at > row['reminded_at']
        )
        completed_after = any(
            moment > row['reminded_at'] for moment in terminal_times.get(item.pk, ())
        )
        notice = _notice_for(notice_index, item.seller_id, row['reminded_at'])
        acted_after = refreshed_after or completed_after
        row_read = bool(notice['is_read']) if notice else False

        reminded_items += 1
        acted += 1 if acted_after else 0
        read += 1 if row_read else 0

        keys = _kind_keys(row['attention_at'], row['price_at'])
        bucket = keys[0] if len(keys) == 1 else 'both'
        kind_totals[bucket]['items'] += 1
        kind_totals[bucket]['read'] += 1 if row_read else 0
        kind_totals[bucket]['acted'] += 1 if acted_after else 0

    kind_rows = []
    for key in KIND_ORDER:
        totals = kind_totals[key]
        if not totals['items']:
            continue
        kind_rows.append({
            'key': key,
            'label': {
                'attention': '仅待处理提醒',
                'price': '仅价格参考提醒',
                'both': '两类同时触达',
            }[key],
            'items': totals['items'],
            'read_rate': _rate(totals['read'], totals['items']),
            'action_rate': _rate(totals['acted'], totals['items']),
        })

    return {
        'reminded_items': reminded_items,
        'notices': notices,
        'read_notices': read_notices,
        'notice_read_rate': _rate(read_notices, notices),
        'read_rate': _rate(read, reminded_items),
        'action_rate': _rate(acted, reminded_items),
        'kind_rows': kind_rows,
    }


def _classify_direction(current, previous):
    """The direction of the action rate, or None when there is nothing to read.

    A rate present in only one of the two windows is never given a direction:
    there is no earlier figure to subtract from, and inventing one would be
    arithmetic on a number nobody measured.
    """
    if current is None or previous is None:
        return None
    delta = current - previous
    if delta >= TREND_DELTA_POINTS:
        return 'rising'
    if delta <= -TREND_DELTA_POINTS:
        return 'falling'
    return 'flat'


def _metric_rows(current, previous, *, has_sample):
    """One row per compared metric, with its delta and direction.

    A metric present in only one window is reported without a delta, for the
    same reason the schedule trend panel gives none.
    """
    rows = []
    for key, label, unit in COMPARED_METRICS:
        current_value = current.get(key)
        previous_value = previous.get(key)
        if current_value is None or previous_value is None:
            delta = None
            delta_display = '—'
        else:
            delta = round(current_value - previous_value, 1)
            delta_display = f'{delta:+.1f}'

        if key in DIRECTION_METRICS:
            direction = _classify_direction(current_value, previous_value)
            if not has_sample or direction is None:
                direction = 'insufficient'
            direction_label = TREND_LABELS[direction]
            tone = DIRECTION_TONES.get((key, direction), 'insufficient')
        else:
            direction = None
            direction_label = ''
            tone = None

        rows.append({
            'key': key,
            'label': label,
            'unit': unit,
            'current': current_value,
            'previous': previous_value,
            'delta': delta,
            'delta_display': delta_display,
            'direction': direction,
            'direction_label': direction_label,
            'tone': tone,
            'has_direction': key in DIRECTION_METRICS,
        })
    return rows


def _kind_trend_rows(current, previous, *, has_sample):
    """Cross-period rows for each reminder type, with the same direction rules.

    The `both` bucket is deliberately excluded from direction: a listing
    reached by both kinds cannot attribute its outcome to either, and the
    single-period panel already says so. A direction computed here would read
    that same unattributable bucket as a trend.
    """
    previous_by_key = {row['key']: row for row in previous['kind_rows']}
    rows = []
    for row in current['kind_rows']:
        previous_row = previous_by_key.get(row['key'])
        previous_rate = previous_row['action_rate'] if previous_row else None
        direction = None
        if row['key'] != 'both':
            direction = _classify_direction(row['action_rate'], previous_rate)
            if not has_sample or direction is None:
                direction = 'insufficient'
        rows.append({
            'key': row['key'],
            'label': row['label'],
            'current_items': row['items'],
            'current_action_rate': row['action_rate'],
            'previous_items': previous_row['items'] if previous_row else None,
            'previous_action_rate': previous_rate,
            'direction': direction,
            'direction_label': TREND_LABELS.get(direction, '') if direction else '',
            'tone': DIRECTION_TONES.get(('action_rate', direction), 'insufficient') if direction else None,
            'has_direction': row['key'] != 'both',
        })
    return rows


def _conclusion(current, previous, *, has_sample):
    """One sentence naming what actually moved.

    The sentence leads with the action rate, because that is the figure the
    panel exists to answer about. When either window is below the floor it
    says so instead of picking a winner, and when one window has no reminders
    at all it says that rather than reporting a fall to zero.
    """
    if not current['reminded_items'] and not previous['reminded_items']:
        return '两个周期内都没有发送过生命周期提醒，跨周期对比需要先有提醒记录。'
    if not current['reminded_items']:
        return '本周期还没有发送过生命周期提醒，行动率不存在，不是零。'
    if not previous['reminded_items']:
        return '上一周期没有提醒记录，本期是第一个有提醒的周期，暂时没有可比的基数。'
    if not has_sample:
        return (
            f'任一周期的被提醒商品数不足 {MIN_PERIOD_ITEMS} 件，'
            '数字照实报，方向扣下，不判升降。'
        )

    rate_row = next(
        row for row in _metric_rows(current, previous, has_sample=True)
        if row['key'] == 'action_rate'
    )
    direction = rate_row['direction']
    if direction == 'rising':
        return (
            f"与上一周期相比，提醒后的行动率从 {rate_row['previous']}% 升到 "
            f"{rate_row['current']}%（{rate_row['delta_display']} 个百分点），"
            "可以开始考虑扩大覆盖面。"
        )
    if direction == 'falling':
        return (
            f"与上一周期相比，提醒后的行动率从 {rate_row['previous']}% 降到 "
            f"{rate_row['current']}%（{rate_row['delta_display']} 个百分点），"
            "先看发送时段和商品结构，再决定要不要加频。"
        )
    return (
        f"与上一周期相比，提醒后的行动率从 {rate_row['previous']}% 到 "
        f"{rate_row['current']}%，变化在 {TREND_DELTA_POINTS} 个百分点以内，视为持平。"
    )


def build_lifecycle_reminder_action_trend(days=30, *, now=None):
    """Compare this period's reminder action rate with the equal one before it.

    `days` selects the window the same way the dashboard's period selector
    does, and both windows are cut by the run's own local-midnight bounds, so
    a reminder counted here is counted in the same window on every other
    cross-period panel. The current window is still in progress and holds a
    partial day at the end; the previous window is complete, and the panel
    footnote says so rather than letting a reader mistake an unfinished period
    for a bad one.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start, previous_start = _trend_period_bounds(now, days)

    current = _window_figures(start, now)
    previous = _window_figures(previous_start, start)
    has_sample = (
        current['reminded_items'] >= MIN_PERIOD_ITEMS
        and previous['reminded_items'] >= MIN_PERIOD_ITEMS
    )

    return {
        'days': days,
        'min_period_items': MIN_PERIOD_ITEMS,
        'trend_delta_points': TREND_DELTA_POINTS,
        'current_period_start': start,
        'previous_period_start': previous_start,
        'current': current,
        'previous': previous,
        'metric_rows': _metric_rows(current, previous, has_sample=has_sample),
        'kind_rows': _kind_trend_rows(current, previous, has_sample=has_sample),
        'has_data': bool(current['reminded_items'] or previous['reminded_items']),
        'has_sample': has_sample,
        'summary': _conclusion(current, previous, has_sample=has_sample),
    }
# -*- coding: utf-8 -*-
"""Read term-level search trends and filter-preference shifts across two periods.

The aggregate period comparison answers "did search volume go up", which is
useful but shallow. When total searches rise 20%, staff still cannot tell
whether that is one new term taking off or every term drifting up a little, and
when it falls they cannot tell whether a term disappeared or simply became
rarer. A single number also hides the other half of demand: what students
filter by. Two periods with identical volume can still represent different
students wanting different things, and the aggregate view cannot see that at
all.

This module therefore compares each query term and each filter facet across two
windows covering the same number of calendar days: the current period and the
one immediately before it. Both start at local midnight, which is the boundary
the rest of the search insights use, so a term counted here is counted there
too. The current window is still in progress and runs up to now, so it holds a
partial day at the end; the previous window is complete. Terms get a volume
direction and a quality direction, facets get a share direction, and the two
kinds of failed search that the insight rows already separate are placed side
by side so staff can tell "we do not stock it" from "we stock it and nobody
clicks".

Three boundaries keep the comparison honest:

Shares are always compared, never raw counts. A platform-wide rise in searches
lifts every facet's absolute count, so comparing counts would report every
facet as rising and mean nothing. Shares are only meaningful within their own
facet, so each facet is normalised by the searches that used any value of that
facet, not by all searches.

Thin evidence is held back rather than guessed at. A term searched twice can
swing from a 0% to a 50% zero-result rate on one search, and reporting that as
a trend would be noise dressed as signal. Terms below the minimum sample are
marked insufficient and excluded from direction counts.

Nothing here adjusts anything. The comparison reports; it does not move the
occurrence threshold that gates synonym candidates, and it does not reorder the
demand radar. Those decisions stay with staff, for the same reason the synonym
and radar modules keep them there: an automatic change to a threshold is a
change to what the platform acts on, made without nobody looking.
"""

from datetime import datetime, time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import SearchQuery

# A term needs this many searches in a window before its rates mean anything.
# The same floor the demand-radar outcome module uses, kept identical so both
# pages refuse to score thin evidence at the same volume.
MIN_WINDOW_SEARCHES = 2

# A facet needs this many searches that used it before its share means
# anything. This floor sits above the per-term one on purpose: a share is a
# ratio inside a facet, so it is only as trustworthy as the facet's own
# denominator. Four searches split across two values can produce a 50/50
# share that a single extra search overturns completely.
MIN_FACET_SEARCHES = 4

# Percentage-point thresholds for direction. The band in between is "flat" on
# purpose: a term swinging a few points is noise, and calling noise a trend is
# worse than admitting nothing changed.
TREND_DELTA_POINTS = 10

# How many rows of each kind to return. The lists are meant to be worked
# through, so they stay short and the strongest signal comes first.
DEFAULT_LIMIT = 12

# Price is the only facet without its own column: searches record min_price and
# max_price, so a band is derived from whichever bound the student set. A search
# with neither bound belongs to no band, which is correct - a search with no
# price filter carries no evidence about price preference.
PRICE_BANDS = (
    ('free', '免费赠送', 0, 0),
    ('under_50', '50 元以下', 0.01, 50),
    ('50_to_200', '50-200 元', 50.01, 200),
    ('over_200', '200 元以上', 200.01, None),
)

TERM_DIRECTIONS = ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient')

TREND_LABELS = {
    'rising': '搜索量上升',
    'falling': '搜索量下降',
    'flat': '搜索量持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

SHARE_LABELS = {
    'rising': '占比上升',
    'falling': '占比下降',
    'flat': '占比持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

FACET_LABELS = {
    'condition': '成色',
    'category': '分类',
    'location': '地点',
    'price': '价格带',
}

# The four facets read from the search log. Each entry names the field that
# holds the value and, where the value is a foreign key, the field that holds
# its display name.
FACET_FIELDS = (
    ('condition', 'condition', None),
    ('category', 'category_id', 'category__name'),
    ('location', 'location_id', 'location__name'),
    ('price', 'price', None),
)

TERM_ORDER = {direction: index for index, direction in enumerate(TERM_DIRECTIONS)}
SHARE_ORDER = {direction: index for index, direction in enumerate(TERM_DIRECTIONS)}


def _rate(part, whole):
    """A percentage, or None when there is nothing to divide by.

    None rather than 0 on an empty window: a rate computed from no searches is
    unknown, not zero, and reporting it as 0% would make an unused term look
    perfectly healthy.
    """
    return round(part / whole * 100, 1) if whole else None


def _classify_delta(delta_points):
    """Reuse the radar verdict thresholds so both pages speak one language."""
    if delta_points is None:
        return 'insufficient'
    if delta_points >= TREND_DELTA_POINTS:
        return 'rising'
    if delta_points <= -TREND_DELTA_POINTS:
        return 'falling'
    return 'flat'


def _price_band_filter(low, high):
    """The Q object isolating one price band.

    A search may set only one bound, so a band matches when every bound that is
    set falls inside it.
    """
    if low is None:
        return Q(max_price__lte=high)
    if high is None:
        return Q(min_price__gte=low)
    return Q(min_price__gte=low, max_price__lte=high)


def _term_window_rows(queryset, click_filter=None):
    """Aggregate one window's searches by exact term.

    Clicked searches are counted only among searches that returned something,
    because a click cannot happen on an empty result list. The no-click figure
    is therefore derived as result searches minus clicked searches, which is
    what separates "nothing to click" from "something to click and nobody did".

    The click filter is passed in rather than inferred from the search window,
    because a click recorded weeks after its search is still that search's
    click. Restricting clicks to the same window as the searches is what keeps
    a term's click rate attached to the period being described, and it is the
    same boundary the aggregate insight rows already use.
    """
    click_filter = click_filter or Q()
    return list(
        queryset.values('query').annotate(
            search_count=Count('id'),
            result_search_count=Count('id', filter=Q(result_count__gt=0)),
            zero_result_count=Count('id', filter=Q(result_count=0)),
            clicked_searches=Count(
                'clicks__search_query',
                filter=click_filter & Q(result_count__gt=0),
                distinct=True,
            ),
        )
    )


def _build_term_trends(
    current_queryset, previous_queryset,
    current_click_filter=None, previous_click_filter=None,
):
    """Compare each term's volume, zero-result rate and click rate."""
    current_rows = {
        row['query']: row
        for row in _term_window_rows(current_queryset, current_click_filter)
    }
    previous_rows = {
        row['query']: row
        for row in _term_window_rows(previous_queryset, previous_click_filter)
    }

    rows = []
    for term in set(current_rows) | set(previous_rows):
        current = current_rows.get(term, {})
        previous = previous_rows.get(term, {})
        current_count = current.get('search_count', 0)
        previous_count = previous.get('search_count', 0)
        current_zero_rate = _rate(current.get('zero_result_count', 0), current_count)
        previous_zero_rate = _rate(previous.get('zero_result_count', 0), previous_count)
        current_click_rate = _rate(
            current.get('clicked_searches', 0), current.get('result_search_count', 0),
        )
        previous_click_rate = _rate(
            previous.get('clicked_searches', 0), previous.get('result_search_count', 0),
        )

        if not previous_count and current_count:
            direction = 'new'
        elif previous_count and not current_count:
            direction = 'gone'
        elif current_count < MIN_WINDOW_SEARCHES or previous_count < MIN_WINDOW_SEARCHES:
            direction = 'insufficient'
        else:
            direction = 'rising' if current_count > previous_count else (
                'falling' if current_count < previous_count else 'flat'
            )

        rows.append({
            'query': term,
            'current_searches': current_count,
            'previous_searches': previous_count,
            'delta': current_count - previous_count,
            'direction': direction,
            'direction_label': TREND_LABELS[direction],
            'zero_result_count': current.get('zero_result_count', 0),
            'result_search_count': current.get('result_search_count', 0),
            'current_zero_result_rate': current_zero_rate,
            'previous_zero_result_rate': previous_zero_rate,
            'zero_result_delta': (
                round(current_zero_rate - previous_zero_rate, 1)
                if current_zero_rate is not None and previous_zero_rate is not None else None
            ),
            'current_click_rate': current_click_rate,
            'previous_click_rate': previous_click_rate,
            'click_delta': (
                round(current_click_rate - previous_click_rate, 1)
                if current_click_rate is not None and previous_click_rate is not None else None
            ),
            'no_click_count': max(
                current.get('result_search_count', 0) - current.get('clicked_searches', 0), 0,
            ),
        })

    rows.sort(
        key=lambda row: (
            TERM_ORDER[row['direction']],
            -row['current_searches'],
            -abs(row['delta']),
            row['query'],
        )
    )
    return rows


def _facet_counts(queryset, field, label_field):
    """Aggregate one facet of a window into value -> count, with a label."""
    if field == 'price':
        counts = {}
        for key, label, low, high in PRICE_BANDS:
            counts[key] = {
                'key': key,
                'label': label,
                'count': queryset.filter(_price_band_filter(low, high)).count(),
            }
        return counts

    rows = queryset.exclude(**{field: None}).values(field).annotate(count=Count('id'))
    counts = {}
    for row in rows:
        value = row[field]
        if value in (None, ''):
            continue
        label = value
        if label_field:
            labelled = queryset.filter(**{field: value}).values(label_field)[:1]
            if labelled:
                label = labelled[0][label_field]
        counts[value] = {'key': value, 'label': label, 'count': row['count']}
    return counts


def _build_facet_shifts(current_queryset, previous_queryset):
    """Compare each facet's value shares between the two windows."""
    facets = []
    for kind, field, label_field in FACET_FIELDS:
        current_counts = _facet_counts(current_queryset, field, label_field)
        previous_counts = _facet_counts(previous_queryset, field, label_field)
        current_total = sum(row['count'] for row in current_counts.values())
        previous_total = sum(row['count'] for row in previous_counts.values())

        rows = []
        for key in set(current_counts) | set(previous_counts):
            current = current_counts.get(key, {}).get('count', 0)
            previous = previous_counts.get(key, {}).get('count', 0)
            label = current_counts.get(key, previous_counts.get(key, {})).get('label', str(key))
            current_share = _rate(current, current_total)
            previous_share = _rate(previous, previous_total)
            delta = (
                round(current_share - previous_share, 1)
                if current_share is not None and previous_share is not None else None
            )

            # A value is "new" when nobody used it last period, and "gone" when
            # nobody uses it now. The test is on the value itself, not on the
            # facet's total: a location that appears while another location
            # disappears is a shift in taste, and the facet's total says
            # nothing about either value on its own.
            if not previous and current:
                direction = 'new'
            elif previous and not current:
                direction = 'gone'
            elif current_total < MIN_FACET_SEARCHES or previous_total < MIN_FACET_SEARCHES:
                direction = 'insufficient'
            else:
                direction = _classify_delta(delta)

            rows.append({
                'key': key,
                'label': label,
                'current_count': current,
                'previous_count': previous,
                'current_share': current_share,
                'previous_share': previous_share,
                'share_delta': delta,
                'direction': direction,
                'direction_label': SHARE_LABELS[direction],
            })

        rows.sort(
            key=lambda row: (
                SHARE_ORDER[row['direction']],
                -row['current_count'],
                -(abs(row['share_delta']) if row['share_delta'] is not None else 0),
                str(row['label']),
            )
        )
        facets.append({
            'kind': kind,
            'label': FACET_LABELS[kind],
            'current_total': current_total,
            'previous_total': previous_total,
            'rows': rows[:DEFAULT_LIMIT],
            'rising': [row for row in rows if row['direction'] == 'rising'][:6],
            'falling': [row for row in rows if row['direction'] == 'falling'][:6],
            'has_data': bool(current_total or previous_total),
        })
    return facets


def _build_lead_comparison(term_rows):
    """Put the two kinds of failed search side by side.

    A term with no results and a term with results nobody clicks are different
    problems: the first is a supply gap, the second is a relevance or pricing
    problem. Ranking them in one list hides which one a term is, so each row
    carries both signals and the caller reads them together.
    """
    rows = []
    for row in term_rows:
        if not row['zero_result_count'] and not row['no_click_count']:
            continue
        if row['zero_result_count'] and row['no_click_count']:
            kind, kind_label = 'both', '既搜不到也搜到不点'
        elif row['zero_result_count']:
            kind, kind_label = 'gap', '搜不到'
        else:
            kind, kind_label = 'no_click', '搜到了不点'
        rows.append({
            'query': row['query'],
            'kind': kind,
            'kind_label': kind_label,
            'search_count': row['current_searches'],
            'zero_result_count': row['zero_result_count'],
            'zero_result_rate': row['current_zero_result_rate'],
            'no_click_count': row['no_click_count'],
            'click_rate': row['current_click_rate'],
            'direction_label': row['direction_label'],
        })
    rows.sort(
        key=lambda row: (
            -row['zero_result_count'], -row['no_click_count'], -row['search_count'], row['query'],
        )
    )
    return rows[:DEFAULT_LIMIT]


def _period_bounds(now, days):
    """The current window and the equal-length window immediately before it."""
    today = timezone.localdate(now)
    start = timezone.make_aware(datetime.combine(today - timedelta(days=days - 1), time.min))
    return start, start - timedelta(days=days)


def build_search_trend(days=30, *, now=None, limit=DEFAULT_LIMIT, query=''):
    """Compare this period's search demand and filter use with the previous one.

    `query` narrows both windows to one term, the same way the rest of the
    search insight page does. The trend panel sits on that page, so a page
    filtered to one word must not show the whole platform's trends next to it;
    a reader who narrowed the view has already said which term they care about.
    """
    now = now or timezone.now()
    start, previous_start = _period_bounds(now, days)

    current_period = SearchQuery.objects.filter(created_at__gte=start, created_at__lte=now)
    previous_period = SearchQuery.objects.filter(
        created_at__gte=previous_start, created_at__lt=start,
    )
    query = (query or '').strip()[:120]
    if query:
        current_period = current_period.filter(query__icontains=query)
        previous_period = previous_period.filter(query__icontains=query)

    current_click_filter = Q(clicks__created_at__gte=start, clicks__created_at__lte=now)
    previous_click_filter = Q(
        clicks__created_at__gte=previous_start, clicks__created_at__lt=start,
    )

    term_rows = _build_term_trends(
        current_period, previous_period,
        current_click_filter, previous_click_filter,
    )
    facet_shifts = _build_facet_shifts(current_period, previous_period)
    lead_rows = _build_lead_comparison(term_rows)

    current_total = current_period.count()
    previous_total = previous_period.count()
    direction_counts = {}
    for row in term_rows:
        direction_counts[row['direction']] = direction_counts.get(row['direction'], 0) + 1

    if not current_total and not previous_total:
        volume_change = {'delta': None, 'change_display': '\u2014', 'direction': 'flat'}
    elif previous_total and current_total != previous_total:
        volume_delta = round((current_total - previous_total) / previous_total * 100, 1)
        volume_change = {
            'delta': volume_delta,
            'change_display': f'{volume_delta:+.1f}%',
            'direction': 'up' if current_total > previous_total else 'down',
        }
    elif current_total == previous_total:
        volume_change = {'delta': 0, 'change_display': '持平', 'direction': 'flat'}
    else:
        volume_change = {
            'delta': None,
            'change_display': '新增' if current_total else '\u2014',
            'direction': 'up' if current_total else 'flat',
        }

    return {
        'period_days': days,
        'query_filter': query,
        'min_window_searches': MIN_WINDOW_SEARCHES,
        'current_period_start': start,
        'previous_period_start': previous_start,
        'current_period_end': now,
        'current_searches': current_total,
        'previous_searches': previous_total,
        'volume_change': volume_change,
        'current_terms': len({row['query'] for row in term_rows if row['current_searches']}),
        'previous_terms': len({row['query'] for row in term_rows if row['previous_searches']}),
        'term_trends': term_rows[:limit],
        'term_total': len(term_rows),
        'direction_counts': direction_counts,
        'facet_shifts': facet_shifts,
        'lead_comparison': lead_rows,
        'has_data': bool(current_total or previous_total),
    }

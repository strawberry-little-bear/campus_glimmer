# -*- coding: utf-8 -*-
"""Cross-period comparison of filter combinations, not just single facets.

The single-facet comparison answers "did the share of searches that filter by
category X rise". That is useful but it stops one dimension short of the
question an operator actually asks. Seeing the 50-200 yuan price band rise and
seeing the book category rise at the same time does not tell anyone that
50-200 yuan books are what is being searched for more: the two signals could
come from two unrelated groups of students, and combining them by adding the
two shares is arithmetic on a number nobody measured.

This module therefore reads the same two windows as the single-facet comparison
- the current period and the equal-length period immediately before it, both
bounded by local midnight - and compares the share of every *combination* of
category and price band. A combination is one pair, such as "books in the
50-200 band", and its share is taken inside the category: of the searches that
filtered by books at all, how many also landed in the 50-200 band. That
normalisation is the one that answers the operator's question, because the
alternative - comparing a pair's share against all searches - would answer a
different and much weaker question, "how common is this pair overall", which
moves with total volume and tells nobody about price taste inside a category.

Four boundaries keep the comparison readable:

Only two dimensions are crossed. Category and price band are the two facets
whose values a student can hold in mind at once, and they are the two that
carry the most values, so a cross of them is the one worth reading. A
category-by-location-by-price cross would be three dimensions at once and on
campus search volumes almost every cell would be empty, which means the table
would be mostly dashes and would take more effort to read than the two
single-facet tables it was supposed to summarise.

Sparse cells are reported as insufficient rather than filled in. A category
crossed with four price bands produces up to four times as many rows as the
single-facet view, and most of them will hold a handful of searches. A share
computed from two searches is a coin flip described as a percentage. Every
combination therefore needs the same minimum number of searches the
single-facet view uses before it is given a direction, and the ones that fall
short are listed as insufficient and excluded from the direction counts. The
module never lowers the floor to make a table look fuller: an empty cell that
says "we cannot tell" is worth more than a filled cell that lies.

Only pairs present in both periods are compared. A pair that appears this
period and did not exist last period is reported as new and a pair that
disappeared is reported as gone, in the same language the single-facet
comparison uses, but neither is given a share delta, because there is no
earlier share to subtract from.

Nothing here adjusts anything, exactly as in the single-facet comparison. No
threshold, no band boundary and no category is written. The module reads
`SearchQuery` only, adds no table and no migration, and reports.
"""

from django.db.models import Count
from django.utils import timezone

from .models import SearchQuery
from .search_trend import (
    PRICE_BANDS,
    _classify_delta,
    _period_bounds,
    _price_band_filter,
    _rate,
)

# A combination needs this many searches inside its category before its share
# inside that category means anything. This is the same floor the single-facet
# comparison uses, and it is kept identical on purpose: the combination view
# must not become a place where evidence that was too thin one table over is
# thin enough here. A combination is one cell of the cross rather than a row
# of it, so it is strictly less well evidenced than either of its single
# facets, and reusing the single-facet floor is already the loosest defensible
# choice.
MIN_COMBINATION_SEARCHES = 4

# The pair of facets that are crossed. Category is the row dimension and price
# band the column dimension, so the share is always read inside a category and
# the table answers "within this category, which price band is being searched
# more". Both facets are ones a student can hold in mind together, and both
# carry enough distinct values that a cross of them is worth a table.
CROSS_FACETS = ('category', 'price')

CROSS_LABELS = {
    'category': '分类',
    'price': '价格带',
}

COMBINATION_DIRECTIONS = ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient')

COMBINATION_LABELS = {
    'rising': '占比上升',
    'falling': '占比下降',
    'flat': '占比持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

COMBINATION_ORDER = {
    direction: index for index, direction in enumerate(COMBINATION_DIRECTIONS)
}


def _band_count(queryset, band):
    """How many of a category's searches landed in one price band.

    The band test is the single-facet comparison's own `_price_band_filter`
    reused verbatim, not a re-implementation of it. Reusing it is the whole
    point: a reader comparing this table with the price-band rows one section
    above is comparing the same searches under the same rule, and a second
    implementation would eventually disagree with the first about an edge case
    - which is exactly the kind of quiet inconsistency that makes an operator
    stop trusting both tables. The rule is "every bound that is set falls
    inside the band", so a closed band needs both bounds and the open top band
    needs only the lower one; a search with neither bound belongs to no band.
    """
    _key, _label, low, high = band
    return queryset.filter(_price_band_filter(low, high)).count()


def _combination_rows(queryset):
    """One row per category, carrying that category's four price-band counts.

    The counts are of searches, not of pair records: a search that filters by
    books and sits in the 50-200 band is one search in the "books / 50-200"
    cell. Counting anything else would make the shares below incomparable with
    the single-facet view, where a share is also a share of searches.
    """
    counts = {}
    category_rows = (
        queryset.exclude(category__isnull=True)
        .values('category_id', 'category__name')
        .annotate(searches_with_category=Count('id'))
    )
    for row in category_rows:
        category_id = row['category_id']
        category_queryset = queryset.filter(category_id=category_id)
        bands = {
            band[0]: _band_count(category_queryset, band) for band in PRICE_BANDS
        }
        counts[category_id] = {
            'category_id': category_id,
            'category_label': row['category__name'] or str(category_id),
            'bands': bands,
            'searches_with_category': row['searches_with_category'],
        }
    return counts


def _build_combination_shifts(current_queryset, previous_queryset):
    """Compare each category-and-price-band pair's share inside its category.

    The denominator is the category's own searches in the same window, which is
    what makes the share a statement about price taste inside that category.
    Using all searches as the denominator would compare pairs across
    categories and would move with total volume, which is exactly the mistake
    the single-facet comparison already refuses to make.
    """
    current_counts = _combination_rows(current_queryset)
    previous_counts = _combination_rows(previous_queryset)

    rows = []
    for category_id in set(current_counts) | set(previous_counts):
        current = current_counts.get(category_id, {})
        previous = previous_counts.get(category_id, {})
        current_total = current.get('searches_with_category', 0)
        previous_total = previous.get('searches_with_category', 0)
        label = (
            current.get('category_label')
            or previous.get('category_label')
            or str(category_id)
        )

        band_rows = []
        for key, band_label, _low, _high in PRICE_BANDS:
            current_count = current.get('bands', {}).get(key, 0)
            previous_count = previous.get('bands', {}).get(key, 0)
            current_share = _rate(current_count, current_total)
            previous_share = _rate(previous_count, previous_total)
            delta = (
                round(current_share - previous_share, 1)
                if current_share is not None and previous_share is not None
                else None
            )

            # A pair is new when nobody searched it last period and gone when
            # nobody searches it now, tested on the pair itself rather than on
            # the category's total: a band appearing while another disappears
            # is a shift in price taste, and the category total says nothing
            # about either band on its own.
            if not previous_count and current_count:
                direction = 'new'
            elif previous_count and not current_count:
                direction = 'gone'
            elif (
                current_count < MIN_COMBINATION_SEARCHES
                or previous_count < MIN_COMBINATION_SEARCHES
            ):
                direction = 'insufficient'
            else:
                direction = _classify_delta(delta)

            band_rows.append({
                'key': key,
                'label': band_label,
                'current_count': current_count,
                'previous_count': previous_count,
                'current_share': current_share,
                'previous_share': previous_share,
                'share_delta': delta,
                'direction': direction,
                'direction_label': COMBINATION_LABELS[direction],
            })

        band_rows.sort(
            key=lambda row: (
                COMBINATION_ORDER[row['direction']],
                -row['current_count'],
                -(abs(row['share_delta']) if row['share_delta'] is not None else 0),
                row['label'],
            )
        )
        rows.append({
            'category_id': category_id,
            'category_label': label,
            'current_total': current_total,
            'previous_total': previous_total,
            'rows': band_rows,
            'has_data': bool(current_total or previous_total),
        })

    rows.sort(key=lambda row: (-row['current_total'], row['category_label']))
    return rows


def build_search_combo_shift(days=30, *, now=None, limit=8, query=''):
    """Compare category-and-price-band combinations across two windows.

    `query` narrows both windows to one term, the same way the single-facet
    comparison on the same page does: a reader who narrowed the page to one
    word has already said which searches they care about, and the combination
    table must not quietly widen the scope back to the whole platform.
    """
    now = now or timezone.now()
    start, previous_start = _period_bounds(now, days)

    current_period = SearchQuery.objects.filter(
        created_at__gte=start, created_at__lte=now,
    ).exclude(category__isnull=True)
    previous_period = SearchQuery.objects.filter(
        created_at__gte=previous_start, created_at__lt=start,
    ).exclude(category__isnull=True)

    query = (query or '').strip()[:120]
    if query:
        current_period = current_period.filter(query__icontains=query)
        previous_period = previous_period.filter(query__icontains=query)

    rows = _build_combination_shifts(current_period, previous_period)

    current_total = current_period.count()
    previous_total = previous_period.count()
    direction_counts = {}
    for category in rows:
        for row in category['rows']:
            direction_counts[row['direction']] = (
                direction_counts.get(row['direction'], 0) + 1
            )

    return {
        'period_days': days,
        'query_filter': query,
        'cross_facets': [
            {'key': key, 'label': CROSS_LABELS[key]} for key in CROSS_FACETS
        ],
        'min_combination_searches': MIN_COMBINATION_SEARCHES,
        'current_period_start': start,
        'previous_period_start': previous_start,
        'current_period_end': now,
        'current_searches': current_total,
        'previous_searches': previous_total,
        'direction_counts': direction_counts,
        'rows': rows[:limit],
        'row_total': len(rows),
        'has_data': bool(current_total or previous_total),
    }

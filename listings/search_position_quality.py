# -*- coding: utf-8 -*-
"""Search result position quality: where the clicks actually land.

Two fields have been collected since search exposure tracking went in --
``SearchImpression.position`` and ``SearchClick.position`` -- and neither had
ever been read by anything but the admin list view. That is a real gap rather
than a cosmetic one, because the platform ships its own relevance sort and
nothing measured whether that sort helps.

The existing click-through rate is a single platform-wide number. It answers
"do people click what we show them", and it cannot answer the question the
sorting work actually raises: when nobody clicks, is the top of the page
unappealing, or are the good listings buried past the fold? Those two call for
opposite fixes -- the first is titles and photos, the second is ranking.

So this module reports three things. How click-through decays with position,
which is the shape a healthy ranking produces. Whether clicked listings sit
higher than exposed ones, which is the ranking doing its job. And, when the
overall rate is low, whether the clicks that do happen cluster at the front or
spread into the depth, which separates a ranking problem from a depth problem.

It only reports. It does not re-weight the relevance sort, does not move items
between pages, and does not write anything back to the impression or click
rows.
"""

from datetime import datetime, timedelta

from django.db.models import Avg, Count, Q
from django.utils import timezone

from .models import SearchClick, SearchImpression, SearchQuery


PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# Fixed buckets rather than one row per position: a single position carries too
# few exposures to read, and an operator cannot act on "position 7 is weak" as
# readily as on "the second half of the page is weak".
POSITION_BUCKETS = (
    (1, 3, '第 1-3 位'),
    (4, 6, '第 4-6 位'),
    (7, 9, '第 7-9 位'),
    (10, None, '第 10 位及以后'),
)

# A bucket needs this many exposures before it earns a verdict. With twelve
# exposures, one click is an 8-point swing in the rate.
MIN_BUCKET_SAMPLE = 12

# A click-through share this far below the first bucket's, at or above the
# sample floor, reads as real decay rather than noise.
DECAY_FLAG_POINTS = 15


def _period_start(days, today):
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(start_date, datetime.min.time()))


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _bucket_of(position):
    for low, high, label in POSITION_BUCKETS:
        if position >= low and (high is None or position <= high):
            return label
    return POSITION_BUCKETS[-1][2]


def _position_bucket_rows(queryset, position_field):
    """Group a position-carrying queryset into the fixed buckets."""
    rows = queryset.values(position_field).annotate(count=Count('id'))
    bucket_counts = {}
    for row in rows:
        position = row[position_field] or 0
        if position <= 0:
            # A row without a usable position says nothing about where it sat.
            continue
        label = _bucket_of(position)
        entry = bucket_counts.setdefault(label, {'count': 0, 'position_sum': 0})
        entry['count'] += row['count']
        entry['position_sum'] += row['count'] * position
    return bucket_counts


def _flag_real_decay(curve):
    """Mark the buckets whose click-through really trails the front of the page.

    A bucket only earns the flag when it has enough exposures to be read and
    when it trails the first bucket by at least ``DECAY_FLAG_POINTS``. Below
    that, the gap is the size of a single click and reads as noise, so the
    bucket keeps its numbers and drops the flag.
    """
    if not curve:
        return
    front = curve[0]
    if front['is_small_sample']:
        # Comparing against a front bucket we cannot read tells us nothing.
        return
    front_rate = front['click_through_rate']
    for row in curve[1:]:
        row['decay_flag'] = bool(
            not row['is_small_sample']
            and front_rate - row['click_through_rate'] >= DECAY_FLAG_POINTS
        )


def _build_decay_curve(impression_rows, click_rows):
    """Click-through rate by position bucket, plus the average position of each."""
    impressions = _position_bucket_rows(impression_rows, 'position')
    clicks = _position_bucket_rows(click_rows, 'position')
    curve = []
    for low, high, label in POSITION_BUCKETS:
        impression_entry = impressions.get(label, {'count': 0, 'position_sum': 0})
        click_entry = clicks.get(label, {'count': 0, 'position_sum': 0})
        impression_count = impression_entry['count']
        click_count = click_entry['count']
        curve.append({
            'label': label,
            'low': low,
            'high': high,
            'impression_count': impression_count,
            'click_count': click_count,
            'click_through_rate': _percent(click_count, impression_count),
            'average_impression_position': (
                round(impression_entry['position_sum'] / impression_count, 1)
                if impression_count else None
            ),
            'average_click_position': (
                round(click_entry['position_sum'] / click_count, 1)
                if click_count else None
            ),
            'is_small_sample': impression_count < MIN_BUCKET_SAMPLE,
            'decay_flag': False,
        })
    _flag_real_decay(curve)
    return curve


def _build_ranking_effect(impression_rows, click_rows):
    """Do the listings people click sit higher than the ones we show?"""
    exposure = impression_rows.aggregate(
        average_position=Avg('position'),
        total=Count('id'),
    )
    clicks = click_rows.aggregate(
        average_position=Avg('position'),
        total=Count('id'),
    )
    exposure_average = exposure['average_position']
    click_average = clicks['average_position']
    lift = None
    direction = 'unknown'
    direction_label = '样本不足'
    if exposure_average is not None and click_average is not None and exposure['total']:
        # A lower position number means higher on the page, so the ranking is
        # helping when clicked listings sit below the exposure average.
        lift = round(exposure_average - click_average, 2)
        if clicks['total'] < MIN_BUCKET_SAMPLE:
            direction = 'unknown'
            direction_label = '点击样本不足，只报数字'
        elif lift >= 1:
            direction = 'helping'
            direction_label = '被点的商品确实排在更前面'
        elif lift <= -1:
            direction = 'hurting'
            direction_label = '被点的商品反而排在更后面'
        else:
            direction = 'flat'
            direction_label = '点击与位置基本无关'
    return {
        'exposure_average_position': (
            round(exposure_average, 2) if exposure_average is not None else None
        ),
        'click_average_position': (
            round(click_average, 2) if click_average is not None else None
        ),
        'lift': lift,
        'exposure_count': exposure['total'],
        'click_count': clicks['total'],
        'direction': direction,
        'direction_label': direction_label,
        'is_small_sample': clicks['total'] < MIN_BUCKET_SAMPLE,
    }


def _classify_depth(overall_rate, curve):
    """Separate a weak first screen from clicks that only happen deeper in."""
    usable = [row for row in curve if row['impression_count'] >= MIN_BUCKET_SAMPLE]
    front = next((row for row in usable if row['label'] == POSITION_BUCKETS[0][2]), None)
    if front is None:
        return {
            'kind': 'unknown',
            'kind_label': '样本不足',
            'summary': '前排样本还不够，暂不区分是排序问题还是深度问题。',
        }
    front_rate = front['click_through_rate']
    deeper_rates = [
        row['click_through_rate']
        for row in usable
        if row['label'] != POSITION_BUCKETS[0][2]
    ]
    if not deeper_rates:
        return {
            'kind': 'unknown',
            'kind_label': '样本不足',
            'summary': '只有前排有足够样本，深度侧读不出结论。',
        }
    best_deeper = max(deeper_rates)
    if overall_rate >= 45:
        return {
            'kind': 'healthy',
            'kind_label': '前排足够吸引人',
            'summary': '总体点击率不低，排序与呈现没有明显拖累。',
        }
    if front_rate <= best_deeper:
        return {
            'kind': 'ranking',
            'kind_label': '前排没吸引力',
            'summary': (
                f'前排（{front["label"]}）点击率 {front_rate}% 不高于后面的位置，'
                '问题更可能在标题、首图和价格呈现上，而不是排序把好商品埋住了。'
            ),
        }
    if best_deeper > 0:
        return {
            'kind': 'depth',
            'kind_label': '有人翻到后面才点',
            'summary': (
                f'前排点击率 {front_rate}% 明显高于后面，说明排序认得出来好坏，'
                '但用户要翻更久才点到——值得看分页深度和每页条数。'
            ),
        }
    return {
        'kind': 'shallow',
        'kind_label': '翻到后面就没人点了',
        'summary': (
            f'前排点击率 {front_rate}%，而后面几乎为零：用户看完第一屏就离开，'
            '既不是排序埋掉了好商品，也不是耐心问题，是第一屏之外没有想看的东西。'
        ),
    }


def build_search_position_quality(days=30, now=None):
    """Measure whether the platform's own ranking produces a sane click curve."""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localdate())

    search_period = SearchQuery.objects.filter(
        created_at__gte=start, created_at__lte=now, result_count__gt=0,
    )
    impression_rows = SearchImpression.objects.filter(
        search_query__in=search_period, position__gt=0,
    )
    click_rows = SearchClick.objects.filter(
        search_query__in=search_period, position__gt=0, search_query__result_count__gt=0,
    )

    impression_count = impression_rows.count()
    click_count = click_rows.count()
    overall_rate = _percent(click_count, impression_count)
    curve = _build_decay_curve(impression_rows, click_rows)
    ranking = _build_ranking_effect(impression_rows, click_rows)
    depth = _classify_depth(overall_rate, curve)

    measurable_buckets = [row for row in curve if row['impression_count'] >= MIN_BUCKET_SAMPLE]
    decayed = [row['label'] for row in curve if row['decay_flag']]
    if not measurable_buckets:
        summary = '当前周期还没有足够带上结果位置的曝光，位置质量需要先积累样本。'
    else:
        summary = (
            f'共 {impression_count} 次曝光、{click_count} 次点击，总体点击率 {overall_rate}%。'
            f'{depth["kind_label"]}：{depth["summary"]}'
        )
        if decayed:
            summary += f'点击率相对前排低 {DECAY_FLAG_POINTS} 个百分点以上的位置区间：{"、".join(decayed)}。'

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(impression_count),
        'summary': summary,
        'metrics': {
            'impressions': impression_count,
            'clicks': click_count,
            'overall_click_through_rate': overall_rate,
            'measured_buckets': len(measurable_buckets),
            'decayed_buckets': len(decayed),
            'decay_flag_points': DECAY_FLAG_POINTS,
            'min_bucket_sample': MIN_BUCKET_SAMPLE,
        },
        'decay_curve': curve,
        'ranking_effect': ranking,
        'depth_diagnosis': depth,
        'buckets': [label for _, _, label in POSITION_BUCKETS],
        'recommendations': _recommendations(overall_rate, curve, ranking, depth),
    }


def _recommendations(overall_rate, curve, ranking, depth):
    """Actions an operator can take, each tied to the number that justifies it."""
    notes = []
    if depth['kind'] == 'ranking':
        notes.append(
            '先改第一屏的呈现：标题、首图和价格区间是用户在这一屏唯一看到的东西，'
            '在排序没动之前，调权重解决不了没人点的问题。'
        )
    if depth['kind'] == 'depth':
        notes.append(
            '排序已经认得出来好坏，但要点得翻更久；可以看每页条数和分页深度，'
            '而不是继续加排序权重。'
        )
    if depth['kind'] == 'shallow':
        notes.append(
            '第一屏之后几乎没有点击，说明深页供给本身不吸引人；'
            '补商品比调排序更直接。'
        )
    if ranking['direction'] == 'hurting' and not ranking['is_small_sample']:
        notes.append(
            '被点击商品的平均位置比曝光平均位置还要靠后，这是排序把好东西压在后面的直接证据，'
            '但只报告，不改 relevance 权重。'
        )
    if not notes:
        notes.append('位置分布与点击表现没有明显异常，可继续观察更长的周期。')
    return notes

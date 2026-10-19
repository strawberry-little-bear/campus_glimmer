# -*- coding: utf-8 -*-
"""Did the platform's own demand matching score mean anything?

``_match_demand`` writes a score onto every seller response, and that score is
load-bearing: it is what lets a listing respond to a demand at all. The score
has never been compared against the one judgement that could validate it -- the
requester, who accepts or rejects each response.

Every other demand report counts responses by category or location, which
answers "how much demand was matched". This module answers the different
question that the matching code raises: when the score is high, is the
requester actually more likely to accept, and when it is low, are we turning
away goods that would have been wanted? Those two failures point in opposite
directions. An inflated score wastes responders' effort and hides good listings
behind a gate; an understated score turns demand matching into noise that
everyone learns to ignore. Neither shows up in a per-category response count.

So it reports three things. Whether acceptance rises with the score, which is
the shape a working score produces. Which segments contain the responses the
score got wrong -- high-scored but rejected, and low-scored but accepted. And
whether the individual dimensions the score is built from carry their weight,
since a dimension that never separates accepted from rejected is a weight that
has not earned its place.

It only reports. It does not re-weight ``_match_demand``, does not change any
threshold, and does not write anything back to a response. Segments without
enough decided responses report their numbers and withhold their verdict.
"""

from datetime import datetime, timedelta

from django.db.models import Avg, Count, Q
from django.utils import timezone

from .models import DemandResponse

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# The four dimensions ``_match_demand`` scores, and the reason text each one
# writes. Parsing the stored reason is what keeps this module honest about
# which dimension a response actually earned, rather than re-deriving a score
# from data that may have changed since the response was created.
MATCH_DIMENSIONS = (
    ('category', '分类一致'),
    ('location', '地点一致'),
    ('budget', '价格符合预算'),
    ('keyword', '关键词相近'),
)

# A decided response is one the requester actually acted on. A pending response
# says nothing about the score, and a withdrawn one says nothing about the
# requester's judgement.
DECIDED_STATUSES = ('accepted', 'rejected')

# Fixed score bands rather than one band per possible score. The score spans 2
# to 11 and a single score carries too few responses to read, while an
# operator cannot act on "score 7 is weak" as readily as on "the middle band is
# weak".
SCORE_BANDS = (
    (2, 4, '2-4 分'),
    (5, 6, '5-6 分'),
    (7, 8, '7-8 分'),
    (9, 11, '9-11 分'),
)

# A band needs this many decided responses before its acceptance rate earns a
# verdict. With twelve, one acceptance is an eight-point swing.
MIN_BAND_RESPONSES = 12

# Acceptance rates this far apart count as a real difference rather than noise.
# A band that swings a few points tells an operator nothing they can act on.
DECISION_NOISE_POINTS = 8

# A band whose acceptance rate sits this far below the strongest band's is
# flagged as a segment the score over-promised.
OVER_PROMISE_POINTS = 15

# A response scored at least this high but rejected anyway is the clearest
# evidence that the score over-promised.
HIGH_SCORE_FLOOR = 7


def _period_start(days, today):
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(start_date, datetime.min.time()))


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _band_of(score):
    for low, high, label in SCORE_BANDS:
        if low <= score <= high:
            return label
    return SCORE_BANDS[-1][2]


def _decided_responses(start, end):
    return DemandResponse.objects.filter(
        updated_at__gte=start, updated_at__lte=end, status__in=DECIDED_STATUSES,
    )


def _build_score_bands(responses):
    """Acceptance rate by score band, plus the average score of each outcome."""
    # 一个分段的总数和其中被采纳的数原本分两次 GROUP BY 拉，问的却是同一批行。
    # 合成一次带条件计数的聚合，按 match_score 分组一趟拿出来：查询数减半，
    # 每个分段的四格一个都没换。
    rows = responses.values('match_score').annotate(
        count=Count('id'),
        accepted=Count('id', filter=Q(status='accepted')),
    )
    band_counts = {}
    for row in rows:
        label = _band_of(row['match_score'])
        entry = band_counts.setdefault(label, {
            'count': 0, 'accepted': 0, 'score_sum': 0, 'accepted_score_sum': 0,
        })
        entry['count'] += row['count']
        entry['score_sum'] += row['count'] * row['match_score']
        entry['accepted'] += row['accepted']
        entry['accepted_score_sum'] += row['accepted'] * row['match_score']

    bands = []
    for low, high, label in SCORE_BANDS:
        entry = band_counts.get(label, {
            'count': 0, 'accepted': 0, 'score_sum': 0, 'accepted_score_sum': 0,
        })
        count = entry['count']
        accepted = entry['accepted']
        bands.append({
            'label': label,
            'low': low,
            'high': high,
            'response_count': count,
            'accepted_count': accepted,
            'rejected_count': count - accepted,
            'acceptance_rate': _percent(accepted, count),
            'average_score': round(entry['score_sum'] / count, 1) if count else None,
            'average_accepted_score': (
                round(entry['accepted_score_sum'] / accepted, 1) if accepted else None
            ),
            'is_small_sample': count < MIN_BAND_RESPONSES,
        })
    return bands


def _build_dimension_effect(responses, total, accepted_count):
    """Whether each dimension the score adds actually separates accepted from rejected."""
    # 四个维度原本各自四次 COUNT（带/不带 × 总数/采纳数），16 次往返问的都是同一批
    # 已决断响应。合成一次 aggregate：`match_reason` 是 CharField(blank=True)，
    # 永远不为 NULL，所以「不带」正好是全集减去「带」，不需要另发一次 exclude。
    # 维度之间的四格因此仍然逐维度独立算，只是共享同一次扫描。
    aggregates = {}
    for key, marker in MATCH_DIMENSIONS:
        aggregates['with_%s' % key] = Count(
            'id', filter=Q(match_reason__contains=marker),
        )
        aggregates['with_accepted_%s' % key] = Count(
            'id', filter=Q(match_reason__contains=marker, status='accepted'),
        )
    counts = responses.aggregate(**aggregates)

    rows = []
    for key, marker in MATCH_DIMENSIONS:
        with_count = counts['with_%s' % key]
        without_count = total - with_count
        with_accepted = counts['with_accepted_%s' % key]
        without_accepted = accepted_count - with_accepted
        with_rate = _percent(with_accepted, with_count)
        without_rate = _percent(without_accepted, without_count)
        gap = round(with_rate - without_rate, 1) if with_count and without_count else None
        rows.append({
            'key': key,
            'label': marker,
            'with_count': with_count,
            'with_accepted_count': with_accepted,
            'with_acceptance_rate': with_rate,
            'without_count': without_count,
            'without_accepted_count': without_accepted,
            'without_acceptance_rate': without_rate,
            'gap_points': gap,
            'is_small_sample': with_count < MIN_BAND_RESPONSES or without_count < MIN_BAND_RESPONSES,
        })
    return rows


def _build_monotonicity(bands):
    """Does acceptance actually rise as the score rises?"""
    measurable = [band for band in bands if band['response_count'] >= MIN_BAND_RESPONSES]
    if len(measurable) < 2:
        return {
            'kind': 'unknown',
            'kind_label': '样本不足',
            'summary': '没有两个以上的分段达到样本门槛，读不出打分与采纳的关系。',
            'is_small_sample': True,
        }
    rates = [band['acceptance_rate'] for band in measurable]
    spread = round(max(rates) - min(rates), 1)
    strongest = max(measurable, key=lambda band: band['acceptance_rate'])
    weakest = min(measurable, key=lambda band: band['acceptance_rate'])
    top_band = measurable[-1]
    bottom_band = measurable[0]

    if spread < DECISION_NOISE_POINTS:
        kind, kind_label = 'flat', '打分高低与采纳无关'
        summary = (
            f'各分段的采纳率只相差 {spread} 个百分点，'
            '落在噪声范围内：打分目前分不出求购人想不想要。'
        )
    elif top_band['acceptance_rate'] > bottom_band['acceptance_rate'] and (
        top_band['acceptance_rate'] == strongest['acceptance_rate']
    ):
        kind, kind_label = 'rising', '打分越高越容易被采纳'
        summary = (
            f'{top_band["label"]}的采纳率 {top_band["acceptance_rate"]}% '
            f'高于{bottom_band["label"]}的 {bottom_band["acceptance_rate"]}%，'
            '打分方向是对的。'
        )
    else:
        kind, kind_label = 'inverted', '打分最高的分段反而不是最容易被采纳的'
        summary = (
            f'采纳率最高的是{strongest["label"]}（{strongest["acceptance_rate"]}%），'
            f'而{top_band["label"]}只有 {top_band["acceptance_rate"]}%，'
            f'最弱的{weakest["label"]}为 {weakest["acceptance_rate"]}%：'
            '打分把最不被看好的分段排到了前面。'
        )
    return {
        'kind': kind,
        'kind_label': kind_label,
        'summary': summary,
        'spread_points': spread,
        'strongest_label': strongest['label'],
        'weakest_label': weakest['label'],
        'is_small_sample': False,
    }


def _build_over_promised(responses, bands):
    """High scores the requester turned down -- the clearest over-promise signal."""
    high = responses.filter(match_score__gte=HIGH_SCORE_FLOOR)
    # 总数与被拒数问的是同一批高分响应，合成一次聚合，不再先 count 再 filter count。
    high_stats = high.aggregate(
        high_count=Count('id'),
        rejected_count=Count('id', filter=Q(status='rejected')),
    )
    high_count = high_stats['high_count']
    rejected_count = high_stats['rejected_count']
    rate = _percent(rejected_count, high_count)
    if high_count < MIN_BAND_RESPONSES:
        return {
            'has_sample': False,
            'summary': f'达到 {HIGH_SCORE_FLOOR} 分的响应还不足 {MIN_BAND_RESPONSES} 条，暂不判断打分是否虚高。',
        }
    band_labels = ', '.join(
        band['label'] for band in bands
        if band['low'] >= HIGH_SCORE_FLOOR and band['response_count']
    ) or f'{HIGH_SCORE_FLOOR} 分以上'
    return {
        'has_sample': True,
        'high_count': high_count,
        'rejected_count': rejected_count,
        'rejection_rate': rate,
        'band_labels': band_labels,
        'is_small_sample': high_count < MIN_BAND_RESPONSES,
        'summary': (
            f'{band_labels}共有 {high_count} 条响应，其中 {rejected_count} 条被拒'
            f'（{rate}%）。这部分是打分最不该出错的地方。'
        ),
    }


def _build_dimension_findings(dimension_rows):
    """Dimensions whose weight has not earned its place, stated as a verdict."""
    findings = []
    for row in dimension_rows:
        if row['is_small_sample'] or row['gap_points'] is None:
            continue
        if row['gap_points'] < 0:
            findings.append({
                'key': row['key'],
                'label': row['label'],
                'kind': 'negative',
                'message': (
                    f'带{row["label"]}的采纳率 {row["with_acceptance_rate"]}% '
                    f'反而低于不带的 {row["without_acceptance_rate"]}%，'
                    f'差 {abs(row["gap_points"])} 个百分点：这个维度没有把好商品区分出来。'
                ),
            })
    return findings


def _recommendations(monotonicity, over_promised, findings):
    """Actions an operator can take, each tied to the number that justifies it."""
    notes = []
    if monotonicity['kind'] == 'inverted':
        notes.append(
            '打分最高的分段不是最容易被采纳的，说明权重的分配和求购人的实际取舍不一致；'
            '但只报告，不改 _match_demand 的权重。'
        )
    if monotonicity['kind'] == 'flat':
        notes.append(
            '各分段采纳率相差在噪声范围内，打分目前只是一个准入门槛而不是排序依据；'
            '在改权重之前，先确认样本是否已经足够。'
        )
    if over_promised.get('has_sample') and over_promised.get('rejection_rate', 0) >= OVER_PROMISE_POINTS:
        notes.append(
            f'{over_promised["band_labels"]}的拒绝率达到 {over_promised["rejection_rate"]}%，'
            '这些响应占用了卖家一次响应机会却没有成交；先看这些被拒响应集中在哪些分类或求购上。'
        )
    for finding in findings:
        notes.append(finding['message'] + '只报告，不调整该维度的分值。')
    if not notes:
        notes.append('打分与求购人的决定没有明显背离，可继续观察更长的周期。')
    return notes


def build_demand_match_quality(days=30, now=None):
    """Measure whether the platform's demand matching score predicts acceptance."""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localdate(now))

    responses = _decided_responses(start, now)
    # 三个头条计数原本各发一次 COUNT，而 accepted + rejected 正好铺满 total
    # （`_decided_responses` 只收这两种状态）。合成一次聚合拿齐，省两次往返。
    headline = responses.aggregate(
        total=Count('id'),
        accepted_count=Count('id', filter=Q(status='accepted')),
        rejected_count=Count('id', filter=Q(status='rejected')),
    )
    total = headline['total']
    accepted_count = headline['accepted_count']
    rejected_count = headline['rejected_count']
    overall_rate = _percent(accepted_count, total)
    # 平均分原来先 exists() 再 aggregate：空集时 Avg 返回 None，exists() 只是
    # 为了不让 float(None) 抛异常。直接判空即可，省掉一次 exists。
    average_score = responses.aggregate(avg=Avg('match_score'))['avg']

    bands = _build_score_bands(responses)
    dimension_rows = _build_dimension_effect(responses, total, accepted_count)
    monotonicity = _build_monotonicity(bands)
    over_promised = _build_over_promised(responses, bands)
    findings = _build_dimension_findings(dimension_rows)

    measurable_bands = [band for band in bands if band['response_count'] >= MIN_BAND_RESPONSES]
    if not measurable_bands:
        summary = '当前周期还没有足够已决断的求购响应，匹配质量需要先积累样本。'
    else:
        summary = (
            f'共 {total} 条已决断响应，整体采纳率 {overall_rate}%。'
            f'{monotonicity["kind_label"]}：{monotonicity["summary"]}'
        )
        if over_promised.get('has_sample'):
            summary += over_promised['summary']

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(total),
        'summary': summary,
        'metrics': {
            'decided_responses': total,
            'accepted_count': accepted_count,
            'rejected_count': rejected_count,
            'acceptance_rate': overall_rate,
            'measured_bands': len(measurable_bands),
            'min_band_responses': MIN_BAND_RESPONSES,
            'high_score_floor': HIGH_SCORE_FLOOR,
            'noise_points': DECISION_NOISE_POINTS,
            # 数据库给回来的是 Decimal，统一成 float，免得模板和导出各自处理一次 None。
            'average_score': (
                round(float(average_score), 1) if average_score is not None else None
            ),
        },
        'score_bands': bands,
        'dimension_effect': dimension_rows,
        'monotonicity': monotonicity,
        'over_promised': over_promised,
        'findings': findings,
        'recommendations': _recommendations(monotonicity, over_promised, findings),
    }

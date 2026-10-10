# -*- coding: utf-8 -*-
"""Turn the synonym hit rate into a recommendation about the mining threshold.

The synonym pipeline has two ends that never used to meet. Candidates are mined
from real query rewrites and ranked by how often they recur, which makes
MIN_OCCURRENCES the gate that decides what an operator ever gets to see. On the
other end, the effect module measures whether the pairs that were actually
confirmed moved the zero-result rate. Nobody connected the two, so the gate
stayed at whatever value somebody first guessed and never got corrected in
either direction.

This module makes the connection, and it deliberately stops one step short of
acting on it. It reads the effect summary, decides which way the gate is
wrong, and names the adjacent value on a fixed ladder. It never rewrites the
constant, because a threshold is a switch on what the platform acts on: a pair
that recurs twice and is never shown is a convention nobody learns about, and
silently changing that is exactly the kind of unattended change the rest of the
search modules refuse to make.

Three things shape the recommendation:

The operator's confirmation is itself a filter. A hit rate therefore measures
the whole chain - candidate pool plus human judgement - and not the pool alone.
That is the honest reading of the number, and it is also this module's main
limitation: a pool that looks bad may hide a pool that is fine and a reviewer
who confirmed everything. The panel says so rather than pretending otherwise.

A pool that mostly backfires is not a threshold problem. When a large share of
confirmed pairs made search worse, the pairs themselves are wrong, and moving
the bar would only hide them from review instead of getting them fixed. That
case is reported as a pairing problem and no threshold change is suggested.

The ladder moves one step at a time. A hit rate of 12% and one of 38% are both
"too loose", but they do not justify the same jump, and a threshold that swings
from 2 to 5 on one bad month is a threshold nobody can reason about. Each
recommendation therefore names the adjacent value only.

The impact estimate is the part an operator actually decides on. Both the
current and the suggested threshold are run against the same search log, and
the candidate counts are reported side by side, so the cost of widening the
gate or the cost of narrowing it is visible before anything is changed.
"""

from datetime import timedelta

from django.utils import timezone

from .models import SearchSynonym
from .search_rewrites import DEFAULT_DAYS as CANDIDATE_DAYS, build_search_rewrite_candidates
from .search_synonym_effect import (
    DEFAULT_DAYS as CONFIRMATION_DAYS,
    DEFAULT_WINDOW_DAYS,
    build_search_synonym_effects,
)
from .search_rewrites import MIN_OCCURRENCES


# The values an operator could realistically type into MIN_OCCURRENCES. A
# suggestion is only useful if it names a real setting, so the module never
# returns a computed optimum - it returns the neighbouring rung. The ladder
# stops at five because a pair that recurs five times and is still unconfirmed
# is a pair nobody believes in; raising the bar past that hides the candidate
# list entirely rather than improving it.
THRESHOLD_LADDER = (1, 2, 3, 4, 5)

# How many judged synonyms a recommendation needs. Below this the hit rate is
# "one out of one" or "two out of three", which is an anecdote wearing the
# costume of a proportion.
MIN_JUDGED_SYNONYMS = 4

# A hit rate at or above this means most of the reviewer's time is producing
# measurable improvement, so the candidate pool is worth widening.
LOOSEN_HIT_RATE = 70

# A hit rate below this means most confirmed pairs changed nothing measurable,
# so the pool is mostly noise and the reviewer's time is better spent on fewer
# and stronger candidates.
TIGHTEN_HIT_RATE = 40

# Share of judged pairs that made search worse, above which no threshold change
# is recommended at all. See the module docstring: that is a pairing problem.
WORSE_SHARE_LIMIT = 40

# Share of judged pairs that landed in the flat band. A pool that is mostly
# flat is not confirming equivalences, so tightening is warranted even when the
# hit rate on its own looks acceptable.
FLAT_SHARE_LIMIT = 50

# How much flat evidence is tolerated while still recommending a loosening. Some
# pairs are always neither clearly right nor clearly wrong; a pool with a few of
# them is healthy, a pool that is half flat is not.
LOOSEN_FLAT_CEILING = 25

# How many confirmed synonyms to measure. Kept in step with the effect module's
# own default so both panels describe the same population.
DEFAULT_LIMIT = 20

RECOMMENDATION_LABELS = {
    'loosen': '建议放宽',
    'tighten': '建议收紧',
    'hold': '维持当前档位',
    'insufficient': '样本不足，暂不建议',
    'misconfigured': '不是阈值问题，暂不建议',
}

CONFIDENCE_LABELS = {
    'high': '依据充分',
    'medium': '依据一般',
    'low': '依据偏弱',
}


def _ladder_index(value):
    """Where a threshold sits on the ladder, or None if it is off the ladder."""
    try:
        return THRESHOLD_LADDER.index(int(value))
    except (TypeError, ValueError):
        return None


def _share(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _candidate_volume(days, *, now, min_occurrences):
    """How many candidates one setting of the gate would surface.

    The same log is re-read once per rung, which is cheap at these volumes and
    is the only way to show an operator what a change would actually cost:
    the candidate list is capped by DEFAULT_LIMIT, so the visible count alone
    would understate the work behind a looser gate.
    """
    report = build_search_rewrite_candidates(
        days=days, now=now, limit=1000, min_occurrences=min_occurrences,
    )
    return {
        'threshold': min_occurrences,
        'visible': len(report['rows']),
        'total': report['summary']['total_pair_count'],
        'occurrences': report['summary']['occurrence_total'],
        'is_current': min_occurrences == MIN_OCCURRENCES,
    }


def _build_recommendation(effects_summary):
    """Decide which way the gate is wrong, and by how much.

    The order of the checks is the decision itself. A pool that mostly
    backfires is checked first because it is the only case where moving the
    gate would actively hide a problem rather than solve one. Thin evidence is
    checked second because every number below it would be an anecdote. Only
    then are the two directions compared, and the flat band is what separates
    them: a pool that is half flat is not confirming equivalences, which
    tightens even when the hit rate on its own looks survivable.
    """
    summary = effects_summary['summary']
    judged = summary['judged_count']
    effective = summary['effective_count']
    flat = summary['flat_count']
    worse = summary['worse_count']

    if not judged:
        return {
            'action': 'insufficient',
            'direction': None,
            'suggested_threshold': None,
            'confidence': 'low',
            'reason': (
                '可判定的同义词数量为 0，命中率还说明不了候选池的质量。'
                '等确认并观察满一个周期的同义词累计到 %d 条再看。' % MIN_JUDGED_SYNONYMS
            ),
        }

    hit_rate = summary['hit_rate']
    worse_share = _share(worse, judged)
    flat_share = _share(flat, judged)

    if worse_share is not None and worse_share >= WORSE_SHARE_LIMIT:
        return {
            'action': 'misconfigured',
            'direction': None,
            'suggested_threshold': None,
            'confidence': 'medium',
            'reason': (
                '可判定的 %d 条同义词里有 %d 条让无结果率上升（占 %s%%），'
                '问题出在词对本身而不是候选门槛。收紧门槛只会把这些配错的词对藏起来不给人看，'
                '建议先回头核对这几条，再决定是否调整门槛。' % (judged, worse, worse_share)
            ),
        }

    if judged < MIN_JUDGED_SYNONYMS:
        return {
            'action': 'insufficient',
            'direction': None,
            'suggested_threshold': None,
            'confidence': 'low',
            'reason': (
                '可判定的同义词只有 %d 条，命中率还说明不了候选池的质量。'
                '等确认并观察满一个周期的同义词累计到 %d 条再看。'
                % (judged, MIN_JUDGED_SYNONYMS)
            ),
        }

    if (
        hit_rate is not None and hit_rate >= LOOSEN_HIT_RATE
        and (flat_share is None or flat_share <= LOOSEN_FLAT_CEILING)
    ):
        return {
            'action': 'loosen',
            'direction': 'down',
            'suggested_threshold': max(MIN_OCCURRENCES - 1, THRESHOLD_LADDER[0]),
            'confidence': 'high',
            'reason': (
                '可判定的 %d 条同义词里 %d 条让无结果率下降，命中率 %s%%，'
                '达到 %d%% 的放宽线，基本持平的只占 %s%%。'
                '说明运营确认过的词对大多是真实等价，当前门槛可能把低频但有效的改口挡在了外面。'
                % (
                    judged, effective, hit_rate, LOOSEN_HIT_RATE,
                    flat_share if flat_share is not None else 0,
                )
            ),
        }

    if hit_rate is not None and hit_rate < TIGHTEN_HIT_RATE:
        return {
            'action': 'tighten',
            'direction': 'up',
            'suggested_threshold': min(MIN_OCCURRENCES + 1, THRESHOLD_LADDER[-1]),
            'confidence': 'high',
            'reason': (
                '可判定的 %d 条同义词里只有 %d 条让无结果率下降，命中率 %s%%，'
                '低于 %d%%。确认过的词对大多没起作用，说明当前门槛放进来的候选'
                '噪声偏多，运营的时间被消耗在确认无效词对上。'
                % (judged, effective, hit_rate, TIGHTEN_HIT_RATE)
            ),
        }

    if flat_share is not None and flat_share >= FLAT_SHARE_LIMIT:
        return {
            'action': 'tighten',
            'direction': 'up',
            'suggested_threshold': min(MIN_OCCURRENCES + 1, THRESHOLD_LADDER[-1]),
            'confidence': 'medium',
            'reason': (
                '命中率 %s%% 还在 %d%% 的收紧线之上，但可判定的 %d 条里有 %d 条基本持平'
                '（占 %s%%，达到 %d%% 的持平上限）。这些词对没有让搜索变好也没有变坏，'
                '更可能是碰巧一起出现的两种说法，提高一档门槛能让候选列表集中在真正的等价关系上。'
                % (hit_rate, TIGHTEN_HIT_RATE, judged, flat, flat_share, FLAT_SHARE_LIMIT)
            ),
        }

    return {
        'action': 'hold',
        'direction': None,
        'suggested_threshold': None,
        'confidence': 'medium',
        'reason': (
            '命中率 %s%%，基本持平占 %s%%，落在 %d%%–%d%% 的合理区间内。'
            '当前门槛没有明显偏向噪声或漏判，维持现状继续观察。'
            % (
                hit_rate,
                flat_share if flat_share is not None else 0,
                TIGHTEN_HIT_RATE,
                LOOSEN_HIT_RATE,
            )
        ),
    }


def build_search_threshold_feedback(days=CONFIRMATION_DAYS, *, now=None):
    """Connect the synonym hit rate to the threshold that gates candidates.

    `days` selects which confirmed synonyms the effect module measures, the same
    way it does on the effect panel. The candidate volumes are estimated over
    the candidate module's own default window, because a threshold change is
    judged against what students are searching for now, not against a window
    chosen for a different question.
    """
    now = now or timezone.now()
    effects = build_search_synonym_effects(days=days, now=now)
    recommendation = _build_recommendation(effects)

    # The impact estimate always runs, even when nothing is recommended. An
    # operator who is told to hold still should still be able to see what the
    # neighbouring rung would have cost them, otherwise "hold" is an assertion
    # they cannot check.
    volumes = [
        _candidate_volume(CANDIDATE_DAYS, now=now, min_occurrences=value)
        for value in THRESHOLD_LADDER
    ]
    current_volume = next((row for row in volumes if row['is_current']), volumes[0])
    suggested = recommendation['suggested_threshold']
    suggested_volume = next(
        (row for row in volumes if row['threshold'] == suggested), None,
    )

    # A recommendation that names the value already in use is not a
    # recommendation. This happens at the ends of the ladder, where the
    # adjacent rung does not exist, and reporting it as a change would be
    # worse than saying nothing.
    if suggested == MIN_OCCURRENCES:
        recommendation['suggested_threshold'] = None
        recommendation['action'] = 'hold'
        recommendation['direction'] = None
        recommendation['confidence'] = 'medium'
        recommendation['reason'] += '不过 %d 已经在可调档位的尽头，没有相邻档位可换。' % MIN_OCCURRENCES
        suggested_volume = None

    # The difference each rung would make, computed here rather than in the
    # template: Django has no subtraction filter, and doing arithmetic in a
    # template is the kind of thing that gets copied into a place with no data
    # to guard it.
    for row in volumes:
        if row['is_current']:
            row['delta_total'] = 0
            row['delta_display'] = '当前档位'
        elif row['total'] < current_volume['total']:
            row['delta_total'] = current_volume['total'] - row['total']
            row['delta_display'] = '少 %d 条待确认' % row['delta_total']
        else:
            row['delta_total'] = row['total'] - current_volume['total']
            row['delta_display'] = '多 %d 条待确认' % row['delta_total']

    return {
        'period_days': days,
        'current_threshold': MIN_OCCURRENCES,
        'threshold_ladder': list(THRESHOLD_LADDER),
        'synonym_effects': effects,
        'recommendation': recommendation,
        'recommendation_label': RECOMMENDATION_LABELS[recommendation['action']],
        'confidence_label': CONFIDENCE_LABELS[recommendation['confidence']],
        'threshold_volumes': volumes,
        'current_volume': current_volume,
        'suggested_volume': suggested_volume,
        'has_data': bool(effects['rows']),
    }

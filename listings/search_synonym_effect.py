# -*- coding: utf-8 -*-
"""Close the loop between a confirmed synonym and what it did to search.

Synonym candidates are mined from real rewrites and confirmed by an operator,
and that is where the story stops today. Nobody can say whether the pairs that
were confirmed actually helped, which leaves the mining thresholds - how often a
rewrite must recur before it is worth showing - as a guess that never gets
corrected. If most confirmed pairs turn out not to move the needle, the
threshold is too loose and the operator is wading through noise; if confirmed
pairs routinely halve the zero-result rate, it is too tight and real conventions
are being withheld. Neither question is answerable from a one-way pipeline.

This module measures each confirmed synonym the same way the demand radar
measures a follow-up task: split the search log at the moment the synonym was
created, and compare the zero-result rate of the affected term on either side.
A falling rate means the expansion is doing its job. A flat rate means the pair
was probably not an equivalence after all, and a rising rate means the expansion
is actively harming that search.

The term being measured is the one students actually type, which is not the
same as the one the operator confirmed. Search expansion adds the synonym to
the query but the SearchQuery row keeps the original wording, so the measurement
has to follow the recorded query. Measuring the expanded term instead would
compare a term against itself and always report success.

A synonym that has been switched off is left out entirely. It no longer expands
anything, so any change in its search rate after that point belongs to the
market, not to the configuration - and crediting a disabled pair with an
improvement would be the easiest way to make this module lie.

The classification thresholds are borrowed from demand_radar_outcome rather
than reinvented. An operator who has learned that a ten-point drop means
"converged" on the radar page should not have to learn a second set of
numbers to read the same verdict on the search page.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .demand_radar_outcome import (
    CONVERGENCE_DELTA_POINTS,
    MATURITY_DAYS,
    MIN_WINDOW_SEARCHES,
)
from .models import SearchQuery, SearchSynonym


# How far either side of the confirmation moment to measure. The demand radar
# uses 14 days and the reasoning carries over unchanged: long enough to collect
# a few searches on a quiet term, short enough that the market has not moved on
# to different vocabulary since.
DEFAULT_WINDOW_DAYS = 14

# How many confirmed synonyms to report. The list is meant to be worked through
# the way the candidate list is, so it stays short and the strongest signal
# comes first.
DEFAULT_LIMIT = 20

# How far back to look for confirmed synonyms. A pair confirmed eight months ago
# has long since been overtaken by changes in what students search for, so the
# default window is deliberately shorter than the platform's whole history.
DEFAULT_DAYS = 180


def _rate(zero_count, total):
    return round(zero_count / total * 100, 1) if total else None


def _affected_terms(synonym):
    """The terms a student could type that this synonym expands.

    Both directions are returned because the expansion is symmetric: searching
    either side pulls in the other. Measuring only the keyword would miss the
    half of the traffic that arrives through the synonym's own wording.
    """
    terms = []
    for value in (synonym.keyword, synonym.synonym):
        cleaned = (value or '').strip()
        if cleaned:
            terms.append(cleaned)
    return list(dict.fromkeys(terms))


def _term_window_stats(term, start, end):
    """Count searches for one exact term and their zero-result subset."""
    queryset = SearchQuery.objects.filter(
        created_at__gte=start, created_at__lt=end, query__iexact=term,
    )
    totals = queryset.aggregate(
        total=Count('id'),
        zero=Count('id', filter=Q(result_count=0)),
    )
    return totals['total'] or 0, totals['zero'] or 0


def _classify_delta(delta_points):
    """Reuse the radar verdicts so both pages speak one language."""
    if delta_points is None:
        return 'insufficient'
    if delta_points >= CONVERGENCE_DELTA_POINTS:
        return 'effective'
    if delta_points <= -CONVERGENCE_DELTA_POINTS:
        return 'worse'
    return 'flat'


# Outcome labels for the search page. The keys line up with the radar's so a
# reader who knows one page can read the other, but the wording is about search
# rather than about supply gaps.
EFFECT_LABELS = {
    'effective': '无结果率下降',
    'flat': '基本持平',
    'worse': '无结果率上升',
    'insufficient': '样本不足',
    'pending': '观察期未结束',
}



def build_synonym_effect(synonym, *, now=None, window_days=DEFAULT_WINDOW_DAYS):
    """Measure one confirmed synonym against the searches it was meant to fix."""
    now = now or timezone.now()
    window = timedelta(days=window_days)
    created_at = synonym.created_at

    base = {
        'synonym_id': synonym.id,
        'keyword': synonym.keyword,
        'synonym': synonym.synonym,
        'is_active': synonym.is_active,
        'created_at': created_at,
        'window_days': window_days,
        'terms': _affected_terms(synonym),
        'baseline_searches': 0,
        'baseline_zero_searches': 0,
        'observation_searches': 0,
        'observation_zero_searches': 0,
        'baseline_zero_rate': None,
        'observation_zero_rate': None,
        'delta_points': None,
        'outcome': 'pending',
        'outcome_label': EFFECT_LABELS['pending'],
        'signal_total': 0,
        'is_mature': (now - created_at) >= timedelta(days=MATURITY_DAYS),
        'verdict': None,
    }

    # A switched-off synonym expands nothing, so the searches after that point
    # say nothing about it. The row is still returned - an operator looking at a
    # pair they disabled deserves to see that it is excluded and why - but it is
    # never scored.
    if not synonym.is_active:
        base['verdict'] = '该同义词已停用，停用后的搜索变化与它无关，不参与效果统计。'
        return base

    base_start = created_at - window
    base_end = created_at
    obs_start = created_at
    obs_end = created_at + window

    # Both sides of the pair are measured and added together, because a student
    # may arrive through either wording.
    for term in base['terms']:
        baseline_total, baseline_zero = _term_window_stats(term, base_start, base_end)
        observation_total, observation_zero = _term_window_stats(term, obs_start, obs_end)
        base['baseline_searches'] += baseline_total
        base['baseline_zero_searches'] += baseline_zero
        base['observation_searches'] += observation_total
        base['observation_zero_searches'] += observation_zero

    base['signal_total'] = base['baseline_searches'] + base['observation_searches']
    base['baseline_zero_rate'] = _rate(base['baseline_zero_searches'], base['baseline_searches'])
    base['observation_zero_rate'] = _rate(base['observation_zero_searches'], base['observation_searches'])

    # The observation window is clipped at "now": a rate computed partly from
    # searches that have not happened yet would understate failure.
    if now < obs_end:
        base['outcome'] = 'pending'
        base['outcome_label'] = EFFECT_LABELS['pending']
        base['verdict'] = '观察期尚未结束，暂不判定这条同义词的效果。'
        return base

    if (
        base['baseline_searches'] < MIN_WINDOW_SEARCHES
        or base['observation_searches'] < MIN_WINDOW_SEARCHES
    ):
        base['outcome'] = 'insufficient'
        base['outcome_label'] = EFFECT_LABELS['insufficient']
        base['verdict'] = '确认前后任意一侧搜索量不足，结论不可靠。'
        return base

    baseline_rate = base['baseline_zero_rate']
    observation_rate = base['observation_zero_rate']
    delta = round(baseline_rate - observation_rate, 1)
    base['delta_points'] = delta
    outcome = _classify_delta(delta)
    base['outcome'] = outcome
    base['outcome_label'] = EFFECT_LABELS[outcome]

    if outcome == 'effective':
        base['verdict'] = '无结果率明显下降，这条同义词确实把原来的死路救活了。'
    elif outcome == 'worse':
        base['verdict'] = '无结果率反而上升，需要核对该词对是否配错，或是否该停用。'
    else:
        base['verdict'] = '无结果率基本持平，可能这两个词本来就不指同一类东西。'
    return base



def build_search_synonym_effects(
    days=DEFAULT_DAYS, *, now=None, limit=DEFAULT_LIMIT, window_days=DEFAULT_WINDOW_DAYS,
):
    """Summarise what the confirmed synonyms actually did to the searches.

    Two windows are involved again, and they mean different things. "days"
    selects which confirmed synonyms are reported - how recently an operator
    confirmed them - while "window_days" is the measurement span on either
    side of each confirmation. Keeping them apart matters: an operator asking
    about the last six months of synonym work should not be forced into a
    six-month comparison window, which would bury every pair's signal in
    unrelated drift in what students search for.
    """
    now = now or timezone.now()
    synonyms = list(
        SearchSynonym.objects.filter(
            created_at__gte=now - timedelta(days=days), created_at__lte=now,
        ).order_by('-created_at')[:max(limit, 1)]
    )

    rows = [
        build_synonym_effect(synonym, now=now, window_days=window_days)
        for synonym in synonyms
    ]
    # The pairs an operator can act on come first: a synonym that is making
    # search worse is more urgent than one that is helping, and both outrank a
    # pair nobody has searched for since.
    rows.sort(key=lambda row: (
        0 if row['outcome'] == 'worse' else 1,
        0 if row['outcome'] == 'pending' else 1,
        -(row['delta_points'] or 0),
        -row['signal_total'],
        row['keyword'],
    ))

    scored = [
        row for row in rows
        if row['outcome'] not in ('pending', 'insufficient')
    ]
    effective = sum(row['outcome'] == 'effective' for row in scored)
    worse = sum(row['outcome'] == 'worse' for row in scored)
    # The hit rate is computed only over synonyms with usable evidence on both
    # sides. Counting a pair nobody searched for as a failure would punish the
    # metric for a quiet term, and counting it as a success would flatter it.
    judged = len(scored)
    hit_rate = round(effective / judged * 100, 1) if judged else None

    active_total = SearchSynonym.objects.filter(is_active=True).count()
    deltas = [row['delta_points'] for row in scored if row['delta_points'] is not None]
    median_delta = None
    if deltas:
        ordered = sorted(deltas)
        middle = len(ordered) // 2
        median_delta = (
            ordered[middle]
            if len(ordered) % 2
            else round((ordered[middle - 1] + ordered[middle]) / 2, 1)
        )

    return {
        'rows': rows,
        'period_days': days,
        'window_days': window_days,
        'has_data': bool(rows),
        'summary': {
            'synonym_count': len(rows),
            'active_total': active_total,
            'pending_count': sum(row['outcome'] == 'pending' for row in rows),
            'insufficient_count': sum(row['outcome'] == 'insufficient' for row in rows),
            'effective_count': effective,
            'flat_count': sum(row['outcome'] == 'flat' for row in scored),
            'worse_count': worse,
            'judged_count': judged,
            'hit_rate': hit_rate,
            'median_delta_points': median_delta,
        },
        'summary_text': _summary_text(rows, scored, effective, worse, hit_rate),
    }


def _summary_text(rows, scored, effective, worse, hit_rate):
    """One sentence for the page, the most actionable fact first."""
    if not rows:
        return '周期内没有新确认的同义词，暂无回流数据。'
    if worse:
        return (
            f'周期内确认的 {len(rows)} 条同义词中有 {worse} 条让无结果率上升，'
            f'建议优先核对这些词对是否配错。'
        )
    if not scored:
        return (
            f'周期内确认的 {len(rows)} 条同义词都还没有足够的搜索量，'
            f'暂时无法判断效果。'
        )
    if hit_rate is None:
        return f'周期内确认的 {len(rows)} 条同义词暂无可判定样本。'
    return (
        f'周期内确认的 {len(rows)} 条同义词中，{effective} 条让无结果率下降，'
        f'占可判定样本的 {hit_rate}%。'
    )


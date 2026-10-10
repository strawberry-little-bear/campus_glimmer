# -*- coding: utf-8 -*-
"""Read the pairs an operator dismissed, and what their frequency says.

A rejected candidate used to leave no trace. The pair came back into the list
the following week, the reviewer read it a third time, and the only signal
`search_threshold_feedback` had was the confirmation hit rate - a pool with two
hundred confirmations and two hundred quiet dismissals was indistinguishable
from one with two hundred confirmations and none. The gate that decides what an
operator ever sees was therefore being corrected from one side only.

This module is the other side. It joins the rejection table against the same
rewrite log the miner uses, so each dismissed pair is reported with how often
the rewrite actually recurs, and the reasons are broken out separately. That
frequency is the point: `not_equivalent` on a pair that recurs forty times is a
statement about the platform's vocabulary, while `not_equivalent` on a pair that
recurs twice is a statement about one reviewer's taste. Counting rejections
without weighting them by occurrence would let an operator who dismisses
aggressively look like an operator facing a noisy pool.

Two boundaries keep the reading honest.

A rejection is not counted as a judgement on the threshold unless the pair
would have passed it. Dismissing a pair that only ever occurred once says
nothing about whether the bar sits at two or at three, because the bar was never
the reason the reviewer saw it. Only pairs that reached the current threshold
are fed to the threshold summary, and the rest are reported as below-threshold
rather than dropped.

The module reports and stops there. It never writes to the rejection table, never
reverses a rejection, and never touches the constant the threshold module is
advising about. What it produces is a description an operator can argue with,
including the cases where the description is unflattering to the operator.
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import SearchQuery, SearchSynonymRejection
from .search_rewrites import (
    DEFAULT_DAYS as CANDIDATE_DAYS,
    MAX_REWRITE_GAP,
    MIN_OCCURRENCES,
    _normalize,
)

# How many days of the rewrite log to mine when attaching occurrences to a
# rejected pair. The candidate module's own default is reused so both panels
# describe the same window; a rejection judged over a longer window would carry
# occurrence counts that the candidate list cannot reproduce.
DEFAULT_DAYS = CANDIDATE_DAYS

# How many rejections the table reports before the panel stops listing them one
# by one. The list is meant to be worked through, and a page of it that scrolls
# forever gets scrolled past instead.
DEFAULT_LIMIT = 20

# A dismissed pair whose rewrite recurs at least this often is strong evidence
# that the pool is being filtered by a human rather than by the data. The number
# is the candidate module's own "strong evidence" rung, so both panels call the
# same frequency strong.
STRONG_OCCURRENCES = 3

# Share of threshold-passing rejections whose reason is the pair being wrong,
# above which the pool is described as misread rather than mistuned. A pool that
# keeps surfacing pairs the reviewer insists are unrelated is a mining problem,
# and moving the bar would only hide the pairs that are merely rare.
MISREAD_REJECTION_LIMIT = 50

# Share of threshold-passing rejections whose reason is thin evidence. When most
# dismissals are "too rare", the reviewer is agreeing with the miner that these
# are not yet conventions, which is an argument for a higher bar.
THIN_EVIDENCE_LIMIT = 40

# How many threshold-passing rejections a statement about the pool needs. Below
# this the shares are one-out-of-two and one-out-of-three, which is an anecdote.
MIN_JUDGED_REJECTIONS = 3

REASON_LABELS = dict(SearchSynonymRejection.REASON_CHOICES)


def _reason_label(code):
    return REASON_LABELS.get(code, code)


def _rewrite_rows(*, start, now):
    """Yield the search rows the miner chains into rewrites, in order.

    The filter mirrors `build_search_rewrite_candidates` exactly - same window,
    same requirement that the search belongs to a user, same ordering. A
    different query set here would attach occurrence counts to a rejection that
    the candidate panel cannot reproduce, and the two panels would disagree
    about the same pair for no visible reason.
    """
    return SearchQuery.objects.filter(
        created_at__gte=start, created_at__lte=now, user__isnull=False,
    ).exclude(query='').select_related('user').order_by(
        'user_id', 'created_at', 'id',
    ).values('user_id', 'query', 'result_count', 'created_at')


def _collect_rejected_pairs(days=DEFAULT_DAYS, *, now=None):
    """Attach occurrence counts to every standing rejection in the window.

    The rewrite log is walked once for all rejected pairs rather than once per
    pair: the log is the expensive part of this module and the number of
    rejections is small.
    """
    now = now or timezone.now()
    start = now - timedelta(days=days)
    rejected = {}
    for source, target, reason, note, created_at in SearchSynonymRejection.objects.filter(
        superseded_at__isnull=True,
    ).values_list('source', 'target', 'reason', 'note', 'created_at'):
        left, right = _normalize(source), _normalize(target)
        if left and right:
            rejected[(left, right)] = {
                'source': left,
                'target': right,
                'reason': reason,
                'reason_label': _reason_label(reason),
                'note': note,
                'created_at': created_at,
                'occurrences': 0,
                'user_count': 0,
                'last_seen': None,
            }
    if not rejected:
        return [], rejected

    wanted = set(rejected)
    rows = _rewrite_rows(start=start, now=now)

    pairs = {}
    previous = None
    for row in rows:
        if previous is None or previous['user_id'] != row['user_id']:
            previous = row
            continue
        gap = row['created_at'] - previous['created_at']
        if gap > MAX_REWRITE_GAP:
            previous = row
            continue
        source = _normalize(previous['query'])
        target = _normalize(row['query'])
        if (
            source and target and source != target
            and previous['result_count'] == 0 and row['result_count'] > 0
        ):
            key = (source, target)
            if key in wanted:
                entry = pairs.setdefault(
                    key, {'occurrences': 0, 'users': set(), 'last_seen': row['created_at']},
                )
                entry['occurrences'] += 1
                entry['users'].add(row['user_id'])
                if row['created_at'] > entry['last_seen']:
                    entry['last_seen'] = row['created_at']
        previous = row

    for key, entry in pairs.items():
        row = rejected[key]
        row['occurrences'] = entry['occurrences']
        row['user_count'] = len(entry['users'])
        row['last_seen'] = entry['last_seen']
    return list(rejected.values()), rejected


def supersede_rejections_for_pair(source, target):
    """Mark dismissals of a pair that has just been confirmed as a synonym.

    A rejection and a confirmation of the same pair are contradictory records,
    and the confirmation wins: the pair is now in the synonym table and expands
    searches. The rejection is not deleted, because the reviewer who reconsiders
    should be able to see that somebody already said no and was overruled -
    that is a different situation from nobody ever having looked.

    Both directions are superseded, matching how the miner excludes pairs. A
    dismissal of "移动电源 → 充电宝" is the same judgement as one of the reverse
    pair, and leaving it in force would hide a candidate the platform now
    actively supports.
    """
    left, right = _normalize(source), _normalize(target)
    if not left or not right:
        return 0
    return SearchSynonymRejection.objects.filter(
        superseded_at__isnull=True,
    ).filter(
        Q(source=left, target=right) | Q(source=right, target=left),
    ).update(superseded_at=timezone.now())


def restore_rejections_for_pair(source, target):
    """Bring back dismissals of a pair whose synonym has been removed.

    Deleting the synonym is the operator saying the pair should not expand
    searches after all. Whether the original dismissal was right is a separate
    question this function does not answer - it only puts the record back in
    force so the candidate stops being hidden by a decision that no longer has
    anything to do with it.
    """
    left, right = _normalize(source), _normalize(target)
    if not left or not right:
        return 0
    return SearchSynonymRejection.objects.filter(
        superseded_at__isnull=False,
    ).filter(
        Q(source=left, target=right) | Q(source=right, target=left),
    ).update(superseded_at=None)


def build_search_rejection_stats(days=DEFAULT_DAYS, *, now=None):
    """Describe what was dismissed, how often it recurs, and what that implies."""
    now = now or timezone.now()
    rows, rejected = _collect_rejected_pairs(days=days, now=now)

    for row in rows:
        row['passes_threshold'] = row['occurrences'] >= MIN_OCCURRENCES
        row['is_strong'] = row['occurrences'] >= STRONG_OCCURRENCES
        row['occurrence_display'] = '%d 次' % row['occurrences']
        row['threshold_display'] = '达到门槛' if row['passes_threshold'] else '低于门槛'

    rows.sort(
        key=lambda row: (
            not row['passes_threshold'], -row['occurrences'], row['source'],
        ),
    )

    total = len(rows)
    passing = [row for row in rows if row['passes_threshold']]
    strong = [row for row in rows if row['is_strong']]
    by_reason = {}
    for row in rows:
        bucket = by_reason.setdefault(
            row['reason'],
            {
                'reason': row['reason'],
                'reason_label': row['reason_label'],
                'count': 0,
                'passing_count': 0,
                'occurrence_total': 0,
                'strong_count': 0,
            },
        )
        bucket['count'] += 1
        bucket['occurrence_total'] += row['occurrences']
        bucket['strong_count'] += 1 if row['is_strong'] else 0
        bucket['passing_count'] += 1 if row['passes_threshold'] else 0

    reason_rows = sorted(
        by_reason.values(), key=lambda row: (-row['count'], row['reason']),
    )
    for row in reason_rows:
        row['share'] = round(row['count'] / total * 100, 1) if total else 0
        row['average_occurrences'] = (
            round(row['occurrence_total'] / row['count'], 1) if row['count'] else 0
        )

    judged = len(passing)
    not_equivalent = sum(1 for row in passing if row['reason'] == 'not_equivalent')
    too_rare = sum(1 for row in passing if row['reason'] == 'too_rare')
    ambiguous = sum(1 for row in passing if row['reason'] == 'ambiguous')
    occurrence_total = sum(row['occurrences'] for row in passing)

    if not judged:
        signal = {
            'kind': 'insufficient',
            'label': '否决记录还不足以判断门槛',
            'reason': (
                '目前有 %d 条否决记录，其中没有一条的出现次数达到当前门槛 %d 次。'
                '这些否决是在候选列表之外做的判断，说明不了门槛该松还是该紧。'
                '等运营在候选列表里否决够 %d 条再回头看。'
                % (total, MIN_OCCURRENCES, MIN_JUDGED_REJECTIONS)
            ),
        }
    elif judged < MIN_JUDGED_REJECTIONS:
        signal = {
            'kind': 'insufficient',
            'label': '否决样本还太少',
            'reason': (
                '达到当前门槛的否决只有 %d 条，占比类结论在这么小的样本上只是观感。'
                '等累计到 %d 条再据此判断门槛。' % (judged, MIN_JUDGED_REJECTIONS)
            ),
        }
    else:
        misread_share = round(not_equivalent / judged * 100, 1)
        thin_share = round(too_rare / judged * 100, 1)
        if misread_share >= MISREAD_REJECTION_LIMIT:
            signal = {
                'kind': 'misread',
                'label': '更像候选池读错了，不是门槛问题',
                'reason': (
                    '达到门槛的 %d 条否决里有 %d 条被判定为「不是同一个东西」（占 %s%%）。'
                    '这些词对已经反复出现，运营仍然认为它们不是等价关系，'
                    '问题出在「先搜 A 无结果、随后搜 B 有结果」这条挖掘规则本身，'
                    '而不在门槛高低：调高一档只会把这些词对藏起来不给人看，'
                    '和把配错的同义词藏起来是同一种回避。建议先回头改挖掘规则。'
                    % (judged, not_equivalent, misread_share)
                ),
            }
        elif thin_share >= THIN_EVIDENCE_LIMIT:
            signal = {
                'kind': 'tighten',
                'label': '否决集中在证据不足，支持收紧',
                'reason': (
                    '达到门槛的 %d 条否决里有 %d 条被判定为「出现次数太少，证据不足」（占 %s%%）。'
                    '这些词对恰好卡在门槛上，运营看到之后的第一反应是证据不够而不是配错，'
                    '说明当前 %d 次的门槛放进来的候选偏薄。这与门槛建议模块的收紧方向一致，'
                    '但抬高一档会让多少候选不再出现，仍以门槛建议里的档位表为准。'
                    % (judged, too_rare, thin_share, MIN_OCCURRENCES)
                ),
            }
        else:
            signal = {
                'kind': 'hold',
                'label': '否决没有指向单一方向',
                'reason': (
                    '达到门槛的 %d 条否决里，「不是同一个东西」占 %s%%、「出现次数太少」占 %s%%。'
                    '两类理由都不占多数，说明这些否决更可能是逐条判断而不是门槛偏向某一侧，'
                    '维持当前 %d 次门槛继续观察。'
                    % (judged, misread_share, thin_share, MIN_OCCURRENCES)
                ),
            }

    return {
        'period_days': days,
        'current_threshold': MIN_OCCURRENCES,
        'rows': rows[:DEFAULT_LIMIT],
        'total_count': total,
        'reason_rows': reason_rows,
        'judged_count': judged,
        'passing_count': judged,
        'below_threshold_count': total - judged,
        'strong_count': len(strong),
        'occurrence_total': occurrence_total,
        'not_equivalent_count': not_equivalent,
        'too_rare_count': too_rare,
        'ambiguous_count': ambiguous,
        'signal': signal,
        'has_data': bool(rows),
    }

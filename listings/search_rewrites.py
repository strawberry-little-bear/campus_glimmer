# -*- coding: utf-8 -*-
"""Mine real query rewrites into synonym candidates for operator review.

Synonyms are maintained by hand, which forces an operator to guess that a
student who cannot find "充电宝" will next try "移动电源". The guesses are
usually incomplete, and every missing pair leaves a search that returns nothing
when the platform actually has the item.

The behaviour is already recorded. When one user searches a term, gets nothing,
and then immediately searches a different term that does return results, that
pair is evidence of an equivalence the student already discovered. This module
collects those pairs and ranks them by how often they recur.

Candidates are deliberately not applied automatically. A synonym expands the
result set of every future search, so a wrong pair would silently degrade
search for everyone - "iPhone" paired with "手机壳" would flood a phone search
with cases. The module therefore only proposes; an operator still confirms in
the admin, where a single action creates the SearchSynonym row.

Two boundaries keep the evidence honest. The pair must come from one user's
adjacent searches, because two unrelated users searching two unrelated terms
prove nothing about equivalence. And the gap between the two searches must be
short, because a rewrite half a day later is a new need rather than a
restatement of the old one.

A third boundary is written by the operator rather than inferred from the log.
A pair that has been dismissed with a SearchSynonymRejection row is dropped
from the list, because a reviewer who already read the pair and said no should
not be made to read it again next week. Rejections are consulted in both
directions like active synonyms, and a rejection that has been superseded by a
later confirmation stops hiding the pair. The module never writes those rows;
it only reads them.
"""

from datetime import timedelta

from django.utils import timezone

from .models import SearchQuery, SearchSynonym, SearchSynonymRejection

# Two searches further apart than this are treated as separate needs. The
# student has moved on to something else, so the second term says nothing about
# how to restate the first.
MAX_REWRITE_GAP = timedelta(minutes=30)

# A pair must be seen this many times before it is worth an operator's
# attention. One person changing their wording is a coincidence; several people
# changing it the same way is a convention.
MIN_OCCURRENCES = 2

# How many candidates to return. The list is meant to be worked through, not
# scrolled, so it stays short and the strongest evidence comes first.
DEFAULT_LIMIT = 12

# How many search days to look back. Longer windows surface rarer rewrites but
# also drift into vocabulary that has since changed, so the default is short.
DEFAULT_DAYS = 30


def _normalize(value):
    """Collapse a query to a comparable form.

    Case and surrounding whitespace are not meaningful differences: "台灯"
    and "台灯 " are the same need, and reporting them as a rewrite pair would
    waste an operator's time.
    """
    return ' '.join((value or '').strip().casefold().split())[:120]


def _existing_pairs():
    """Load active synonym pairs in both directions for exclusion."""
    pairs = set()
    for keyword, synonym in SearchSynonym.objects.filter(is_active=True).values_list(
        'keyword', 'synonym',
    ):
        left, right = _normalize(keyword), _normalize(synonym)
        if left and right:
            pairs.add((left, right))
            pairs.add((right, left))
    return pairs


def _rejected_pairs():
    """Load dismissed rewrite pairs in both directions for exclusion.

    Only rejections that still stand are returned. One whose pair was confirmed
    as a synonym afterwards is marked superseded, and a superseded rejection
    hides nothing: the operator reconsidered, and this module reports the state
    of the reviewer's queue now, not a decision that has since been overruled.

    The set is expanded in both directions because a dismissal is one judgement,
    not two: recording "移动电源 -> 充电宝" must hide the candidate the miner
    built as "充电宝 -> 移动电源". That expansion makes the set the wrong thing
    to count. A panel that reports len() of it tells an operator they dismissed
    two pairs when they dismissed one, and a count that grows just because the
    same judgement was written down in the other order is a count nobody can
    reconcile with the table. `rejected_count` is therefore taken from the row
    count, not from the set size.
    """
    pairs = set()
    for source, target in SearchSynonymRejection.objects.filter(
        superseded_at__isnull=True,
    ).values_list('source', 'target'):
        left, right = _normalize(source), _normalize(target)
        if left and right:
            pairs.add((left, right))
            pairs.add((right, left))
    return pairs


def _rejected_row_count():
    """How many dismissals are still in force, one per recorded judgement."""
    return SearchSynonymRejection.objects.filter(superseded_at__isnull=True).count()


def _build_pair_row(source, target, occurrences, users, last_seen):
    return {
        'source': source,
        'target': target,
        'occurrences': occurrences,
        'user_count': len(users),
        'last_seen': last_seen,
    }


def build_search_rewrite_candidates(
    days=DEFAULT_DAYS, *, now=None, limit=DEFAULT_LIMIT,
    min_occurrences=MIN_OCCURRENCES,
):
    """Rank observed query rewrites as synonym candidates for review."""
    now = now or timezone.now()
    start = now - timedelta(days=days)
    existing = _existing_pairs()
    rejected = _rejected_pairs()

    # Only searches tied to a user can form a pair; anonymous sessions carry no
    # continuity, so chaining them would attribute one person's rewrite to
    # whoever happened to search next.
    rows = list(
        SearchQuery.objects.filter(
            created_at__gte=start, created_at__lte=now, user__isnull=False,
        )
        .exclude(query='')
        .select_related('user')
        .order_by('user_id', 'created_at', 'id')
        .values('user_id', 'query', 'result_count', 'created_at')
    )

    pairs = {}
    previous = None
    for row in rows:
        # A new user starts a fresh timeline: the previous row belongs to
        # somebody else and cannot be chained to this one.
        if previous is None or previous['user_id'] != row['user_id']:
            previous = row
            continue

        gap = row['created_at'] - previous['created_at']
        if gap > MAX_REWRITE_GAP:
            previous = row
            continue

        source = _normalize(previous['query'])
        target = _normalize(row['query'])
        # A failed search followed by a successful one is the signal. Requiring
        # both directions is what separates "I rephrased and found it" from
        # "I gave up and tried something else".
        if (
            source and target and source != target
            and previous['result_count'] == 0 and row['result_count'] > 0
        ):
            key = (source, target)
            entry = pairs.setdefault(key, {
                'occurrences': 0, 'users': set(), 'last_seen': row['created_at'],
            })
            entry['occurrences'] += 1
            entry['users'].add(row['user_id'])
            if row['created_at'] > entry['last_seen']:
                entry['last_seen'] = row['created_at']
        previous = row

    candidates = [
        _build_pair_row(source, target, entry['occurrences'], entry['users'], entry['last_seen'])
        for (source, target), entry in pairs.items()
        if entry['occurrences'] >= min_occurrences
    ]
    # Pairs already configured as synonyms are dropped: re-listing them would
    # make the operator re-read work they have already done.
    candidates = [row for row in candidates if (row['source'], row['target']) not in existing]
    # A dismissed pair is not evidence any more, it is a decision already made.
    # Filtering here rather than at render time keeps the candidate counts
    # honest: the panel's totals describe the queue an operator still has to
    # work through, not a longer list with the finished rows painted over.
    candidates = [row for row in candidates if (row['source'], row['target']) not in rejected]
    candidates.sort(
        key=lambda row: (-row['occurrences'], -row['user_count'], row['source']),
    )

    visible = candidates[:max(limit, 1)]
    for row in visible:
        # Repeated rewrites by the same person are stronger evidence than one
        # rewrite each by many people, so the confidence hint rewards depth.
        row['confidence'] = min(100, round(row['occurrences'] / max(1, row['user_count']) * 40))
    return {
        'rows': visible,
        'period_days': days,
        'has_data': bool(visible),
        'summary': {
            'candidate_count': len(visible),
            'total_pair_count': len(candidates),
            'occurrence_total': sum(row['occurrences'] for row in visible),
            'strong_count': sum(row['occurrences'] >= 3 for row in visible),
            'rejected_count': _rejected_row_count(),
        },
    }

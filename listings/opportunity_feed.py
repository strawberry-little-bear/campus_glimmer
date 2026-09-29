"""Build a personalized, explainable stream of campus help opportunities."""

from django.db.models import Q
from django.utils import timezone

from .demand_matching import _match_demand
from .lost_found_matching import score_lost_found_posts
from .models import DemandPost, Item, LostFoundPost


def _active_demands():
    now = timezone.now()
    return DemandPost.objects.select_related('requester', 'category', 'location').filter(
        status='active',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )


def _active_lost_found_posts(post_type=None):
    """Return public lost-and-found records that are still within their window."""
    now = timezone.now()
    queryset = LostFoundPost.objects.select_related(
        'reporter', 'category', 'location',
    ).filter(
        status='active',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    if post_type:
        queryset = queryset.filter(post_type=post_type)
    return queryset


def build_opportunity_feed(user, *, limit=12):
    """Return opportunities where the user's existing actions can help someone.

    The feed intentionally only uses records the user is already allowed to see:
    available items owned by the user and active public lost-and-found records.
    Each recommendation carries the matching reasons so it remains explainable.
    A target demand or lost-and-found post appears once, using the strongest
    available match when the user has multiple possible source records.
    """
    available_items = list(
        Item.objects.available().filter(seller=user).select_related('category', 'location')[:80]
    )
    demands = list(_active_demands().exclude(requester=user)[:300])
    best_demands = {}
    for item in available_items:
        for demand in demands:
            match = _match_demand(demand, item)
            if not match:
                continue
            score, reason_text = match
            opportunity = {
                'kind': 'demand',
                'title': demand.title,
                'description': demand.description,
                'target': demand,
                'item': item,
                'score': score,
                'reason_text': reason_text,
                'created_at': demand.created_at,
            }
            previous = best_demands.get(demand.pk)
            if not previous or (score, item.created_at) > (
                previous['score'], previous['item'].created_at,
            ):
                best_demands[demand.pk] = opportunity

    own_posts = list(_active_lost_found_posts().filter(reporter=user)[:40])
    candidate_types = {post.post_type for post in own_posts}
    opposite_types = {'found' if post_type == 'lost' else 'lost' for post_type in candidate_types}
    candidates = list(
        _active_lost_found_posts().filter(post_type__in=opposite_types).exclude(reporter=user)[:300]
    ) if opposite_types else []
    candidates_by_type = {}
    for candidate in candidates:
        candidates_by_type.setdefault(candidate.post_type, []).append(candidate)

    best_lost_found = {}
    for post in own_posts:
        opposite_type = 'found' if post.post_type == 'lost' else 'lost'
        for candidate in candidates_by_type.get(opposite_type, []):
            score, reasons = score_lost_found_posts(post, candidate)
            if score < 30:
                continue
            opportunity = {
                'kind': 'lost_found',
                'title': candidate.title,
                'description': candidate.description,
                'target': candidate,
                'source': post,
                'score': score,
                'reason_text': '、'.join(reasons),
                'created_at': candidate.created_at,
            }
            previous = best_lost_found.get(candidate.pk)
            if not previous or (score, post.created_at) > (
                previous['score'], previous['source'].created_at,
            ):
                best_lost_found[candidate.pk] = opportunity

    opportunities = sorted(
        list(best_demands.values()) + list(best_lost_found.values()),
        key=lambda row: (row['score'], row['created_at']),
        reverse=True,
    )[:limit]
    return {
        'opportunities': opportunities,
        'demand_count': len(best_demands),
        'lost_found_count': len(best_lost_found),
        'available_item_count': len(available_items),
        'active_post_count': len(own_posts),
    }
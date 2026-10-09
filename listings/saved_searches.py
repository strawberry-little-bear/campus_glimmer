from collections import defaultdict

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from .models import Item, SavedSearch, SavedSearchMatch
from .notifications import create_notification


def _item_searchable_text(item):
    return ' '.join(
        value for value in (
            item.title, item.description, item.condition,
            item.category.name if item.category else '',
            item.location.name if item.location else '',
            item.location.building if item.location else '',
        ) if value
    ).casefold()


def describe_saved_search_match(saved_search, item):
    """Return the human-readable dimensions that made this item match.

    The reasons are reused by the instant notification, the digest notice and
    the saved-search page, so a user always sees the same explanation for why a
    condition fired.
    """
    reasons = []
    if saved_search.query:
        query = saved_search.query.casefold()
        if query in _item_searchable_text(item):
            reasons.append(f'包含关键词“{saved_search.query}”')
    if saved_search.condition and saved_search.condition.casefold() in item.condition.casefold():
        reasons.append(f'成色为“{saved_search.condition}”')
    if saved_search.category_id and saved_search.category_id == item.category_id:
        reasons.append(f'分类为“{saved_search.category.name}”')
    if saved_search.location_id and saved_search.location_id == item.location_id:
        reasons.append(f'交易地点在“{saved_search.location.name}”')
    if saved_search.min_price is not None and item.price >= saved_search.min_price:
        reasons.append(f'价格不低于 ¥{saved_search.min_price}')
    if saved_search.max_price is not None and item.price <= saved_search.max_price:
        reasons.append(f'价格不高于 ¥{saved_search.max_price}')
    return reasons


def matches_saved_search(saved_search, item):
    """Whether an item still satisfies every configured criterion of a saved search."""
    if saved_search.query:
        if saved_search.query.casefold() not in _item_searchable_text(item):
            return False
    if saved_search.condition and saved_search.condition.casefold() not in item.condition.casefold():
        return False
    if saved_search.category_id and saved_search.category_id != item.category_id:
        return False
    if saved_search.location_id and saved_search.location_id != item.location_id:
        return False
    if saved_search.min_price is not None and item.price < saved_search.min_price:
        return False
    if saved_search.max_price is not None and item.price > saved_search.max_price:
        return False
    return True


def _notify_instant(saved_search, item, reasons):
    """Send the single-item notification used by the default instant cadence."""
    reason_text = '、'.join(reasons) if reasons else '符合你设置的条件'
    return create_notification(
        saved_search.user,
        kind='saved_search_match',
        title='发现符合条件的新商品',
        message=f'新商品“{item.title}”符合你的关注“{saved_search.name}”（{reason_text}），快去看看吧。',
        item=item,
        target_url=reverse('item_detail', args=[item.id]),
        dedupe_key=f'saved-search:{saved_search.pk}:{item.pk}',
        dedupe_forever=True,
    )


def notify_saved_search_matches(item):
    """Route a newly published item to every saved search that can still be notified.

    Instant searches are notified immediately. Daily and weekly searches buffer
    the hit so a scheduled digest can merge several items into one notice, and
    searches inside a silent window are simply skipped for now. Buffering is
    idempotent: the unique constraint means re-publishing the same item never
    produces a duplicate digest entry.
    """
    item = (
        Item.objects.select_related('category', 'location', 'seller')
        .get(pk=item.pk)
    )
    active_searches = SavedSearch.objects.filter(is_active=True).exclude(user=item.seller)
    now = timezone.now()
    instant_matches_by_user = defaultdict(list)

    for saved_search in active_searches.select_related('user', 'category', 'location'):
        if not matches_saved_search(saved_search, item):
            continue
        reasons = describe_saved_search_match(saved_search, item)
        if saved_search.deliverable_now(now=now):
            if _notify_instant(saved_search, item, reasons):
                instant_matches_by_user[saved_search.user].append(saved_search.name)
        else:
            try:
                # A savepoint keeps an IntegrityError from poisoning the caller's
                # transaction, which matters inside tests and scheduled runs.
                with transaction.atomic():
                    SavedSearchMatch.objects.create(saved_search=saved_search, item=item)
            except IntegrityError:
                # Already buffered for this pair; a rerun must stay idempotent.
                continue

    return {
        'instant_users': len(instant_matches_by_user),
        'buffered': SavedSearchMatch.objects.filter(
            saved_search__is_active=True, notified_at__isnull=True,
        ).count(),
    }

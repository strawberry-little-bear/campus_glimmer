from collections import defaultdict

from django.urls import reverse

from .models import Item, SavedSearch
from .notifications import create_notification


def _matches(saved_search, item):
    if saved_search.query:
        query = saved_search.query.casefold()
        searchable = ' '.join(
            value for value in (
                item.title, item.description, item.condition,
                item.category.name if item.category else '',
                item.location.name if item.location else '',
                item.location.building if item.location else '',
            ) if value
        ).casefold()
        if query not in searchable:
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


def notify_saved_search_matches(item):
    """Notify each user once when a newly published item matches saved criteria."""
    item = (
        Item.objects.select_related('category', 'location', 'seller')
        .get(pk=item.pk)
    )
    matches_by_user = defaultdict(list)
    active_searches = SavedSearch.objects.filter(is_active=True).exclude(user=item.seller)
    for saved_search in active_searches.select_related('user', 'category', 'location'):
        if _matches(saved_search, item):
            matches_by_user[saved_search.user].append(saved_search.name)

    for user, names in matches_by_user.items():
        if len(names) == 1:
            criteria_text = f'“{names[0]}”'
        else:
            criteria_text = f'“{names[0]}”等 {len(names)} 个关注条件'
        create_notification(
            user,
            kind='saved_search_match',
            title='发现符合条件的新商品',
            message=f'新商品“{item.title}”符合你的{criteria_text}，快去看看吧。',
            item=item,
            target_url=reverse('item_detail', args=[item.id]),
        )

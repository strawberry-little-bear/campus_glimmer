from django.db import transaction
from django.urls import reverse

from .models import ItemAvailabilityWatch
from .notifications import create_notification


def notify_item_available(item, *, actor=None):
    """Notify and consume one-shot watchers after an item becomes available."""
    if item.status != 'available':
        return 0

    notified_count = 0
    with transaction.atomic():
        watches = list(
            ItemAvailabilityWatch.objects.select_for_update()
            .select_related('user')
            .filter(item=item)
        )
        for watch in watches:
            notification = create_notification(
                watch.user,
                actor=actor,
                kind='item_available',
                title='关注的商品重新有货了',
                message=f'你关注的商品“{item.title}”已恢复为在售，可以回来看看了。',
                item=item,
                target_url=reverse('item_detail', args=[item.id]),
            )
            if notification:
                watch.delete()
                notified_count += 1
    return notified_count

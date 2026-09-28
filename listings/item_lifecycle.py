from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Item
from .notifications import create_notification


def expire_items(*, now=None):
    """Archive public listings whose optional display deadline has passed."""
    now = now or timezone.now()
    expired_count = 0
    candidate_ids = Item.objects.filter(
        status='available', expires_at__isnull=False, expires_at__lte=now,
    ).values_list('id', flat=True)

    for item_id in candidate_ids:
        with transaction.atomic():
            item = Item.objects.select_for_update().select_related('seller').get(id=item_id)
            if item.status != 'available' or not item.expires_at or item.expires_at > now:
                continue
            item.status = 'expired'
            item.save(update_fields=['status', 'updated_at'])
            create_notification(
                item.seller,
                kind='item_expired',
                title='商品展示已到期',
                message=f'商品“{item.title}”已超过展示截止时间，系统已自动下架。你可以编辑商品后重新发布。',
                item=item,
                target_url=reverse('item_detail', args=[item.id]),
                dedupe_key=f'item-expired:{item.id}',
                dedupe_forever=True,
            )
            expired_count += 1
    return {'expired': expired_count}

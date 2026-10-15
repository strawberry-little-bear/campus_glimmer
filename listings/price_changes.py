"""Price-change records and notices for favorited sale items.

A price edit has two consequences with different reliability requirements: the
seller's edit must always be auditable, while a buyer notice is helpful but must
not make that edit fail. This module keeps those boundaries explicit instead of
expanding ``edit_item`` with notification policy.
"""

import logging

from django.urls import reverse
from django.utils import timezone

from .models import Favorite, Item, ItemPriceChange
from .notifications import create_notification

logger = logging.getLogger(__name__)


def record_item_price_change(item, *, old_price, changed_by=None, old_trade_mode=None):
    """Write one trajectory row for a successful ordinary-sale price edit."""
    if item.trade_mode != 'sale':
        return None
    if old_trade_mode is not None and old_trade_mode != 'sale':
        return None
    if old_price is None or item.price is None or old_price == item.price:
        return None
    return ItemPriceChange.objects.create(
        item=item,
        old_price=old_price,
        new_price=item.price,
        changed_by=changed_by,
    )


def notify_item_price_drop(item, price_change, *, actor=None, now=None):
    """Notify current favoriters after a usable sale item becomes cheaper.

    Notification creation is deliberately isolated per recipient: a failed
    preference lookup or insert is logged, but the already committed price edit
    remains valid and the seller is not sent back an error page.
    """
    if price_change.item_id != item.pk:
        return 0
    if item.trade_mode != 'sale':
        return 0

    now = now or timezone.now()
    if item.status != 'available':
        return 0
    if item.expires_at and item.expires_at <= now:
        return 0
    if price_change.new_price >= price_change.old_price:
        return 0

    actor = actor or price_change.changed_by
    notified_count = 0
    favorites = (
        Favorite.objects.select_related('user')
        .filter(item=item)
        .exclude(user_id=item.seller_id)
    )
    for favorite in favorites:
        try:
            notification = create_notification(
                favorite.user,
                actor=actor,
                kind='item_price_drop',
                title='收藏的商品降价了',
                message=(
                    f'你收藏的“{item.title}”从 ¥{price_change.old_price:.2f} '
                    f'降到 ¥{price_change.new_price:.2f}，便宜 '
                    f'¥{price_change.old_price - price_change.new_price:.2f}。'
                ),
                item=item,
                target_url=reverse('item_detail', args=[item.id]),
                dedupe_key=f'item-price-drop:{item.pk}:{price_change.pk}',
            )
        except Exception as exc:
            logger.warning(
                'Favorite price-drop notification failed (user=%s, item=%s, change=%s): %s',
                favorite.user_id, item.pk, price_change.pk, type(exc).__name__,
            )
            continue
        if notification:
            notified_count += 1
    return notified_count

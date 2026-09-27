from datetime import timedelta

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Item, Order, OrderEvent
from .notifications import create_notification

DEFAULT_CONFIRMATION_TIMEOUT_HOURS = 24
DEFAULT_REMINDER_HOURS = 6


def process_order_timeouts(*, now=None, reminder_hours=DEFAULT_REMINDER_HOURS):
    """Send one reminder and release pending reservations after the deadline."""
    now = now or timezone.now()
    try:
        reminder_hours = max(0, int(reminder_hours))
    except (TypeError, ValueError):
        reminder_hours = DEFAULT_REMINDER_HOURS

    expired_count = 0
    reminded_count = 0
    target_url = lambda order: reverse('order_detail', args=[order.id])

    expired_ids = Order.objects.filter(
        status='pending',
        confirmation_deadline__isnull=False,
        confirmation_deadline__lte=now,
    ).values_list('id', flat=True)
    for order_id in expired_ids:
        with transaction.atomic():
            order = Order.objects.select_for_update().select_related(
                'item', 'buyer', 'seller',
            ).get(id=order_id)
            if order.status != 'pending' or not order.confirmation_deadline or order.confirmation_deadline > now:
                continue
            item = Item.objects.select_for_update().get(id=order.item_id)
            order.status = 'cancelled'
            order.save(update_fields=['status', 'updated_at'])
            OrderEvent.objects.create(
                order=order,
                to_status='cancelled',
                note='卖家确认超时，系统自动释放预约',
            )
            if item.status == 'reserved':
                item.status = 'available'
                item.save(update_fields=['status', 'updated_at'])
            message = f'商品“{order.item.title}”的交易预约因卖家未在截止时间前确认，已自动取消。'
            create_notification(
                order.buyer,
                kind='order_expired',
                title='交易预约已超时取消',
                message=message,
                order=order,
                item=order.item,
                target_url=target_url(order),
            )
            create_notification(
                order.seller,
                kind='order_expired',
                title='交易预约已超时取消',
                message=message,
                order=order,
                item=order.item,
                target_url=target_url(order),
            )
            expired_count += 1

    reminder_deadline = now + timedelta(hours=reminder_hours)
    reminder_ids = Order.objects.filter(
        status='pending',
        confirmation_deadline__gt=now,
        confirmation_deadline__lte=reminder_deadline,
        confirmation_reminder_sent_at__isnull=True,
    ).values_list('id', flat=True)
    for order_id in reminder_ids:
        with transaction.atomic():
            order = Order.objects.select_for_update().select_related(
                'item', 'seller', 'buyer',
            ).get(id=order_id)
            if (
                order.status != 'pending'
                or not order.confirmation_deadline
                or order.confirmation_deadline <= now
                or order.confirmation_reminder_sent_at
            ):
                continue
            order.confirmation_reminder_sent_at = now
            order.save(update_fields=['confirmation_reminder_sent_at', 'updated_at'])
            hours_left = max(1, round((order.confirmation_deadline - now).total_seconds() / 3600))
            create_notification(
                order.seller,
                kind='order_expiring',
                title='交易预约即将超时',
                message=f'商品“{order.item.title}”还有约 {hours_left} 小时需要确认，逾期后系统会自动释放预约。',
                order=order,
                item=order.item,
                target_url=target_url(order),
            )
            reminded_count += 1

    return {'expired': expired_count, 'reminded': reminded_count}

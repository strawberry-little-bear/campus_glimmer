from django.db import transaction
from django.urls import reverse

from .availability import notify_item_available
from .models import Order, OrderEvent
from .notifications import create_notification


ORDER_TRANSITIONS = {
    'pending': {'confirmed', 'cancelled'},
    'confirmed': {'meeting', 'cancelled'},
    'meeting': {'cancelled'},
    'completed': set(),
    'borrowed': set(),
    'returned': set(),
    'cancelled': set(),
}

ORDER_EVENT_NOTES = {
    'confirmed': '卖家确认了交易预约',
    'meeting': '卖家将订单推进到当面交付',
    'cancelled': '订单被取消，商品恢复为在售',
}


class OrderTransitionError(Exception):
    """Raised when an order status transition is not allowed."""


def transition_order(*, order_id, actor, target_status):
    """Apply one user-driven order transition under a row lock."""
    with transaction.atomic():
        order = Order.objects.select_for_update().select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order_id)
        if actor not in {order.buyer, order.seller}:
            raise OrderTransitionError('你没有权限操作这笔订单。')

        seller_can_update = (
            actor == order.seller
            and target_status in ORDER_TRANSITIONS.get(order.status, set())
        )
        buyer_can_update = (
            actor == order.buyer
            and target_status == 'cancelled'
            and order.status in {'pending', 'confirmed', 'meeting'}
        )
        if not (seller_can_update or buyer_can_update):
            raise OrderTransitionError('当前订单状态不允许执行这个操作。')

        previous_status = order.status
        order.status = target_status
        order.save(update_fields=['status', 'updated_at'])
        OrderEvent.objects.create(
            order=order,
            actor=actor,
            from_status=previous_status,
            to_status=target_status,
            note=ORDER_EVENT_NOTES.get(target_status, '订单状态已更新'),
        )

        other_party = order.buyer if actor == order.seller else order.seller
        create_notification(
            other_party,
            actor=actor,
            kind='order_status',
            title='订单状态有更新',
            message=f'商品“{order.item.title}”的订单已更新为“{order.get_status_display()}”。',
            order=order,
            item=order.item,
            target_url=reverse('order_detail', args=[order.id]),
        )

        if target_status == 'cancelled':
            previous_item_status = order.item.status
            order.item.status = 'available'
            order.item.save(update_fields=['status', 'updated_at'])
            if previous_item_status != 'available':
                notify_item_available(order.item, actor=actor)

        return order

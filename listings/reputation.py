"""Public, privacy-safe seller reputation summaries."""

from django.db.models import Avg, Count, Q

from .models import Item, Order, Rating


def build_seller_reputation(seller):
    """Build a small explainable reputation card without exposing private data."""
    order_stats = Order.objects.filter(seller=seller).aggregate(
        completed=Count('id', filter=Q(status__in={'completed', 'returned'})),
        cancelled=Count('id', filter=Q(status='cancelled')),
    )
    completed = order_stats['completed'] or 0
    cancelled = order_stats['cancelled'] or 0
    closed_orders = completed + cancelled
    completion_rate = round(completed * 100 / closed_orders, 1) if closed_orders else None

    rating_stats = Rating.objects.filter(ratee=seller).aggregate(
        average=Avg('score'), count=Count('id'),
    )
    rating_count = rating_stats['count'] or 0
    average = round(float(rating_stats['average']), 1) if rating_stats['average'] is not None else None
    active_listings = Item.objects.available().filter(seller=seller).count()

    badges = []
    if completed:
        badges.append('有完成交易记录')
    if closed_orders >= 3 and completion_rate >= 90:
        badges.append('交易履约稳定')
    if rating_count >= 3 and average >= 4.5:
        badges.append('评价表现良好')

    if badges:
        label = '交易记录良好'
    elif closed_orders:
        label = '有交易经验'
    else:
        label = '新卖家'

    return {
        'label': label,
        'badges': badges,
        'completed_orders': completed,
        'closed_orders': closed_orders,
        'completion_rate': completion_rate,
        'rating_average': average,
        'rating_count': rating_count,
        'active_listings': active_listings,
    }

from datetime import datetime

from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import Order


CIRCULATION_STATUSES = ('completed', 'returned')
def _previous_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _month_window(months):
    current = timezone.localdate().replace(day=1)
    points = []
    year, month = current.year, current.month
    for _ in range(months):
        points.append((year, month))
        year, month = _previous_month(year, month)
    return list(reversed(points))


def _month_label(year, month):
    return f'{month}月'


def build_circular_impact_report(user, *, months=6):
    """Build a privacy-safe summary of a user's completed campus circulation.

    Only terminal order states count as circulation. Cancelled, pending and
    still-active orders are intentionally excluded so the report describes
    verified reuse rather than planned activity.
    """
    months = max(3, min(int(months), 12))
    orders = Order.objects.filter(
        Q(buyer=user) | Q(seller=user),
        status__in=CIRCULATION_STATUSES,
    ).select_related('item__category', 'item__location', 'buyer', 'seller')

    total_orders = orders.count()
    unique_items = orders.values('item_id').distinct().count()
    gift_count = orders.filter(item__trade_mode='free').count()
    borrow_return_count = orders.filter(status='returned').count()
    sale_count = orders.filter(
        status='completed', item__trade_mode='sale',
    ).count()
    other_participants = set(
        orders.filter(buyer=user).values_list('seller_id', flat=True)
    ) | set(
        orders.filter(seller=user).values_list('buyer_id', flat=True)
    )
    other_participants.discard(user.pk)

    category_rows = list(
        orders.values('item__category__name')
        .annotate(count=Count('id'))
        .order_by('-count', 'item__category__name')[:5]
    )
    categories = [
        {'name': row['item__category__name'] or '未分类', 'count': row['count']}
        for row in category_rows
    ]
    location_rows = list(
        orders.values('item__location__name')
        .annotate(count=Count('id'))
        .order_by('-count', 'item__location__name')[:5]
    )
    locations = [
        {'name': row['item__location__name'] or '未指定地点', 'count': row['count']}
        for row in location_rows
    ]

    month_points = _month_window(months)
    first_month = timezone.make_aware(datetime(month_points[0][0], month_points[0][1], 1))
    monthly_rows = {
        (row['month'].year, row['month'].month): row['count']
        for row in orders.filter(updated_at__gte=first_month)
        .annotate(month=TruncMonth('updated_at'))
        .values('month')
        .annotate(count=Count('id'))
    }
    monthly = [
        {
            'label': _month_label(year, month),
            'year': year,
            'month': month,
            'count': monthly_rows.get((year, month), 0),
        }
        for year, month in month_points
    ]
    max_monthly = max((point['count'] for point in monthly), default=0)
    for point in monthly:
        point['percent'] = round(point['count'] * 100 / max_monthly) if max_monthly else 0

    impact_score = sale_count * 8 + gift_count * 12 + borrow_return_count * 10
    return {
        'total_orders': total_orders,
        'unique_items': unique_items,
        'gift_count': gift_count,
        'borrow_return_count': borrow_return_count,
        'sale_count': sale_count,
        'participant_count': len(other_participants),
        'impact_score': impact_score,
        'categories': categories,
        'locations': locations,
        'monthly': monthly,
        'has_activity': bool(total_orders),
        'period_label': f'最近 {months} 个月',
    }

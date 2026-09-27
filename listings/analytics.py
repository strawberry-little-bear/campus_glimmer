from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import CampusLocation, Category, Item, Order, Report, SearchQuery


PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)


def _period_start(days, today):
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(start_date, time.min))


def _daily_counts(queryset, start, end):
    rows = queryset.filter(created_at__gte=start, created_at__lte=end).annotate(
        day=TruncDate('created_at'),
    ).values('day').annotate(count=Count('id')).order_by('day')
    return {row['day']: row['count'] for row in rows}


def build_operations_dashboard(days=30):
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30

    now = timezone.now()
    today = timezone.localdate()
    start = _period_start(days, today)
    dates = [start.date() + timedelta(days=index) for index in range(days)]

    item_period = Item.objects.filter(created_at__gte=start, created_at__lte=now)
    order_period = Order.objects.filter(created_at__gte=start, created_at__lte=now)
    search_period = SearchQuery.objects.filter(created_at__gte=start, created_at__lte=now)
    user_period = get_user_model().objects.filter(date_joined__gte=start, date_joined__lte=now)
    report_period = Report.objects.filter(created_at__gte=start, created_at__lte=now)

    order_count = order_period.count()
    completed_order_count = order_period.filter(status='completed').count()
    completion_rate = round(completed_order_count / order_count * 100, 1) if order_count else 0
    average_order_price = order_period.aggregate(value=Avg('agreed_price'))['value']

    item_daily = _daily_counts(Item.objects, start, now)
    order_daily = _daily_counts(Order.objects, start, now)
    search_daily = _daily_counts(SearchQuery.objects, start, now)
    activity_trend = []
    for day in dates:
        item_count = item_daily.get(day, 0)
        search_count = search_daily.get(day, 0)
        order_count_for_day = order_daily.get(day, 0)
        activity_trend.append({
            'date': day,
            'items': item_count,
            'searches': search_count,
            'orders': order_count_for_day,
            'total': item_count + search_count + order_count_for_day,
        })
    trend_max = max((point['total'] for point in activity_trend), default=1) or 1

    top_searches = list(
        search_period.values('query').annotate(
            search_count=Count('id'),
            zero_result_count=Count('id', filter=Q(result_count=0)),
            average_results=Avg('result_count'),
        ).order_by('-search_count', 'query')[:8]
    )
    zero_result_searches = [row for row in top_searches if row['zero_result_count']]

    category_stats = Category.objects.annotate(
        new_count=Count(
            'items',
            filter=Q(items__created_at__gte=start, items__created_at__lte=now),
            distinct=True,
        ),
        available_count=Count(
            'items',
            filter=Q(items__status='available'),
            distinct=True,
        ),
    ).filter(new_count__gt=0).order_by('-new_count', 'name')[:8]

    location_stats = CampusLocation.objects.annotate(
        new_count=Count(
            'items',
            filter=Q(items__created_at__gte=start, items__created_at__lte=now),
            distinct=True,
        ),
        order_count=Count(
            'orders',
            filter=Q(orders__created_at__gte=start, orders__created_at__lte=now),
            distinct=True,
        ),
    ).filter(new_count__gt=0).order_by('-new_count', 'sort_order', 'name')[:8]

    status_labels = dict(Order.STATUS_CHOICES)
    order_statuses = [
        {
            'key': status,
            'label': status_labels[status],
            'count': order_period.filter(status=status).count(),
        }
        for status, _ in Order.STATUS_CHOICES
    ]

    return {
        'period_days': days,
        'period_choices': PERIOD_CHOICES,
        'period_start': start,
        'period_end': now,
        'metrics': {
            'active_items': Item.objects.filter(status='available').count(),
            'new_items': item_period.count(),
            'orders': order_count,
            'completed_orders': completed_order_count,
            'completion_rate': completion_rate,
            'average_order_price': average_order_price,
            'searches': search_period.count(),
            'zero_result_searches': search_period.filter(result_count=0).count(),
            'new_users': user_period.count(),
            'new_reports': report_period.count(),
            'pending_reports': Report.objects.filter(status__in=['pending', 'reviewing']).count(),
        },
        'activity_trend': activity_trend,
        'trend_max': trend_max,
        'top_searches': top_searches,
        'zero_result_searches': zero_result_searches,
        'category_stats': category_stats,
        'location_stats': location_stats,
        'order_statuses': order_statuses,
    }
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import BrowsingHistory, CampusLocation, Category, Favorite, Item, Order, Report, SearchQuery


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


def build_search_insights(days=30, query=''):
    """Build an actionable view of search demand for staff operations work."""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30

    now = timezone.now()
    start = _period_start(days, timezone.localdate())
    search_period = SearchQuery.objects.filter(
        created_at__gte=start,
        created_at__lte=now,
    )
    query = (query or '').strip()[:120]
    if query:
        search_period = search_period.filter(query__icontains=query)

    term_rows = list(
        search_period.values('query').annotate(
            search_count=Count('id'),
            zero_result_count=Count('id', filter=Q(result_count=0)),
            average_results=Avg('result_count'),
            unique_users=Count('user', distinct=True),
            last_searched=Max('created_at'),
        ).order_by('-search_count', 'query')
    )
    for row in term_rows:
        row['zero_result_rate'] = round(
            row['zero_result_count'] / row['search_count'] * 100, 1
        ) if row['search_count'] else 0

    search_count = search_period.count()
    zero_result_count = search_period.filter(result_count=0).count()
    gap_terms = sorted(
        (row for row in term_rows if row['zero_result_count']),
        key=lambda row: (-row['zero_result_count'], -row['search_count'], row['query']),
    )[:12]
    insights = []
    for row in gap_terms[:6]:
        insights.append({
            'query': row['query'],
            'search_count': row['search_count'],
            'zero_result_count': row['zero_result_count'],
            'zero_result_rate': row['zero_result_rate'],
            'message': (
                f"“{row['query']}”有 {row['zero_result_count']} 次搜索没有结果，"
                '可以考虑补充库存、调整分类或发布求购引导。'
            ),
        })

    return {
        'period_days': days,
        'period_choices': PERIOD_CHOICES,
        'period_start': start,
        'period_end': now,
        'query_filter': query,
        'metrics': {
            'searches': search_count,
            'unique_terms': len(term_rows),
            'zero_result_searches': zero_result_count,
            'zero_result_rate': round(zero_result_count / search_count * 100, 1) if search_count else 0,
        },
        'term_rows': term_rows[:30],
        'gap_terms': gap_terms,
        'insights': insights,
    }


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
    view_period = BrowsingHistory.objects.filter(last_viewed_at__gte=start, last_viewed_at__lte=now)
    favorite_period = Favorite.objects.filter(created_at__gte=start, created_at__lte=now)

    order_count = order_period.count()
    completed_order_count = order_period.filter(status='completed').count()
    completion_rate = round(completed_order_count / order_count * 100, 1) if order_count else 0
    average_order_price = order_period.aggregate(value=Avg('agreed_price'))['value']
    detail_view_count = view_period.count()
    favorite_count = favorite_period.count()

    def conversion_rate(current, previous):
        return round(current / previous * 100, 1) if previous else 0

    conversion_funnel = [
        {
            'key': 'views',
            'label': '详情浏览',
            'count': detail_view_count,
            'rate': 100,
            'note': '去重后的用户-商品浏览',
        },
        {
            'key': 'favorites',
            'label': '加入心愿单',
            'count': favorite_count,
            'rate': conversion_rate(favorite_count, detail_view_count),
            'note': '从浏览到收藏',
        },
        {
            'key': 'orders',
            'label': '发起预约',
            'count': order_count,
            'rate': conversion_rate(order_count, favorite_count),
            'note': '从收藏到预约',
        },
        {
            'key': 'completed',
            'label': '完成交易',
            'count': completed_order_count,
            'rate': conversion_rate(completed_order_count, order_count),
            'note': '从预约到完成',
        },
    ]
    funnel_max = max((stage['count'] for stage in conversion_funnel), default=1) or 1
    search_count = search_period.count()
    zero_result_search_count = search_period.filter(result_count=0).count()

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
            'detail_views': detail_view_count,
            'favorites': favorite_count,
            'orders': order_count,
            'completed_orders': completed_order_count,
            'completion_rate': completion_rate,
            'average_order_price': average_order_price,
            'searches': search_count,
            'zero_result_searches': zero_result_search_count,
            'zero_result_rate': round(zero_result_search_count / search_count * 100, 1) if search_count else 0,
            'new_users': user_period.count(),
            'new_reports': report_period.count(),
            'pending_reports': Report.objects.filter(status__in=['pending', 'reviewing']).count(),
        },
        'activity_trend': activity_trend,
        'trend_max': trend_max,
        'conversion_funnel': conversion_funnel,
        'funnel_max': funnel_max,
        'top_searches': top_searches,
        'zero_result_searches': zero_result_searches,
        'category_stats': category_stats,
        'location_stats': location_stats,
        'order_statuses': order_statuses,
    }
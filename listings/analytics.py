from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate
from django.utils import timezone

from chat_messages.models import PrivateMessage

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


def _build_search_rhythm(search_period):
    """Summarize when search demand appears so staff can time replenishment work."""
    hourly_rows = search_period.annotate(
        hour=ExtractHour('created_at'),
    ).values('hour').annotate(
        count=Count('id'),
        zero_result_count=Count('id', filter=Q(result_count=0)),
    ).order_by('hour')
    hourly_map = {int(row['hour']): row for row in hourly_rows}
    max_hour_count = max((row['count'] for row in hourly_map.values()), default=0)
    hourly = []
    for hour in range(24):
        row = hourly_map.get(hour, {})
        count = row.get('count', 0)
        zero_result_count = row.get('zero_result_count', 0)
        hourly.append({
            'hour': hour,
            'label': f'{hour:02d}:00',
            'count': count,
            'zero_result_count': zero_result_count,
            'zero_result_rate': round(zero_result_count / count * 100, 1) if count else 0,
            'bar_height': max(12, round(count / max_hour_count * 100)) if count and max_hour_count else 4,
            'show_label': hour % 3 == 0,
        })

    weekday_rows = search_period.annotate(
        weekday=ExtractIsoWeekDay('created_at'),
    ).values('weekday').annotate(
        count=Count('id'),
        zero_result_count=Count('id', filter=Q(result_count=0)),
    ).order_by('weekday')
    weekday_map = {int(row['weekday']): row for row in weekday_rows}
    weekday_labels = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
    max_weekday_count = max((row['count'] for row in weekday_map.values()), default=0)
    weekdays = []
    for weekday, label in enumerate(weekday_labels, start=1):
        row = weekday_map.get(weekday, {})
        count = row.get('count', 0)
        zero_result_count = row.get('zero_result_count', 0)
        weekdays.append({
            'weekday': weekday,
            'label': label,
            'count': count,
            'zero_result_count': zero_result_count,
            'bar_width': max(8, round(count / max_weekday_count * 100)) if count and max_weekday_count else 0,
        })

    peak_hour = max(hourly, key=lambda row: (row['count'], -row['hour'])) if max_hour_count else None
    peak_weekday = max(weekdays, key=lambda row: (row['count'], -row['weekday'])) if max_weekday_count else None
    return {
        'hourly': hourly,
        'weekdays': weekdays,
        'peak_hour': peak_hour,
        'peak_weekday': peak_weekday,
        'has_data': bool(max_hour_count),
    }


def _build_search_period_comparison(current_period, previous_period):
    """Compare search demand with the equivalent previous period."""
    current_count = current_period.count()
    previous_count = previous_period.count()
    current_zero_count = current_period.filter(result_count=0).count()
    previous_zero_count = previous_period.filter(result_count=0).count()
    current_zero_rate = round(current_zero_count / current_count * 100, 1) if current_count else 0
    previous_zero_rate = round(previous_zero_count / previous_count * 100, 1) if previous_count else 0

    if current_count == previous_count:
        search_change = {'delta': 0, 'change_display': '持平', 'direction': 'flat'}
    elif previous_count:
        delta = round((current_count - previous_count) / previous_count * 100, 1)
        search_change = {
            'delta': delta,
            'change_display': f'{delta:+.1f}%',
            'direction': 'up' if delta > 0 else 'down',
        }
    else:
        search_change = {
            'delta': None,
            'change_display': '新增' if current_count else '—',
            'direction': 'up' if current_count else 'flat',
        }

    zero_rate_delta = round(current_zero_rate - previous_zero_rate, 1)
    current_terms = dict(
        current_period.values('query').annotate(count=Count('id')).values_list('query', 'count')
    )
    previous_terms = dict(
        previous_period.values('query').annotate(count=Count('id')).values_list('query', 'count')
    )
    term_changes = []
    for term in set(current_terms) | set(previous_terms):
        current_term_count = current_terms.get(term, 0)
        previous_term_count = previous_terms.get(term, 0)
        delta = current_term_count - previous_term_count
        if delta:
            term_changes.append({
                'query': term,
                'current_count': current_term_count,
                'previous_count': previous_term_count,
                'delta': delta,
                'direction': 'up' if delta > 0 else 'down',
            })

    return {
        'current_searches': current_count,
        'previous_searches': previous_count,
        'search_change': search_change,
        'current_zero_result_rate': current_zero_rate,
        'previous_zero_result_rate': previous_zero_rate,
        'zero_result_rate_delta': zero_rate_delta,
        'zero_result_rate_change_display': (
            '持平' if zero_rate_delta == 0 else f'{zero_rate_delta:+.1f} 个百分点'
        ),
        'rising_terms': sorted(
            (row for row in term_changes if row['direction'] == 'up'),
            key=lambda row: (-row['delta'], -row['current_count'], row['query']),
        )[:8],
        'falling_terms': sorted(
            (row for row in term_changes if row['direction'] == 'down'),
            key=lambda row: (row['delta'], -row['current_count'], row['query']),
        )[:8],
    }


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
        'search_rhythm': _build_search_rhythm(search_period),
        'period_comparison': _build_search_period_comparison(
            search_period,
            SearchQuery.objects.filter(created_at__gte=start - timedelta(days=days), created_at__lt=start),
        ),
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


def _build_user_activity_segments(start, end):
    """Aggregate current-period activity once, then classify users by behavior."""
    user_model = get_user_model()
    users = user_model.objects.annotate(
        listing_count=Count(
            'listed_items',
            filter=Q(listed_items__created_at__gte=start, listed_items__created_at__lte=end),
            distinct=True,
        ),
        search_count=Count(
            'search_queries',
            filter=Q(search_queries__created_at__gte=start, search_queries__created_at__lte=end),
            distinct=True,
        ),
        browse_count=Count(
            'browsing_history',
            filter=Q(browsing_history__last_viewed_at__gte=start, browsing_history__last_viewed_at__lte=end),
            distinct=True,
        ),
        favorite_count=Count(
            'favorites',
            filter=Q(favorites__created_at__gte=start, favorites__created_at__lte=end),
            distinct=True,
        ),
        purchase_count=Count(
            'purchased_orders',
            filter=Q(purchased_orders__created_at__gte=start, purchased_orders__created_at__lte=end),
            distinct=True,
        ),
        sale_count=Count(
            'sold_orders',
            filter=Q(sold_orders__created_at__gte=start, sold_orders__created_at__lte=end),
            distinct=True,
        ),
        sent_message_count=Count(
            'sent_messages',
            filter=Q(sent_messages__created_at__gte=start, sent_messages__created_at__lte=end),
            distinct=True,
        ),
        received_message_count=Count(
            'received_messages',
            filter=Q(received_messages__created_at__gte=start, received_messages__created_at__lte=end),
            distinct=True,
        ),
    )
    segment_definitions = {
        'trader': {'label': '交易参与者', 'note': '周期内发起或承接过交易预约'},
        'supplier': {'label': '供给贡献者', 'note': '周期内发布了多件商品'},
        'explorer': {'label': '高频探索者', 'note': '搜索与浏览行为较为集中'},
        'light': {'label': '轻度活跃者', 'note': '有访问、收藏或单次互动'},
    }
    segment_counts = {key: 0 for key in segment_definitions}
    active_user_count = 0
    for user in users:
        behavior_count = (
            user.listing_count + user.search_count + user.browse_count
            + user.favorite_count + user.purchase_count + user.sale_count
            + user.sent_message_count + user.received_message_count
        )
        if not behavior_count:
            continue
        active_user_count += 1
        if user.purchase_count or user.sale_count:
            segment_key = 'trader'
        elif user.listing_count >= 2:
            segment_key = 'supplier'
        elif user.search_count + user.browse_count >= 5:
            segment_key = 'explorer'
        else:
            segment_key = 'light'
        segment_counts[segment_key] += 1

    segments = []
    for key, definition in segment_definitions.items():
        count = segment_counts[key]
        segments.append({
            'key': key,
            'label': definition['label'],
            'note': definition['note'],
            'count': count,
            'share': round(count / active_user_count * 100, 1) if active_user_count else 0,
        })
    return active_user_count, segments



def _build_user_retention(start, previous_start, end):
    """Measure how many users from the previous new-user cohort returned this period."""
    user_model = get_user_model()
    cohort_ids = set(user_model.objects.filter(
        date_joined__gte=previous_start,
        date_joined__lt=start,
    ).values_list('id', flat=True))
    if not cohort_ids:
        return {
            'cohort_size': 0,
            'retained_users': 0,
            'rate': 0,
            'note': '上一周期没有足够的新用户样本',
        }

    active_ids = set()
    active_ids.update(Item.objects.filter(
        seller_id__in=cohort_ids, created_at__gte=start, created_at__lte=end,
    ).values_list('seller_id', flat=True))
    active_ids.update(SearchQuery.objects.filter(
        user_id__in=cohort_ids, created_at__gte=start, created_at__lte=end,
    ).values_list('user_id', flat=True))
    active_ids.update(BrowsingHistory.objects.filter(
        user_id__in=cohort_ids, last_viewed_at__gte=start, last_viewed_at__lte=end,
    ).values_list('user_id', flat=True))
    active_ids.update(Favorite.objects.filter(
        user_id__in=cohort_ids, created_at__gte=start, created_at__lte=end,
    ).values_list('user_id', flat=True))
    active_ids.update(Order.objects.filter(
        Q(buyer_id__in=cohort_ids) | Q(seller_id__in=cohort_ids),
        created_at__gte=start, created_at__lte=end,
    ).values_list('buyer_id', flat=True))
    active_ids.update(Order.objects.filter(
        Q(buyer_id__in=cohort_ids) | Q(seller_id__in=cohort_ids),
        created_at__gte=start, created_at__lte=end,
    ).values_list('seller_id', flat=True))
    active_ids.update(Report.objects.filter(
        reporter_id__in=cohort_ids, created_at__gte=start, created_at__lte=end,
    ).values_list('reporter_id', flat=True))
    active_ids.update(PrivateMessage.objects.filter(
        sender_id__in=cohort_ids, created_at__gte=start, created_at__lte=end,
    ).values_list('sender_id', flat=True))
    active_ids.update(PrivateMessage.objects.filter(
        receiver_id__in=cohort_ids, created_at__gte=start, created_at__lte=end,
    ).values_list('receiver_id', flat=True))

    retained_users = len(cohort_ids & active_ids)
    return {
        'cohort_size': len(cohort_ids),
        'retained_users': retained_users,
        'rate': round(retained_users / len(cohort_ids) * 100, 1),
        'note': '上一周期新用户在当前周期产生至少一次结构化行为',
    }



def build_operational_alerts(metrics, period_comparisons):
    """Turn dashboard signals into a short, actionable operations queue."""
    alerts = []
    search_count = metrics['searches']
    zero_result_rate = metrics['zero_result_rate']
    zero_result_count = metrics['zero_result_searches']
    if search_count >= 3 and zero_result_count >= 3 and zero_result_rate >= 35:
        alerts.append({
            'key': 'search_supply_gap',
            'severity': 'warning',
            'severity_label': '需要关注',
            'title': '搜索供给缺口',
            'message': (
                f'最近 {search_count} 次搜索中有 {zero_result_count} 次没有结果，'
                f'无结果占比达到 {zero_result_rate}%，建议优先补充相关商品。'
            ),
            'metric': f'{zero_result_rate}%',
            'metric_label': '无结果占比',
            'action_label': '查看搜索洞察',
            'action_url_name': 'search_insights',
        })

    pending_reports = metrics['pending_reports']
    if pending_reports >= 3:
        alerts.append({
            'key': 'report_backlog',
            'severity': 'critical' if pending_reports >= 10 else 'warning',
            'severity_label': '优先处理' if pending_reports >= 10 else '需要关注',
            'title': '举报审核队列积压',
            'message': f'当前有 {pending_reports} 条举报仍在待处理队列，建议安排审核并及时反馈。',
            'metric': str(pending_reports),
            'metric_label': '待处理举报',
            'action_label': '进入举报审核',
            'action_url_name': 'report_list',
        })

    completed_comparison = next(
        (row for row in period_comparisons if row['key'] == 'completed_orders'),
        None,
    )
    if completed_comparison and completed_comparison['previous'] >= 3:
        previous = completed_comparison['previous']
        current = completed_comparison['current']
        drop_rate = round((previous - current) / previous * 100, 1)
        if current < previous and drop_rate >= 30:
            alerts.append({
                'key': 'completed_order_drop',
                'severity': 'critical' if drop_rate >= 50 else 'warning',
                'severity_label': '优先关注' if drop_rate >= 50 else '需要关注',
                'title': '交易转化下滑',
                'message': (
                    f'完成交易从上一周期的 {previous} 笔降至 {current} 笔，'
                    f'下降 {drop_rate}%，建议排查供给、预约和交付环节。'
                ),
                'metric': f'-{drop_rate}%',
                'metric_label': '完成交易变化',
                'action_label': '查看运营看板',
                'action_url_name': 'operations_dashboard',
            })

    severity_order = {'critical': 0, 'warning': 1, 'info': 2}
    return sorted(alerts, key=lambda alert: severity_order.get(alert['severity'], 9))


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
    previous_start = start - timedelta(days=days)
    previous_item_period = Item.objects.filter(created_at__gte=previous_start, created_at__lt=start)
    previous_order_period = Order.objects.filter(created_at__gte=previous_start, created_at__lt=start)
    previous_search_period = SearchQuery.objects.filter(created_at__gte=previous_start, created_at__lt=start)
    previous_user_period = get_user_model().objects.filter(date_joined__gte=previous_start, date_joined__lt=start)
    previous_view_period = BrowsingHistory.objects.filter(last_viewed_at__gte=previous_start, last_viewed_at__lt=start)
    previous_favorite_period = Favorite.objects.filter(created_at__gte=previous_start, created_at__lt=start)
    previous_report_period = Report.objects.filter(created_at__gte=previous_start, created_at__lt=start)

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

    active_user_count, activity_segments = _build_user_activity_segments(start, now)
    user_retention = _build_user_retention(start, previous_start, now)

    current_comparison_values = {
        'new_items': item_period.count(),
        'detail_views': detail_view_count,
        'favorites': favorite_count,
        'orders': order_count,
        'completed_orders': completed_order_count,
        'searches': search_count,
        'new_users': user_period.count(),
        'new_reports': report_period.count(),
    }
    previous_comparison_values = {
        'new_items': previous_item_period.count(),
        'detail_views': previous_view_period.count(),
        'favorites': previous_favorite_period.count(),
        'orders': previous_order_period.count(),
        'completed_orders': previous_order_period.filter(status='completed').count(),
        'searches': previous_search_period.count(),
        'new_users': previous_user_period.count(),
        'new_reports': previous_report_period.count(),
    }
    comparison_labels = {
        'new_items': '新增商品',
        'detail_views': '详情浏览',
        'favorites': '加入心愿单',
        'orders': '交易预约',
        'completed_orders': '完成交易',
        'searches': '搜索次数',
        'new_users': '新增用户',
        'new_reports': '新增举报',
    }
    period_comparisons = []
    for key, label in comparison_labels.items():
        current = current_comparison_values[key]
        previous = previous_comparison_values[key]
        if current == previous:
            change_display = '持平'
            direction = 'flat'
            delta = 0
        elif previous:
            delta = round((current - previous) / previous * 100, 1)
            change_display = f'{delta:+.1f}%'
            direction = 'up' if delta > 0 else 'down'
        else:
            delta = None
            change_display = '新增' if current else '—'
            direction = 'up' if current else 'flat'
        period_comparisons.append({
            'key': key,
            'label': label,
            'current': current,
            'previous': previous,
            'delta': delta,
            'change_display': change_display,
            'direction': direction,
        })

    metrics = {
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
        'active_users': active_user_count,
        'retention_rate': user_retention['rate'],
        'new_reports': report_period.count(),
        'pending_reports': Report.objects.filter(status__in=['pending', 'reviewing']).count(),
    }
    operational_alerts = build_operational_alerts(metrics, period_comparisons)


    return {
        'period_days': days,
        'period_choices': PERIOD_CHOICES,
        'period_start': start,
        'period_end': now,
        'previous_period_start': previous_start,
        'previous_period_end': start - timedelta(days=1),
        'period_comparisons': period_comparisons,
        'operational_alerts': operational_alerts,
        'activity_segments': activity_segments,
        'user_retention': user_retention,
        'metrics': metrics,
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
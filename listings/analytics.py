from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Max, Q, Sum
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate
from django.utils import timezone

from chat_messages.models import PrivateMessage

from .models import BrowsingHistory, CampusLocation, Category, Favorite, Item, Notification, Order, OrderEvent, Report, SearchClick, SearchImpression, SearchQuery


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



def _build_facet_supply_gaps(search_period):
    """Turn filtered search demand into category/location replenishment signals."""
    def enrich(rows, available_counts, kind):
        enriched = []
        for row in rows:
            search_count = row['search_count']
            zero_result_count = row['zero_result_count']
            zero_result_rate = round(zero_result_count / search_count * 100, 1) if search_count else 0
            available_count = available_counts.get(row[f'{kind}_id'], 0)
            # A higher score means repeated failed demand with little live supply.
            priority_score = round(
                zero_result_count * (1 + zero_result_rate / 100) / (available_count + 1), 2
            )
            enriched.append({
                **row,
                'zero_result_rate': zero_result_rate,
                'available_count': available_count,
                'priority_score': priority_score,
                'has_gap': bool(zero_result_count),
            })
        return sorted(
            (row for row in enriched if row['has_gap']),
            key=lambda row: (-row['priority_score'], -row['zero_result_count'], row[f'{kind}__name']),
        )[:8]

    category_rows = list(
        search_period.filter(category__isnull=False)
        .values('category_id', 'category__name')
        .annotate(
            search_count=Count('id'),
            zero_result_count=Count('id', filter=Q(result_count=0)),
            average_results=Avg('result_count'),
        )
    )
    location_rows = list(
        search_period.filter(location__isnull=False)
        .values('location_id', 'location__name')
        .annotate(
            search_count=Count('id'),
            zero_result_count=Count('id', filter=Q(result_count=0)),
            average_results=Avg('result_count'),
        )
    )
    category_available = dict(
        Item.objects.filter(status='available')
        .values('category_id')
        .annotate(count=Count('id'))
        .values_list('category_id', 'count')
    )
    location_available = dict(
        Item.objects.filter(status='available', location__isnull=False)
        .values('location_id')
        .annotate(count=Count('id'))
        .values_list('location_id', 'count')
    )
    categories = enrich(category_rows, category_available, 'category')
    locations = enrich(location_rows, location_available, 'location')
    return {
        'categories': categories,
        'locations': locations,
        'total_facets': len(categories) + len(locations),
        'gap_facets': sum(row['has_gap'] for row in categories + locations),
    }


def _build_search_quality(search_count, zero_result_count, result_search_count, clicked_search_count):
    """Translate search funnel signals into a transparent, actionable quality score."""
    if not search_count:
        return {
            'score': 0,
            'level': '暂无数据',
            'level_key': 'empty',
            'sample_size': 0,
            'sample_label': '等待搜索记录积累',
            'supply_score': 0,
            'engagement_score': 0,
            'recommendations': ['扩大统计周期或等待新的搜索记录，再评估搜索质量。'],
        }

    zero_result_rate = zero_result_count / search_count * 100
    click_through_rate = (
        clicked_search_count / result_search_count * 100
        if result_search_count else 0
    )
    supply_score = round(max(0, 100 - zero_result_rate))
    engagement_score = round(click_through_rate)
    score = round(supply_score * 0.55 + engagement_score * 0.45)
    if score >= 80:
        level, level_key = '健康', 'healthy'
    elif score >= 60:
        level, level_key = '需要优化', 'attention'
    else:
        level, level_key = '重点关注', 'critical'

    recommendations = []
    if zero_result_rate >= 20:
        recommendations.append(
            f'无结果占比为 {zero_result_rate:.1f}%，优先补充高频缺口或配置同义词。'
        )
    if result_search_count and click_through_rate < 45:
        recommendations.append(
            f'有结果搜索的点击率为 {click_through_rate:.1f}%，建议检查排序、首图和标题信息。'
        )
    if not recommendations:
        recommendations.append('供给覆盖与结果点击表现稳定，可继续观察趋势并做小步优化。')

    return {
        'score': score,
        'level': level,
        'level_key': level_key,
        'sample_size': search_count,
        'sample_label': '样本量较小，建议结合更长周期判断' if search_count < 10 else '基于当前统计周期',
        'supply_score': supply_score,
        'engagement_score': engagement_score,
        'recommendations': recommendations,
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
    previous_search_period = SearchQuery.objects.filter(
        created_at__gte=start - timedelta(days=days),
        created_at__lt=start,
    )
    if query:
        previous_search_period = previous_search_period.filter(query__icontains=query)

    click_filter = Q(clicks__created_at__gte=start, clicks__created_at__lte=now)
    impression_filter = Q(impressions__created_at__gte=start, impressions__created_at__lte=now)
    term_rows = list(
        search_period.values('query').annotate(
            search_count=Count('id'),
            result_search_count=Count('id', filter=Q(result_count__gt=0)),
            zero_result_count=Count('id', filter=Q(result_count=0)),
            average_results=Avg('result_count'),
            unique_users=Count('user', distinct=True),
            click_count=Count('clicks', filter=click_filter),
            impression_count=Count('impressions', filter=impression_filter),
            clicked_searches=Count(
                'clicks__search_query',
                filter=click_filter & Q(result_count__gt=0),
                distinct=True,
            ),
            clicked_items=Count('clicks__item', filter=click_filter, distinct=True),
            last_searched=Max('created_at'),
        ).order_by('-search_count', 'query')
    )
    for row in term_rows:
        row['zero_result_rate'] = round(
            row['zero_result_count'] / row['search_count'] * 100, 1
        ) if row['search_count'] else 0
        row['click_rate'] = round(
            row['clicked_searches'] / row['result_search_count'] * 100, 1
        ) if row['result_search_count'] else 0

    search_count = search_period.count()
    zero_result_count = search_period.filter(result_count=0).count()
    click_period = SearchClick.objects.filter(
        created_at__gte=start, created_at__lte=now,
        search_query__isnull=False, search_query__result_count__gt=0,
    )
    if query:
        click_period = click_period.filter(search_query__query__icontains=query)
    impression_period = SearchImpression.objects.filter(
        created_at__gte=start, created_at__lte=now,
        search_query__isnull=False,
    )
    if query:
        impression_period = impression_period.filter(search_query__query__icontains=query)
    click_count = click_period.count()
    impression_count = impression_period.count()
    exposed_search_count = impression_period.values('search_query_id').distinct().count()
    exposed_items = list(
        impression_period.values('item_id', 'item__title').annotate(
            impression_count=Count('id'),
            exposed_searches=Count('search_query', distinct=True),
        ).order_by('-impression_count', 'item__title')[:10]
    )
    result_search_count = search_period.filter(result_count__gt=0).count()
    clicked_search_count = click_period.values('search_query_id').distinct().count()
    zero_click_search_count = max(result_search_count - clicked_search_count, 0)
    clicked_items = list(
        click_period.values('item_id', 'item__title').annotate(
            click_count=Count('id'),
            unique_searches=Count('search_query', distinct=True),
        ).order_by('-click_count', 'item__title')[:10]
    )
    gap_terms = sorted(
        (row for row in term_rows if row['zero_result_count']),
        key=lambda row: (-row['zero_result_count'], -row['search_count'], row['query']),
    )[:12]
    facet_supply_gaps = _build_facet_supply_gaps(search_period)
    search_quality = _build_search_quality(
        search_count,
        zero_result_count,
        result_search_count,
        clicked_search_count,
    )
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
            previous_search_period,
        ),
        'period_start': start,
        'period_end': now,
        'query_filter': query,
        'search_quality': search_quality,
        'metrics': {
            'searches': search_count,
            'unique_terms': len(term_rows),
            'zero_result_searches': zero_result_count,
            'zero_result_rate': round(zero_result_count / search_count * 100, 1) if search_count else 0,
            'clicks': click_count,
            'impressions': impression_count,
            'exposed_searches': exposed_search_count,
            'searches_with_results': result_search_count,
            'click_through_rate': round(clicked_search_count / result_search_count * 100, 1) if result_search_count else 0,
            'zero_click_searches': zero_click_search_count,
            'zero_click_rate': round(zero_click_search_count / result_search_count * 100, 1) if result_search_count else 0,
        },
        'clicked_items': clicked_items,
        'exposed_items': exposed_items,
        'no_click_terms': sorted(
            (row for row in term_rows if row['result_search_count'] and not row['clicked_searches']),
            key=lambda row: (-row['search_count'], row['query']),
        )[:12],
        'term_rows': term_rows[:30],
        'gap_terms': gap_terms,
        'facet_supply_gaps': facet_supply_gaps,
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

    searches_with_results = metrics.get('searches_with_results', 0)
    search_click_through_rate = metrics.get('search_click_through_rate', 0)
    if searches_with_results >= 5 and search_click_through_rate < 35:
        alerts.append({
            'key': 'search_engagement_drop',
            'severity': 'critical' if search_click_through_rate < 20 else 'warning',
            'severity_label': '优先关注' if search_click_through_rate < 20 else '需要关注',
            'title': '搜索结果互动偏低',
            'message': (
                f'最近有结果的搜索共有 {searches_with_results} 次，但点击率只有 '
                f'{search_click_through_rate}%，建议检查首屏排序、标题和图片质量。'
            ),
            'metric': f'{search_click_through_rate}%',
            'metric_label': '结果点击率',
            'action_label': '查看搜索质量',
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



def _build_order_health(order_period, now):
    """Summarize order friction signals that are easy to miss in a status count."""
    total_orders = order_period.count()
    completed_orders = order_period.filter(status='completed').count()
    cancelled_orders = order_period.filter(status='cancelled').count()
    pending_orders = order_period.filter(status='pending').count()
    overdue_pending_orders = order_period.filter(
        status='pending', confirmation_deadline__lt=now,
    ).count()

    created_at_by_order = dict(order_period.values_list('id', 'created_at'))
    confirmation_events = OrderEvent.objects.filter(
        order__in=order_period,
        to_status='confirmed',
    ).values('order_id', 'created_at').order_by('order_id', 'created_at')
    confirmation_hours = []
    seen_orders = set()
    for event in confirmation_events:
        order_id = event['order_id']
        if order_id in seen_orders or order_id not in created_at_by_order:
            continue
        elapsed = (event['created_at'] - created_at_by_order[order_id]).total_seconds() / 3600
        if elapsed >= 0:
            confirmation_hours.append(elapsed)
            seen_orders.add(order_id)

    average_confirmation_hours = (
        round(sum(confirmation_hours) / len(confirmation_hours), 1)
        if confirmation_hours else None
    )
    cancellation_rate = round(cancelled_orders / total_orders * 100, 1) if total_orders else 0
    if overdue_pending_orders:
        risk_level = 'critical'
        risk_label = '需要立即跟进'
        risk_message = f'有 {overdue_pending_orders} 笔预约已经超过卖家确认截止时间。'
    elif total_orders >= 3 and cancellation_rate >= 30:
        risk_level = 'warning'
        risk_label = '建议复盘'
        risk_message = f'取消率达到 {cancellation_rate}%，建议检查预约确认和沟通流程。'
    elif average_confirmation_hours is not None and average_confirmation_hours > 24:
        risk_level = 'warning'
        risk_label = '响应偏慢'
        risk_message = f'平均确认耗时 {average_confirmation_hours} 小时，建议优化确认提醒。'
    else:
        risk_level = 'stable'
        risk_label = '交易健康'
        risk_message = '当前周期暂未发现明显的交易流程风险。'

    return {
        'total_orders': total_orders,
        'completed_orders': completed_orders,
        'cancelled_orders': cancelled_orders,
        'pending_orders': pending_orders,
        'overdue_pending_orders': overdue_pending_orders,
        'cancellation_rate': cancellation_rate,
        'completion_rate': round(completed_orders / total_orders * 100, 1) if total_orders else 0,
        'average_confirmation_hours': average_confirmation_hours,
        'confirmation_sample_size': len(confirmation_hours),
        'risk_level': risk_level,
        'risk_label': risk_label,
        'risk_message': risk_message,
    }


def _build_notification_insights(notification_period, dates):
    """Measure notification reach, unread backlog, and deduplication effectiveness."""
    aggregate = notification_period.aggregate(
        row_count=Count('id'),
        event_count=Sum('occurrence_count'),
        unread_count=Count('id', filter=Q(is_read=False)),
    )
    row_count = aggregate['row_count'] or 0
    event_count = aggregate['event_count'] or 0
    unread_count = aggregate['unread_count'] or 0
    compressed_event_count = max(event_count - row_count, 0)

    kind_labels = dict(Notification.KIND_CHOICES)
    kind_rows = []
    for row in notification_period.values('kind').annotate(
        row_count=Count('id'),
        event_count=Sum('occurrence_count'),
        unread_count=Count('id', filter=Q(is_read=False)),
    ).order_by('-event_count', 'kind'):
        kind_event_count = row['event_count'] or 0
        kind_row_count = row['row_count'] or 0
        kind_rows.append({
            'kind': row['kind'],
            'label': kind_labels.get(row['kind'], row['kind']),
            'row_count': kind_row_count,
            'event_count': kind_event_count,
            'unread_count': row['unread_count'] or 0,
            'compression_rate': round(
                max(kind_event_count - kind_row_count, 0) / kind_event_count * 100, 1,
            ) if kind_event_count else 0,
        })

    daily_rows = notification_period.annotate(day=TruncDate('created_at')).values('day').annotate(
        row_count=Count('id'),
        event_count=Sum('occurrence_count'),
        unread_count=Count('id', filter=Q(is_read=False)),
    )
    daily_map = {row['day']: row for row in daily_rows}
    trend = []
    for day in dates:
        row = daily_map.get(day, {})
        trend.append({
            'date': day,
            'row_count': row.get('row_count', 0),
            'event_count': row.get('event_count', 0) or 0,
            'unread_count': row.get('unread_count', 0),
        })

    return {
        'notification_rows': row_count,
        'notification_events': event_count,
        'unread_notifications': unread_count,
        'compressed_events': compressed_event_count,
        'compression_rate': round(compressed_event_count / event_count * 100, 1) if event_count else 0,
        'unread_rate': round(unread_count / row_count * 100, 1) if row_count else 0,
        'kind_rows': kind_rows,
        'trend': trend,
        'trend_max': max((point['event_count'] for point in trend), default=1) or 1,
        'has_data': bool(row_count),
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
    notification_period = Notification.objects.filter(created_at__gte=start, created_at__lte=now)
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
    order_health = _build_order_health(order_period, now)
    detail_view_count = view_period.count()
    favorite_count = favorite_period.count()
    notification_insights = _build_notification_insights(notification_period, dates)

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
    searches_with_results = search_period.filter(result_count__gt=0).count()
    clicked_searches = SearchClick.objects.filter(
        search_query__in=search_period,
        search_query__result_count__gt=0,
    ).values('search_query_id').distinct().count()
    search_quality = _build_search_quality(
        search_count,
        zero_result_search_count,
        searches_with_results,
        clicked_searches,
    )

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
        'searches_with_results': searches_with_results,
        'search_click_through_rate': round(
            clicked_searches / searches_with_results * 100, 1,
        ) if searches_with_results else 0,
        'search_quality_score': search_quality['score'],
        'search_quality_level': search_quality['level'],
        'new_users': user_period.count(),
        'active_users': active_user_count,
        'retention_rate': user_retention['rate'],
        'new_reports': report_period.count(),
        'pending_reports': Report.objects.filter(status__in=['pending', 'reviewing']).count(),
        'notification_rows': notification_insights['notification_rows'],
        'notification_events': notification_insights['notification_events'],
        'unread_notifications': notification_insights['unread_notifications'],
        'notification_compression_rate': notification_insights['compression_rate'],
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
        'order_health': order_health,
        'top_searches': top_searches,
        'zero_result_searches': zero_result_searches,
        'search_quality': search_quality,
        'category_stats': category_stats,
        'location_stats': location_stats,
        'order_statuses': order_statuses,
        'notification_insights': notification_insights,
    }

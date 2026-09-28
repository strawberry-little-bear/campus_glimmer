from datetime import timedelta

from django.utils import timezone

from .models import BrowsingHistory, CampusCampaign, Favorite, Item, Order


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else 0


def _campaign_period(campaign, start, end):
    """Return the overlap between the report period and the campaign window."""
    window_start = max(start, campaign.starts_at)
    window_end = min(end, campaign.ends_at) if campaign.ends_at else end
    if window_start > window_end:
        return None
    return window_start, window_end


def _performance_label(*, new_items, current_items, views, orders, completed_orders):
    if not new_items and not current_items and not views and not orders:
        return '暂无数据', 'muted', '暂时没有商品或用户行为，适合补充专题内容后再观察。'
    if (new_items or current_items) and views >= 5 and not orders:
        return '浏览未转化', 'warning', '已有关注但还没有预约，建议检查价格、描述和交付地点。'
    if orders and not completed_orders:
        return '交易推进中', 'info', '专题已经产生预约，可继续关注确认和交付环节。'
    if completed_orders:
        return '表现良好', 'success', '专题已经产生完成交易，可以复用选品和活动组织方式。'
    return '持续观察', 'neutral', '已有部分供给或曝光，建议结合后续周期继续观察。'


def build_campaign_analytics(days=30, *, now=None):
    """Aggregate campaign performance without exposing individual user behavior.

    Metrics use the overlap between each campaign window and the selected report
    period. Views, favorites and orders are event counts; ``viewers`` is a
    distinct-user count used only as an additional context signal.
    """
    now = now or timezone.now()
    start = now - timedelta(days=days)
    campaigns = CampusCampaign.objects.filter(
        starts_at__lte=now,
    ).filter(
        ends_at__isnull=True,
    ) | CampusCampaign.objects.filter(
        starts_at__lte=now,
        ends_at__gte=start,
    )
    campaigns = campaigns.distinct().order_by('-starts_at', 'title')

    rows = []
    for campaign in campaigns:
        window = _campaign_period(campaign, start, now)
        if not window:
            continue
        window_start, window_end = window
        items = Item.objects.filter(campaign=campaign)
        new_items = items.filter(
            created_at__gte=window_start,
            created_at__lte=window_end,
        )
        views = BrowsingHistory.objects.filter(
            item__campaign=campaign,
            last_viewed_at__gte=window_start,
            last_viewed_at__lte=window_end,
        )
        favorites = Favorite.objects.filter(
            item__campaign=campaign,
            created_at__gte=window_start,
            created_at__lte=window_end,
        )
        orders = Order.objects.filter(
            item__campaign=campaign,
            created_at__gte=window_start,
            created_at__lte=window_end,
        )
        new_item_count = new_items.count()
        current_item_count = items.available(now).count()
        view_count = views.count()
        favorite_count = favorites.count()
        order_count = orders.count()
        completed_order_count = orders.filter(status='completed').count()
        seller_count = items.values('seller_id').distinct().count()
        performance_label, performance_tone, performance_note = _performance_label(
            new_items=new_item_count,
            current_items=current_item_count,
            views=view_count,
            orders=order_count,
            completed_orders=completed_order_count,
        )
        rows.append({
            'id': campaign.id,
            'title': campaign.title,
            'slug': campaign.slug,
            'starts_at': campaign.starts_at,
            'ends_at': campaign.ends_at,
            'new_items': new_item_count,
            'current_items': current_item_count,
            'seller_count': seller_count,
            'views': view_count,
            'viewers': views.values('user_id').distinct().count(),
            'favorites': favorite_count,
            'orders': order_count,
            'completed_orders': completed_order_count,
            'view_to_favorite_rate': _rate(favorite_count, view_count),
            'favorite_to_order_rate': _rate(order_count, favorite_count),
            'view_to_order_rate': _rate(order_count, view_count),
            'completion_rate': _rate(completed_order_count, order_count),
            'performance_label': performance_label,
            'performance_tone': performance_tone,
            'performance_note': performance_note,
        })

    rows.sort(
        key=lambda row: (
            -row['completed_orders'],
            -row['orders'],
            -row['views'],
            -row['new_items'],
            row['title'],
        ),
    )
    attention_rows = [row for row in rows if row['performance_tone'] in {'warning', 'muted'}]
    return {
        'rows': rows[:8],
        'attention_rows': attention_rows[:3],
        'period_days': days,
        'summary': {
            'campaign_count': len(rows),
            'new_items': sum(row['new_items'] for row in rows),
            'current_items': sum(row['current_items'] for row in rows),
            'views': sum(row['views'] for row in rows),
            'favorites': sum(row['favorites'] for row in rows),
            'orders': sum(row['orders'] for row in rows),
            'completed_orders': sum(row['completed_orders'] for row in rows),
            'view_to_order_rate': _rate(
                sum(row['orders'] for row in rows),
                sum(row['views'] for row in rows),
            ),
            'attention_count': len(attention_rows),
        },
    }

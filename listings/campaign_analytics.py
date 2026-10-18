from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import (
    BrowsingHistory, CampusCampaign, CampaignGoal, CampaignRegistration,
    CampaignUpdateMatch, Favorite, Item, Notification, Order,
)


GOAL_ROW_LIMIT = 12


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


def _funnel_steps(*, viewers, ordered_viewers, completed_viewers):
    maximum = max(viewers, ordered_viewers, completed_viewers, 1)
    return [
        {
            'key': 'viewers',
            'label': '浏览用户',
            'count': viewers,
            'rate': 100 if viewers else 0,
            'width': round(viewers / maximum * 100, 1),
            'note': '周期内最近一次浏览落在窗口内的去重用户',
        },
        {
            'key': 'ordered',
            'label': '预约用户',
            'count': ordered_viewers,
            'rate': _rate(ordered_viewers, viewers),
            'width': round(ordered_viewers / maximum * 100, 1),
            'note': '浏览用户中创建了预约的去重买家',
        },
        {
            'key': 'completed',
            'label': '完成交易用户',
            'count': completed_viewers,
            'rate': _rate(completed_viewers, ordered_viewers),
            'width': round(completed_viewers / maximum * 100, 1),
            'note': '预约用户中对应订单当前状态为 completed 的去重买家',
        },
    ]


def _goal_window(goal, now):
    campaign = goal.campaign
    starts_at = goal.starts_at or campaign.starts_at
    deadline = goal.ends_at or campaign.ends_at
    measurement_end = min(deadline, now) if deadline else now
    return starts_at, deadline, measurement_end


def _goal_value(goal, *, starts_at, measurement_end):
    if starts_at > measurement_end:
        return 0

    campaign = goal.campaign
    if goal.metric == 'new_items':
        return Item.objects.filter(
            campaign=campaign,
            created_at__gte=starts_at,
            created_at__lte=measurement_end,
        ).count()
    if goal.metric == 'viewers':
        return BrowsingHistory.objects.filter(
            item__campaign=campaign,
            last_viewed_at__gte=starts_at,
            last_viewed_at__lte=measurement_end,
        ).values('user_id').distinct().count()
    if goal.metric == 'favorite_users':
        return Favorite.objects.filter(
            item__campaign=campaign,
            created_at__gte=starts_at,
            created_at__lte=measurement_end,
        ).values('user_id').distinct().count()
    if goal.metric == 'registration_users':
        return CampaignRegistration.objects.filter(
            campaign=campaign,
            joined_at__gte=starts_at,
            joined_at__lte=measurement_end,
        ).values('user_id').distinct().count()
    if goal.metric == 'order_users':
        return Order.objects.filter(
            item__campaign=campaign,
            created_at__gte=starts_at,
            created_at__lte=measurement_end,
        ).values('buyer_id').distinct().count()
    if goal.metric == 'completed_order_users':
        return Order.objects.filter(
            item__campaign=campaign,
            status='completed',
            created_at__gte=starts_at,
            created_at__lte=measurement_end,
        ).values('buyer_id').distinct().count()
    return 0


def _goal_row(goal, now):
    starts_at, deadline, measurement_end = _goal_window(goal, now)
    value = _goal_value(
        goal,
        starts_at=starts_at,
        measurement_end=measurement_end,
    )
    target = goal.target_value
    progress = _rate(value, target)

    if now < starts_at:
        elapsed_percent = 0
        pace_delta = None
        status_label = '待开始'
        status_tone = 'neutral'
    elif deadline and deadline > starts_at:
        elapsed_percent = round(
            min(max((measurement_end - starts_at) / (deadline - starts_at), 0), 1) * 100,
            1,
        )
        pace_delta = round(progress - elapsed_percent, 1)
        if value >= target:
            status_label = '已达成'
            status_tone = 'success'
        elif deadline <= now:
            status_label = '已到期未达成'
            status_tone = 'warning'
        elif pace_delta < -10:
            status_label = '需要推进'
            status_tone = 'warning'
        else:
            status_label = '按计划推进'
            status_tone = 'info'
    else:
        elapsed_percent = None
        pace_delta = None
        if value >= target:
            status_label = '已达成'
            status_tone = 'success'
        else:
            status_label = '开放目标'
            status_tone = 'neutral'

    return {
        'id': goal.id,
        'campaign_id': goal.campaign_id,
        'campaign_title': goal.campaign.title,
        'metric': goal.metric,
        'metric_label': goal.get_metric_display(),
        'target_value': target,
        'current_value': value,
        'remaining_value': max(target - value, 0),
        'progress_percent': progress,
        'progress_width': min(max(progress, 0), 100),
        'elapsed_percent': elapsed_percent,
        'pace_delta': pace_delta,
        'status_label': status_label,
        'status_tone': status_tone,
        'starts_at': starts_at,
        'ends_at': deadline,
        'note': goal.note,
    }


def build_campaign_analytics(days=30, *, now=None):
    """Aggregate campaign performance without exposing individual users.

    The report follows the campaign overlap with the selected dashboard window.
    Goals are evaluated separately against their own configured window, because
    changing the dashboard from 7 to 30 days must not silently redefine whether
    an operating target was met. The funnel starts from distinct viewers and
    then keeps only viewers who also created an order and, finally, a completed
    order. Favorites and registrations are reported as intent branches rather
    than forced into one artificially sequential path.
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
    campaigns = list(campaigns.distinct().order_by('-starts_at', 'title'))

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
        registrations = CampaignRegistration.objects.filter(campaign=campaign)
        new_registrations = registrations.filter(
            joined_at__gte=window_start,
            joined_at__lte=window_end,
        )

        viewer_ids = set(views.values_list('user_id', flat=True))
        favorite_user_ids = set(favorites.values_list('user_id', flat=True))
        registration_user_ids = set(new_registrations.values_list('user_id', flat=True))
        order_user_ids = set(orders.values_list('buyer_id', flat=True))
        completed_order_user_ids = set(
            orders.filter(status='completed').values_list('buyer_id', flat=True),
        )
        ordered_viewers = viewer_ids & order_user_ids
        completed_viewers = ordered_viewers & completed_order_user_ids
        viewer_favorites = viewer_ids & favorite_user_ids
        viewer_registrations = viewer_ids & registration_user_ids

        new_item_count = new_items.count()
        current_item_count = items.available(now).count()
        view_count = views.count()
        favorite_count = favorites.count()
        order_count = orders.count()
        completed_order_count = orders.filter(status='completed').count()
        seller_count = items.values('seller_id').distinct().count()
        active_registration_count = registrations.filter(is_active=True).count()
        new_registration_count = new_registrations.count()
        current_subscription_count = registrations.filter(
            is_active=True,
            notify_updates=True,
        ).count()
        pending_update_match_count = CampaignUpdateMatch.objects.filter(
            registration__campaign=campaign,
            registration__is_active=True,
            registration__notify_updates=True,
            registration__campaign__is_active=True,
            registration__campaign__starts_at__lte=now,
            notified_at__isnull=True,
            cancelled_at__isnull=True,
            item__campaign=campaign,
            item__status='available',
        ).filter(
            Q(item__expires_at__isnull=True) | Q(item__expires_at__gt=now),
            Q(registration__campaign__ends_at__isnull=True)
            | Q(registration__campaign__ends_at__gt=now),
        ).count()
        digest_notifications = Notification.objects.filter(
            kind='campaign_update',
            campaign_update_matches__registration__campaign=campaign,
            created_at__gte=window_start,
            created_at__lte=window_end,
        ).distinct()
        sent_digest_count = digest_notifications.count()
        read_digest_count = digest_notifications.filter(
            is_read=True,
            read_at__isnull=False,
        ).count()
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
            'viewers': len(viewer_ids),
            'favorites': favorite_count,
            'favorite_users': len(favorite_user_ids),
            'registration_users': len(registration_user_ids),
            'order_users': len(order_user_ids),
            'completed_order_users': len(completed_order_user_ids),
            'viewer_favorite_users': len(viewer_favorites),
            'viewer_registration_users': len(viewer_registrations),
            'funnel_viewers': len(viewer_ids),
            'funnel_order_users': len(ordered_viewers),
            'funnel_completed_users': len(completed_viewers),
            'funnel_steps': _funnel_steps(
                viewers=len(viewer_ids),
                ordered_viewers=len(ordered_viewers),
                completed_viewers=len(completed_viewers),
            ),
            'intent_user_count': len(viewer_favorites | viewer_registrations),
            'intent_rate': _rate(
                len(viewer_favorites | viewer_registrations), len(viewer_ids),
            ),
            'viewer_to_order_user_rate': _rate(len(ordered_viewers), len(viewer_ids)),
            'order_to_complete_user_rate': _rate(
                len(completed_viewers), len(ordered_viewers),
            ),
            'active_registration_count': active_registration_count,
            'new_registration_count': new_registration_count,
            'current_subscription_count': current_subscription_count,
            'pending_update_match_count': pending_update_match_count,
            'period_sent_digest_count': sent_digest_count,
            'period_read_digest_count': read_digest_count,
            'digest_read_rate': _rate(read_digest_count, sent_digest_count),
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
    campaign_ids = [row['id'] for row in rows]

    goals = CampaignGoal.objects.filter(
        campaign_id__in=campaign_ids,
        is_active=True,
    ).select_related('campaign').order_by('campaign__title', 'metric')
    goal_rows = [_goal_row(goal, now) for goal in goals]
    achieved_goal_count = sum(row['status_label'] == '已达成' for row in goal_rows)
    goal_attention_count = sum(
        row['status_label'] in {'需要推进', '已到期未达成'} for row in goal_rows
    )

    period_digests = Notification.objects.filter(
        kind='campaign_update',
        campaign_update_matches__registration__campaign_id__in=campaign_ids,
        created_at__gte=start,
        created_at__lte=now,
    ).distinct()
    period_sent_digest_count = period_digests.count()
    period_read_digest_count = period_digests.filter(
        is_read=True,
        read_at__isnull=False,
    ).count()
    return {
        'rows': rows[:8],
        'attention_rows': attention_rows[:3],
        'goal_rows': goal_rows[:GOAL_ROW_LIMIT],
        'all_goal_rows': goal_rows,
        'goal_rows_truncated': len(goal_rows) > GOAL_ROW_LIMIT,
        'period_days': days,
        'summary': {
            'campaign_count': len(rows),
            'new_items': sum(row['new_items'] for row in rows),
            'current_items': sum(row['current_items'] for row in rows),
            'views': sum(row['views'] for row in rows),
            'favorites': sum(row['favorites'] for row in rows),
            'viewers': sum(row['viewers'] for row in rows),
            'active_registration_count': sum(
                row['active_registration_count'] for row in rows
            ),
            'new_registration_count': sum(
                row['new_registration_count'] for row in rows
            ),
            'current_subscription_count': sum(
                row['current_subscription_count'] for row in rows
            ),
            'pending_update_match_count': sum(
                row['pending_update_match_count'] for row in rows
            ),
            'period_sent_digest_count': period_sent_digest_count,
            'period_read_digest_count': period_read_digest_count,
            'digest_read_rate': _rate(
                period_read_digest_count,
                period_sent_digest_count,
            ),
            'orders': sum(row['orders'] for row in rows),
            'completed_orders': sum(row['completed_orders'] for row in rows),
            'view_to_order_rate': _rate(
                sum(row['orders'] for row in rows),
                sum(row['views'] for row in rows),
            ),
            'goal_count': len(goal_rows),
            'achieved_goal_count': achieved_goal_count,
            'goal_attention_count': goal_attention_count,
            'funnel_viewers': sum(row['funnel_viewers'] for row in rows),
            'funnel_order_users': sum(row['funnel_order_users'] for row in rows),
            'funnel_completed_users': sum(
                row['funnel_completed_users'] for row in rows
            ),
            'funnel_steps': _funnel_steps(
                viewers=sum(row['funnel_viewers'] for row in rows),
                ordered_viewers=sum(row['funnel_order_users'] for row in rows),
                completed_viewers=sum(
                    row['funnel_completed_users'] for row in rows
                ),
            ),
            'attention_count': len(attention_rows),
        },
    }

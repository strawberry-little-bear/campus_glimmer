from chat_messages.models import PrivateMessage
from listings.models import Notification


NAV_ACTIVE_URL_NAMES = {
    'home': frozenset({'home'}),
    'items': frozenset({
        'item_list', 'item_list_by_category', 'search_items', 'item_detail',
        'new_item', 'edit_item', 'delete_item', 'mark_sold', 'refresh_item',
        'report_item', 'create_order', 'manage_gift_applications',
    }),
    'demands': frozenset({
        'demand_list', 'my_demands', 'demand_detail', 'new_demand',
        'edit_demand', 'close_demand', 'respond_to_demand',
        'submit_demand_feedback', 'review_demand_response',
    }),
    'lost_found': frozenset({
        'lost_found_list', 'my_lost_found', 'lost_found_detail',
        'new_lost_found', 'edit_lost_found', 'close_lost_found',
        'submit_lost_found_lead', 'submit_lost_found_feedback',
        'review_lost_found_lead',
    }),
    'opportunities': frozenset({'opportunity_feed'}),
    'campaigns': frozenset({
        'campaign_list', 'my_campaigns', 'campaign_detail', 'join_campaign',
        'leave_campaign', 'update_campaign_subscription',
    }),
}


def navigation_state(request):
    """Mark the top-level section for the current resolver route."""
    match = getattr(request, 'resolver_match', None)
    url_name = getattr(match, 'url_name', None)
    active_section = next(
        (section for section, names in NAV_ACTIVE_URL_NAMES.items()
         if url_name in names),
        '',
    )
    return {'active_nav_section': active_section}


def unread_counts(request):
    """Expose compact unread counters to the global navigation."""
    if not request.user.is_authenticated:
        return {
            'unread_notification_count': 0,
            'unread_message_count': 0,
        }

    return {
        'unread_notification_count': Notification.objects.filter(
            recipient=request.user,
            is_read=False,
        ).count(),
        'unread_message_count': PrivateMessage.objects.filter(
            receiver=request.user,
            is_read=False,
        ).count(),
    }

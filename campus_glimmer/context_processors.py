from chat_messages.models import PrivateMessage
from listings.models import Notification


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

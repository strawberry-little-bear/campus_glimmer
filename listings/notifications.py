from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Notification, NotificationPreference


def create_notification(
    recipient, *, kind, title, message, actor=None, order=None, item=None,
    target_url='', dedupe_key='', dedupe_window_seconds=300,
):
    """Create a notification, aggregating repeated unread events when requested."""
    if not recipient or (actor and recipient.pk == actor.pk):
        return None
    preference, _ = NotificationPreference.objects.get_or_create(user=recipient)
    if not getattr(preference, kind, True):
        return None

    now = timezone.now()
    if dedupe_key:
        cutoff = now - timedelta(seconds=max(0, dedupe_window_seconds))
        with transaction.atomic():
            existing = Notification.objects.select_for_update().filter(
                recipient=recipient,
                kind=kind,
                dedupe_key=dedupe_key,
                is_read=False,
            ).filter(
                last_occurred_at__gte=cutoff,
            ).order_by('-created_at').first()
            if existing:
                existing.actor = actor
                existing.order = order
                existing.item = item
                existing.message = message
                existing.target_url = target_url
                existing.occurrence_count += 1
                existing.last_occurred_at = now
                existing.save(update_fields=[
                    'actor', 'order', 'item', 'message', 'target_url',
                    'occurrence_count', 'last_occurred_at',
                ])
                return existing

    return Notification.objects.create(
        recipient=recipient,
        actor=actor,
        order=order,
        item=item,
        kind=kind,
        title=title,
        message=message,
        target_url=target_url,
        dedupe_key=dedupe_key,
        last_occurred_at=now,
    )

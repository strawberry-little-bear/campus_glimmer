from .models import Notification


def create_notification(recipient, *, kind, title, message, actor=None, order=None, item=None, target_url=''):
    """Create one user-facing notification, ignoring self-notifications."""
    if not recipient or (actor and recipient.pk == actor.pk):
        return None
    return Notification.objects.create(
        recipient=recipient,
        actor=actor,
        order=order,
        item=item,
        kind=kind,
        title=title,
        message=message,
        target_url=target_url,
    )

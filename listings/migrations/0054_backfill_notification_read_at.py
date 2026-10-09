from django.db import migrations
from django.utils import timezone


def backfill_read_at(apps, schema_editor):
    """Stamp historical read notifications so response-time stats stay sane.

    Existing rows have no read_at, which would otherwise make every historical
    notification look unread or produce a zero response time. The stamp uses
    last_occurred_at when available (the last time the event fired) and falls
    back to created_at, so the resulting duration is never negative.
    """
    Notification = apps.get_model('listings', 'Notification')
    rows = Notification.objects.filter(is_read=True, read_at__isnull=True)
    for notification in rows.iterator():
        stamp = notification.last_occurred_at or notification.created_at or timezone.now()
        if stamp < notification.created_at:
            stamp = notification.created_at
        notification.read_at = stamp
        notification.save(update_fields=['read_at'])


def clear_backfilled_read_at(apps, schema_editor):
    """Reverse the backfill so the migration can be rolled back cleanly."""
    Notification = apps.get_model('listings', 'Notification')
    Notification.objects.filter(read_at__isnull=False).update(read_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0053_notification_read_at_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_read_at, clear_backfilled_read_at),
    ]
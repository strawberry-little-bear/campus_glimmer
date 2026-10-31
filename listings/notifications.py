from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Notification, NotificationPreference


MAX_BULK_NOTIFICATION_IDS = 200
NOTIFICATION_SNOOZE_DURATIONS = {
    '2h': (timedelta(hours=2), '2 小时'),
    '24h': (timedelta(hours=24), '24 小时'),
    '3d': (timedelta(days=3), '3 天'),
}
NOTIFICATION_SNOOZE_OPTIONS = tuple(
    (key, label)
    for key, (_, label) in NOTIFICATION_SNOOZE_DURATIONS.items()
)


def actionable_unread_q(now=None):
    """Return the query condition for unread notifications that are due now."""
    now = now or timezone.now()
    return Q(is_read=False) & (
        Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now)
    )


def quiet_window_active(preference, *, now=None):
    """Whether an already-resolved preference row sits inside its quiet window now.

    Windows that cross midnight are supported, for example 22:00–08:00.

    This is the time-of-day arithmetic on its own, with no database access. Keeping it
    apart from `quiet_hours_active` lets a caller that already holds a batch of
    preference rows evaluate every one of them without paying a query per user.
    """
    now = now or timezone.now()
    current = timezone.localtime(now).time().replace(tzinfo=None)
    start = preference.quiet_hours_start
    end = preference.quiet_hours_end
    if start < end:
        return start <= current < end
    return current >= start or current < end


def quiet_hours_active(recipient, *, now=None):
    """Whether the recipient is currently inside their local quiet-hours window.

    Quiet hours only suppress badge-style reminders; notifications remain stored
    and can still be opened deliberately from the notification center.
    """
    now = now or timezone.now()
    preference = NotificationPreference.objects.filter(user=recipient).first()
    if not preference or not preference.quiet_hours_enabled:
        return False
    return quiet_window_active(preference, now=now)


def quiet_hours_for_users(user_ids, *, now=None):
    """The subset of ``user_ids`` currently inside their quiet-hours window.

    Batch counterpart to `quiet_hours_active`. The single-user version resolves one
    recipient's preference row per call, so a caller looking at a whole cohort would
    turn the cohort size into a query count. This resolves every row in one query and
    applies the same `quiet_window_active` arithmetic to each of them, so the two paths
    cannot drift apart.

    Users without a preference row are never in quiet hours: the row does not exist
    until the user first opens the settings page, and the default is no window.
    """
    now = now or timezone.now()
    user_ids = list(user_ids)
    if not user_ids:
        return set()
    rows = NotificationPreference.objects.filter(
        user_id__in=user_ids,
    ).only(
        'user_id', 'quiet_hours_enabled', 'quiet_hours_start', 'quiet_hours_end',
    )
    return {
        row.user_id
        for row in rows
        if row.quiet_hours_enabled and quiet_window_active(row, now=now)
    }


def active_unread_notifications(recipient, *, now=None, quiet=None):
    """Return unread notifications that should currently affect the inbox badge.

    Callers that already resolved the recipient's quiet-hours state pass it
    in as ``quiet`` so the preference row is not fetched a second time.
    """
    now = now or timezone.now()
    if quiet is None:
        quiet = quiet_hours_active(recipient, now=now)
    if quiet:
        return Notification.objects.none()
    return Notification.objects.filter(
        recipient=recipient,
    ).filter(actionable_unread_q(now))


def normalize_notification_ids(raw_ids, *, limit=MAX_BULK_NOTIFICATION_IDS):
    """Return unique positive IDs in request order, capped for bounded updates."""
    if limit <= 0:
        return []
    normalized = []
    seen = set()
    for raw_id in raw_ids:
        try:
            notification_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if notification_id <= 0 or notification_id in seen:
            continue
        seen.add(notification_id)
        normalized.append(notification_id)
        if len(normalized) >= limit:
            break
    return normalized


def bulk_snooze_notifications(recipient, notification_ids, *, duration_key, now=None):
    """Snooze only owned notifications that are currently actionable and unread.

    The returned skipped count keeps the view honest about records the user saw
    on the page but which changed state or no longer belong in that action.
    """
    duration = NOTIFICATION_SNOOZE_DURATIONS.get(duration_key)
    if duration is None:
        return 0, len(notification_ids)

    now = now or timezone.now()
    updated_count = Notification.objects.filter(
        recipient=recipient,
        id__in=notification_ids,
    ).filter(actionable_unread_q(now)).update(
        snoozed_until=now + duration[0],
    )
    return updated_count, max(len(notification_ids) - updated_count, 0)


def restore_snoozed_notifications(recipient, notification_ids, *, now=None):
    """Restore only owned unread notifications that are still snoozed."""
    now = now or timezone.now()
    updated_count = Notification.objects.filter(
        recipient=recipient,
        id__in=notification_ids,
        is_read=False,
        snoozed_until__gt=now,
    ).update(snoozed_until=None)
    return updated_count, max(len(notification_ids) - updated_count, 0)


def create_notification(
    recipient, *, kind, title, message, actor=None, order=None, item=None, demand=None,
    target_url='', dedupe_key='', dedupe_window_seconds=300, dedupe_forever=False,
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
            existing_query = Notification.objects.select_for_update(of=('self',)).filter(
                recipient=recipient,
                kind=kind,
                dedupe_key=dedupe_key,
            )
            if not dedupe_forever:
                existing_query = existing_query.filter(
                    is_read=False,
                    last_occurred_at__gte=cutoff,
                )
            existing = existing_query.order_by('-created_at').first()
            if existing:
                existing.actor = actor
                existing.order = order
                existing.item = item
                existing.demand = demand
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
        demand=demand,
        kind=kind,
        title=title,
        message=message,
        target_url=target_url,
        dedupe_key=dedupe_key,
        last_occurred_at=now,
    )

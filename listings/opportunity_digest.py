"""Deliver an idempotent daily digest for personalized campus opportunities."""

from django.contrib.auth.models import User
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Item, LostFoundPost, Notification, NotificationPreference
from .notifications import create_notification
from .opportunity_feed import build_opportunity_feed
from .academic_calendar import academic_phase_summary_line


def _candidate_user_ids():
    """Return users who have something that can produce an opportunity."""
    now = timezone.now()
    item_users = Item.objects.available().values_list('seller_id', flat=True)
    lost_found_users = LostFoundPost.objects.filter(
        reporter_id__isnull=False, status='active',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    ).values_list('reporter_id', flat=True)
    return set(item_users).union(lost_found_users)


def send_opportunity_digest(*, digest_date=None, user_ids=None):
    """Send one digest per user and local calendar date.

    The digest is deliberately generated from the same explainable opportunity
    feed shown in the UI. This keeps scheduled notifications and the page
    consistent, while the date-scoped dedupe key makes rerunning a scheduler
    safe.
    """
    digest_date = digest_date or timezone.localdate()
    candidate_ids = set(user_ids) if user_ids is not None else _candidate_user_ids()
    recipients = User.objects.filter(pk__in=candidate_ids).order_by('pk')
    result = {'sent': 0, 'skipped': 0, 'empty': 0}

    for user in recipients: # pragma: no branch - queryset is finite and explicit
        feed = build_opportunity_feed(user)
        if not feed['opportunities']:
            result['empty'] += 1
            continue

        preference, _ = NotificationPreference.objects.get_or_create(user=user)
        if not preference.opportunity_digest:
            result['skipped'] += 1
            continue

        dedupe_key = f'opportunity-digest:{digest_date.isoformat()}:{user.pk}'
        if Notification.objects.filter(
            recipient=user, kind='opportunity_digest', dedupe_key=dedupe_key,
        ).exists():
            result['skipped'] += 1
            continue

        demand_count = feed['demand_count']
        lost_found_count = feed['lost_found_count']
        message = (
            f'为你找到 {demand_count} 条可响应求购和 {lost_found_count} 条失物招领线索，'
            '打开互助机会页面查看匹配依据。'
        )
        phase_line = academic_phase_summary_line()
        if phase_line:
            message += phase_line
        notification = create_notification(
            user,
            kind='opportunity_digest',
            title='今天有新的校园互助机会',
            message=message,
            target_url=reverse('opportunity_feed'),
            dedupe_key=dedupe_key,
            dedupe_forever=True,
        )
        if notification:
            result['sent'] += 1

    return result

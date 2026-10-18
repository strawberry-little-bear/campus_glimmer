"""Queue and deliver campaign update digests without reserving inventory.

Joining a campaign and subscribing to its update digest are deliberately two
separate decisions. This module keeps the buffer idempotent, re-checks every
boundary at delivery time, and merges all of one user's eligible matches into a
single notification per cadence period.
"""

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    CampaignRegistration, CampaignUpdateMatch, Notification,
    NotificationPreference,
)
from .notifications import create_notification, quiet_hours_active


FREQUENCIES = ('daily', 'weekly')
MAX_LISTED_TITLES = 5
LQ = '“'
RQ = '”'
SEP = '、'


class CampaignUpdateSubscriptionUnavailable(ValueError):
    """Raised when there is no live registration that can own a subscription."""


def _digest_period(frequency, *, today=None):
    today = today or timezone.localdate()
    if frequency == 'weekly':
        iso_year, iso_week, _ = today.isocalendar()
        return f'{iso_year}-W{iso_week:02d}'
    return today.isoformat()


def _digest_key(frequency, period, user_id):
    return f'campaign-update-digest:{frequency}:{period}:{user_id}'


def _eligible_item(item, campaign, *, now):
    """Return whether a buffered item can still be shown in today's digest."""
    return bool(
        item.status == 'available'
        and (item.expires_at is None or item.expires_at > now)
        and item.campaign_id == campaign.pk
    )


def _cancel_invalid_pending_matches(frequencies, *, now, allowed_user_ids=None):
    """Cancel pending matches whose delivery boundary is already gone.

    Candidate scans intentionally ignore inactive registrations and closed
    campaigns. Without this separate audit pass, their old pending rows would
    never become visible to the sender and would look forever queued. Global
    notification preferences are deliberately not checked here: a temporarily
    disabled digest keeps its eligible buffer until the user opts back in.
    """
    pending = CampaignUpdateMatch.objects.filter(
        notified_at__isnull=True,
        cancelled_at__isnull=True,
        registration__update_frequency__in=frequencies,
    ).select_related(
        'registration', 'registration__campaign', 'item',
    )
    if allowed_user_ids is not None:
        pending = pending.filter(registration__user_id__in=allowed_user_ids)

    invalid_ids = []
    for match in pending.iterator():
        registration = match.registration
        campaign = registration.campaign
        if (
            not registration.is_active
            or not registration.notify_updates
            or not campaign.is_live(now)
            or not _eligible_item(match.item, campaign, now=now)
        ):
            invalid_ids.append(match.pk)
    if invalid_ids:
        CampaignUpdateMatch.objects.filter(
            pk__in=invalid_ids,
            notified_at__isnull=True,
            cancelled_at__isnull=True,
        ).update(cancelled_at=now)
    return len(invalid_ids)


def queue_campaign_update_matches(item, *, now=None):
    """Buffer one newly attached item for every actively subscribed registration.

    The unique ``registration + item`` constraint makes publication retries and
    repeated bulk operations safe. Only active registrations whose owner opted in
    are selected; joining a campaign alone never creates a match.
    """
    now = now or timezone.now()
    if not item.campaign_id:
        return 0
    if item.status != 'available':
        return 0
    if item.expires_at and item.expires_at <= now:
        return 0

    campaign = item.campaign
    if not campaign.is_live(now):
        return 0

    registration_ids = CampaignRegistration.objects.filter(
        campaign=campaign,
        is_active=True,
        notify_updates=True,
    ).values_list('id', flat=True)
    existing_registration_ids = set(
        CampaignUpdateMatch.objects.filter(
            item=item,
            registration_id__in=registration_ids,
        ).values_list('registration_id', flat=True)
    )
    matches = [
        CampaignUpdateMatch(registration_id=registration_id, item=item, matched_at=now)
        for registration_id in registration_ids
        if registration_id not in existing_registration_ids
    ]
    if not matches:
        return 0
    CampaignUpdateMatch.objects.bulk_create(matches, ignore_conflicts=True)
    return len(matches)


def set_campaign_update_preference(
    user, campaign, *, notify_updates, update_frequency='daily', now=None,
):
    """Update the subscription owned by the user's current active registration.

    Turning the subscription off cancels pending matches so a later opt-in never
    delivers items the user had already declined to receive.
    """
    now = now or timezone.now()
    if update_frequency not in FREQUENCIES:
        raise ValueError('不支持的专题摘要频率。')
    if not campaign.is_live(now):
        raise CampaignUpdateSubscriptionUnavailable('专题当前不在可订阅时间内。')

    with transaction.atomic():
        registration = CampaignRegistration.objects.select_for_update(of=('self',)).filter(
            campaign=campaign,
            user=user,
            is_active=True,
        ).first()
        if registration is None:
            raise CampaignUpdateSubscriptionUnavailable('请先加入专题，再设置上新摘要。')

        changed = (
            registration.notify_updates != notify_updates
            or registration.update_frequency != update_frequency
        )
        if changed:
            registration.notify_updates = notify_updates
            registration.update_frequency = update_frequency
            registration.save(update_fields=['notify_updates', 'update_frequency'])

        if not notify_updates:
            CampaignUpdateMatch.objects.filter(
                registration=registration,
                notified_at__isnull=True,
                cancelled_at__isnull=True,
            ).update(cancelled_at=now)

    return registration, changed


def _candidate_registrations(frequency, now):
    return CampaignRegistration.objects.filter(
        is_active=True,
        notify_updates=True,
        update_frequency=frequency,
        campaign__is_active=True,
        campaign__starts_at__lte=now,
    ).filter(
        Q(campaign__ends_at__isnull=True)
        | Q(campaign__ends_at__gt=now)
    ).select_related('campaign', 'user').order_by('user_id', 'pk')


def _build_message(matches):
    shown = matches[:MAX_LISTED_TITLES]
    campaign_titles = []
    for match in shown:
        title = match.registration.campaign.title
        if title not in campaign_titles:
            campaign_titles.append(title)
    titles = SEP.join(f'{LQ}{match.item.title}{RQ}' for match in shown)
    hidden = len(matches) - len(shown)
    campaign_text = SEP.join(f'{LQ}{title}{RQ}' for title in campaign_titles)
    message = f'你订阅的校园专题{campaign_text}本次新增 {len(matches)} 件商品：{titles}'
    if hidden:
        message += f'，另有 {hidden} 件未列出'
    return (message + '。打开我的专题足迹查看。')[:255]


def send_campaign_update_digest(*, frequency=None, now=None, user_ids=None):
    """Merge pending campaign matches into one notification per user and period.

    Invalid matches are cancelled for auditability. Preference-disabled and
    quiet-hours users keep their pending rows for a later run, while users with
    no still-eligible match are counted without a write.
    """
    frequencies = [frequency] if frequency else list(FREQUENCIES)
    now = now or timezone.now()
    today = timezone.localdate(now)
    allowed_user_ids = set(user_ids) if user_ids is not None else None
    result = {
        'sent': 0,
        'skipped': 0,
        'empty': 0,
        'quiet': 0,
        'invalid': 0,
        'delivered_matches': 0,
    }

    result['invalid'] += _cancel_invalid_pending_matches(
        frequencies,
        now=now,
        allowed_user_ids=allowed_user_ids,
    )

    for cadence in frequencies:
        period = _digest_period(cadence, today=today)
        registrations = _candidate_registrations(cadence, now)
        if allowed_user_ids is not None:
            registrations = registrations.filter(user_id__in=allowed_user_ids)
        by_user = {}
        for registration in registrations:
            by_user.setdefault(registration.user_id, []).append(registration)

        for user_id, user_registrations in by_user.items():
            user = User.objects.filter(pk=user_id).first()
            if user is None:
                continue

            preference, _ = NotificationPreference.objects.get_or_create(user=user)
            if not preference.campaign_update:
                result['skipped'] += 1
                continue
            if quiet_hours_active(user, now=now):
                result['quiet'] += 1
                continue

            registration_by_id = {reg.pk: reg for reg in user_registrations}
            pending = list(
                CampaignUpdateMatch.objects.filter(
                    registration_id__in=registration_by_id,
                    notified_at__isnull=True,
                    cancelled_at__isnull=True,
                ).select_related(
                    'registration', 'registration__campaign', 'item',
                ).order_by('matched_at', 'pk')
            )
            eligible = []
            invalid_ids = []
            for match in pending:
                registration = registration_by_id[match.registration_id]
                if _eligible_item(match.item, registration.campaign, now=now):
                    match.registration = registration
                    eligible.append(match)
                else:
                    invalid_ids.append(match.pk)
            if invalid_ids:
                CampaignUpdateMatch.objects.filter(pk__in=invalid_ids).update(
                    cancelled_at=now,
                )
                result['invalid'] += len(invalid_ids)
            if not eligible:
                result['empty'] += 1
                continue

            dedupe_key = _digest_key(cadence, period, user_id)
            if Notification.objects.filter(
                recipient=user,
                kind='campaign_update',
                dedupe_key=dedupe_key,
            ).exists():
                result['skipped'] += 1
                continue

            with transaction.atomic():
                if Notification.objects.filter(
                    recipient=user,
                    kind='campaign_update',
                    dedupe_key=dedupe_key,
                ).exists():
                    result['skipped'] += 1
                    continue
                notification = create_notification(
                    user,
                    kind='campaign_update',
                    title='校园专题新上架摘要',
                    message=_build_message(eligible),
                    target_url=reverse('my_campaigns'),
                    dedupe_key=dedupe_key,
                    dedupe_forever=True,
                )
                if notification is None:
                    result['skipped'] += 1
                    continue
                CampaignUpdateMatch.objects.filter(
                    pk__in=[match.pk for match in eligible],
                    notified_at__isnull=True,
                    cancelled_at__isnull=True,
                ).update(
                    notified_at=now,
                    notification=notification,
                )
            result['sent'] += 1
            result['delivered_matches'] += len(eligible)

    return result

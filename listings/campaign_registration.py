from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import CampaignRegistration


class CampaignRegistrationClosed(ValueError):
    """Raised when a campaign no longer accepts registration changes."""


@dataclass(frozen=True)
class CampaignRegistrationResult:
    registration: CampaignRegistration | None
    changed: bool
    state: str


def _ensure_campaign_is_live(campaign, now):
    if not campaign.is_live(now):
        raise CampaignRegistrationClosed('专题当前不在可参与时间内。')


def join_campaign(user, campaign, *, now=None):
    """Join once, idempotently, while preserving earlier participation intervals."""
    now = now or timezone.now()
    _ensure_campaign_is_live(campaign, now)

    active = CampaignRegistration.objects.filter(
        campaign=campaign,
        user=user,
        is_active=True,
    ).first()
    if active is not None:
        return CampaignRegistrationResult(active, False, 'already_joined')

    try:
        with transaction.atomic():
            active = CampaignRegistration.objects.select_for_update().filter(
                campaign=campaign,
                user=user,
                is_active=True,
            ).first()
            if active is not None:
                return CampaignRegistrationResult(active, False, 'already_joined')
            registration = CampaignRegistration.objects.create(
                campaign=campaign,
                user=user,
                joined_at=now,
            )
            return CampaignRegistrationResult(registration, True, 'joined')
    except IntegrityError:
        active = CampaignRegistration.objects.filter(
            campaign=campaign,
            user=user,
            is_active=True,
        ).first()
        if active is not None:
            return CampaignRegistrationResult(active, False, 'already_joined')
        raise


def leave_campaign(user, campaign, *, now=None):
    """Close the current participation interval without touching older ones."""
    now = now or timezone.now()
    _ensure_campaign_is_live(campaign, now)

    with transaction.atomic():
        registration = CampaignRegistration.objects.select_for_update().filter(
            campaign=campaign,
            user=user,
            is_active=True,
        ).first()
        if registration is None:
            return CampaignRegistrationResult(None, False, 'not_joined')
        registration.is_active = False
        registration.left_at = now
        registration.save(update_fields=['is_active', 'left_at'])
        return CampaignRegistrationResult(registration, True, 'left')

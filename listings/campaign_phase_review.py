# -*- coding: utf-8 -*-
"""Review a closed campus campaign through its own three-part timeline.

`build_campaign_analytics` answers what a campaign contributed inside the
dashboard period. It cannot answer when the contribution happened inside the
campaign. A campaign can collect every reservation in its first week and then
go quiet, or it can only begin moving near the end; both campaigns can have the
same thirty-day funnel and completely different operating stories.

This module therefore reads only campaigns whose end time has passed, splits
each campaign's own start-to-end window into opening, middle, and closing
phases, and counts supply and behaviour in each phase. The dashboard period
selector is deliberately ignored: a closed campaign is already its own bounded
reporting window, and forcing it through "the last 30 days" would mix the shape
of the campaign with the shape of the operator's current view.

The phase boundary is half-open. A record exactly on an internal boundary
belongs to the later phase, never both; a record exactly at the campaign end is
outside the campaign and is not counted. All four event families are read in
one pass over the selected campaigns and then bucketed in Python, so adding the
panel does not add twelve database round trips per campaign.

Direction is intentionally narrow. Phases have almost equal lengths, but the
comparison still uses orders per 100 days rather than raw orders. A campaign
gets a peak sentence only when every one of its three phases has at least three
orders. Thin phases still show their counts; they simply do not get a direction
dressed up as a finding. Cross-campaign text appears only when at least two
closed campaigns were reviewed, and it counts explicit peaks rather than
ranking campaigns.

Nothing in this module changes a campaign, item, order, exposure setting, or
notification. It is a read-only explanation of an already closed timeline.
"""

from collections import Counter

from django.db.models import Q
from django.utils import timezone

from .models import BrowsingHistory, CampusCampaign, Favorite, Item, Order


DEFAULT_CAMPAIGN_LIMIT = 8
MIN_PHASE_ORDERS = 3
PHASE_DEFINITIONS = (
    ('opening', '前段'),
    ('middle', '中段'),
    ('closing', '后段'),
)


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _per_100_days(count, days):
    return round(count / days * 100, 2) if days else None


def _phase_windows(campaign):
    """Return three contiguous half-open windows for one campaign."""
    if not campaign.ends_at or campaign.ends_at <= campaign.starts_at:
        return []
    duration = campaign.ends_at - campaign.starts_at
    one_third = duration / 3
    boundaries = (
        campaign.starts_at,
        campaign.starts_at + one_third,
        campaign.starts_at + one_third * 2,
        campaign.ends_at,
    )
    windows = []
    for index, (key, label) in enumerate(PHASE_DEFINITIONS):
        windows.append({
            'key': key,
            'label': label,
            'starts_at': boundaries[index],
            'ends_at': boundaries[index + 1],
        })
    return windows


def _phase_for_occurrence(phase_maps, campaign_id, occurred_at):
    """Return the phase containing an event, never both neighbouring phases."""
    for phase in phase_maps.get(campaign_id, {}).values():
        if phase['starts_at'] <= occurred_at < phase['ends_at']:
            return phase
    return None


def _empty_stats():
    return {
        'new_items': 0,
        'views': 0,
        'favorites': 0,
        'orders': 0,
        'completed_orders': 0,
    }


def _finalise_phase(phase):
    days = (phase['ends_at'] - phase['starts_at']).total_seconds() / 86400
    phase['days'] = round(days, 1)
    phase['view_to_order_rate'] = _rate(phase['orders'], phase['views'])
    phase['completion_rate'] = _rate(
        phase['completed_orders'], phase['orders'],
    )
    phase['orders_per_100_days'] = _per_100_days(phase['orders'], days)
    phase['views_per_100_days'] = _per_100_days(phase['views'], days)
    phase['new_items_per_100_days'] = _per_100_days(phase['new_items'], days)
    phase['has_data'] = any(
        phase[key]
        for key in ('new_items', 'views', 'favorites', 'orders', 'completed_orders')
    )
    phase['has_order_sample'] = phase['orders'] >= MIN_PHASE_ORDERS
    phase['is_order_peak'] = False
    return phase


def _campaign_rhythm(phases):
    if not all(phase['has_order_sample'] for phase in phases):
        return None, '样本不足，只报数字', 'muted'
    intensities = [phase['orders_per_100_days'] for phase in phases]
    peak = max(intensities)
    peak_phases = [
        phase for phase in phases if phase['orders_per_100_days'] == peak
    ]
    if len(peak_phases) != 1:
        return None, '预约节奏均衡', 'neutral'
    peak_phase = peak_phases[0]
    peak_phase['is_order_peak'] = True
    return (
        peak_phase['key'],
        f"{peak_phase['label']}预约强度最高",
        'info',
    )


def _cross_campaign_summary(rows):
    if len(rows) < 2:
        return {
            'has_conclusion': False,
            'eligible_campaign_count': 0,
            'peak_counts': [],
            'conclusion': '已结束专题少于 2 个，本轮不输出跨专题总结。',
        }
    eligible_rows = [
        row for row in rows if row['order_peak_key'] and row['has_order_sample']
    ]
    if len(eligible_rows) < 2:
        return {
            'has_conclusion': False,
            'eligible_campaign_count': len(eligible_rows),
            'peak_counts': [],
            'conclusion': '样本足够且能判出预约高峰的专题少于 2 个，本轮不输出跨专题总结。',
        }
    counter = Counter(row['order_peak_key'] for row in eligible_rows)
    labels = dict(PHASE_DEFINITIONS)
    peak_counts = [
        {'key': key, 'label': labels[key], 'count': counter.get(key, 0)}
        for key, _ in PHASE_DEFINITIONS
    ]
    detail = '、'.join(
        f"{item['label']} {item['count']} 个" for item in peak_counts
    )
    return {
        'has_conclusion': True,
        'eligible_campaign_count': len(eligible_rows),
        'peak_counts': peak_counts,
        'conclusion': (
            f"{len(eligible_rows)} 个专题样本足够并判出预约高峰：{detail}。"
            '这里只统计高峰位置，不做专题排名。'
        ),
    }


def build_campaign_phase_review(days=30, *, now=None, limit=DEFAULT_CAMPAIGN_LIMIT):
    """Build phase-level review rows for the most recently closed campaigns.

    ``days`` is accepted because every shared dashboard builder receives the
    dashboard period. It is not used here; the campaign's own window is the
    reporting period.
    """
    now = now or timezone.now()
    try:
        limit = max(int(limit), 1)
    except (TypeError, ValueError):
        limit = DEFAULT_CAMPAIGN_LIMIT

    closed = CampusCampaign.objects.filter(
        ends_at__isnull=False,
        ends_at__lte=now,
    )
    closed_count = closed.count()
    campaigns = list(
        closed.order_by('-ends_at', '-starts_at', 'title')[:limit],
    )
    open_count = CampusCampaign.objects.filter(
        starts_at__lte=now,
    ).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
    ).count()

    valid_campaigns = []
    phase_maps = {}
    rows = []
    invalid_campaign_count = 0
    for campaign in campaigns:
        windows = _phase_windows(campaign)
        if len(windows) != len(PHASE_DEFINITIONS):
            invalid_campaign_count += 1
            continue
        phases = []
        for window in windows:
            phase = {
                **window,
                **_empty_stats(),
            }
            phases.append(phase)
        valid_campaigns.append(campaign)
        phase_maps[campaign.id] = {
            phase['key']: phase for phase in phases
        }
        rows.append({
            'id': campaign.id,
            'title': campaign.title,
            'slug': campaign.slug,
            'starts_at': campaign.starts_at,
            'ends_at': campaign.ends_at,
            'duration_days': round(
                (campaign.ends_at - campaign.starts_at).total_seconds() / 86400,
                1,
            ),
            'phases': phases,
        })

    campaign_ids = [campaign.id for campaign in valid_campaigns]
    if campaign_ids:
        first_start = min(campaign.starts_at for campaign in valid_campaigns)
        last_end = max(campaign.ends_at for campaign in valid_campaigns)
        for campaign_id, created_at in Item.objects.filter(
            campaign_id__in=campaign_ids,
            created_at__gte=first_start,
            created_at__lt=last_end,
        ).values_list('campaign_id', 'created_at'):
            phase = _phase_for_occurrence(phase_maps, campaign_id, created_at)
            if phase is not None:
                phase['new_items'] += 1

        for campaign_id, viewed_at in BrowsingHistory.objects.filter(
            item__campaign_id__in=campaign_ids,
            last_viewed_at__gte=first_start,
            last_viewed_at__lt=last_end,
        ).values_list('item__campaign_id', 'last_viewed_at'):
            phase = _phase_for_occurrence(phase_maps, campaign_id, viewed_at)
            if phase is not None:
                phase['views'] += 1

        for campaign_id, created_at in Favorite.objects.filter(
            item__campaign_id__in=campaign_ids,
            created_at__gte=first_start,
            created_at__lt=last_end,
        ).values_list('item__campaign_id', 'created_at'):
            phase = _phase_for_occurrence(phase_maps, campaign_id, created_at)
            if phase is not None:
                phase['favorites'] += 1

        for campaign_id, created_at, status in Order.objects.filter(
            item__campaign_id__in=campaign_ids,
            created_at__gte=first_start,
            created_at__lt=last_end,
        ).values_list('item__campaign_id', 'created_at', 'status'):
            phase = _phase_for_occurrence(phase_maps, campaign_id, created_at)
            if phase is not None:
                phase['orders'] += 1
                if status == 'completed':
                    phase['completed_orders'] += 1

    for row in rows:
        for phase in row['phases']:
            _finalise_phase(phase)
        peak_key, rhythm_label, rhythm_tone = _campaign_rhythm(row['phases'])
        row['order_peak_key'] = peak_key
        row['has_order_sample'] = all(
            phase['has_order_sample'] for phase in row['phases']
        )
        row['rhythm_label'] = rhythm_label
        row['rhythm_tone'] = rhythm_tone

    totals = {
        key: sum(
            phase[key]
            for row in rows
            for phase in row['phases']
        )
        for key in ('new_items', 'views', 'favorites', 'orders', 'completed_orders')
    }
    return {
        'rows': rows,
        'summary': {
            'closed_campaign_count': closed_count,
            'reviewed_campaign_count': len(rows),
            'open_campaign_count': open_count,
            'invalid_campaign_count': invalid_campaign_count,
            'phase_count': len(rows) * len(PHASE_DEFINITIONS),
            'order_sample_campaign_count': sum(
                1 for row in rows if row['has_order_sample']
            ),
            **totals,
        },
        'cross_campaign': _cross_campaign_summary(rows),
        'limit': limit,
        'dashboard_days': days,
        'is_truncated': closed_count > len(campaigns),
    }

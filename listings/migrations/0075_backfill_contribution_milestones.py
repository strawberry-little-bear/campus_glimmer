# -*- coding: utf-8 -*-
"""Backfill first contribution milestones from the auditable point ledger."""

from django.db import migrations

from listings.contributions import calculate_contribution_milestones


# Freeze the rules used at backfill time.  Future product changes must not make
# a fresh deployment reconstruct a different historical milestone.
LEVELS = (
    (10, '热心参与者'),
    (30, '可靠交易伙伴'),
    (80, '校园互助达人'),
    (160, '循环倡导者'),
)
BADGE_RULES = (
    {'code': 'first_contribution', 'title': '迈出第一步', 'threshold': 1, 'metric': 'events'},
    {'code': 'gift_giver', 'title': '分享闲置', 'threshold': 1, 'metric': 'kind', 'kind': 'gift_completed'},
    {'code': 'borrow_keeper', 'title': '借用有始有终', 'threshold': 1, 'metric': 'kind', 'kind': 'borrow_returned'},
    {'code': 'demand_helper', 'title': '需求响应者', 'threshold': 1, 'metric': 'kind', 'kind': 'demand_helped'},
    {'code': 'lost_found_helper', 'title': '失物线索员', 'threshold': 1, 'metric': 'kind', 'kind': 'lost_found_help'},
    {'code': 'closure_builder', 'title': '互助有始有终', 'threshold': 1, 'metric': 'kind', 'kind': 'mutual_aid_completed'},
    {'code': 'community_builder', 'title': '社区循环倡导者', 'threshold': 5, 'metric': 'events'},
)


def backfill(apps, schema_editor):
    Contribution = apps.get_model('listings', 'CommunityContribution')
    Milestone = apps.get_model('listings', 'ContributionMilestoneEvent')
    user_ids = Contribution.objects.order_by('user_id').values_list('user_id', flat=True).distinct()

    for user_id in user_ids:
        points = 0
        events = 0
        kind_counts = {}
        rows = []
        contributions = Contribution.objects.filter(user_id=user_id).order_by(
            'occurred_at', 'id',
        )
        for contribution in contributions:
            milestone_rows = calculate_contribution_milestones(
                contribution,
                previous_points=points,
                previous_events=events,
                previous_kind_counts=kind_counts,
                levels=LEVELS,
                badge_rules=BADGE_RULES,
            )
            rows.extend(Milestone(**{**row, 'backfilled': True}) for row in milestone_rows)
            points += contribution.points
            events += 1
            kind_counts[contribution.kind] = kind_counts.get(contribution.kind, 0) + 1

        if rows:
            Milestone.objects.bulk_create(rows, batch_size=100, ignore_conflicts=True)


def rollback(apps, schema_editor):
    apps.get_model('listings', 'ContributionMilestoneEvent').objects.filter(backfilled=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0075_contributionmilestoneevent'),
    ]

    operations = [
        migrations.RunPython(backfill, rollback),
    ]

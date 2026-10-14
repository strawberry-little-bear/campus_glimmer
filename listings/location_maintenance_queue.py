# -*- coding: utf-8 -*-
"""校园地点维护队列的只读分诊。

地点档案质量面板回答“哪里有信号”；这个模块把同一批信号排成维护顺序。
它不做加权评分，也不自动修复，只按固定优先级说明先核对什么。维护决策由独立的
``CampusLocationMaintenanceDecision`` 留痕，本模块只读它，不写任何决策。
"""

from collections import defaultdict

from django.db.models import Count, Q
from django.utils import timezone

from .analytics import PERIOD_CHOICES
from .location_registry_quality import normalise_location_name
from .models import (
    CampusLocation, CampusLocationMaintenanceDecision, DemandPost, Item,
    SavedSearch,
)

LIST_LIMIT = 20
PRIORITY_SPECS = (
    ('check_first', '优先核对', '影响当前选择、匹配或安全提示'),
    ('disambiguate', '需要分辨', '多个启用地点归一化后同名'),
    ('enrich', '择期补充', '档案字段不完整，但不一定阻断当前使用'),
)
REASON_SPECS = (
    ('stale_reference', '停用仍被当前引用'),
    ('public_safety_gap', '公共地点缺安全提示'),
    ('duplicate_name', '近似重名'),
    ('profile_gap', '档案字段缺失'),
)
OUTCOME_SPECS = (
    ('handled', '已处理'),
    ('accepted', '维持现状'),
    ('deferred', '暂缓处理'),
)
REASON_PRIORITY = {
    'stale_reference': 'check_first',
    'public_safety_gap': 'check_first',
    'duplicate_name': 'disambiguate',
    'profile_gap': 'enrich',
}
ACTION_HINTS = {
    'stale_reference': '核对旧记录应迁移、地点应恢复启用，还是保持停用。',
    'public_safety_gap': '补充公共地点安全提示，或确认它确实无需提示。',
    'duplicate_name': '核对多个地点是否指同一处；不判断哪一个是“正版”。',
    'profile_gap': '补齐配置字段；不改商品、求购或关注搜索记录。',
}
FIELD_LABELS = (
    ('building', '楼栋 / 校区'),
    ('address', '详细位置'),
    ('description', '地点说明'),
    ('safety_note', '安全提示'),
)
PRIORITY_LABELS = {key: label for key, label, _ in PRIORITY_SPECS}
REASON_LABELS = dict(REASON_SPECS)
OUTCOME_LABELS = dict(OUTCOME_SPECS)
PRIORITY_INDEX = {key: index for index, (key, _, _) in enumerate(PRIORITY_SPECS)}


def _count_by_location(queryset):
    counts = defaultdict(int)
    rows = queryset.values('location').annotate(total=Count('id'))
    for row in rows:
        counts[row['location']] = row['total']
    return counts


def signal_fingerprint(reason_keys):
    """Build the stable identity of one location's current signal combination."""
    return '|'.join(sorted(reason_keys))


def _collect_location_entries(now):
    locations = list(CampusLocation.objects.order_by('sort_order', 'name'))
    active_locations = [location for location in locations if location.is_active]

    available_items = Item.objects.filter(
        status='available',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    active_demands = DemandPost.objects.filter(
        status='active',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    active_saved_searches = SavedSearch.objects.filter(is_active=True)
    item_counts = _count_by_location(available_items)
    demand_counts = _count_by_location(active_demands)
    saved_search_counts = _count_by_location(active_saved_searches)

    duplicate_groups = defaultdict(list)
    for location in active_locations:
        duplicate_groups[normalise_location_name(location.name)].append(location)
    duplicate_sizes = {}
    for members in duplicate_groups.values():
        for location in members:
            duplicate_sizes[location.id] = len(members)

    entries = []
    for location in locations:
        item_count = item_counts.get(location.id, 0)
        demand_count = demand_counts.get(location.id, 0)
        saved_search_count = saved_search_counts.get(location.id, 0)
        current_use_total = item_count + demand_count + saved_search_count
        reasons = []
        missing_fields = []

        if not location.is_active:
            if current_use_total:
                reasons.append('stale_reference')
        else:
            if location.is_public and not location.safety_note:
                reasons.append('public_safety_gap')

            if duplicate_sizes[location.id] >= 2:
                reasons.append('duplicate_name')

            for field, label in FIELD_LABELS:
                is_public_safety = field == 'safety_note' and location.is_public
                if not is_public_safety and not getattr(location, field):
                    missing_fields.append({'field': field, 'label': label})
            if missing_fields:
                reasons.append('profile_gap')

        if not reasons:
            continue

        primary_reason = reasons[0]
        priority = REASON_PRIORITY[primary_reason]
        entries.append({
            'id': location.id,
            'name': location.name,
            'building': location.building,
            'address': location.address,
            'is_active': location.is_active,
            'is_public': location.is_public,
            'available_item_count': item_count,
            'active_demand_count': demand_count,
            'active_saved_search_count': saved_search_count,
            'current_use_total': current_use_total,
            'reason_keys': reasons,
            'reason_labels': [
                label for key, label in REASON_SPECS if key in reasons
            ],
            'primary_reason': primary_reason,
            'primary_reason_label': REASON_LABELS[primary_reason],
            'priority': priority,
            'priority_label': PRIORITY_LABELS[priority],
            'missing_fields': missing_fields,
            'duplicate_group_size': duplicate_sizes.get(location.id, 0),
            'action_hint': ACTION_HINTS[primary_reason],
            'signal_fingerprint': signal_fingerprint(reasons),
        })

    return locations, active_locations, entries


def get_location_maintenance_fingerprint(location_id, *, now=None):
    """Return the current queue fingerprint for one location, or ``None``."""
    now = now or timezone.now()
    _, _, entries = _collect_location_entries(now)
    for entry in entries:
        if entry['id'] == location_id:
            return entry['signal_fingerprint']
    return None


def _decision_map(entries):
    if not entries:
        return {}

    decisions = CampusLocationMaintenanceDecision.objects.filter(
        location_id__in=[entry['id'] for entry in entries],
    ).select_related('location')
    return {
        (decision.location_id, decision.signal_fingerprint): decision
        for decision in decisions
    }


def build_location_maintenance_queue(days=30, *, now=None):
    """Build a read-only maintenance queue with optional decision history."""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    days = days if days in allowed_days else 30
    now = now or timezone.now()

    locations, active_locations, entries = _collect_location_entries(now)
    decisions = _decision_map(entries)
    outcome_counts = {key: 0 for key, _ in OUTCOME_SPECS}

    for entry in entries:
        decision = decisions.get(
            (entry['id'], entry['signal_fingerprint'])
        )
        if decision is None:
            entry['review_status'] = 'unreviewed'
            entry['review_status_label'] = '待核对'
            entry['review_outcome'] = None
            entry['review_outcome_label'] = None
            entry['decision_updated_at'] = None
            entry['reviewed'] = False
            continue

        entry['review_status'] = 'reviewed'
        entry['review_status_label'] = '已复核'
        entry['review_outcome'] = decision.outcome
        entry['review_outcome_label'] = OUTCOME_LABELS[decision.outcome]
        entry['decision_updated_at'] = decision.updated_at
        entry['reviewed'] = True
        outcome_counts[decision.outcome] += 1

    entries.sort(key=lambda row: (
        PRIORITY_INDEX[row['priority']],
        row['reviewed'],
        -row['current_use_total'],
        -len(row['reason_keys']),
        row['name'],
    ))

    priority_counts = {key: 0 for key, _, _ in PRIORITY_SPECS}
    reason_counts = {key: 0 for key, _ in REASON_SPECS}
    for entry in entries:
        priority_counts[entry['priority']] += 1
        for reason in entry['reason_keys']:
            reason_counts[reason] += 1

    queued_ids = {entry['id'] for entry in entries}
    active_clean_total = sum(
        location.is_active and location.id not in queued_ids
        for location in locations
    )
    inactive_clean_total = sum(
        not location.is_active and location.id not in queued_ids
        for location in locations
    )
    reviewed_total = sum(outcome_counts.values())

    priority_rows = [{
        'key': key,
        'label': label,
        'description': description,
        'location_total': priority_counts[key],
    } for key, label, description in PRIORITY_SPECS]
    reason_rows = [{
        'key': key,
        'label': label,
        'location_total': reason_counts[key],
    } for key, label in REASON_SPECS]

    return {
        'has_data': bool(locations),
        'summary': {
            'location_total': len(locations),
            'active_total': len(active_locations),
            'queued_total': len(entries),
            'unreviewed_total': len(entries) - reviewed_total,
            'reviewed_total': reviewed_total,
            'outcome_totals': outcome_counts,
            'active_clean_total': active_clean_total,
            'inactive_clean_total': inactive_clean_total,
            'priority_totals': priority_counts,
            'signal_totals': reason_counts,
        },
        'priority_rows': priority_rows,
        'reason_rows': reason_rows,
        'location_rows': entries[:LIST_LIMIT],
        'location_row_total': len(entries),
        'location_row_limit': LIST_LIMIT,
        'location_row_hidden_total': max(0, len(entries) - LIST_LIMIT),
        'period_days': days,
    }

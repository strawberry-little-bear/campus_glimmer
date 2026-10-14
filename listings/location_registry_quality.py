# -*- coding: utf-8 -*-
"""校园地点档案与覆盖质量的只读观测。

地点在站内同时承担筛选条件、求购匹配条件、交付安排和安全提示四类职责。
这个模块不评价供需，也不推断用户行为，只回答档案本身：启用形状、字段覆盖、
当前业务是否仍能解释地点，以及近似重名和停用地点引用的规模。
"""

import unicodedata
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .analytics import PERIOD_CHOICES
from .models import CampusLocation, DemandPost, Item, MeetingAppointment, Order, SavedSearch

LIST_LIMIT = 20
DELIVERY_SOURCES = (
    ('order', '订单地点'),
    ('appointment', '交付安排地点'),
    ('item', '商品地点'),
    ('none', '无可解释地点'),
)
FIELD_SPECS = (
    ('building', '楼栋 / 校区'),
    ('address', '详细位置'),
    ('description', '地点说明'),
)
SAFETY_FIELD_SPECS = (
    (True, '安全提示（公共地点）'),
    (False, '安全提示（非公共地点）'),
)


def normalise_location_name(value):
    """Fold Unicode, case and whitespace without changing the stored name."""
    normalised = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(normalised.split())


def _share(numerator, denominator):
    """Return None rather than a fake 0% when a denominator does not exist."""
    if not denominator:
        return None
    return round(numerator / denominator * 100, 1)


def _count_by_location(queryset):
    counts = defaultdict(int)
    rows = queryset.values('location').annotate(total=Count('id'))
    for row in rows:
        counts[row['location']] = row['total']
    return counts


def build_location_registry_quality(days=30, *, now=None):
    """Build a read-only location registry quality snapshot for operations."""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    days = days if days in allowed_days else 30
    now = now or timezone.now()
    start = now - timedelta(days=days - 1)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    locations = list(CampusLocation.objects.order_by('sort_order', 'name'))
    active_locations = [location for location in locations if location.is_active]
    location_by_id = {location.id: location for location in locations}

    total = len(locations)
    active_total = len(active_locations)
    inactive_total = total - active_total
    active_public_total = sum(location.is_public for location in active_locations)
    active_private_total = active_total - active_public_total
    inactive_public_total = sum(
        location.is_public for location in locations if not location.is_active
    )

    available_items = Item.objects.filter(
        status='available',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    available_item_counts = _count_by_location(available_items)
    available_item_total = sum(available_item_counts.values())
    available_item_with_location = available_item_total - available_item_counts.get(None, 0)

    active_demands = DemandPost.objects.filter(
        status='active',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    active_demand_counts = _count_by_location(active_demands)
    active_demand_total = sum(active_demand_counts.values())
    active_demand_with_location = active_demand_total - active_demand_counts.get(None, 0)

    active_saved_searches = SavedSearch.objects.filter(is_active=True)
    saved_search_counts = _count_by_location(active_saved_searches)
    saved_search_total = sum(saved_search_counts.values())
    saved_search_without_location = saved_search_counts.get(None, 0)
    saved_search_active_location = sum(
        count for location_id, count in saved_search_counts.items()
        if location_id is not None and location_by_id[location_id].is_active
    )
    saved_search_inactive_location = (
        saved_search_total
        - saved_search_without_location
        - saved_search_active_location
    )

    period_orders = Order.objects.filter(
        created_at__gte=start,
        created_at__lte=now,
    ).select_related('item', 'appointment')
    delivery_counts = {source: 0 for source, _ in DELIVERY_SOURCES}
    order_total = 0
    for order in period_orders:
        order_total += 1
        appointment = getattr(order, 'appointment', None)
        if order.meeting_location_id:
            source = 'order'
        elif appointment is not None and appointment.location_id:
            source = 'appointment'
        elif order.item.location_id:
            source = 'item'
        else:
            source = 'none'
        delivery_counts[source] += 1

    delivery_rows = [{
        'source': source,
        'label': label,
        'count': delivery_counts[source],
        'share': _share(delivery_counts[source], order_total),
    } for source, label in DELIVERY_SOURCES]

    field_rows = []
    for field, label in FIELD_SPECS:
        filled = sum(bool(getattr(location, field)) for location in active_locations)
        field_rows.append({
            'field': field,
            'scope': '启用地点',
            'label': label,
            'filled': filled,
            'total': active_total,
            'missing': active_total - filled,
            'share': _share(filled, active_total),
        })
    for is_public, label in SAFETY_FIELD_SPECS:
        scoped = [
            location for location in active_locations
            if location.is_public is is_public
        ]
        filled = sum(bool(location.safety_note) for location in scoped)
        field_rows.append({
            'field': 'safety_note',
            'scope': '公共地点' if is_public else '非公共地点',
            'label': label,
            'filled': filled,
            'total': len(scoped),
            'missing': len(scoped) - filled,
            'share': _share(filled, len(scoped)),
        })

    location_rows = [{
        'name': location.name,
        'building': location.building,
        'address': location.address,
        'description': location.description,
        'is_public': location.is_public,
        'safety_note': location.safety_note,
        'available_item_count': available_item_counts.get(location.id, 0),
        'active_demand_count': active_demand_counts.get(location.id, 0),
        'active_saved_search_count': saved_search_counts.get(location.id, 0),
    } for location in active_locations]
    location_rows.sort(key=lambda row: (
        -row['available_item_count'],
        -row['active_demand_count'],
        row['name'],
    ))

    duplicate_groups = defaultdict(list)
    for location in active_locations:
        duplicate_groups[normalise_location_name(location.name)].append({
            'name': location.name,
            'building': location.building,
            'address': location.address,
            'available_item_count': available_item_counts.get(location.id, 0),
        })
    duplicate_name_groups = []
    for normalised_name, members in duplicate_groups.items():
        if len(members) < 2:
            continue
        duplicate_name_groups.append({
            'normalized_name': normalised_name,
            'location_count': len(members),
            'locations': sorted(members, key=lambda row: row['name']),
        })
    duplicate_name_groups.sort(
        key=lambda group: (-group['location_count'], group['normalized_name']),
    )

    stale_reference_rows = []
    for location in locations:
        if location.is_active:
            continue
        available_count = available_item_counts.get(location.id, 0)
        demand_count = active_demand_counts.get(location.id, 0)
        saved_count = saved_search_counts.get(location.id, 0)
        reference_total = available_count + demand_count + saved_count
        if not reference_total:
            continue
        stale_reference_rows.append({
            'name': location.name,
            'building': location.building,
            'address': location.address,
            'is_public': location.is_public,
            'available_item_count': available_count,
            'active_demand_count': demand_count,
            'active_saved_search_count': saved_count,
            'reference_total': reference_total,
        })
    stale_reference_rows.sort(key=lambda row: (
        -row['reference_total'], -row['active_saved_search_count'], row['name'],
    ))

    stale_reference_total = len(stale_reference_rows)
    duplicate_group_total = len(duplicate_name_groups)
    has_data = any((
        total,
        available_item_total,
        active_demand_total,
        saved_search_total,
        order_total,
    ))

    return {
        'has_data': has_data,
        'summary': {
            'location_total': total,
            'active_total': active_total,
            'inactive_total': inactive_total,
            'active_public_total': active_public_total,
            'active_private_total': active_private_total,
            'inactive_public_total': inactive_public_total,
        },
        'field_rows': field_rows,
        'coverage': {
            'item_total': available_item_total,
            'item_with_location': available_item_with_location,
            'item_without_location': available_item_total - available_item_with_location,
            'item_with_location_share': _share(
                available_item_with_location, available_item_total,
            ),
            'demand_total': active_demand_total,
            'demand_with_location': active_demand_with_location,
            'demand_without_location': active_demand_total - active_demand_with_location,
            'demand_with_location_share': _share(
                active_demand_with_location, active_demand_total,
            ),
            'saved_search_total': saved_search_total,
            'saved_search_active_location': saved_search_active_location,
            'saved_search_inactive_location': saved_search_inactive_location,
            'saved_search_without_location': saved_search_without_location,
            'saved_search_active_location_share': _share(
                saved_search_active_location, saved_search_total,
            ),
        },
        'delivery_rows': delivery_rows,
        'order_total': order_total,
        'location_rows': location_rows[:LIST_LIMIT],
        'location_row_total': len(location_rows),
        'location_row_limit': LIST_LIMIT,
        'location_row_hidden_total': max(0, len(location_rows) - LIST_LIMIT),
        'duplicate_name_groups': duplicate_name_groups[:LIST_LIMIT],
        'duplicate_group_total': duplicate_group_total,
        'duplicate_group_limit': LIST_LIMIT,
        'duplicate_group_hidden_total': max(0, duplicate_group_total - LIST_LIMIT),
        'stale_reference_rows': stale_reference_rows[:LIST_LIMIT],
        'stale_reference_row_total': stale_reference_total,
        'stale_reference_row_limit': LIST_LIMIT,
        'stale_reference_row_hidden_total': max(
            0, stale_reference_total - LIST_LIMIT,
        ),
        'period_days': days,
    }

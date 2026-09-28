from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import CampusLocation


def build_campus_pulse(days=30, *, now=None):
    """Build a privacy-preserving supply/demand pulse for campus locations.

    The result is aggregated at configured campus locations. It never uses a
    user's live position; demand pressure is inferred from public listing,
    purchase-demand and appointment records for the selected period.
    """
    now = now or timezone.now()
    start = now - timedelta(days=days)
    available_filter = (
        Q(items__status='available')
        & (Q(items__expires_at__isnull=True) | Q(items__expires_at__gt=now))
    )
    locations = CampusLocation.objects.filter(is_active=True).annotate(
        available_supply=Count('items', filter=available_filter, distinct=True),
        new_supply=Count(
            'items',
            filter=Q(items__created_at__gte=start, items__created_at__lte=now),
            distinct=True,
        ),
        active_demands=Count(
            'demand_posts',
            filter=(
                Q(demand_posts__status='active')
                & (
                    Q(demand_posts__expires_at__isnull=True)
                    | Q(demand_posts__expires_at__gt=now)
                )
            ),
            distinct=True,
        ),
        period_orders=Count(
            'orders',
            filter=Q(orders__created_at__gte=start, orders__created_at__lte=now),
            distinct=True,
        ),
        completed_orders=Count(
            'orders',
            filter=Q(
                orders__status='completed',
                orders__created_at__gte=start,
                orders__created_at__lte=now,
            ),
            distinct=True,
        ),
    )

    rows = []
    for location in locations:
        supply = location.available_supply
        demand = location.active_demands
        activity_score = location.new_supply + location.period_orders * 2 + demand * 2
        pressure_ratio = round(demand / max(supply + demand, 1) * 100)
        if demand and not supply:
            pressure, pressure_label = 'high', '需求紧张'
        elif pressure_ratio >= 60:
            pressure, pressure_label = 'high', '需求偏高'
        elif supply >= demand * 2 and supply:
            pressure, pressure_label = 'low', '供给较充足'
        else:
            pressure, pressure_label = 'balanced', '供需相对平衡'
        if activity_score or supply or demand:
            rows.append({
                'name': location.name,
                'building': location.building,
                'is_public': location.is_public,
                'available_supply': supply,
                'new_supply': location.new_supply,
                'active_demands': demand,
                'period_orders': location.period_orders,
                'completed_orders': location.completed_orders,
                'activity_score': activity_score,
                'pressure_ratio': pressure_ratio,
                'pressure': pressure,
                'pressure_label': pressure_label,
            })

    rows.sort(key=lambda row: (-row['activity_score'], -row['active_demands'], row['name']))
    max_activity = max((row['activity_score'] for row in rows), default=0)
    for row in rows:
        row['heat_percent'] = round(row['activity_score'] / max_activity * 100) if max_activity else 0

    return {
        'rows': rows[:10],
        'summary': {
            'location_count': len(rows),
            'high_pressure_count': sum(row['pressure'] == 'high' for row in rows),
            'available_supply': sum(row['available_supply'] for row in rows),
            'active_demands': sum(row['active_demands'] for row in rows),
            'period_orders': sum(row['period_orders'] for row in rows),
        },
        'period_days': days,
    }

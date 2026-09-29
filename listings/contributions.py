from django.db.models import Count, Sum

from .models import CommunityContribution


CONTRIBUTION_DEFAULTS = {
    'trade_completed': {
        'seller': (10, '完成一笔交易', '完成商品交付并由双方确认。'),
        'buyer': (6, '完成一笔交易', '完成商品交付并由双方确认。'),
    },
    'borrow_returned': {
        'seller': (12, '完成一次借用归还', '借用品已由双方完成归还确认。'),
        'buyer': (10, '完成一次借用归还', '按流程完成借用品归还确认。'),
    },
}

CONTRIBUTION_LEVELS = (
    (0, '新加入校园社区'),
    (10, '热心参与者'),
    (30, '可靠交易伙伴'),
    (80, '校园互助达人'),
    (160, '循环倡导者'),
)


def record_contribution(*, user, kind, points, title, description='', source_key, occurred_at=None):
    'Create one contribution record; source_key makes the award idempotent.'
    defaults = {
        'user': user,
        'kind': kind,
        'points': points,
        'title': title,
        'description': description,
    }
    if occurred_at is not None:
        defaults['occurred_at'] = occurred_at
    contribution, created = CommunityContribution.objects.get_or_create(
        source_key=source_key,
        defaults=defaults,
    )
    return contribution, created


def record_order_contribution(order, *, phase):
    'Award both parties once when a sale completes or a borrowing cycle returns.'
    if phase not in CONTRIBUTION_DEFAULTS:
        raise ValueError(f'不支持的贡献阶段：{phase}')

    defaults = CONTRIBUTION_DEFAULTS[phase]
    for role, user in (('seller', order.seller), ('buyer', order.buyer)):
        points, title, description = defaults[role]
        record_contribution(
            user=user,
            kind=phase,
            points=points,
            title=title,
            description=description,
            source_key=f'order:{order.pk}:{phase}:{role}',
            occurred_at=order.updated_at,
        )


def _level_for_points(points):
    current = CONTRIBUTION_LEVELS[0]
    next_level = None
    for index, level in enumerate(CONTRIBUTION_LEVELS):
        if points >= level[0]:
            current = level
            next_level = CONTRIBUTION_LEVELS[index + 1] if index + 1 < len(CONTRIBUTION_LEVELS) else None
        else:
            break
    return current, next_level


def build_contribution_summary(user):
    'Return a privacy-safe, explainable contribution summary for profile pages.'
    queryset = CommunityContribution.objects.filter(user=user)
    aggregate = queryset.aggregate(total_points=Sum('points'), total_events=Count('id'))
    total_points = aggregate['total_points'] or 0
    total_events = aggregate['total_events'] or 0
    current_level, next_level = _level_for_points(total_points)

    if next_level:
        start_points = current_level[0]
        span = next_level[0] - start_points
        progress = round((total_points - start_points) * 100 / span)
        progress = min(max(progress, 0), 100)
        remaining_points = max(next_level[0] - total_points, 0)
    else:
        progress = 100
        remaining_points = 0

    kind_counts = {
        row['kind']: row['count']
        for row in queryset.values('kind').annotate(count=Count('id'))
    }
    return {
        'total_points': total_points,
        'total_events': total_events,
        'level': current_level[1],
        'level_threshold': current_level[0],
        'next_level': next_level[1] if next_level else None,
        'next_level_threshold': next_level[0] if next_level else None,
        'remaining_points': remaining_points,
        'progress_percent': progress,
        'kind_counts': kind_counts,
        'recent_events': list(queryset[:5]),
    }

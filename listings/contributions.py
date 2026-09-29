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
    'gift_completed': {
        'seller': (8, '完成一次免费赠送', '将闲置物品交给有需要的同学，并完成双方交付确认。'),
        'buyer': (8, '完成一次免费领取', '按约完成免费物品领取，并完成双方交付确认。'),
    },
}

CONTRIBUTION_LEVELS = (
    (0, '新加入校园社区'),
    (10, '热心参与者'),
    (30, '可靠交易伙伴'),
    (80, '校园互助达人'),
    (160, '循环倡导者'),
)

CONTRIBUTION_BADGE_RULES = (
    {
        'code': 'first_contribution',
        'title': '迈出第一步',
        'description': '完成一次可验证的校园互助。',
        'icon': 'bi-footprints',
        'threshold': 1,
        'metric': 'events',
    },
    {
        'code': 'gift_giver',
        'title': '分享闲置',
        'description': '完成一次免费赠送，让物品继续流转。',
        'icon': 'bi-gift',
        'threshold': 1,
        'metric': 'kind',
        'kind': 'gift_completed',
    },
    {
        'code': 'borrow_keeper',
        'title': '借用有始有终',
        'description': '完成一次借用归还确认。',
        'icon': 'bi-arrow-repeat',
        'threshold': 1,
        'metric': 'kind',
        'kind': 'borrow_returned',
    },
    {
        'code': 'demand_helper',
        'title': '需求响应者',
        'description': '提供的商品响应被求购发布者确认。',
        'icon': 'bi-hand-thumbs-up',
        'threshold': 1,
        'metric': 'kind',
        'kind': 'demand_helped',
    },
    {
        'code': 'lost_found_helper',
        'title': '失物线索员',
        'description': '提交的失物招领线索被确认。',
        'icon': 'bi-search-heart',
        'threshold': 1,
        'metric': 'kind',
        'kind': 'lost_found_help',
    },
    {
        'code': 'closure_builder',
        'title': '互助有始有终',
        'description': '至少一次互助被发布者确认已经完成。',
        'icon': 'bi-check2-all',
        'threshold': 1,
        'metric': 'kind',
        'kind': 'mutual_aid_completed',
    },
    {
        'code': 'community_builder',
        'title': '社区循环倡导者',
        'description': '累计完成 5 次可验证校园互助。',
        'icon': 'bi-stars',
        'threshold': 5,
        'metric': 'events',
    },
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
    'Award both parties once when a sale, gift, or borrowing cycle completes.'
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


def record_demand_response_contribution(response):
    'Award the responder once when a campus demand response is accepted.'
    return record_contribution(
        user=response.responder,
        kind='demand_helped',
        points=8,
        title='响应校园求购',
        description=f'你提供的“{response.item.title}”已被求购发布者确认。',
        source_key=f'demand-response:{response.pk}:accepted',
        occurred_at=response.updated_at,
    )


def record_lost_found_lead_contribution(lead):
    'Award the lead author once when a lost-and-found clue is accepted.'
    return record_contribution(
        user=lead.respondent,
        kind='lost_found_help',
        points=12,
        title='协助确认失物线索',
        description=f'你提交的“{lead.post.title}”线索已被发布者确认。',
        source_key=f'lost-found-lead:{lead.pk}:accepted',
        occurred_at=lead.updated_at,
    )


def record_mutual_aid_feedback_contribution(feedback):
    """Award a small, idempotent bonus only when an interaction is completed."""
    if feedback.outcome != 'completed':
        return None, False
    return record_contribution(
        user=feedback.helper,
        kind='mutual_aid_completed',
        points=10,
        title='完成一次校园互助',
        description='互助发布者确认这次响应已经形成闭环。',
        source_key=f'mutual-aid-feedback:{feedback.pk}:completed',
        occurred_at=feedback.updated_at,
    )


def build_contribution_badges(queryset):
    'Return earned and next-step badge progress without exposing source records.'
    total_events = queryset.count()
    kind_counts = {
        row['kind']: row['count']
        for row in queryset.values('kind').annotate(count=Count('id'))
    }
    badges = []
    for rule in CONTRIBUTION_BADGE_RULES:
        current = kind_counts.get(rule['kind'], 0) if rule['metric'] == 'kind' else total_events
        threshold = rule['threshold']
        badges.append({
            **rule,
            'current': current,
            'remaining': max(threshold - current, 0),
            'earned': current >= threshold,
            'progress_percent': min(round(current * 100 / threshold), 100),
        })
    return badges


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
        'badges': build_contribution_badges(queryset),
        'recent_events': list(queryset[:5]),
    }

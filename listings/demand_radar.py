from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import DemandPost, Item, SearchQuery


def _normalize_term(value):
    return ' '.join((value or '').strip().lower().split())[:120]


def _display_label(term, category_name, location_name):
    if term:
        return term
    if category_name:
        return f'{category_name}类需求'
    if location_name:
        return f'{location_name}附近需求'
    return '未分类校园需求'


def _opportunity_key(term, category_id, location_id):
    return (_normalize_term(term), category_id or 0, location_id or 0)


def _new_signal(term='', category_id=None, category_name='', location_id=None, location_name=''):
    return {
        'term': _normalize_term(term),
        'category_id': category_id,
        'category_name': category_name or '',
        'location_id': location_id,
        'location_name': location_name or '',
        'search_count': 0,
        'search_users': set(),
        'demand_count': 0,
        'demand_users': set(),
        'last_activity': None,
    }


def _available_supply_count(signal, now):
    items = Item.objects.filter(status='available').filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    if signal['category_id']:
        items = items.filter(category_id=signal['category_id'])
    if signal['location_id']:
        items = items.filter(location_id=signal['location_id'])
    if signal['term']:
        items = items.filter(
            Q(title__icontains=signal['term'])
            | Q(description__icontains=signal['term'])
        )
    return items.count()


def _build_recommendations(signal, available_supply):
    recommendations = []
    if available_supply == 0:
        recommendations.append('补充相关供给')
    elif available_supply < max(2, signal['search_count'] + signal['demand_count']):
        recommendations.append('增加当前分类供给')
    if signal['demand_count']:
        recommendations.append('优先关注求购用户')
    if signal['location_name']:
        recommendations.append(f'关注“{signal["location_name"]}”附近')
    if signal['search_count'] >= 2:
        recommendations.append('完善搜索同义词')
    if signal['search_count'] + signal['demand_count'] >= 4:
        recommendations.append('考虑创建校园专题')
    return recommendations or ['持续观察需求变化']


def _build_row(signal, now):
    available_supply = _available_supply_count(signal, now)
    search_users = len(signal['search_users'])
    demand_users = len(signal['demand_users'])
    signal_count = signal['search_count'] + signal['demand_count']
    # 搜索无结果代表显性缺口，求购代表更强的主动意愿；供给越少，缺口加成越高。
    score = (
        signal['search_count'] * 2
        + search_users
        + signal['demand_count'] * 4
        + demand_users * 2
        + max(0, 3 - available_supply)
    )
    if score >= 12 or (signal['demand_count'] >= 2 and available_supply == 0):
        level, level_label = 'critical', '优先处理'
    elif score >= 6:
        level, level_label = 'high', '值得补给'
    else:
        level, level_label = 'medium', '持续观察'

    evidence = []
    if signal['search_count']:
        evidence.append(f'无结果搜索 {signal["search_count"]} 次')
    if search_users:
        evidence.append(f'{search_users} 位用户搜索')
    if signal['demand_count']:
        evidence.append(f'有效求购 {signal["demand_count"]} 条')
    evidence.append(f'当前匹配供给 {available_supply} 件')

    return {
        'label': _display_label(signal['term'], signal['category_name'], signal['location_name']),
        'term': signal['term'],
        'category_name': signal['category_name'],
        'location_name': signal['location_name'],
        'search_count': signal['search_count'],
        'unique_searchers': search_users,
        'demand_count': signal['demand_count'],
        'unique_requesters': demand_users,
        'available_supply': available_supply,
        'signal_count': signal_count,
        'opportunity_score': score,
        'level': level,
        'level_label': level_label,
        'evidence': evidence,
        'recommendations': _build_recommendations(signal, available_supply),
        'last_activity': signal['last_activity'],
    }


def build_demand_radar(days=30, *, now=None, limit=12):
    """Aggregate explicit campus demand into explainable replenishment opportunities.

    The radar intentionally keeps the result aggregated. It exposes counts,
    categories and locations to staff, but never returns user identities.
    """
    now = now or timezone.now()
    start = now - timedelta(days=days)
    signals = {}

    search_rows = SearchQuery.objects.filter(
        created_at__gte=start, created_at__lte=now, result_count=0,
    ).exclude(query='').values(
        'query', 'category_id', 'category__name', 'location_id', 'location__name',
        'user_id', 'created_at',
    )
    for row in search_rows:
        key = _opportunity_key(row['query'], row['category_id'], row['location_id'])
        signal = signals.setdefault(
            key, _new_signal(
                row['query'], row['category_id'], row['category__name'],
                row['location_id'], row['location__name'],
            ),
        )
        signal['search_count'] += 1
        if row['user_id']:
            signal['search_users'].add(row['user_id'])
        if not signal['last_activity'] or row['created_at'] > signal['last_activity']:
            signal['last_activity'] = row['created_at']

    demand_rows = DemandPost.objects.filter(
        status='active', created_at__gte=start, created_at__lte=now,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    ).values(
        'title', 'category_id', 'category__name', 'location_id', 'location__name',
        'requester_id', 'created_at',
    )
    for row in demand_rows:
        key = _opportunity_key(row['title'], row['category_id'], row['location_id'])
        signal = signals.setdefault(
            key, _new_signal(
                row['title'], row['category_id'], row['category__name'],
                row['location_id'], row['location__name'],
            ),
        )
        signal['demand_count'] += 1
        if row['requester_id']:
            signal['demand_users'].add(row['requester_id'])
        if not signal['last_activity'] or row['created_at'] > signal['last_activity']:
            signal['last_activity'] = row['created_at']

    # First reduce obviously low-signal rows, then perform the more expensive
    # supply lookup only for candidates that can appear on the dashboard.
    candidates = [signal for signal in signals.values() if signal['search_count'] or signal['demand_count']]
    candidates.sort(
        key=lambda signal: (
            -(signal['search_count'] * 2 + signal['demand_count'] * 4),
            -(signal['search_count'] + signal['demand_count']),
            signal['term'],
        ),
    )
    rows = [_build_row(signal, now) for signal in candidates[:max(limit * 4, limit)]]
    rows.sort(key=lambda row: (-row['opportunity_score'], -row['signal_count'], row['label']))
    max_score = max((row['opportunity_score'] for row in rows), default=0)
    for row in rows:
        row['score_percent'] = round(row['opportunity_score'] / max_score * 100) if max_score else 0

    visible_rows = rows[:limit]
    return {
        'rows': visible_rows,
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(visible_rows),
        'summary': {
            'opportunity_count': len(candidates),
            'visible_count': len(visible_rows),
            'critical_count': sum(row['level'] == 'critical' for row in visible_rows),
            'high_count': sum(row['level'] == 'high' for row in visible_rows),
            'search_gap_count': sum(row['search_count'] for row in visible_rows),
            'active_demand_count': sum(row['demand_count'] for row in visible_rows),
            'no_supply_count': sum(row['available_supply'] == 0 for row in visible_rows),
        },
    }

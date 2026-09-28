"""Match newly published items to active campus purchase demands."""

import re
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import DemandPost, Item, Notification
from .notifications import create_notification


_STOP_WORDS = {
    '求购', '求一个', '求一件', '求一台', '求一部', '需要', '想要', '希望',
    '最好', '可以', '能够', '附近', '同学', '闲置', '商品',
}


def _keyword_tokens(text):
    """Return small Chinese/Latin fragments useful for lightweight matching."""
    chunks = re.findall(r'[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}', (text or '').casefold())
    tokens = set()
    for chunk in chunks:
        if chunk in _STOP_WORDS:
            continue
        tokens.add(chunk)
        if all('\u4e00' <= char <= '\u9fff' for char in chunk) and len(chunk) > 3:
            for size in (2, 3, 4):
                tokens.update(chunk[index:index + size] for index in range(len(chunk) - size + 1))
    return {token for token in tokens if token not in _STOP_WORDS and len(token) >= 2}


def _match_demand(demand, item):
    score = 0
    reasons = []

    if demand.category_id:
        if demand.category_id != item.category_id:
            return None
        score += 4
        reasons.append('分类一致')
    if demand.location_id:
        if demand.location_id != item.location_id:
            return None
        score += 2
        reasons.append('地点一致')
    if demand.min_price is not None and item.price < demand.min_price:
        return None
    if demand.max_price is not None and item.price > demand.max_price:
        return None
    if demand.min_price is not None or demand.max_price is not None:
        score += 2
        reasons.append('价格符合预算')

    demand_tokens = _keyword_tokens(f'{demand.title} {demand.description}')
    item_text = ' '.join(
        value for value in (
            item.title, item.description, item.condition,
            item.category.name if item.category else '',
            item.location.name if item.location else '',
        ) if value
    ).casefold()
    keyword_matches = sorted(
        (token for token in demand_tokens if token in item_text),
        key=len, reverse=True,
    )
    if keyword_matches:
        score += min(3, len(keyword_matches))
        reasons.append(f'关键词相近（{keyword_matches[0]}）')

    has_structured_constraint = any((
        demand.category_id, demand.location_id,
        demand.min_price is not None, demand.max_price is not None,
    ))
    if not has_structured_constraint and not keyword_matches:
        return None
    if score < 2:
        return None
    return score, '、'.join(reasons) or '商品信息相近'


def notify_demand_matches(item):
    """Send one idempotent notification for every active demand matched by item."""
    item = Item.objects.select_related('category', 'location', 'seller').get(pk=item.pk)
    now = timezone.now()
    demands = DemandPost.objects.select_related('requester', 'category', 'location').filter(
        status='active',
    ).filter(
        expires_at__isnull=True,
    ) | DemandPost.objects.select_related('requester', 'category', 'location').filter(
        status='active', expires_at__gt=now,
    )
    demands = demands.exclude(requester_id=item.seller_id).distinct()

    matches = []
    for demand in demands:
        result = _match_demand(demand, item)
        if result:
            matches.append((demand, result[0], result[1]))

    notified_count = 0
    with transaction.atomic():
        for demand, score, reason in sorted(matches, key=lambda value: (-value[1], value[0].created_at)):
            dedupe_key = f'demand-match:{demand.pk}:{item.pk}'
            if Notification.objects.filter(
                recipient=demand.requester, kind='demand_match', dedupe_key=dedupe_key,
            ).exists():
                continue
            notification = create_notification(
                demand.requester,
                actor=item.seller,
                kind='demand_match',
                title='发现可能符合你求购需求的商品',
                message=f'“{item.title}”可能符合“{demand.title}”（{reason}），快去看看吧。',
                item=item,
                demand=demand,
                target_url=reverse('demand_detail', args=[demand.id]),
                dedupe_key=dedupe_key,
                dedupe_forever=True,
            )
            if notification:
                notified_count += 1
    return notified_count

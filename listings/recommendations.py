from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.db.models import Count
from django.utils import timezone

from .models import Favorite, Item, Order


@dataclass(frozen=True)
class Recommendation:
    item: Item
    reason: str
    score: float


class RecommendationService:
    """轻量级、可解释的校园商品推荐服务。"""

    def __init__(self, user=None):
        self.user = user or AnonymousUser()
        self.category_ids = set()
        self.location_ids = set()
        self._load_preferences()

    def _load_preferences(self):
        if not self.user.is_authenticated:
            return
        favorite_items = Favorite.objects.filter(user=self.user).select_related('item')
        self.category_ids.update(favorite_items.values_list('item__category_id', flat=True))
        self.location_ids.update(
            favorite_items.exclude(item__location_id=None).values_list('item__location_id', flat=True)
        )
        completed_orders = Order.objects.filter(
            buyer=self.user,
            status__in=['confirmed', 'meeting', 'completed'],
        ).select_related('item')
        self.category_ids.update(completed_orders.values_list('item__category_id', flat=True))
        self.location_ids.update(
            completed_orders.exclude(item__location_id=None).values_list('item__location_id', flat=True)
        )

    def get(self, limit=8, exclude_item_id=None):
        items = Item.objects.filter(status='available').annotate(
            favorite_count=Count('favorites'),
        ).select_related('category', 'seller', 'location').prefetch_related('images')
        if self.user.is_authenticated:
            items = items.exclude(seller=self.user)
        if exclude_item_id:
            items = items.exclude(id=exclude_item_id)

        now = timezone.now()
        recommendations = []
        for item in items:
            age_days = max((now - item.created_at).total_seconds() / 86400, 0)
            recency_score = max(0, 4 - min(age_days, 4))
            popularity_score = min(item.favorite_count, 10) * 0.8
            category_match = item.category_id in self.category_ids
            location_match = item.location_id is not None and item.location_id in self.location_ids
            score = recency_score + popularity_score
            if category_match:
                score += 8
            if location_match:
                score += 5

            if category_match and location_match:
                reason = '符合你的分类与交易地点偏好'
            elif category_match:
                reason = '与你收藏或交易过的分类相近'
            elif location_match:
                reason = '就在你关注过的校园地点附近'
            elif item.favorite_count:
                reason = '校园用户正在关注'
            else:
                reason = '校园新上架'
            recommendations.append(Recommendation(item=item, reason=reason, score=score))

        recommendations.sort(key=lambda recommendation: (-recommendation.score, -recommendation.item.created_at.timestamp()))
        return recommendations[:limit]


def get_recommendations(user=None, limit=8, exclude_item_id=None):
    return RecommendationService(user).get(limit=limit, exclude_item_id=exclude_item_id)

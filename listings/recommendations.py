from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.db.models import Count
from django.utils import timezone

from .models import BrowsingHistory, Favorite, Item, Order, RecommendationFeedback


@dataclass(frozen=True)
class Recommendation:
    item: Item
    reason: str
    score: float
    # 这句理由的稳定 key，与 reason 是同一句话的两种形态：reason 给人看，reason_key
    # 给运营面板按它分组用。文案会改，key 不改——拿文案当分组键，改一句文案历史行
    # 就全部归错组。见 docs/recommendation-impression-design.md 第 4 节。
    reason_key: str = ''


# 五句理由，顺序与打分检查它们的顺序一致。导出成常量，是为了让运营面板读到
# 的就是首页渲染出来的那几句：看板若自己另起一套说法，运营就会去改一个用户在
# 页面上根本看不见的理由。
REASONS = (
    'feedback_match',
    'category_and_location',
    'category',
    'location',
    'popular',
    'fresh',
)

REASON_LABELS = {
    'feedback_match': '你标记过想看，优先为你保留',
    'category_and_location': '符合你的分类与交易地点偏好',
    'category': '与你收藏或交易过的分类相近',
    'location': '就在你关注过的校园地点附近',
    'popular': '校园用户正在关注',
    'fresh': '校园新上架',
}


def recommendation_reason_key(*, feedback_match, category_match, location_match, favorite_count):
    """当前命中的是哪一句理由，返回稳定的 key。

    检查顺序与服务里的打分一致，单独收成一个函数，首页上那句话和运营面板按它归
    组的那句话就不会各走各的。它是五个匹配标记与收藏数的纯函数：不读数据库、也
    不取时钟，所以可以拿反馈那一行当时的输入重算一遍。

    返回 REASONS 里的一个 key，渲染文案走 recommendation_reason_label。
    """
    if feedback_match:
        return 'feedback_match'
    if category_match and location_match:
        return 'category_and_location'
    if category_match:
        return 'category'
    if location_match:
        return 'location'
    if favorite_count:
        return 'popular'
    return 'fresh'


def recommendation_reason_label(key):
    """一个理由 key 对应的、首页渲染出来的那句话。"""
    return REASON_LABELS[key]


class RecommendationService:
    """轻量级、可解释的校园商品推荐服务。"""

    def __init__(self, user=None):
        self.user = user or AnonymousUser()
        self.category_ids = set()
        self.location_ids = set()
        self.interested_item_ids = set()
        self.dismissed_item_ids = set()
        self._load_preferences()

    def _load_preferences(self):
        if not self.user.is_authenticated:
            return
        feedbacks = RecommendationFeedback.objects.filter(user=self.user)
        self.interested_item_ids.update(feedbacks.filter(action='interested').values_list('item_id', flat=True))
        self.dismissed_item_ids.update(feedbacks.filter(action='dismiss').values_list('item_id', flat=True))
        favorite_items = Favorite.objects.filter(user=self.user).select_related('item')
        self.category_ids.update(favorite_items.values_list('item__category_id', flat=True))
        self.location_ids.update(
            favorite_items.exclude(item__location_id=None).values_list('item__location_id', flat=True)
        )
        viewed_items = BrowsingHistory.objects.filter(user=self.user).select_related('item')
        self.category_ids.update(viewed_items.values_list('item__category_id', flat=True))
        self.location_ids.update(
            viewed_items.exclude(item__location_id=None).values_list('item__location_id', flat=True)
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
        items = Item.objects.available().annotate(
            favorite_count=Count('favorites'),
        ).select_related('category', 'seller', 'location').prefetch_related('images')
        if self.user.is_authenticated:
            items = items.exclude(seller=self.user).exclude(id__in=self.dismissed_item_ids)
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
            feedback_match = item.id in self.interested_item_ids
            score = recency_score + popularity_score
            if feedback_match:
                score += 10
            if category_match:
                score += 8
            if location_match:
                score += 5

            reason_key = recommendation_reason_key(
                feedback_match=feedback_match,
                category_match=category_match,
                location_match=location_match,
                favorite_count=item.favorite_count,
            )
            recommendations.append(
                Recommendation(
                    item=item,
                    reason=REASON_LABELS[reason_key],
                    score=score,
                    reason_key=reason_key,
                )
            )

        recommendations.sort(key=lambda recommendation: (-recommendation.score, -recommendation.item.created_at.timestamp()))
        return recommendations[:limit]


def get_recommendations(user=None, limit=8, exclude_item_id=None):
    return RecommendationService(user).get(limit=limit, exclude_item_id=exclude_item_id)

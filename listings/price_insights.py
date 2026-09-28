from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import math

from django.db.models import Sum
from django.utils import timezone

from .models import BrowsingHistory, Favorite, Item, Order


MONEY_QUANTUM = Decimal('0.01')


@dataclass(frozen=True)
class PriceInsight:
    current_price: Decimal
    comparable_count: int
    median_price: Decimal
    lower_price: Decimal
    upper_price: Decimal
    confidence: str
    confidence_label: str
    position: str
    position_label: str
    demand_level: str
    demand_label: str
    demand_detail: str
    detail: str
    action: str

    @property
    def tone(self):
        return {
            'below': 'positive',
            'within': 'neutral',
            'above': 'warning',
        }.get(self.position, 'neutral')


def _money(value):
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _percentile(values, fraction):
    if not values:
        return Decimal('0')
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * fraction
    lower_index = math.floor(index)
    upper_index = math.ceil(index)
    if lower_index == upper_index:
        return values[lower_index]
    weight = Decimal(str(index - lower_index))
    return values[lower_index] + (values[upper_index] - values[lower_index]) * weight


def _market_prices(item, *, now):
    base_queryset = Item.objects.filter(
        category_id=item.category_id,
        trade_mode='sale',
        price__gt=0,
        status__in={'available', 'reserved', 'sold'},
    ).exclude(pk=item.pk).order_by('-created_at')
    recent_prices = list(
        base_queryset.filter(created_at__gte=now - timedelta(days=180))
        .values_list('price', flat=True)[:500]
    )
    # 新分类可能没有足够的近期样本，回退到历史样本，但仍限制数量避免详情页查询失控。
    if recent_prices:
        return sorted(_money(price) for price in recent_prices), '近 180 天'
    historical_prices = list(base_queryset.values_list('price', flat=True)[:500])
    return sorted(_money(price) for price in historical_prices), '历史样本'


def _demand_signal(item):
    view_count = BrowsingHistory.objects.filter(item=item).aggregate(total=Sum('view_count'))['total'] or 0
    favorite_count = Favorite.objects.filter(item=item).count()
    has_active_order = Order.objects.filter(
        item=item, status__in=Order.ACTIVE_STATUS_VALUES,
    ).exists()
    score = min(100, min(view_count, 20) * 2 + favorite_count * 15 + (30 if has_active_order else 0))
    if score >= 60:
        level, label = 'hot', '近期关注度较高'
    elif score >= 30:
        level, label = 'warm', '已经有一些关注'
    else:
        level, label = 'steady', '暂时没有明显热度信号'
    parts = [f'{view_count} 次浏览', f'{favorite_count} 次收藏']
    if has_active_order:
        parts.append('已有进行中的预约')
    return level, label, '、'.join(parts) + '。'


def build_price_insight(item, *, now=None):
    """Build an explainable price reference from comparable campus listings.

    This is guidance rather than an automatic repricing rule: the service uses
    category, recent listing prices and existing engagement signals, while
    leaving the final price decision to the owner.
    """
    current_price = _money(item.price or 0)
    if item.trade_mode != 'sale' or current_price <= 0:
        return None

    now = now or timezone.now()
    prices, sample_scope = _market_prices(item, now=now)
    if not prices:
        return None

    median_price = _money(_percentile(prices, 0.5))
    lower_price = _money(_percentile(prices, 0.25))
    upper_price = _money(_percentile(prices, 0.75))
    if lower_price == upper_price:
        lower_price = _money(median_price * Decimal('0.9'))
        upper_price = _money(median_price * Decimal('1.1'))

    if current_price < lower_price:
        position, position_label = 'below', '低于同类常见区间'
        action = '如果商品成色和配件完整，可以考虑适当上调价格；低价出售也可以保留当前策略。'
    elif current_price > upper_price:
        position, position_label = 'above', '高于同类常见区间'
        action = '如果希望更快成交，可以考虑补充成色说明或适当下调价格。'
    else:
        position, position_label = 'within', '处于同类常见区间'
        action = '当前价格与同类商品较接近，可以重点完善图片、描述和交付安排。'

    if len(prices) >= 8:
        confidence, confidence_label = 'high', '参考样本较充分'
    elif len(prices) >= 3:
        confidence, confidence_label = 'medium', '参考样本有限'
    else:
        confidence, confidence_label = 'low', '仅供趋势参考'

    demand_level, demand_label, demand_detail = _demand_signal(item)
    detail = (
        f'参考{sample_scope}同类商品 {len(prices)} 件，价格中位数为 ¥{median_price:.2f}，'
        f'常见区间为 ¥{lower_price:.2f}～¥{upper_price:.2f}。'
    )
    return PriceInsight(
        current_price=current_price,
        comparable_count=len(prices),
        median_price=median_price,
        lower_price=lower_price,
        upper_price=upper_price,
        confidence=confidence,
        confidence_label=confidence_label,
        position=position,
        position_label=position_label,
        demand_level=demand_level,
        demand_label=demand_label,
        demand_detail=demand_detail,
        detail=detail,
        action=action,
    )

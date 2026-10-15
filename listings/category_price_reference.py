# -*- coding: utf-8 -*-
"""分类级出售价格参考，只读当前供给的形状。

商品详情页的 ``build_price_insight`` 回答的是「这一件和同类比在哪里」；本模块回答
运营侧的问题：「哪个分类里有一批当前在售商品同时落在常见区间外」。两者共用同一套
样本边界和分位数算法，但这里不做逐件循环——否则看板会为每件商品重新查一遍市场样本。
"""

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from django.utils import timezone

from .models import Category, Item
from .price_insights import _money, _percentile

RECENT_SAMPLE_DAYS = 180
MAX_SAMPLE_COUNT = 500
MIN_SHARE_SAMPLE_COUNT = 3
WIDE_RANGE_RATIO = Decimal('0.80')
PRICE_STATUSES = ('available', 'reserved', 'sold')


def _sample_prices(category, *, now):
    """取一个分类的价格样本，优先近期，再回退历史。

    每个分类最多 500 件，和详情页价格参考一致；这是防止一个巨大历史分类把运营
    看板拖入无界计算，不是 statistically unbiased sample 的声明。
    """
    base = Item.objects.filter(
        category=category,
        trade_mode='sale',
        price__gt=0,
        status__in=PRICE_STATUSES,
    )
    recent = list(
        base.filter(created_at__gte=now - timedelta(days=RECENT_SAMPLE_DAYS))
        .order_by('-created_at')
        .values_list('price', flat=True)[:MAX_SAMPLE_COUNT]
    )
    if recent:
        return sorted(_money(price) for price in recent), '近 180 天', 'recent_180'

    historical = list(
        base.order_by('-created_at')
        .values_list('price', flat=True)[:MAX_SAMPLE_COUNT]
    )
    return (
        sorted(_money(price) for price in historical),
        '历史样本',
        'historical',
    )


def _active_prices(category, *, now):
    """只取当前还能被卖家操作的出售供给。"""
    prices = Item.objects.filter(
        category=category,
        trade_mode='sale',
        price__gt=0,
        status='available',
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    return [
        _money(price)
        for price in prices.order_by('price', 'pk').values_list('price', flat=True)
    ]


def _confidence(sample_count):
    if sample_count >= 8:
        return 'high', '参考样本较充分'
    if sample_count >= MIN_SHARE_SAMPLE_COUNT:
        return 'medium', '参考样本有限'
    return 'low', '仅供趋势参考'


def _share(part, whole):
    if not whole:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(
        Decimal('0.1'), rounding=ROUND_HALF_UP,
    )


def _range(prices):
    median_price = _money(_percentile(prices, 0.5))
    lower_price = _money(_percentile(prices, 0.25))
    upper_price = _money(_percentile(prices, 0.75))
    is_widened = False
    if lower_price == upper_price:
        lower_price = _money(median_price * Decimal('0.9'))
        upper_price = _money(median_price * Decimal('1.1'))
        is_widened = True
    return median_price, lower_price, upper_price, is_widened


def _position_counts(prices, lower_price, upper_price):
    below_count = sum(price < lower_price for price in prices)
    above_count = sum(price > upper_price for price in prices)
    within_count = len(prices) - below_count - above_count
    return below_count, within_count, above_count


def _hint(*, below_count, within_count, above_count, sample_count):
    if sample_count < MIN_SHARE_SAMPLE_COUNT:
        return '样本不足，只报数量，不作为调价依据。'
    if above_count > below_count:
        return f'{above_count} 件在常见区间上方，可结合成色与周转继续观察。'
    if below_count > above_count:
        return f'{below_count} 件在常见区间下方，价格竞争较充分，不建议直接涨价。'
    if above_count or below_count:
        return f'上方 {above_count} 件、下方 {below_count} 件，先看区间宽度和成色说明。'
    return f'{within_count} 件当前在售都落在常见区间内。'


def _build_row(category, *, now):
    sample_prices, sample_scope, sample_scope_key = _sample_prices(category, now=now)
    if not sample_prices:
        return None

    active_prices = _active_prices(category, now=now)
    median_price, lower_price, upper_price, is_widened = _range(sample_prices)
    below_count, within_count, above_count = _position_counts(
        active_prices, lower_price, upper_price,
    )
    out_of_range_count = below_count + above_count
    confidence, confidence_label = _confidence(len(sample_prices))
    width_ratio = (
        (upper_price - lower_price) / median_price
        if median_price else Decimal('0')
    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    is_wide_range = width_ratio > WIDE_RANGE_RATIO

    status_labels = []
    if confidence == 'low':
        status_labels.append('样本不足')
    if is_widened:
        status_labels.append('区间已放宽')
    if is_wide_range:
        status_labels.append('区间过宽')
    if not status_labels:
        status_labels.append('样本可读')

    return {
        'category_id': category.pk,
        'name': category.name,
        'sample_count': len(sample_prices),
        'sample_scope': sample_scope,
        'sample_scope_key': sample_scope_key,
        'median_price': median_price,
        'lower_price': lower_price,
        'upper_price': upper_price,
        'active_count': len(active_prices),
        'below_count': below_count,
        'within_count': within_count,
        'above_count': above_count,
        'out_of_range_count': out_of_range_count,
        'out_of_range_share': _share(
            out_of_range_count, len(active_prices),
        ) if len(sample_prices) >= MIN_SHARE_SAMPLE_COUNT else None,
        'confidence': confidence,
        'confidence_label': confidence_label,
        'is_widened': is_widened,
        'width_ratio': width_ratio,
        'width_percent': width_ratio * 100,
        'is_wide_range': is_wide_range,
        'status_label': '、'.join(status_labels),
        'is_attention_needed': (
            len(sample_prices) >= MIN_SHARE_SAMPLE_COUNT and out_of_range_count > 0
        ),
        'hint': _hint(
            below_count=below_count,
            within_count=within_count,
            above_count=above_count,
            sample_count=len(sample_prices),
        ),
    }


def build_category_price_reference(*, days=30, now=None):
    """按分类聚合当前出售价格参考，不写任何业务表。"""
    now = now or timezone.now()
    rows = []
    for category in Category.objects.order_by('name'):
        row = _build_row(category, now=now)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: (-row['out_of_range_count'], row['name']))

    active_total = sum(row['active_count'] for row in rows)
    below_total = sum(row['below_count'] for row in rows)
    above_total = sum(row['above_count'] for row in rows)
    low_sample_count = sum(row['confidence'] == 'low' for row in rows)
    wide_range_count = sum(row['is_wide_range'] for row in rows)

    return {
        'requested_period_days': days,
        'snapshot_at': now,
        'recent_sample_days': RECENT_SAMPLE_DAYS,
        'max_sample_count': MAX_SAMPLE_COUNT,
        'min_share_sample_count': MIN_SHARE_SAMPLE_COUNT,
        'wide_range_ratio': WIDE_RANGE_RATIO,
        'has_data': bool(rows),
        'summary': {
            'category_count': len(rows),
            'active_item_total': active_total,
            'below_item_total': below_total,
            'above_item_total': above_total,
            'out_of_range_item_total': below_total + above_total,
            'low_sample_category_count': low_sample_count,
            'wide_range_category_count': wide_range_count,
            'top_category_name': rows[0]['name'] if rows else None,
        },
        'rows': rows,
        'empty_text': '还没有可形成价格参考的出售商品。',
        'note': (
            '本面板读取当前价格快照，看板周期天数不筛选它。样本沿用商品详情页价格参考：'
            '优先近 180 天、每分类最多 500 件，已预订和已售出只作价格证据；区间外计数只看当前在售。'
            '不输出商品或卖家，不自动改价，也不把区间外读成定价错误。'
        ),
    }

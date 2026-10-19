"""Buyer-facing sale-price history for one item.

The trajectory is useful only when it is continuous and large enough.  This
module deliberately fails closed: one broken link or a current price that no
longer matches the latest row hides the whole card instead of stitching a
plausible-looking chart from unverifiable data.
"""

from decimal import Decimal

from django.utils import timezone

MIN_CHANGE_COUNT = 3
MAX_CHANGE_COUNT = 6
_CHART_WIDTH = 260
_CHART_HEIGHT = 76
_CHART_PADDING = 8


def _hidden():
    return {'available': False}


def _money(value):
    return f'¥{value:.2f}'


def _change_label(old_price, new_price):
    amount = new_price - old_price
    if old_price == 0:
        return f'零元基线，调整为 {_money(new_price)}'

    percent = (amount / old_price * Decimal('100')).quantize(Decimal('0.1'))
    direction = '上涨' if amount > 0 else '降价'
    sign = '+' if amount > 0 else '-'
    return f'{direction} {_money(abs(amount))}（{sign}{abs(percent):.1f}%）'


def _local_date(value):
    return timezone.localtime(value).strftime('%Y年%m月%d日')


def _chart(prices):
    lowest = min(prices)
    highest = max(prices)
    span = float(highest - lowest)
    usable_width = _CHART_WIDTH - _CHART_PADDING * 2
    usable_height = _CHART_HEIGHT - _CHART_PADDING * 2
    markers = []
    coordinates = []

    for index, price in enumerate(prices):
        x = _CHART_PADDING + usable_width * index / (len(prices) - 1)
        vertical_ratio = 0.5 if span == 0 else (float(price) - float(lowest)) / span
        y = _CHART_HEIGHT - _CHART_PADDING - usable_height * vertical_ratio
        coordinate = f'{x:.2f},{y:.2f}'
        coordinates.append(coordinate)
        markers.append({
            'x': f'{x:.2f}',
            'y': f'{y:.2f}',
            'is_start': index == 0,
            'is_current': index == len(prices) - 1,
        })

    return {
        'view_box': f'0 0 {_CHART_WIDTH} {_CHART_HEIGHT}',
        'polyline': ' '.join(coordinates),
        'markers': markers,
        'lowest_price': lowest,
        'highest_price': highest,
    }


def _load_recent_changes(item):
    total_change_count = item.price_changes.count()
    if total_change_count < MIN_CHANGE_COUNT:
        return total_change_count, []

    changes = list(
        item.price_changes.order_by('-created_at', '-pk')[:MAX_CHANGE_COUNT]
    )
    changes.reverse()
    return total_change_count, changes


def load_item_price_history_state(item):
    """Load the price-change window once for both price-history surfaces.

    The item detail page renders the public chart and the seller-only
    maintenance card from the same rows, so the view loads them a single
    time and hands the tuple to both builders.  ``None`` is returned when
    the item cannot have a price history at all, so a caller never pays
    for a query whose result would be discarded anyway.
    """
    if item.trade_mode != 'sale' or item.price is None:
        return None
    return _load_recent_changes(item)


def build_item_price_history(item, *, state=None):
    """Return a renderable, date-granularity price window for a sale item."""
    if item.trade_mode != 'sale' or item.price is None:
        return _hidden()

    total_change_count, changes = (
        _load_recent_changes(item) if state is None else state
    )
    if len(changes) < MIN_CHANGE_COUNT:
        return _hidden()

    for previous, current in zip(changes, changes[1:]):
        if previous.new_price != current.old_price:
            return _hidden()
    if changes[-1].new_price != item.price:
        return _hidden()

    points = [{
        'price': changes[0].old_price,
        'date_label': '—',
        'state_label': '窗口起始价',
        'change_label': '窗口起始价，不代表发布价',
        'direction': 'baseline',
        'zero_base': changes[0].old_price == 0,
        'is_start': True,
        'is_current': False,
    }]
    for index, change in enumerate(changes):
        is_current = index == len(changes) - 1
        direction = 'rise' if change.new_price > change.old_price else 'drop'
        points.append({
            'price': change.new_price,
            'date_label': _local_date(change.created_at),
            'state_label': '当前价' if is_current else '调整后',
            'change_label': _change_label(change.old_price, change.new_price),
            'direction': direction,
            'zero_base': change.old_price == 0,
            'is_start': False,
            'is_current': is_current,
        })

    prices = [point['price'] for point in points]
    chart = _chart(prices)
    scope_label = (
        f'最近 {len(changes)} 次（共 {total_change_count} 次调整）'
        if total_change_count > MAX_CHANGE_COUNT
        else f'共 {len(changes)} 次调整'
    )
    return {
        'available': True,
        'scope_label': scope_label,
        'window_is_partial': total_change_count > MAX_CHANGE_COUNT,
        'total_change_count': total_change_count,
        'displayed_change_count': len(changes),
        'chart': chart,
        'points': points,
        'aria_label': (
            f'价格走势图：{scope_label}，区间 '
            f'{_money(chart["lowest_price"])} 到 {_money(chart["highest_price"])}'
        ),
    }


def build_item_price_history_maintenance(item, viewer, *, state=None):
    """Return a read-only price-history diagnosis for the item seller."""
    if (
        item.trade_mode != 'sale'
        or item.price is None
        or not getattr(viewer, 'is_authenticated', False)
        or viewer.pk != item.seller_id
    ):
        return {'visible': False}

    total_change_count, changes = (
        _load_recent_changes(item) if state is None else state
    )
    if total_change_count == 0:
        return {
            'visible': True,
            'state': 'empty',
            'tone': 'info',
            'title': '暂无轨迹',
            'summary': '还没有价格轨迹记录。出售商品成功改价后会自动开始记录。',
            'change_count': total_change_count,
        }

    if total_change_count < MIN_CHANGE_COUNT:
        return {
            'visible': True,
            'state': 'waiting',
            'tone': 'info',
            'title': '等待积累',
            'summary': (
                f'已记录 {total_change_count} 次价格调整，'
                f'达到 {MIN_CHANGE_COUNT} 次连续调整后会向买家展示。'
            ),
            'change_count': total_change_count,
        }

    is_broken = any(
        previous.new_price != current.old_price
        for previous, current in zip(changes, changes[1:])
    )
    current_mismatch = changes[-1].new_price != item.price
    mismatch_summary = (
        f'最近轨迹记录为 {_money(changes[-1].new_price)}，当前价是 {_money(item.price)}。'
        if current_mismatch else ''
    )

    if is_broken:
        summary = (
            '最近窗口内有价格不衔接的记录，买家侧暂不展示。继续通过正常编辑积累轨迹；'
            '若确认没有绕过系统编辑，再联系管理员。'
        )
        if current_mismatch:
            summary = f'{summary}{mismatch_summary}'
        return {
            'visible': True,
            'state': 'broken',
            'tone': 'warning',
            'title': '轨迹断链',
            'summary': summary,
            'change_count': total_change_count,
        }

    if current_mismatch:
        return {
            'visible': True,
            'state': 'current_mismatch',
            'tone': 'warning',
            'title': '价格不一致',
            'summary': (
                f'{mismatch_summary}两者不一致时买家侧不会展示价格轨迹，'
                '请核对最近一次价格修改来源。'
            ),
            'change_count': total_change_count,
        }

    return {
        'visible': True,
        'state': 'public',
        'tone': 'positive',
        'title': '正常展示',
        'summary': (
            f'买家侧正在展示最近 {len(changes)} 次价格调整。'
            if len(changes) == total_change_count
            else f'买家侧正在展示最近 {len(changes)} 次价格调整（共 {total_change_count} 次）。'
        ),
        'change_count': total_change_count,
    }

# -*- coding: utf-8 -*-
"""默认借用天数的形状：卖家定的信任期限，第一次被数字读到。

``Item.borrow_days`` 是借用域里唯一一个由出借方亲手写下的数字，也是 ``Order.return_due_at``
的唯一来源。它是全站唯一一个在业务模块里出现次数为零的字段：写入路径有（表单 1 到 90 的
校验），显示路径有（商品详情、下单页、订单详情都在念它），统计路径没有。

借用域已经有四个模块在数东西：``borrow_risk`` 按分类和押金分层，``borrow_rhythm`` 把它
和学期阶段交叉，``borrow_escalation`` 管三级催收阶梯，``governance_sla`` 管治理队列时效。
四个模块全部从「东西还没回来」切进去，没有一个从「卖家当初允许借多久」切进去。

本模块补上这一刀。它读同一批借用单（订单侧、含从未升级过的，与 ``borrow_risk`` 分母口径
一致），按 ``borrow_days`` 的取值重新聚合，报五件事：

1. 借用商品配了多长的期限（按档分布，含 0 样本档）。
2. 配的天数 vs 实际借了多久（配置档与实际档两列对照）。
3. 借用天数 × 催收（升级率、第三级率、闭环时长）。
4. 逾期幅度相对配置天数的比例（超期天数 / 配置天数）。
5. 默认值 7 天本身被用了多少。

## 边界

**``borrow_days`` 与 ``return_due_at`` 的关系。** 下单时按下单时刻加 ``borrow_days`` 推出
归还截止时刻，之后这个数字本身不再参与任何事。改商品的 ``borrow_days`` 不追溯旧订单——一笔
已经借出去的订单，它的归还截止时刻在下单那一刻就定了。所以第二节只做「配置档 vs 实际档」
的对照，不做任何重算。

**不报「配置被改过几次」。** ``Item.updated_at`` 会被任何一次保存刷新，包括将来的任何字段
变更，拿它推断修改次数是拿噪声当信号。库里没有记录借用天数变更历史的表，这个数不知道，
不编。

**不把相关性讲成因果。** 配 90 天的商品三天就还，可能是期限形同虚设，也可能这类东西本来
就不需要借那么久，本模块没有能力区分，只把分布报出来。

**不把长期限读成「更信任别人」。** 一个人可能只是因为懒或者照抄了默认值。

本模块是只读聚合。不加迁移、不加字段、不改模型、不改默认值、不改 1 到 90 的校验、不追改
历史订单、不动催收阶梯、不按用户排名、不输出用户名、不列商品名、不读自由文本。
"""

from datetime import timedelta

from django.utils import timezone

from .borrow_risk import MIN_SAMPLE_SIZE, _rate
from .models import BorrowReturnEscalation, Item, Order


# 期限档。档位取表单上真实会出现的值（1/3/7/14/30/90），加一个「其它」兜住中间的散值。
#
# 取这些值不取分位数，理由和 borrow_risk 的押金档一样：运营看表时要能认出自己在表单上选的
# 那一档，一个每月都会移动的分位边界什么也说明不了。档位互斥、左闭右开，全列——包括 0 样本
# 档，因为「90 天上没有人」本身就是结论。
BORROW_DAY_BANDS = (
    (1, 4, '1-3 天'),
    (4, 8, '4-7 天'),
    (8, 15, '8-14 天'),
    (15, 31, '15-30 天'),
    (31, 91, '31-90 天'),
    (91, None, '90 天以上'),
)

# 表单上的占位符，也是模型默认值。单独数它，因为它是运营唯一能一眼认出来的数。
DEFAULT_BORROW_DAYS = 7

# 窗口与 borrow_risk 一致，两表读同一段时间才有可比性。
DEFAULT_LOOKBACK_DAYS = 120

# 一个期限档要给升级率、第三级率方向，至少 5 笔该档借用。和 borrow_risk.MIN_SAMPLE_SIZE
# 同一量级，因为两表并排读，门槛不同会让读者误判哪张表更可信。
MIN_BAND_SAMPLE = MIN_SAMPLE_SIZE

# 要给「7 天档占比」这类占比一个方向，至少 12 件借用商品。全站统一量级。
MIN_ITEM_SAMPLE = 12

# 百分点差值不满此值视为噪声，不给方向。全站同一套。
NOISE_POINTS = 8


def _band_label(borrow_days):
    """Which borrow-day band a given configuration falls into."""
    if borrow_days is None:
        return '未设置'
    for lower, upper, label in BORROW_DAY_BANDS:
        if borrow_days >= lower and (upper is None or borrow_days < upper):
            return label
    return BORROW_DAY_BANDS[-1][2]


def _all_band_labels():
    """Every band label, in table order. Zero-sample bands stay in the table."""
    return [label for _, _, label in BORROW_DAY_BANDS]


def _fetch_borrow_items():
    """Every borrow-mode item with its configured borrow days.

    Read from the item side, not the order side: this table is about what sellers
    configured, and an item that has never been borrowed still carries a
    configuration. Reading only borrowed items would make the distribution of
    configured terms look like the distribution of used terms, which is a
    different question.
    """
    return list(
        Item.objects
        .filter(trade_mode='borrow')
        .values('id', 'borrow_days', 'status')
    )


def _item_band_rows(items):
    """Distribution of configured borrow days over borrow-mode items.

    Zero-sample bands are kept on purpose. A band with no items in it is a
    finding, not a gap in the table.
    """
    total = len(items)
    in_stock = sum(1 for item in items if item['status'] == 'available')
    counts = {label: 0 for label in _all_band_labels()}
    in_stock_counts = {label: 0 for label in _all_band_labels()}
    for item in items:
        label = _band_label(item['borrow_days'])
        counts[label] += 1
        if item['status'] == 'available':
            in_stock_counts[label] += 1
    rows = []
    for label in _all_band_labels():
        rows.append({
            'label': label,
            'item_count': counts[label],
            'in_stock_count': in_stock_counts[label],
            'share': _rate(counts[label], total),
            'is_small_sample': counts[label] < MIN_ITEM_SAMPLE,
        })
    return rows


def _configured_vs_actual_rows(borrows):
    """Configured term vs the term the borrow actually took.

    Two distributions on the same six bands, not one difference column. A
    reader wants to see them side by side: if the 4-7 day band holds ten
    configured borrows while the 1-3 day band holds eight actual ones, most
    seven-day borrows came back in three days. A single difference column would
    hide which side moved.

    Each column carries its own share and its own denominator, and the two
    denominators are not the same number. The configured column counts every
    borrow in the window; the actual column counts only the borrows that came
    back. Stating both is the point - a reader who compares the columns without
    knowing that will read a completion rate as a duration distribution.

    A borrow still in flight is a right-censored sample: its actual duration
    does not exist yet. Folding it into the actual column would make every
    band's duration look shorter than it is, so those borrows are counted
    separately and never enter either column.

    The configured side reads ``item__borrow_days`` as the item stands now. An
    item re-configured after the order was placed therefore compares its new
    value against an old order's duration. That is stated on the panel rather
    than hidden, because the alternative - pretending to know the historical
    value - is not available: nothing records when the field changed.
    """
    configured_by_band = {label: 0 for label in _all_band_labels()}
    actual_by_band = {label: 0 for label in _all_band_labels()}
    actual_durations = {label: [] for label in _all_band_labels()}
    matched = 0
    mismatch = 0
    censored = 0
    for borrow in borrows:
        if borrow['item__borrow_days'] is None:
            continue
        configured_by_band[_band_label(borrow['item__borrow_days'])] += 1
        if borrow['status'] != 'returned' or not borrow['returned_at']:
            censored += 1
            continue
        duration_days = max((borrow['returned_at'] - borrow['created_at']).total_seconds(), 0.0) / 86400
        band = _band_label(duration_days)
        actual_by_band[band] += 1
        actual_durations[band].append(duration_days)
        if band == _band_label(borrow['item__borrow_days']):
            matched += 1
        else:
            mismatch += 1
    total_configured = sum(configured_by_band.values())
    total_actual = sum(actual_by_band.values())
    rows = []
    for label in _all_band_labels():
        durations = actual_durations[label]
        rows.append({
            'label': label,
            'configured_count': configured_by_band[label],
            'configured_share': _rate(configured_by_band[label], total_configured),
            'actual_count': actual_by_band[label],
            'actual_share': _rate(actual_by_band[label], total_actual),
            'average_actual_days': round(sum(durations) / len(durations), 1) if durations else None,
            'max_actual_days': round(max(durations), 1) if durations else None,
            'is_small_sample': configured_by_band[label] < MIN_BAND_SAMPLE,
        })
    return rows, matched, mismatch, censored


def _escalation_band_rows(borrows, escalations, *, now):
    """Escalation outcomes by configured borrow-day band.

    Read from the order side exactly as ``borrow_risk`` reads it: never-escalated
    borrows are in the denominator. The two tables are meant to be read side by
    side and a reader assumes they are looking at the same population.

    Overdue is measured on the closed side - a returned borrow compared against
    its own deadline - so that the ratio and the absolute days describe the same
    set of borrows. An open borrow's overdue amount keeps growing and would make
    the band's average drift with the time of day the report is read.
    """
    groups = {label: {
        'label': label,
        'borrow_count': 0,
        'escalated_count': 0,
        'level_three_count': 0,
        'resolved_count': 0,
        'open_count': 0,
        'overdue_count': 0,
        'overdue_ratios': [],
        'overdue_days': [],
        'closed_durations': [],
    } for label in _all_band_labels()}
    unknown = 0
    for borrow in borrows:
        if borrow['item__borrow_days'] is None:
            unknown += 1
            continue
        band = _band_label(borrow['item__borrow_days'])
        group = groups[band]
        group['borrow_count'] += 1
        configured_days = borrow['item__borrow_days'] or DEFAULT_BORROW_DAYS
        escalation = escalations.get(borrow['id'])
        if not escalation:
            continue
        group['escalated_count'] += 1
        if escalation['escalation_level'] >= 3:
            group['level_three_count'] += 1
        resolved_at = escalation['resolved_at']
        if resolved_at:
            group['resolved_count'] += 1
            started_at = escalation['last_escalated_at'] or borrow['return_due_at']
            if started_at:
                group['closed_durations'].append(
                    max((resolved_at - started_at).total_seconds(), 0.0) / 86400
                )
        else:
            group['open_count'] += 1
        if borrow['return_due_at'] and borrow['returned_at']:
            overdue_days = (borrow['returned_at'] - borrow['return_due_at']).total_seconds() / 86400
            if overdue_days > 0:
                group['overdue_count'] += 1
                # Overdue by three days is a different fact on a seven-day
                # borrow than on a ninety-day one, so the leading figure is the
                # ratio against the configured term. The absolute days stay in
                # the table because that is the number an operator actually
                # uses when talking to a borrower.
                group['overdue_ratios'].append(overdue_days / max(configured_days, 1))
                group['overdue_days'].append(overdue_days)
    rows = []
    for group in groups.values():
        sample = group['borrow_count']
        durations = group['closed_durations']
        ratios = group['overdue_ratios']
        rows.append({
            'label': group['label'],
            'borrow_count': sample,
            'escalated_count': group['escalated_count'],
            'level_three_count': group['level_three_count'],
            'resolved_count': group['resolved_count'],
            'open_count': group['open_count'],
            'overdue_count': group['overdue_count'],
            'escalation_rate': _rate(group['escalated_count'], sample),
            'level_three_rate': _rate(group['level_three_count'], sample),
            'overdue_rate': _rate(group['overdue_count'], sample),
            'average_overdue_days': (
                round(sum(group['overdue_days']) / len(group['overdue_days']), 1)
                if group['overdue_days'] else None
            ),
            'average_overdue_ratio': (
                round(sum(ratios) / len(ratios) * 100, 1) if ratios else None
            ),
            'average_close_days': round(sum(durations) / len(durations), 1) if durations else None,
            'is_small_sample': sample < MIN_BAND_SAMPLE,
        })
    rows.sort(key=lambda row: (-row['borrow_count'], row['label']))
    return rows, unknown


def _conclusion(item_rows, band_rows, *, total_items, has_sample):
    """One sentence naming the bands that actually stand out."""
    if not total_items:
        return '站内还没有限期借用商品，默认借用天数的形状暂时无从读起。'
    parts = []
    if total_items >= MIN_ITEM_SAMPLE:
        top = max(item_rows, key=lambda row: row['item_count'])
        if top['item_count']:
            parts.append(
                f'{top["item_count"]} 件借用商品配的是「{top["label"]}」，'
                f'占全部借用商品的 {top["share"]}%'
            )
    usable = [
        row for row in band_rows
        if not row['is_small_sample'] and row['level_three_rate'] is not None
    ]
    if usable:
        worst = max(usable, key=lambda row: row['level_three_rate'])
        parts.append(
            f'「{worst["label"]}」的第三级催收率最高，'
            f'{worst["borrow_count"]} 笔借用中有 {worst["level_three_count"]} 笔拖到第三级'
        )
    if not parts:
        return '借用商品整体偏少，暂不足以支撑期限分层的结论。'
    return '；'.join(parts) + '。'


def _fetch_borrow_orders(*, days, now):
    """Every borrow order in the window, with its configured term attached.

    Read from the order side exactly as ``borrow_risk`` reads it, and for the
    same reason: an order that was never escalated still belongs in the
    denominator. If the table only listed escalated orders, every band would
    show a 100% escalation rate and the stratification would be worthless.

    The configured term is joined from the item because ``Order`` carries the
    computed ``return_due_at`` but not the ``borrow_days`` it was computed from.
    An item whose row has since disappeared (deleted) still has its order, and
    those orders are reported separately rather than dropped.
    """
    start = now - timedelta(days=max(1, int(days)))
    borrows = list(
        Order.objects
        .filter(
            item__trade_mode='borrow',
            created_at__gte=start,
            created_at__lte=now,
        )
        .select_related('item')
        .values(
            'id',
            'item_id',
            'item__borrow_days',
            'status',
            'return_due_at',
            'returned_at',
            'created_at',
        )
    )
    escalations = {
        row['order_id']: row
        for row in BorrowReturnEscalation.objects.filter(
            order__item__trade_mode='borrow',
            order__created_at__gte=start,
            order__created_at__lte=now,
        ).values('order_id', 'escalation_level', 'resolved_at', 'last_escalated_at')
    }
    return borrows, escalations


def build_borrow_days_shape(*, days=DEFAULT_LOOKBACK_DAYS, now=None):
    """Read the shape of the configured borrow term for the operations board."""
    now = now or timezone.now()
    days = max(1, int(days))

    items = _fetch_borrow_items()
    item_rows = _item_band_rows(items)

    borrows, escalations = _fetch_borrow_orders(days=days, now=now)
    configured_rows, matched, mismatch, censored = _configured_vs_actual_rows(borrows)
    band_rows, unknown = _escalation_band_rows(borrows, escalations, now=now)

    total_items = len(items)
    default_share = _rate(
        sum(1 for item in items if (item['borrow_days'] or 0) == DEFAULT_BORROW_DAYS),
        total_items,
    )
    total_borrows = len(borrows)
    total_escalated = sum(1 for borrow in borrows if borrow['id'] in escalations)
    total_level_three = sum(
        1 for borrow in borrows
        if borrow['id'] in escalations
        and escalations[borrow['id']]['escalation_level'] >= 3
    )

    return {
        'days': days,
        'item_count': total_items,
        'in_stock_count': sum(1 for item in items if item['status'] == 'available'),
        'borrow_count': total_borrows,
        'escalated_count': total_escalated,
        'level_three_count': total_level_three,
        'escalation_rate': _rate(total_escalated, total_borrows),
        'item_rows': item_rows,
        'configured_rows': configured_rows,
        'band_rows': band_rows,
        'default_borrow_days': DEFAULT_BORROW_DAYS,
        'default_share': default_share,
        'default_share_has_sample': total_items >= MIN_ITEM_SAMPLE,
        'matched_count': matched,
        'censored_count': censored,
        'mismatch_count': mismatch,
        'unknown_item_count': unknown,
        'min_band_sample': MIN_BAND_SAMPLE,
        'min_item_sample': MIN_ITEM_SAMPLE,
        'has_sample': total_borrows >= MIN_BAND_SAMPLE,
        'has_data': bool(items) or bool(borrows),
        'summary': _conclusion(
            item_rows, band_rows,
            total_items=total_items, has_sample=total_borrows >= MIN_BAND_SAMPLE,
        ),
    }

# -*- coding: utf-8 -*-
"""Measure whether lifecycle reminders actually changed seller behaviour.

上一轮把诊断结论推送给卖家，但发送量本身不说明任何问题：一个提醒系统可以
同时做到“发了很多”和“完全没用”。这个模块补上缺失的那一环——提醒发出去
之后，卖家到底有没有动手。

四条必须守住的边界，决定了这个模块能声称什么、不能声称什么：

1. **只报相关性，不报因果。** 卖家完全可能在提醒之前就已经在整理商品。
   本模块能确认的只有“动作发生在提醒之后”，所以所有比例都按提醒时间戳
   切开计算，看板上也标注为观察值，不写成归因结果。
2. **动作只认可观测的写入点。** 刷新看 ``last_refreshed_at``（只在卖家主动
   刷新时写入），成交看 ``OrderEvent`` 到达终态的时间。``Item.updated_at`` 是
   ``auto_now``，任何一次保存都会改写它，拿它证明“卖家做过决策”是不成立的，
   因此一律不用。
3. **受阻原因用当前值重算，不伪装成历史。** 发送时的跳过计数只是进程内的
   返回值，没有落库；这里用当前偏好、当前免打扰时段和当前同类样本重新计算，
   看板脚注会说明这一点，避免把重算值当成发送当天的真实快照。
4. **价格提醒与关注提醒分开统计。** 价格提醒引用同类区间作为证据，关注提醒
   只说明“有商品需要处理”。混在一起就看不出证据是否真的更有说服力，而这是
   唯一能被现有数据检验的假设。
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .lifecycle_diagnostics import _level_for
from .lifecycle_reminders import _price_candidates
from .models import Item, Notification, NotificationPreference, OrderEvent
from .notifications import quiet_hours_active


# 提醒之后观察卖家动作的窗口。与供给侧面板的互动窗口保持一致，方便两个
# 面板的数字互相参照；同时也远小于 45 天的沉底线，不会把“很久以后恰好
# 刷了一次”算成提醒的功劳。
EFFECT_WINDOW_DAYS = 14

# 订单走到这两个状态才算成交。借用交易的终点是“已归还”，与出售的
# “交易完成”分开处理，与订单阶段分析的口径一致。
TERMINAL_ORDER_STATUSES = ('completed', 'returned')

# “提醒了却没有任何动作”的商品只展示前几条，但这个数字本身始终给出。
UNTOUCHED_LIMIT = 8

# 通知写入与冷却字段回写在同一个事务里，但两者仍可能差出毫秒级；
# 匹配通知与商品时留出这点余量，避免把同一次提醒判成“没有触达”。
NOTICE_MATCH_TOLERANCE = timedelta(minutes=5)


def _period_bounds(days, now):
    """Local-date aligned period, matching the other insight modules."""
    today = timezone.localdate(now)
    start_date = today - timedelta(days=days - 1)
    start = timezone.make_aware(
        timezone.datetime.combine(start_date, timezone.datetime.min.time()),
    )
    return start, start_date


def _reminded_items(start, now):
    """Listings whose seller was actually reached inside the period.

    A listing counts as reached when either reminder timestamp falls inside
    the period. ``reminded_at`` is the later of the in-period timestamps, so
    a listing reminded twice in the period is judged from the most recent
    nudge instead of the first one.
    """
    items = list(
        Item.objects.filter(
            Q(attention_reminder_sent_at__gte=start, attention_reminder_sent_at__lte=now)
            | Q(price_drop_reminder_sent_at__gte=start, price_drop_reminder_sent_at__lte=now),
        ).select_related('seller', 'category')
    )
    rows = []
    for item in items:
        attention_at = item.attention_reminder_sent_at
        price_at = item.price_drop_reminder_sent_at
        stamps = [
            value for value in (attention_at, price_at)
            if value is not None and start <= value <= now
        ]
        if not stamps:
            continue
        rows.append({
            'item': item,
            'attention_at': attention_at,
            'price_at': price_at,
            'reminded_at': max(stamps),
        })
    return rows


def _terminal_event_times(item_ids, floor):
    """When each listing reached a terminal order state, keyed by listing.

    Returned as ``{item_id: [datetime, ...]}`` instead of a set so the caller
    can compare against each listing's own reminder timestamp. A set would
    be cheaper but would also let an order that finished *before* the nudge
    count as a reaction to it.
    """
    if not item_ids:
        return {}
    events = OrderEvent.objects.filter(
        order__item_id__in=item_ids,
        to_status__in=TERMINAL_ORDER_STATUSES,
        created_at__gte=floor,
    ).values('order__item_id', 'created_at')
    times = {}
    for event in events:
        times.setdefault(event['order__item_id'], []).append(event['created_at'])
    return times


def _notice_index(seller_ids, start, now):
    """Group lifecycle notices by recipient, with the moment they arrived.

    The notice is created inside the same transaction that writes the
    cool-down fields, so its ``created_at`` is effectively the send instant.
    Keeping both the timestamp and the read flag lets the caller separate
    "never saw it" from "saw it and did nothing".
    """
    if not seller_ids:
        return {}
    rows = Notification.objects.filter(
        recipient_id__in=seller_ids,
        kind='lifecycle_reminder',
        created_at__gte=start,
        created_at__lte=now,
    ).values('recipient_id', 'created_at', 'is_read')
    index = {}
    for row in rows:
        index.setdefault(row['recipient_id'], []).append(row)
    return index


def _notice_for(index, seller_id, reminded_at):
    """The notice closest in time to a listing's reminder, if there is one.

    A seller can receive two notices on the same day only if the scheduler
    ran twice with different listing sets, which the date-scoped dedupe key
    prevents. The tolerance therefore only absorbs transaction ordering,
    not genuinely separate sends.
    """
    candidates = index.get(seller_id) or []
    if not candidates:
        return None
    best = None
    best_delta = None
    for row in candidates:
        delta = abs(row['created_at'] - reminded_at)
        if best_delta is None or delta < best_delta:
            best = row
            best_delta = delta
    if best_delta is None or best_delta > NOTICE_MATCH_TOLERANCE:
        return None
    return best


def _outcome(row, terminal_times):
    """Classify what happened to one reminded listing.

    ``acted`` means the seller performed an observable action after the nudge.
    Editing a listing is deliberately not one of them: ``updated_at`` is
    rewritten by every save, so it cannot distinguish "the seller reviewed
    this" from "something else touched the row".
    """
    item = row['item']
    reminded_at = row['reminded_at']
    refreshed_after = bool(
        item.last_refreshed_at is not None and item.last_refreshed_at > reminded_at
    )
    completed_after = any(
        moment > reminded_at for moment in terminal_times.get(item.pk, ())
    )
    return refreshed_after, completed_after


def _pct(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else 0


def build_lifecycle_reminder_effect(days=30, *, now=None):
    """Whether lifecycle reminders changed anything for the sellers who got them.

    The panel answers three questions in order, and the order matters:

    1. Were the notices read at all?  A reminder nobody opens is a delivery
       problem, and no amount of better copy fixes it.
    2. Did the seller act afterwards?  Refreshing and reaching a terminal
       order state are the only two actions the data actually records.
    3. What is still stuck?  Listings that were reminded and then left alone
       are the ones worth a human look, and they are listed rather than
       summarised into a single number.

    Every ratio is computed against the listings reminded *inside* the period,
    so the percentages stay comparable when the operator changes the period.
    """
    now = now or timezone.now()
    start, _start_date = _period_bounds(days, now)
    window_end = now

    rows = _reminded_items(start, now)
    item_ids = [row['item'].pk for row in rows]
    seller_ids = {row['item'].seller_id for row in rows}
    notice_index = _notice_index(seller_ids, start, now)
    terminal_times = _terminal_event_times(item_ids, start)

    notices = sum(len(value) for value in notice_index.values())
    read_notices = sum(
        1 for value in notice_index.values()
        for row in value if row['is_read']
    )

    outcome_rows = []
    for row in rows:
        item = row['item']
        refreshed_after, completed_after = _outcome(row, terminal_times)
        notice = _notice_for(notice_index, item.seller_id, row['reminded_at'])
        level, label, _tone, _reasons, _actions = _level_for(item, now=now)
        outcome_rows.append({
            'item': item,
            'seller': item.seller,
            'reminded_at': row['reminded_at'],
            'attention_at': row['attention_at'],
            'price_at': row['price_at'],
            'kinds': _kind_keys(row),
            'read': bool(notice['is_read']) if notice else False,
            'reached': notice is not None,
            'refreshed_after': refreshed_after,
            'completed_after': completed_after,
            'level': level,
            'level_label': label,
        })

    kind_rows = _build_kind_rows(outcome_rows)
    blocked_rows = _build_blocked_rows(now=now)
    untouched_rows = _build_untouched_rows(outcome_rows)

    reminded_items = len(outcome_rows)
    acted = sum(
        1 for row in outcome_rows
        if row['refreshed_after'] or row['completed_after']
    )
    refreshed = sum(1 for row in outcome_rows if row['refreshed_after'])
    completed = sum(1 for row in outcome_rows if row['completed_after'])
    reached = sum(1 for row in outcome_rows if row['reached'])
    read = sum(1 for row in outcome_rows if row['read'])

    recommendations = _build_recommendations(
        reminded_items=reminded_items,
        kind_rows=kind_rows,
        reached=reached,
        read=read,
        acted=acted,
        blocked_rows=blocked_rows,
    )

    return {
        'period_days': days,
        'has_data': bool(reminded_items),
        'summary': {
            'reminded_items': reminded_items,
            'reminded_sellers': len(seller_ids),
            'notices': notices,
            'read_notices': read_notices,
            'notice_read_rate': _pct(read_notices, notices),
            'reached_items': reached,
            'read_items': read,
            'read_rate': _pct(read, reminded_items),
            'refreshed_after': refreshed,
            'completed_after': completed,
            'acted_after': acted,
            'action_rate': _pct(acted, reminded_items),
            'read_but_inactive': sum(
                1 for row in outcome_rows
                if row['read'] and not (row['refreshed_after'] or row['completed_after'])
            ),
            'untouched': reminded_items - acted,
            'window_days': EFFECT_WINDOW_DAYS,
        },
        'kind_rows': kind_rows,
        'blocked_rows': blocked_rows,
        'untouched_rows': untouched_rows,
        'untouched_total': reminded_items - acted,
        'recommendations': recommendations,
    }

def _kind_keys(row):
    """Which reminder kinds this listing was actually reached with."""
    keys = []
    if row["attention_at"] is not None:
        keys.append("attention")
    if row["price_at"] is not None:
        keys.append("price")
    return keys


def _build_kind_rows(outcome_rows):
    """Split the outcome by which kind of reminder the seller received.

    The price reminder is the only one that cites evidence, so its action
    rate is the number that answers whether citing evidence beats a generic
    nudge. The three buckets are mutually exclusive for exactly that reason: a
    listing that received both kinds cannot attribute its outcome to either.
    """
    # 三个桶互斥。若让 both 同时计入 attention 和 price，"带证据是否更有效"
    # 这个问题就答不了了：同一个卖家既收到了待处理提醒，也收到了价格证据，
    # 观察到的动作无法归因给任何一类。所以 both 单独成桶，只报告不比较。
    definitions = (
        ("attention", "仅待处理提醒", "只说明有商品需要决策，不引用外部证据"),
        ("price", "仅价格参考提醒", "引用同类价格区间作为调整依据"),
        ("both", "两类同时触达", "同一轮里既提醒了待处理，也提醒了价格，无法归因"),
    )
    rows = []
    for key, label, note in definitions:
        if key == "both":
            matched = [row for row in outcome_rows if len(row["kinds"]) == 2]
        else:
            matched = [
                row for row in outcome_rows
                if row["kinds"] == [key]
            ]
        if not matched:
            continue
        acted = sum(
            1 for row in matched
            if row["refreshed_after"] or row["completed_after"]
        )
        read = sum(1 for row in matched if row["read"])
        rows.append({
            "key": key,
            "label": label,
            "note": note,
            "items": len(matched),
            "read": read,
            "read_rate": _pct(read, len(matched)),
            "acted": acted,
            "action_rate": _pct(acted, len(matched)),
        })
    return rows


def _build_untouched_rows(outcome_rows):
    """Listings that were reminded and then left completely alone.

    These are the rows a human should look at, so they are listed rather than
    folded into a counter: the seller is still present, the notice was
    delivered, and nothing moved.
    """
    untouched = [
        row for row in outcome_rows
        if not (row["refreshed_after"] or row["completed_after"])
    ]
    untouched.sort(key=lambda row: (not row["read"], -row["item"].freshness_age_days))
    rows = []
    for row in untouched[:UNTOUCHED_LIMIT]:
        item = row["item"]
        rows.append({
            "item": item,
            "seller": item.seller,
            "reminded_at": row["reminded_at"],
            "kinds": row["kinds"],
            "read": row["read"],
            "level_label": row["level_label"],
            "age_days": item.freshness_age_days,
            "days_since_reminder": max((timezone.now() - row["reminded_at"]).days, 0),
        })
    return rows

def _build_blocked_rows(*, now):
    """Why a seller could still have been missed, recomputed from current state.

    The send-time counters live only in the command return value and are never
    stored, so a dashboard cannot replay what they were on the day. These
    numbers describe the current state of the same conditions instead, and the
    panel footnote says so rather than implying a historical snapshot.
    """
    items = list(
        Item.objects.filter(
            Q(attention_reminder_sent_at__isnull=False)
            | Q(price_drop_reminder_sent_at__isnull=False),
        ).select_related("seller", "category")
    )
    if not items:
        return []

    seller_ids = {item.seller_id for item in items}
    preferences = {
        preference.user_id: preference
        for preference in NotificationPreference.objects.filter(
            user_id__in=seller_ids,
        ).select_related('user')
    }
    # quiet_hours_active 需要一个真实的 User 才能查询偏好表，所以这里直接把
    # select_related 带上来的 user 复用掉，既保持真实用户对象，也避免每个
    # 卖家再查一次数据库。
    quiet_sellers = {
        seller_id for seller_id, preference in preferences.items()
        if quiet_hours_active(preference.user, now=now)
    }

    blocked_preference = 0
    blocked_quiet = 0
    for item in items:
        preference = preferences.get(item.seller_id)
        if preference is not None and not getattr(preference, "lifecycle_reminder", True):
            blocked_preference += 1
            continue
        if item.seller_id in quiet_sellers:
            blocked_quiet += 1
            continue

    return [
        {
            "key": "preference",
            "label": "关闭了生命周期提醒",
            "items": blocked_preference,
            "note": "偏好一旦关闭，卖家不会再收到任何一条生命周期提醒",
        },
        {
            "key": "quiet",
            "label": "处于免打扰时段",
            "items": blocked_quiet,
            "note": "只推迟不取消，时段过后仍会收到",
        },
        {
            "key": "no_evidence",
            "label": "缺少可引用的价格证据",
            "items": _count_missing_price_evidence(items),
            "note": "同类样本不足或价格未高于常见区间时，价格提醒不会发送",
        },
    ]


def _count_missing_price_evidence(items):
    """How many reminded listings still lack usable comparable-price evidence.

    The evidence rule lives in the reminder module, so this reuses its
    candidate selection instead of re-deriving the comparable range. Anything
    the reminder module would drop is counted here, which keeps the two views
    from disagreeing about what enough evidence means.
    """
    with_price = [item for item in items if item.price_drop_reminder_sent_at is not None]
    if not with_price:
        return 0
    try:
        evidenced = {item.pk for item, _insight in _price_candidates(now=timezone.now())}
    except Exception:
        return 0
    return sum(1 for item in with_price if item.pk not in evidenced)

def _build_recommendations(*, reminded_items, kind_rows, reached, read, acted, blocked_rows):
    """Turn the outcome into the few things an operator can act on.

    Every row states the threshold that produced it, so the panel stays honest
    when nothing crosses a line. The wording also stays observational: it says
    what was seen, not that the reminder caused it.
    """
    rows = []
    if not reminded_items:
        return rows
    if reached < reminded_items:
        rows.append(
            f"有 {reminded_items - reached} 件被提醒的商品在通知中心找不到对应的提醒记录，"
            "通常是提醒任务在写入通知前就被中断，建议检查调度是否正常执行。"
        )
    if read / reminded_items < 0.5:
        rows.append(
            f"只有 {read} / {reminded_items} 件被提醒的商品对应的通知被打开过，"
            "说明卖家没有注意到提醒；可以先调整发送时段，或把提醒入口做成未读角标。"
        )
    price_row = next((row for row in kind_rows if row['key'] == 'price'), None)
    attention_row = next((row for row in kind_rows if row['key'] == 'attention'), None)
    if price_row and attention_row and price_row['items'] and attention_row['items']:
        if price_row['action_rate'] > attention_row['action_rate']:
            rows.append(
                f"引用同类区间的价格提醒行动率 {price_row['action_rate']}%，"
                f"高于只提示待处理的 {attention_row['action_rate']}%，"
                "带证据的提醒看起来更有效，可以扩大价格参考的覆盖范围。"
            )
        elif attention_row['action_rate'] > price_row['action_rate']:
            rows.append(
                f"只提示待处理的提醒行动率 {attention_row['action_rate']}%，"
                f"高于价格参考的 {price_row['action_rate']}%，"
                "后者的同类样本可能不足，先补充样本再考虑加大发送。"
            )
    for row in blocked_rows:
        if row['items'] >= 5:
            rows.append(
                f"有 {row['items']} 件已提醒商品当前仍满足「{row['label']}」条件，"
                f"这部分卖家实际上没有真正收到提醒（{row['note']}）。"
            )
    return rows

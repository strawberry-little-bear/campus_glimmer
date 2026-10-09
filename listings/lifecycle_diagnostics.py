"""Seller-facing listing lifecycle diagnostics.

The operations dashboard answers questions for operators; this module answers
the question a seller actually asks after publishing: what should I do with
this listing now?  Every conclusion comes from explicit, inspectable rules
over public engagement signals (browsing, favourites, orders) and the
seller's own lifecycle fields (display deadline, refresh history).  There is no
opaque score, and thin evidence is reported as thin instead of being graded.
"""

import math
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import BrowsingHistory, Favorite, Item, Order


FRESH_DAYS = 7
SLOWING_DAYS = 21
STALE_DAYS = 45
EXPIRING_WINDOW_DAYS = 3
MIN_REFRESH_GAP_HOURS = 24
MAX_REFRESH_PER_MONTH = 4
ATTENTION_REMINDER_COOLDOWN_DAYS = 7
PRICE_DROP_REMINDER_COOLDOWN_DAYS = 14

LEVELS = (
    ('fresh', '新鲜在售', 'positive'),
    ('active', '正常在售', 'positive'),
    ('slowing', '关注放缓', 'warning'),
    ('stale', '沉底较久', 'warning'),
    ('expiring', '即将到期', 'warning'),
    ('archived', '已下架或已成交', 'neutral'),
)
LEVEL_LABELS = dict((key, label) for key, label, _tone in LEVELS)

# 需要卖家尽快做决策的等级，运营看板也按这一组计数。
ATTENTION_LEVELS = ('expiring', 'stale', 'slowing')


@dataclass(frozen=True)
class ListingSignal:
    """Public engagement signals aggregated for one listing."""

    views: int = 0
    favorites: int = 0
    orders: int = 0
    completed_orders: int = 0


@dataclass(frozen=True)
class LifecycleDiagnosis:
    """Explainable conclusion for a single listing."""

    item_id: int
    title: str
    status: str
    trade_mode: str
    price: Decimal
    level: str
    level_label: str
    tone: str
    age_days: int
    days_until_expiry: int
    refresh_count: int
    can_refresh: bool
    refresh_block_reason: str
    signals: ListingSignal
    reasons: tuple = field(default_factory=tuple)
    actions: tuple = field(default_factory=tuple)

    @property
    def needs_attention(self):
        return self.level in ATTENTION_LEVELS


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _collect_signals(items, *, now):
    """Aggregate engagement signals for the given listings.

    Each signal is one grouped query, so a seller page with many listings does
    not turn into one query per listing.
    """
    item_ids = [item.pk for item in items]
    if not item_ids:
        return {}

    views = {
        row['item_id']: row['total'] or 0
        for row in BrowsingHistory.objects.filter(item_id__in=item_ids)
        .values('item_id')
        .annotate(total=Sum('view_count'))
    }
    favorites = dict(
        Favorite.objects.filter(item_id__in=item_ids)
        .values('item_id')
        .annotate(total=Count('id'))
        .values_list('item_id', 'total')
    )
    orders = dict(
        Order.objects.filter(item_id__in=item_ids)
        .values('item_id')
        .annotate(total=Count('id'))
        .values_list('item_id', 'total')
    )
    completed = dict(
        Order.objects.filter(item_id__in=item_ids, status__in=['completed', 'returned'])
        .values('item_id')
        .annotate(total=Count('id'))
        .values_list('item_id', 'total')
    )
    return {
        item_id: ListingSignal(
            views=views.get(item_id, 0),
            favorites=favorites.get(item_id, 0),
            orders=orders.get(item_id, 0),
            completed_orders=completed.get(item_id, 0),
        )
        for item_id in item_ids
    }


def _days_until(expires_at, now):
    """Whole days left before a deadline; ``None`` means no deadline.

    Any remaining time counts as a full day, so a deadline 30 hours away
    reads as 2 days rather than 1.  A deadline that has already passed reads
    as 0, which the caller treats as "needs attention" rather than "gone".
    """
    if not expires_at:
        return None
    delta = expires_at - now
    if delta.total_seconds() <= 0:
        return 0
    return math.ceil(delta.total_seconds() / 86400)


def _refresh_state(item, *, now):
    """Whether the seller may refresh this listing right now, and why not."""
    if item.status != 'available':
        return False, '只有仍在展示的商品可以刷新。'
    if item.refresh_count >= MAX_REFRESH_PER_MONTH:
        return False, f'本月刷新次数已达上限（{MAX_REFRESH_PER_MONTH} 次），避免频繁刷屏。'
    if item.last_refreshed_at and item.last_refreshed_at > now - timedelta(hours=MIN_REFRESH_GAP_HOURS):
        return False, f'两次刷新至少间隔 {MIN_REFRESH_GAP_HOURS} 小时。'
    return True, ''


def _level_for(item, *, now):
    """Classify a listing with ordered, explicit rules.

    Returns ``(level, label, tone, reasons, actions)``.  Terminal states are
    checked first so the most specific explanation wins, and every branch
    states the evidence it used in plain language.
    """
    age_days = item.freshness_age_days
    days_left = _days_until(item.expires_at, now)

    if item.status in {'sold', 'reserved'}:
        return (
            'archived', LEVEL_LABELS['archived'], 'neutral',
            ('商品已经进入交易流程，不需要再调整展示策略。',),
            ('如需重新上架，可以先取消当前预约或重新发布。',),
        )

    if item.status == 'expired':
        return (
            'archived', LEVEL_LABELS['archived'], 'neutral',
            ('商品已超过展示截止时间，系统会在下次维护时自动下架。',),
            ('编辑商品并延长展示截止时间后即可重新上架。',),
        )

    if days_left == 0:
        # 截止时间已过但维护任务还没跑到：商品仍在展示，却随时会被下架。
        # 这里按“即将到期”处理，让卖家还有机会延长展示时间。
        return (
            'expiring', LEVEL_LABELS['expiring'], 'warning',
            ('展示截止时间已到，系统会在下次维护时自动下架。',),
            ('如果还想继续展示，请先延长展示截止时间。',),
        )

    if days_left is not None and days_left <= EXPIRING_WINDOW_DAYS:
        return (
            'expiring', LEVEL_LABELS['expiring'], 'warning',
            (f'距离展示截止还有 {days_left} 天，到期后会自动下架。',),
            ('不打算继续展示可以保持不变，否则请延长展示截止时间。',),
        )

    if age_days >= STALE_DAYS:
        return (
            'stale', LEVEL_LABELS['stale'], 'warning',
            (f'距上次发布或刷新已 {age_days} 天，在售列表里基本已经沉底。',),
            ('补充实拍图、更新描述后再刷新一次，比反复发布新商品更有效。',),
        )

    if age_days >= SLOWING_DAYS:
        return (
            'slowing', LEVEL_LABELS['slowing'], 'warning',
            (f'发布已 {age_days} 天，新鲜度带来的曝光已经明显下降。',),
            ('可以先刷新一次让商品回到前排，再观察一周是否有新的浏览和收藏。',),
        )

    if age_days >= FRESH_DAYS:
        return (
            'active', LEVEL_LABELS['active'], 'positive',
            (f'发布 {age_days} 天，仍处于正常曝光周期内。',),
            ('保持图片和描述完整，有预约时及时确认。',),
        )

    return (
        'fresh', LEVEL_LABELS['fresh'], 'positive',
        (f'发布 {age_days} 天，仍处在最有曝光的新鲜期。',),
        ('及时回复留言和预约，新鲜期的响应速度最影响成交。',),
    )


def diagnose_item(item, signals=None, *, now=None):
    """Diagnose one listing, reusing signals when the caller already has them."""
    now = now or timezone.now()
    if signals is None:
        signals = _collect_signals([item], now=now).get(item.pk, ListingSignal())
    level, label, tone, reasons, actions = _level_for(item, now=now)
    can_refresh, block_reason = _refresh_state(item, now=now)
    return LifecycleDiagnosis(
        item_id=item.pk,
        title=item.title,
        status=item.status,
        trade_mode=item.trade_mode,
        price=_money(item.price),
        level=level,
        level_label=label,
        tone=tone,
        age_days=item.freshness_age_days,
        days_until_expiry=_days_until(item.expires_at, now),
        refresh_count=item.refresh_count,
        can_refresh=can_refresh,
        refresh_block_reason=block_reason,
        signals=signals,
        reasons=tuple(reasons),
        actions=tuple(actions),
    )


def build_seller_lifecycle(seller, *, now=None):
    """Diagnose every listing owned by ``seller``.

    Listings that need a decision come first: expiring and stale ones lead,
    healthy ones follow.  ``summary`` carries the counts shown as tiles and
    reused by the operations dashboard.
    """
    now = now or timezone.now()
    items = list(
        Item.objects.filter(seller=seller)
        .select_related('category', 'location', 'campaign')
        .prefetch_related('images')
        .order_by('-created_at')
    )
    signals = _collect_signals(items, now=now)
    diagnoses = [diagnose_item(item, signals.get(item.pk), now=now) for item in items]

    rank = {level: index for index, level in enumerate(
        ('expiring', 'stale', 'slowing', 'active', 'fresh', 'archived'),
    )}
    diagnoses.sort(key=lambda row: (rank.get(row.level, 99), -row.age_days, row.item_id))

    counts = {level: 0 for level, _label, _tone in LEVELS}
    for row in diagnoses:
        counts[row.level] = counts.get(row.level, 0) + 1

    attention = [row for row in diagnoses if row.needs_attention]
    return {
        'diagnoses': diagnoses,
        'attention': attention,
        'summary': {
            'total': len(diagnoses),
            'attention': len(attention),
            'fresh': counts['fresh'],
            'active': counts['active'],
            'slowing': counts['slowing'],
            'stale': counts['stale'],
            'expiring': counts['expiring'],
            'archived': counts['archived'],
            'refreshable': sum(1 for row in diagnoses if row.can_refresh),
        },
    }


def listings_due_for_attention_reminder(*, now=None, cooldown_days=ATTENTION_REMINDER_COOLDOWN_DAYS):
    """Listings whose seller should be nudged once per cooldown window."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=cooldown_days)
    candidates = Item.objects.filter(status='available').filter(
        Q(attention_reminder_sent_at__isnull=True) | Q(attention_reminder_sent_at__lte=cutoff),
    ).select_related('seller')
    due = []
    for item in candidates:
        if item.is_expired or item.freshness_age_days < SLOWING_DAYS:
            continue
        signals = _collect_signals([item], now=now).get(item.pk, ListingSignal())
        level, _label, _tone, _reasons, _actions = _level_for(item, now=now)
        if level in ATTENTION_LEVELS:
            due.append((item, signals))
    return due


def listings_due_for_price_drop_reminder(*, now=None, cooldown_days=PRICE_DROP_REMINDER_COOLDOWN_DAYS):
    """Listings stale long enough that a price review is the honest suggestion."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=cooldown_days)
    candidates = Item.objects.filter(
        status='available', trade_mode='sale', price__gt=0,
    ).filter(
        Q(price_drop_reminder_sent_at__isnull=True) | Q(price_drop_reminder_sent_at__lte=cutoff),
    ).select_related('seller', 'category')
    due = []
    for item in candidates:
        if item.is_expired or item.freshness_age_days < STALE_DAYS:
            continue
        signals = _collect_signals([item], now=now).get(item.pk, ListingSignal())
        if signals.orders:
            continue
        due.append((item, signals))
    return due

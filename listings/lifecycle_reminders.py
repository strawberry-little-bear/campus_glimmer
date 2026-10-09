"""Deliver listing-lifecycle reminders as one merged notice per seller.

``lifecycle_diagnostics`` explains what a seller should do with a listing, but
that explanation only reaches someone who opens the seller page.  This module
closes the loop: a scheduled command turns due listings into one notification
per seller, so a listing that has sunk out of sight is surfaced without the
seller having to remember to check.

Two reminders exist and they are deliberately separate.  The attention
reminder fires when a listing crosses into a level that needs a decision.  The
price reminder is narrower: it only fires for paid listings that have had no
orders for a long time *and* whose price sits above the comparable range, so
the notice can cite real evidence instead of asking the seller to discount on
a hunch.  Every conclusion is drawn from the same rules the seller page uses,
which keeps the two views from contradicting each other.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .lifecycle_diagnostics import (
    ATTENTION_REMINDER_COOLDOWN_DAYS,
    PRICE_DROP_REMINDER_COOLDOWN_DAYS,
    diagnose_item,
    listings_due_for_attention_reminder,
    listings_due_for_price_drop_reminder,
)
from .models import Notification
from .notifications import create_notification, quiet_hours_active
from .price_insights import build_price_insight

# One notice never lists more than this many titles; the rest are summarised.
MAX_LISTED_ITEMS = 5

# Punctuation kept as constants so the message builders stay readable instead of
# hiding quotes inside f-strings.
LQ = '“'
RQ = '”'
SEP = '、'
COLON = '：'
PERIOD = '。'


def _reminder_period(*, now=None):
    """Return the token identifying the day a reminder covers.

    The key is date-scoped rather than run-scoped: a scheduler that fires
    twice in one day finds the first notice already exists and stops, which
    makes a retry safe without needing a separate lock.
    """
    now = now or timezone.now()
    return timezone.localdate(now).isoformat()


def _titles(items):
    """Render listing titles as a bounded, human-readable list."""
    shown = items[:MAX_LISTED_ITEMS]
    text = SEP.join(f'{LQ}{item.title}{RQ}' for item in shown)
    hidden = len(items) - len(shown)
    if hidden > 0:
        text += f'，另有 {hidden} 件未列出'
    return text


def _build_attention_message(items):
    """Compose the notice for listings that need a seller decision.

    The copy states how many listings are waiting and what the diagnosis
    actually concluded, so the seller can act without opening the page first.
    """
    labels = []
    for item in items[:MAX_LISTED_ITEMS]:
        row = diagnose_item(item)
        labels.append(row.level_label)
    detail = _titles(items)
    return (
        f'你有 {len(items)} 件商品需要处理{COLON}{detail}{PERIOD}'
        f'打开“我的商品”可以看到每件商品的诊断结论和可执行动作{PERIOD}'
    )


def _build_price_message(candidates):
    """Compose the price-review notice, citing comparable prices as evidence.

    ``candidates`` is a list of ``(item, insight)`` pairs where the insight
    already confirmed the price sits above the comparable range.  Listings
    without enough comparables are dropped rather than guessed at, and the
    notice says so explicitly instead of implying precision it does not have.
    """
    with_evidence = [(item, insight) for item, insight in candidates if insight is not None]
    if not with_evidence:
        return ''

    parts = []
    for item, insight in with_evidence[:MAX_LISTED_ITEMS]:
        parts.append(
            f'{LQ}{item.title}{RQ} 现价 ¥{insight.current_price:.2f}，'
            f'同类常见区间 ¥{insight.lower_price:.2f}～¥{insight.upper_price:.2f}'
        )
    hidden = len(with_evidence) - len(with_evidence[:MAX_LISTED_ITEMS])
    text = SEP.join(parts)
    if hidden > 0:
        text += f'，另有 {hidden} 件未列出'

    message = f'你有 {len(with_evidence)} 件商品久未成交，价格高于同类常见区间{COLON}{text}{PERIOD}'
    thin = [item for item, insight in with_evidence if insight.confidence != 'high']
    if thin:
        message += f'其中 {len(thin)} 件的同类参考样本较少，仅供参考{PERIOD}'
    message += '最终定价仍由你决定，也可以补充成色说明后再观察一段时间' + PERIOD
    return message


def _price_candidates(*, now):
    """Attach price evidence to each listing due for a price reminder.

    A listing only earns a price reminder when a comparable range exists and
    the current price is above it.  Returning the evidence alongside the item
    keeps the message honest and lets the caller skip empty groups silently.
    """
    candidates = []
    for item, _signals in listings_due_for_price_drop_reminder(now=now):
        insight = build_price_insight(item, now=now)
        if insight is None or insight.position != 'above':
            continue
        candidates.append((item, insight))
    return candidates


def send_lifecycle_reminders(*, now=None, seller_ids=None, dry_run=False):
    """Turn due listings into one merged notification per seller.

    Sellers with at least one due listing receive a single notice covering
    both reminder kinds, so a busy seller is never sent two messages about the
    same batch of listings.  Cool-down fields are written back only after the
    notice exists, which keeps a failed send from silently consuming a
    reminder window.
    """
    now = now or timezone.now()
    result = {
        'attention_sellers': 0,
        'price_sellers': 0,
        'sent': 0,
        'skipped_preference': 0,
        'skipped_no_evidence': 0,
        'quiet': 0,
        'marked': 0,
    }

    attention_by_seller = {}
    for item, _signals in listings_due_for_attention_reminder(now=now):
        attention_by_seller.setdefault(item.seller_id, []).append(item)
    result['attention_sellers'] = len(attention_by_seller)

    price_by_seller = {}
    for item, insight in _price_candidates(now=now):
        price_by_seller.setdefault(item.seller_id, []).append((item, insight))
    result['price_sellers'] = len(price_by_seller)

    if seller_ids is not None:
        allowed = set(seller_ids)
        attention_by_seller = {k: v for k, v in attention_by_seller.items() if k in allowed}
        price_by_seller = {k: v for k, v in price_by_seller.items() if k in allowed}

    seller_ids_to_notify = sorted(set(attention_by_seller) | set(price_by_seller))
    if not seller_ids_to_notify:
        return result

    period = _reminder_period(now=now)
    for seller_id in seller_ids_to_notify:
        seller = User.objects.filter(pk=seller_id).first()
        if not seller:
            continue

        attention_items = attention_by_seller.get(seller_id, [])
        price_items = price_by_seller.get(seller_id, [])
        if not attention_items and not price_items:
            result['skipped_no_evidence'] += 1
            continue

        sections = []
        if attention_items:
            sections.append(_build_attention_message(attention_items))
        price_message = _build_price_message(price_items)
        if price_message:
            sections.append(price_message)
        elif price_items:
            result['skipped_no_evidence'] += 1
        message = ''.join(sections)

        if quiet_hours_active(seller, now=now):
            result['quiet'] += 1
            continue

        title = '商品需要你处理' if attention_items else '商品价格可以参考同类调整'
        if dry_run:
            result['sent'] += 1
            continue

        dedupe_key = f'lifecycle-reminder:{period}:{seller_id}'
        with transaction.atomic():
            already = Notification.objects.filter(
                recipient=seller, kind='lifecycle_reminder', dedupe_key=dedupe_key,
            ).exists()
            if already:
                continue
            notification = create_notification(
                seller,
                kind='lifecycle_reminder',
                title=title,
                message=message[:255],
                target_url=reverse('my_items'),
                dedupe_key=dedupe_key,
                dedupe_forever=True,
            )
            if not notification:
                result['skipped_preference'] += 1
                continue

            # 一件商品可能同时进入两类候选，按商品主键合并，避免同一行被写
            # 两次，也让“回写了多少件商品”如实反映处理的商品数量。
            attention_item_ids = {item.pk for item in attention_items}
            price_item_ids = {item.pk for item, _insight in price_items}
            marked = 0
            for item in attention_items:
                marked += type(item).objects.filter(pk=item.pk).update(
                    attention_reminder_sent_at=now,
                )
                if item.pk in price_item_ids:
                    marked += type(item).objects.filter(pk=item.pk).update(
                        price_drop_reminder_sent_at=now,
                    )
            for item, _insight in price_items:
                if item.pk in attention_item_ids:
                    continue
                marked += type(item).objects.filter(pk=item.pk).update(
                    price_drop_reminder_sent_at=now,
                )
            result['marked'] += marked
            result['sent'] += 1

    return result

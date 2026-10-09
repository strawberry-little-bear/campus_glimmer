"""Deliver buffered saved-search hits as one digest per cadence period.

Instant saved searches notify on publish. Daily and weekly searches buffer
every hit in SavedSearchMatch, and this module merges that buffer into a
single notice per user and period, so a busy week never turns into dozens
of near-identical notifications.
"""

from django.contrib.auth.models import User
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Notification, NotificationPreference, SavedSearch, SavedSearchMatch
from .notifications import create_notification
from .saved_searches import describe_saved_search_match

# Punctuation used by the notice copy, kept as constants so the message
# builders below stay readable instead of hiding quotes inside f-strings.
LQ = '“'
RQ = '”'
SEP = '、'
COLON = '：'
PERIOD = '。'
COMMA = '，'
LPAREN = '（'
RPAREN = '）'

# Hard ceiling so one digest never lists more than a screenful of titles.
MAX_LISTED_MATCHES = 9
# Only the freshest buffered hits are considered, keeping the query bounded.
MAX_SCANNED_MATCHES = 20


def _digest_period(frequency, *, today=None):
    """Return the dedupe token identifying the period a digest covers."""
    today = today or timezone.localdate()
    if frequency == 'weekly':
        iso_year, iso_week, _ = today.isocalendar()
        return f'{iso_year}-W{iso_week:02d}'
    return today.isoformat()


def _candidate_user_ids():
    """Return the users owning at least one active digest-cadence saved search."""
    return set(
        SavedSearch.objects.filter(
            is_active=True, notify_frequency__in=['daily', 'weekly'],
        ).values_list('user_id', flat=True)
    )


def _clamp_limit(saved_search):
    """Clamp the per-notice title budget into the supported 1-9 range."""
    limit = saved_search.max_matches_per_notice or 3
    return max(1, min(limit, MAX_LISTED_MATCHES))


def _pending_matches(saved_search):
    """Return the not-yet-delivered buffered hits, oldest hit first."""
    return list(
        SavedSearchMatch.objects.filter(
            saved_search=saved_search, notified_at__isnull=True,
        )
        .select_related('item', 'item__category', 'item__location')
        .order_by('matched_at', 'pk')[:MAX_SCANNED_MATCHES]
    )


def _build_section(saved_search, matches, *, unit, with_reason=False):
    """Compose the notice text describing one saved search's buffered hits.

    with_reason appends the dimensions that made the first hit match, which
    keeps a single-search digest self-explanatory without bloating the merged
    notice a user with several active searches receives.
    """
    shown = matches[:_clamp_limit(saved_search)]
    titles = SEP.join(f'{LQ}{match.item.title}{RQ}' for match in shown)
    hidden = len(matches) - len(shown)

    section = f'{LQ}{saved_search.name}{RQ} 在本{unit}有 {len(matches)} 条新命中{COLON}{titles}'
    if with_reason and shown:
        reasons = describe_saved_search_match(saved_search, shown[0].item)[:2]
        if reasons:
            reason_text = SEP.join(reasons)
            section += f'{LPAREN}命中原因{COLON}{reason_text}{RPAREN}'
    if hidden > 0:
        section += f'{COMMA}另有 {hidden} 条未展示'
    return section + PERIOD


def send_saved_search_digest(*, frequency=None, today=None, user_ids=None):
    """Merge buffered saved-search hits into one notice per user and period.

    Daily and weekly searches are handled separately so a weekly user never
    receives seven copies of the same summary. Each notice carries a
    period-scoped dedupe key, which makes a rerun of the scheduler safe: the
    second run finds no pending hits and simply reports zero sends.
    """
    frequencies = [frequency] if frequency else ['daily', 'weekly']
    today = today or timezone.localdate()
    candidate_ids = set(user_ids) if user_ids is not None else _candidate_user_ids()
    result = {'sent': 0, 'skipped': 0, 'empty': 0, 'quiet': 0}

    for cadence in frequencies:
        period = _digest_period(cadence, today=today)
        searches = (
            SavedSearch.objects.filter(
                is_active=True, notify_frequency=cadence, user_id__in=candidate_ids,
            )
            .select_related('user', 'category', 'location')
            .order_by('user_id', 'pk')
        )
        by_user = {}
        for saved_search in searches:
            by_user.setdefault(saved_search.user_id, []).append(saved_search)

        for user_id, user_searches in by_user.items():
            user = User.objects.filter(pk=user_id).first()
            if not user:
                continue
            preference, _ = NotificationPreference.objects.get_or_create(user=user)
            if not preference.saved_search_digest:
                result['skipped'] += len(user_searches)
                continue

            pending = []
            for saved_search in user_searches:
                if saved_search.is_quiet:
                    result['quiet'] += 1
                    continue
                matches = _pending_matches(saved_search)
                if not matches:
                    result['empty'] += 1
                    continue
                pending.append((saved_search, matches))

            if not pending:
                continue

            unit = '周' if cadence == 'weekly' else '日'
            if len(pending) == 1:
                saved_search, matches = pending[0]
                message = _build_section(saved_search, matches, unit=unit, with_reason=True)
            else:
                total = sum(len(matches) for _, matches in pending)
                sections = ''.join(
                    _build_section(saved_search, matches, unit=unit)
                    for saved_search, matches in pending
                )
                message = (
                    f'本{unit}你的 {len(pending)} 个关注条件'
                    f'共有 {total} 条新命中{PERIOD}{sections}'
                )
            message += '打开关注搜索查看详情' + PERIOD
            dedupe_key = f'saved-search-digest:{cadence}:{period}:{user_id}'

            with transaction.atomic():
                already = Notification.objects.filter(
                    recipient=user, kind='saved_search_digest', dedupe_key=dedupe_key,
                ).exists()
                if already:
                    result['skipped'] += len(pending)
                    continue
                notification = create_notification(
                    user,
                    kind='saved_search_digest',
                    title='关注搜索汇总提醒',
                    message=message[:255],
                    target_url=reverse('saved_search_list'),
                    dedupe_key=dedupe_key,
                    dedupe_forever=True,
                )
                if not notification:
                    continue
                now = timezone.now()
                for saved_search, _ in pending:
                    SavedSearchMatch.objects.filter(
                        saved_search=saved_search, notified_at__isnull=True,
                    ).update(notified_at=now)
            result['sent'] += 1

    return result


def deliver_buffered_saved_search_matches(*, saved_search):
    """Flush a saved search's buffered hits as one immediate notification.

    Used when a user switches a saved search from a digest cadence back to
    instant: hits that arrived during the digest window would otherwise sit in
    the buffer until the next scheduled run. Returns how many items were
    notified so the caller can explain what happened.
    """
    matches = _pending_matches(saved_search)
    if not matches:
        return 0

    shown = matches[:_clamp_limit(saved_search)]
    titles = SEP.join(f'{LQ}{match.item.title}{RQ}' for match in shown)
    hidden = len(matches) - len(shown)
    message = (
        f'你的关注{LQ}{saved_search.name}{RQ} 在切换节奏前'
        f'累计了 {len(matches)} 条命中{COLON}{titles}'
    )
    if hidden > 0:
        message += f'{COMMA}另有 {hidden} 条未展示'
    message += PERIOD

    notification = create_notification(
        saved_search.user,
        kind='saved_search_match',
        title='补送暂停期间的关注命中',
        message=message[:255],
        target_url=reverse('saved_search_list'),
        dedupe_key=f'saved-search-flush:{saved_search.pk}:{matches[0].pk}',
        dedupe_forever=True,
    )
    if not notification:
        return 0

    SavedSearchMatch.objects.filter(
        saved_search=saved_search, notified_at__isnull=True,
    ).update(notified_at=timezone.now())
    return len(matches)

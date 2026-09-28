from datetime import date
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .analytics import build_operations_dashboard
from .models import Notification, NotificationPreference


DEFAULT_DIGEST_DAYS = 7


def build_operations_digest(days=DEFAULT_DIGEST_DAYS):
    """Build a compact, actionable summary from the operations dashboard."""
    dashboard = build_operations_dashboard(days)
    alerts = dashboard['operational_alerts']
    if not alerts:
        return None

    top_alerts = alerts[:3]
    alert_titles = '、'.join(alert['title'] for alert in top_alerts)
    suffix = f' 等 {len(alerts)} 项' if len(alerts) > len(top_alerts) else ''
    message = f"最近 {days} 天发现 {alert_titles}{suffix}，建议及时打开运营看板处理。"
    return {
        'dashboard': dashboard,
        'alerts': alerts,
        'title': '运营告警日报',
        'message': message[:255],
        'target_url': f"{reverse('operations_dashboard')}?{urlencode({'days': days})}",
    }


def send_operations_digest(*, days=DEFAULT_DIGEST_DAYS, today=None):
    """Send one idempotent daily digest to active staff members."""
    digest = build_operations_digest(days)
    if not digest:
        return {'sent': 0, 'skipped': 0, 'alerts': 0, 'reason': 'no_alerts'}

    digest_date = today or timezone.localdate()
    dedupe_key = f'operations-digest:{digest_date.isoformat()}:{days}'
    User = get_user_model()
    sent = 0
    skipped = 0

    with transaction.atomic():
        staff_users = User.objects.filter(is_active=True, is_staff=True).order_by('pk')
        for user in staff_users:
            preference, _ = NotificationPreference.objects.get_or_create(user=user)
            if not preference.operations_digest:
                skipped += 1
                continue
            if Notification.objects.filter(
                recipient=user,
                kind='operations_digest',
                dedupe_key=dedupe_key,
            ).exists():
                skipped += 1
                continue
            Notification.objects.create(
                recipient=user,
                kind='operations_digest',
                title=digest['title'],
                message=digest['message'],
                target_url=digest['target_url'],
                dedupe_key=dedupe_key,
                last_occurred_at=timezone.now(),
            )
            sent += 1

    return {'sent': sent, 'skipped': skipped, 'alerts': len(digest['alerts']), 'reason': 'sent'}

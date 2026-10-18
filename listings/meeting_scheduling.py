from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .campus_location_service_hours import (
    evaluate_service_windows, get_active_service_exceptions,
    get_active_service_windows,
)
from .models import CampusLocation, MeetingAppointment


@dataclass(frozen=True)
class MeetingLocationRecommendation:
    """A transparent, rule-based delivery location suggestion."""

    location: CampusLocation
    score: int
    reasons: tuple[str, ...]
    safety_note: str
    pair_history_count: int = 0
    recent_usage_count: int = 0
    service_state: str = 'unregistered'
    service_hours_text: str = '未登记每周开放时段'


@dataclass(frozen=True)
class MeetingTimeRecommendation:
    """A conflict-free delivery time suggestion for both parties."""

    start_at: datetime
    end_at: datetime
    score: int
    reasons: tuple[str, ...]

    @property
    def start_input(self):
        return timezone.localtime(self.start_at).strftime('%Y-%m-%dT%H:%M')

    @property
    def end_input(self):
        return timezone.localtime(self.end_at).strftime('%Y-%m-%dT%H:%M')


def find_appointment_conflicts(*, user, start_at, end_at, exclude_order_id=None, lock=False):
    """Return active delivery appointments that overlap a candidate time window."""
    appointments = MeetingAppointment.objects.filter(
        status__in={'pending', 'confirmed'},
        start_at__lt=end_at,
        end_at__gt=start_at,
    ).filter(
        Q(order__buyer=user) | Q(order__seller=user),
    ).select_related('order__item', 'order__buyer', 'order__seller')
    if exclude_order_id is not None:
        appointments = appointments.exclude(order_id=exclude_order_id)
    appointments = appointments.order_by('start_at', 'id')
    return appointments.select_for_update(of=('self',)) if lock else appointments


def _location_safety_note(location):
    if location.is_public:
        return location.safety_note or '建议选择人流较多、光线充足的公共区域，并避免单独前往偏僻地点。'
    return location.safety_note or '该地点未标记为公共交付区域，建议改约到人流较多且光线充足的校园地点。'


def recommend_meeting_locations(
    *, order, limit=3, target_start_at=None, target_end_at=None,
):
    """Recommend locations using item context, pair history, and campus usage.

    The score is intentionally explainable rather than opaque: every returned
    recommendation includes the rules that contributed to its ranking.
    """
    item_location_id = getattr(order.item, 'location_id', None)
    participant_ids = {order.buyer_id, order.seller_id}

    locations = list(
        CampusLocation.objects.filter(is_active=True)
        .prefetch_related('service_windows', 'service_exceptions__exception_windows')
        .order_by('sort_order', 'name')
    )
    if not locations:
        return []
    location_ids = [location.id for location in locations]

    confirmed = MeetingAppointment.objects.filter(
        status='confirmed',
        location_id__in=location_ids,
    )
    pair_filter = (
        Q(order__buyer_id=order.buyer_id, order__seller_id=order.seller_id)
        | Q(order__buyer_id=order.seller_id, order__seller_id=order.buyer_id)
    )
    pair_counts = {
        row['location_id']: row['total']
        for row in confirmed.filter(pair_filter).values('location_id').annotate(total=Count('id'))
    }
    participant_counts = {
        row['location_id']: row['total']
        for row in confirmed.filter(
            Q(order__buyer_id__in=participant_ids) | Q(order__seller_id__in=participant_ids)
        ).values('location_id').annotate(total=Count('id'))
    }
    recent_confirmed = confirmed.filter(updated_at__gte=timezone.now() - timedelta(days=90))
    usage_counts = {
        row['location_id']: row['total']
        for row in recent_confirmed.values('location_id').annotate(total=Count('id'))
    }

    recommendations = []
    for location in locations:
        pair_count = pair_counts.get(location.id, 0)
        participant_count = participant_counts.get(location.id, 0)
        usage_count = usage_counts.get(location.id, 0)
        service_windows = get_active_service_windows(location)
        service_exceptions = get_active_service_exceptions(location)
        service_evaluation = evaluate_service_windows(
            service_windows,
            start_at=target_start_at,
            end_at=target_end_at,
            exceptions=service_exceptions,
        )
        reasons = []
        score = 0

        if location.id == item_location_id:
            score += 45
            reasons.append('商品发布地点，减少双方额外移动')
        if pair_count:
            score += min(pair_count * 18, 36)
            reasons.append(f'你们曾在这里完成 {pair_count} 次交付')
        elif participant_count:
            score += min(participant_count * 5, 15)
            reasons.append('双方至少一人曾在这里完成过交付')
        if usage_count:
            score += min(usage_count * 2, 12)
            reasons.append(f'近期有 {usage_count} 次已确认交付')
        if location.is_public:
            score += 10
            reasons.append('公共区域，更适合当面核验')
        else:
            reasons.append('请优先确认现场安全和开放时间')
        if target_start_at is not None or target_end_at is not None:
            if service_evaluation.state == 'open':
                score += 12
                if service_evaluation.has_exception:
                    reasons.append('当前安排落在当天例外开放时段内')
                else:
                    reasons.append('当前安排落在登记开放时段内')
            elif service_evaluation.state == 'closed':
                score = max(0, score - 10)
                if service_evaluation.closed_exceptions:
                    reasons.append('交付日期地点临时关闭，请调整日期、时间或地点')
                elif service_evaluation.has_exception:
                    reasons.append('当前安排不在当天替换时段内，请核对单日开放例外')
                else:
                    reasons.append('当前安排不在登记开放时段，请确认临时开放或调整时间')
            else:
                reasons.append('未登记开放时段，请自行确认现场安排')
        elif service_evaluation.is_registered:
            score += 5
            reasons.append('已登记每周开放时段')
        else:
            reasons.append('未登记开放时段，请自行确认现场安排')
        if not reasons:
            reasons.append('校园内可用的交付地点')

        recommendations.append(
            MeetingLocationRecommendation(
                location=location,
                score=score,
                reasons=tuple(reasons),
                safety_note=_location_safety_note(location),
                pair_history_count=pair_count,
                recent_usage_count=usage_count,
                service_state=service_evaluation.state,
                service_hours_text=service_evaluation.summary,
            )
        )

    recommendations.sort(
        key=lambda recommendation: (
            -recommendation.score,
            -recommendation.pair_history_count,
            -recommendation.recent_usage_count,
            recommendation.location.sort_order,
            recommendation.location.name,
        )
    )
    return recommendations[:limit]


def recommend_meeting_times(*, order, location=None, limit=3, days=7, now=None):
    """Return upcoming one-hour slots that are free for both participants."""
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    horizon_end = now + timedelta(days=max(days, 1) + 1)
    participant_ids = {order.buyer_id, order.seller_id}
    service_windows = get_active_service_windows(location)
    service_exceptions = get_active_service_exceptions(location)
    blocked_intervals = list(
        MeetingAppointment.objects.filter(
            status__in={'pending', 'confirmed'},
            start_at__lt=horizon_end,
            end_at__gt=now,
        ).filter(
            Q(order__buyer_id__in=participant_ids) | Q(order__seller_id__in=participant_ids),
        ).exclude(order_id=order.id).values_list('start_at', 'end_at')
    )
    recommendations = []

    def has_conflict(start_at, end_at):
        return any(
            blocked_start < end_at and blocked_end > start_at
            for blocked_start, blocked_end in blocked_intervals
        )

    for day_offset in range(days):
        candidate_date = local_now.date() + timedelta(days=day_offset)
        for hour in range(9, 21):
            start_at = local_now.replace(
                year=candidate_date.year,
                month=candidate_date.month,
                day=candidate_date.day,
                hour=hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            if start_at <= now + timedelta(minutes=15):
                continue
            end_at = start_at + timedelta(hours=1)
            if has_conflict(start_at, end_at):
                continue

            reasons = ['双方当前没有其他交付预约冲突']
            score = max(1, 6 - day_offset)
            service_evaluation = evaluate_service_windows(
                service_windows,
                start_at=start_at,
                end_at=end_at,
                exceptions=service_exceptions,
            )
            if service_evaluation.state == 'open':
                score += 6
                if service_evaluation.has_exception:
                    reasons.append('落在交付地点当天例外开放时段内')
                else:
                    reasons.append('落在交付地点登记开放时段内')
            elif service_evaluation.state == 'closed':
                score = max(1, score - 6)
                if service_evaluation.closed_exceptions:
                    reasons.append('交付日期地点临时关闭，请调整日期、时间或地点')
                elif service_evaluation.has_exception:
                    reasons.append('不在交付地点当天替换时段内')
                else:
                    reasons.append('不在交付地点登记开放时段内')
            else:
                reasons.append('交付地点未登记开放时段，请自行确认')
            if start_at.weekday() >= 5:
                score += 2
                reasons.append('周末时段，时间安排更宽松')
            elif 17 <= start_at.hour <= 20:
                score += 5
                reasons.append('工作日课后时段，更适合当面交付')
            elif 12 <= start_at.hour < 14:
                score += 2
                reasons.append('午间时段，适合快速完成交付')
            else:
                score += 3
                reasons.append('校园日间时段，现场更容易找到公共区域')
            recommendations.append(
                MeetingTimeRecommendation(
                    start_at=start_at,
                    end_at=end_at,
                    score=score,
                    reasons=tuple(reasons),
                )
            )

    recommendations.sort(key=lambda recommendation: (-recommendation.score, recommendation.start_at))
    return recommendations[:limit]

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .models import CampusLocationServiceWindow

STATE_UNREGISTERED = 'unregistered'
STATE_OPEN = 'open'
STATE_CLOSED = 'closed'
STATE_REGISTERED = 'registered'

UNREGISTERED_SUMMARY = '未登记每周开放时段'


@dataclass(frozen=True)
class ServiceWindowEvaluation:
    """The explainable opening state of one campus location."""

    state: str
    windows: tuple[CampusLocationServiceWindow, ...]
    summary: str

    @property
    def is_registered(self):
        return bool(self.windows)

    @property
    def is_open(self):
        return self.state == STATE_OPEN


def get_active_service_windows(location):
    """Return active windows without discarding a prefetched location cache."""
    if location is None or location.pk is None:
        return tuple()
    return tuple(
        window for window in location.service_windows.all()
        if window.is_active
    )


def _local_naive(value):
    local_value = timezone.localtime(value) if timezone.is_aware(value) else value
    return local_value.replace(tzinfo=None)


def _window_interval(window, day):
    """Materialise one weekly window as a local date-time interval."""
    start = datetime.combine(day, window.start_time)
    if window.end_time > window.start_time:
        end = datetime.combine(day, window.end_time)
    else:
        end = datetime.combine(day + timedelta(days=1), window.end_time)
    return start, end


def _merged_intervals(windows, first_day, last_day):
    intervals = []
    day = first_day
    while day <= last_day:
        weekday = day.isoweekday()
        for window in windows:
            if window.weekday == weekday:
                intervals.append(_window_interval(window, day))
        day += timedelta(days=1)

    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covers_moment(intervals, moment):
    return any(start <= moment < end for start, end in intervals)


def _covers_interval(intervals, start, end):
    cursor = start
    for interval_start, interval_end in intervals:
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return False


def format_service_windows(windows):
    """Format windows as a compact weekly summary for users and admins."""
    grouped = {}
    for window in sorted(windows, key=lambda item: (item.weekday, item.start_time, item.end_time)):
        end_prefix = '次日 ' if window.end_time <= window.start_time else ''
        grouped.setdefault(window.weekday, []).append(
            f'{window.start_time:%H:%M}–{end_prefix}{window.end_time:%H:%M}'
        )
    return '；'.join(
        f'{CampusLocationServiceWindow.WEEKDAY_LABELS.get(weekday, "未知")} {"、".join(times)}'
        for weekday, times in grouped.items()
    )


def evaluate_service_windows(windows, start_at, end_at=None):
    """Evaluate a moment or interval against the union of weekly windows."""
    windows = tuple(windows)
    if not windows:
        return ServiceWindowEvaluation(
            state=STATE_UNREGISTERED, windows=windows, summary=UNREGISTERED_SUMMARY,
        )
    if start_at is None and end_at is None:
        return ServiceWindowEvaluation(
            state=STATE_REGISTERED, windows=windows, summary=format_service_windows(windows),
        )

    local_start = _local_naive(start_at)
    local_end = _local_naive(end_at) if end_at is not None else local_start
    if local_end < local_start:
        local_start, local_end = local_end, local_start

    intervals = _merged_intervals(
        windows, local_start.date() - timedelta(days=1), local_end.date() + timedelta(days=1),
    )
    if end_at is None:
        is_open = _covers_moment(intervals, local_start)
    else:
        is_open = _covers_interval(intervals, local_start, local_end)
    return ServiceWindowEvaluation(
        state=STATE_OPEN if is_open else STATE_CLOSED,
        windows=windows,
        summary=format_service_windows(windows),
    )


def evaluate_location_service_window(location, start_at, end_at=None):
    return evaluate_service_windows(
        get_active_service_windows(location), start_at=start_at, end_at=end_at,
    )

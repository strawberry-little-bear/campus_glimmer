from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .models import CampusLocationServiceException, CampusLocationServiceWindow

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
    exceptions: tuple[CampusLocationServiceException, ...] = ()

    @property
    def is_registered(self):
        return bool(self.windows)

    @property
    def is_open(self):
        return self.state == STATE_OPEN

    @property
    def has_exception(self):
        return bool(self.exceptions)

    @property
    def closed_exceptions(self):
        return tuple(
            exception for exception in self.exceptions
            if exception.exception_type == CampusLocationServiceException.CLOSED
        )

    @property
    def exception_reasons(self):
        return tuple(
            f'{exception.effective_date:%Y-%m-%d}{exception.type_label}：{exception.reason}'
            for exception in self.exceptions
        )


def get_active_service_windows(location):
    """Return active windows without discarding a prefetched location cache."""
    if location is None or location.pk is None:
        return tuple()
    return tuple(
        window for window in location.service_windows.all()
        if window.is_active
    )


def get_active_service_exceptions(location):
    """Return active date exceptions, preserving a prefetch when it exists."""
    if location is None or location.pk is None:
        return tuple()
    if 'service_exceptions' in getattr(location, '_prefetched_objects_cache', {}):
        return tuple(
            exception for exception in location.service_exceptions.all()
            if exception.is_active
        )
    return tuple(
        location.service_exceptions.filter(is_active=True)
        .prefetch_related('exception_windows')
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


def _exception_intervals(exception):
    return [
        (
            datetime.combine(exception.effective_date, window.start_time),
            datetime.combine(exception.effective_date, window.end_time),
        )
        for window in exception.exception_windows.all()
    ]


def _relevant_exceptions(exceptions, first_day, last_day):
    return tuple(
        exception for exception in exceptions
        if first_day <= exception.effective_date <= last_day
    )


def _merged_intervals(windows, first_day, last_day, exceptions=()):
    exception_by_date = {
        exception.effective_date: exception for exception in exceptions
    }
    intervals = []
    day = first_day
    while day <= last_day:
        exception = exception_by_date.get(day)
        if exception is None:
            weekday = day.isoweekday()
            next_day = day + timedelta(days=1)
            next_day_exception = exception_by_date.get(next_day)
            for window in windows:
                if window.weekday != weekday:
                    continue
                interval_start, interval_end = _window_interval(window, day)
                if next_day_exception is not None and interval_end > datetime.combine(next_day, datetime.min.time()):
                    interval_end = min(
                        interval_end,
                        datetime.combine(next_day, datetime.min.time()),
                    )
                if interval_start < interval_end:
                    intervals.append((interval_start, interval_end))
        elif exception.exception_type == CampusLocationServiceException.REPLACEMENT:
            intervals.extend(_exception_intervals(exception))
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


def format_service_exceptions(exceptions):
    """Format date exceptions without mixing them into the weekly rule."""
    summaries = []
    for exception in sorted(exceptions, key=lambda item: (item.effective_date, item.exception_type)):
        if exception.exception_type == CampusLocationServiceException.CLOSED:
            summaries.append(f'{exception.effective_date:%Y-%m-%d}临时关闭（{exception.reason}）')
            continue
        windows = list(exception.exception_windows.all())
        window_text = (
            '、'.join(window.display_text for window in windows)
            if windows else '当天无开放时段'
        )
        summaries.append(
            f'{exception.effective_date:%Y-%m-%d}替换为 {window_text}（{exception.reason}）'
        )
    return '；'.join(summaries)


def _full_summary(windows, exceptions):
    summaries = [format_service_windows(windows)]
    if exceptions:
        summaries.append(format_service_exceptions(exceptions))
    return '；'.join(summaries)


def evaluate_service_windows(windows, start_at, end_at=None, exceptions=()):
    """Evaluate a moment or interval against weekly and date-specific rules."""
    windows = tuple(windows)
    exceptions = tuple(exceptions)
    if not windows:
        return ServiceWindowEvaluation(
            state=STATE_UNREGISTERED,
            windows=windows,
            summary=UNREGISTERED_SUMMARY,
            exceptions=tuple(),
        )

    moment_source = start_at if start_at is not None else end_at
    if moment_source is None:
        return ServiceWindowEvaluation(
            state=STATE_REGISTERED,
            windows=windows,
            summary=format_service_windows(windows),
            exceptions=tuple(),
        )

    local_start = _local_naive(moment_source)
    local_end = _local_naive(end_at) if end_at is not None else local_start
    if local_end < local_start:
        local_start, local_end = local_end, local_start

    relevant_exceptions = _relevant_exceptions(
        exceptions, local_start.date(), local_end.date(),
    )
    intervals = _merged_intervals(
        windows,
        local_start.date() - timedelta(days=1),
        local_end.date() + timedelta(days=1),
        relevant_exceptions,
    )
    if end_at is None or start_at is None:
        is_open = _covers_moment(intervals, local_start)
    else:
        is_open = _covers_interval(intervals, local_start, local_end)
    return ServiceWindowEvaluation(
        state=STATE_OPEN if is_open else STATE_CLOSED,
        windows=windows,
        summary=_full_summary(windows, relevant_exceptions),
        exceptions=relevant_exceptions,
    )


def evaluate_location_service_window(location, start_at, end_at=None):
    return evaluate_service_windows(
        get_active_service_windows(location),
        start_at=start_at,
        end_at=end_at,
        exceptions=get_active_service_exceptions(location),
    )

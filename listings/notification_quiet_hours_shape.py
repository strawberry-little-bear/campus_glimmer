# -*- coding: utf-8 -*-
"""免打扰时段的形状：两个时间字段第一次被数字读到。

``NotificationPreference`` 上的 ``quiet_hours_enabled`` 已经被
``notification_preference_usage`` 当成一个开关读过；但 ``quiet_hours_start`` 与
``quiet_hours_end`` 一次都没有进过任何数字。开启率只能回答「有没有人在用」，
回答不了「他们把窗口配在什么时候、配了多长、一天里几点叠出峰值」。

本模块按快照读偏好行，报四件事：

1. 总闸与配置：开启率、默认 22:00-08:00 的采用、总闸关着但时间已是非默认。
2. 窗口方向与时长：同日 / 跨午夜、固定时长档、开始与结束小时。
3. 逐小时叠加：每个开启窗口映射到 1440 个分钟格子，再按小时报平均与峰值。
4. 数据质量：总闸开启且开始时间等于结束时间的行单列，不进正常分布。

这是只读聚合。不加迁移、不改 ``quiet_hours_active``、不改表单校验、不读
``Notification`` 表、不按用户输出。免打扰只压顶部角标，通知本身照常入库，
所以本模块报的是偏好形状，不是通知效果。
"""

from datetime import time

from django.contrib.auth.models import User

from .models import NotificationPreference


DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(8, 0)

# 档位互斥、左闭右开、全列含 0 样本档。这里取小时的原值，不取分位数：运营要能
# 把表里的档位和设置页里的时间选择对应起来。
DURATION_BANDS = (
    (0, 60, '1 小时内'),
    (60, 240, '1-4 小时'),
    (240, 480, '4-8 小时'),
    (480, 720, '8-12 小时'),
    (720, 1080, '12-18 小时'),
    (1080, 1440, '18-23 小时'),
)

MIN_SAMPLE = 12
NOISE_POINTS = 8


def _share(numerator, denominator):
    """Return a percentage, or None when the denominator itself does not exist."""
    return round(numerator * 100 / denominator, 1) if denominator else None


def _minute_of_day(value):
    return value.hour * 60 + value.minute


def _duration_minutes(start, end):
    """Window length in minutes; a window crossing midnight wraps around a day."""
    return (_minute_of_day(end) - _minute_of_day(start)) % 1440


def _duration_label(minutes):
    for lower, upper, label in DURATION_BANDS:
        if lower <= minutes < upper:
            return label
    return DURATION_BANDS[-1][2]


def _minute_counts(rows):
    """Overlay every valid enabled window onto the 1,440 minutes of a day."""
    counts = [0] * 1440
    for row in rows:
        start_minute = _minute_of_day(row['quiet_hours_start'])
        duration = _duration_minutes(
            row['quiet_hours_start'], row['quiet_hours_end'],
        )
        for offset in range(duration):
            counts[(start_minute + offset) % 1440] += 1
    return counts


def _hour_label(hour):
    return f'{hour:02d}:00'


def _hour_span_label(hour):
    return f'{hour:02d}:00-{(hour + 1) % 24:02d}:00'


def _peak(counts):
    peak_count = max(counts, default=0)
    if not peak_count:
        return {'count': 0, 'first_minute': None, 'first_minute_label': '—'}
    first_minute = counts.index(peak_count)
    return {
        'count': peak_count,
        'first_minute': first_minute,
        'first_minute_label': f'{first_minute // 60:02d}:{first_minute % 60:02d}',
    }


def _summary(preference_count, enabled_count, no_preference_count):
    if not preference_count:
        return (
            '免打扰的两个时间字段此前没有任何运营读者。当前平台还没有偏好行，'
            f'另有 {no_preference_count} 个用户连偏好行都还没有。'
        )
    return (
        '快照读全部偏好行：开启率、默认窗口采用、同日/跨午夜、时长档与逐小时叠加。'
        '免打扰只压角标、不挡通知入库；本模块只报偏好形状，不报通知效果。'
        f'当前有 {preference_count} 行偏好，其中 {enabled_count} 行开着总闸。'
    )


def build_notification_quiet_hours_shape(days=30):
    """把免打扰开始/结束时间的分布读成运营可读的形状。"""
    rows = list(NotificationPreference.objects.values(
        'quiet_hours_enabled', 'quiet_hours_start', 'quiet_hours_end',
    ))
    preference_count = len(rows)
    user_total = User.objects.count()
    no_preference_count = max(0, user_total - preference_count)
    enabled_rows = [row for row in rows if row['quiet_hours_enabled']]
    valid_rows = [
        row for row in enabled_rows
        if row['quiet_hours_start'] != row['quiet_hours_end']
    ]
    invalid_equal_count = len(enabled_rows) - len(valid_rows)
    valid_count = len(valid_rows)

    enabled_count = len(enabled_rows)
    enabled_share = _share(enabled_count, preference_count)
    default_window_count = sum(
        row['quiet_hours_start'] == DEFAULT_QUIET_START
        and row['quiet_hours_end'] == DEFAULT_QUIET_END
        for row in valid_rows
    )
    custom_window_count = valid_count - default_window_count
    disabled_custom_count = sum(
        not row['quiet_hours_enabled']
        and (
            row['quiet_hours_start'] != DEFAULT_QUIET_START
            or row['quiet_hours_end'] != DEFAULT_QUIET_END
        )
        for row in rows
    )

    same_day_count = sum(row['quiet_hours_start'] < row['quiet_hours_end'] for row in valid_rows)
    cross_midnight_count = valid_count - same_day_count
    direction_rows = [
        {
            'key': 'same_day',
            'label': '同日窗口',
            'count': same_day_count,
            'share': _share(same_day_count, valid_count),
            'is_small_sample': valid_count < MIN_SAMPLE,
        },
        {
            'key': 'cross_midnight',
            'label': '跨午夜窗口',
            'count': cross_midnight_count,
            'share': _share(cross_midnight_count, valid_count),
            'is_small_sample': valid_count < MIN_SAMPLE,
        },
    ]

    duration_counts = {label: 0 for _, _, label in DURATION_BANDS}
    for row in valid_rows:
        duration_counts[_duration_label(_duration_minutes(
            row['quiet_hours_start'], row['quiet_hours_end'],
        ))] += 1
    duration_rows = [
        {
            'label': label,
            'count': duration_counts[label],
            'share': _share(duration_counts[label], valid_count),
            'is_empty': duration_counts[label] == 0,
            'is_small_sample': valid_count < MIN_SAMPLE,
        }
        for _, _, label in DURATION_BANDS
    ]

    start_hour_counts = [0] * 24
    end_hour_counts = [0] * 24
    for row in valid_rows:
        start_hour_counts[row['quiet_hours_start'].hour] += 1
        end_hour_counts[row['quiet_hours_end'].hour] += 1
    start_hour_rows = [
        {
            'hour': hour,
            'label': _hour_label(hour),
            'count': start_hour_counts[hour],
            'share': _share(start_hour_counts[hour], valid_count),
            'is_small_sample': valid_count < MIN_SAMPLE,
        }
        for hour in range(24)
    ]
    end_hour_rows = [
        {
            'hour': hour,
            'label': _hour_label(hour),
            'count': end_hour_counts[hour],
            'share': _share(end_hour_counts[hour], valid_count),
            'is_small_sample': valid_count < MIN_SAMPLE,
        }
        for hour in range(24)
    ]

    minute_counts = _minute_counts(valid_rows)
    hour_rows = []
    for hour in range(24):
        hour_values = minute_counts[hour * 60:(hour + 1) * 60]
        hour_rows.append({
            'hour': hour,
            'label': _hour_span_label(hour),
            'average_count': round(sum(hour_values) / len(hour_values), 1),
            'peak_count': max(hour_values, default=0),
        })

    return {
        'days': days,
        'summary': _summary(preference_count, enabled_count, no_preference_count),
        'has_data': bool(preference_count),
        'user_total': user_total,
        'preference_count': preference_count,
        'no_preference_count': no_preference_count,
        'enabled_count': enabled_count,
        'enabled_share': enabled_share,
        'default_window_count': default_window_count,
        'default_window_share': _share(default_window_count, valid_count),
        'custom_window_count': custom_window_count,
        'custom_window_share': _share(custom_window_count, valid_count),
        'disabled_custom_count': disabled_custom_count,
        'invalid_equal_count': invalid_equal_count,
        'direction_rows': direction_rows,
        'duration_rows': duration_rows,
        'start_hour_rows': start_hour_rows,
        'end_hour_rows': end_hour_rows,
        'hour_rows': hour_rows,
        'peak': _peak(minute_counts),
        'min_sample': MIN_SAMPLE,
        'noise_points': NOISE_POINTS,
    }

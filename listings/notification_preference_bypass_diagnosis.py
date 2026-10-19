# -*- coding: utf-8 -*-
"""通知偏好关闭后的异常链路诊断：可疑下界与正常残留。

上一个面板已经报出每个开关「关着却仍收到过」的规模，但那一格本身答不了
「该不该修」：它可能只是用户先收到通知、后来才关开关留下的历史残留，也可能是
某条写入路径真的绕过了偏好闸门。

本模块用两个时间点把这一格拆开：

1. 通知最近发生时间不晚于偏好行 ``updated_at``：时间顺序允许「先通知、后关闭」，
   记为「与后关一致」；
2. 通知最近发生时间晚于 ``updated_at``：偏好行最后一次写入之后仍出现，记为
   「偏好写入后仍出现」，是需要回到写入链路检查的观察下界。

``updated_at`` 是整行偏好的最后写入时间，不是这个开关自己的关闭时间；用户后来
改别的开关会把时间推晚，从而掩盖更早的异常。所以第二类只能作为下界，不能当总量，
更不能除出一个「绕过率」。

边界：不读取通知标题和正文，不输出用户名，不按用户排名，不自动打开开关，
不修改任何写入链路。
"""

from django.db.models import Count, F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Notification, NotificationPreference
from .notification_preference_usage import _preference_fields

# 三个汇总类型在进入统一写入函数前，还会围绕各自的生成任务做一次判断。
# 这张表只帮助运营从异常类型跳到对应实现，不证明某一行通知当时走过哪条路径。
SUMMARY_WRITERS = {
    'operations_digest': 'operations_digest.send_operations_digest',
    'opportunity_digest': 'opportunity_digest.send_opportunity_digest',
    'saved_search_digest': 'saved_search_digest.deliver_buffered_saved_search_matches',
}


def _kind_labels():
    labels = dict(Notification.KIND_CHOICES)
    labels.update({
        'operations_digest': '运营告警日报',
        'opportunity_digest': '互助机会摘要',
        'saved_search_digest': '关注搜索汇总提醒',
    })
    return labels


def build_notification_preference_bypass_diagnosis(*, days=30, now=None):
    """把「关着却仍收到过」拆成时间顺序可解释的两类。"""
    now = now or timezone.now()
    start = now - timezone.timedelta(days=days)
    fields = list(_preference_fields())
    labels = _kind_labels()
    preference_total = NotificationPreference.objects.count()
    no_preference_users = Notification.objects.filter(
        recipient__notification_preference__isnull=True,
    ).values('recipient').distinct().count()

    if not preference_total:
        return {
            'days': days,
            'field_total': len(fields),
            'summary': (
                '当前平台还没有任何偏好行，能把默认开关和历史通知混在一起比较。'
                '没有偏好行的用户收到通知不算异常，模型默认值本来就是全开。'
            ),
            'rows': [],
            'coverage': {
                'preference_users': 0,
                'no_preference_users': no_preference_users,
            },
            'totals': {
                'off_seen_users': 0,
                'consistent_users': 0,
                'suspicious_users': 0,
                'suspicious_rows': 0,
            },
                'has_data': False,
            'has_candidates': False,
        }

    # 「有多少人关掉了这个开关」和 usage 面板问的是同一批偏好行。这里同样一次
    # aggregate 拿全部开关，不再一个字段一次 COUNT。
    off_counts = NotificationPreference.objects.aggregate(**{
        'off_%s' % field: Count('id', filter=Q(**{field: False})) for field in fields
    })

    rows = []
    totals = {
        'off_seen_users': 0,
        'consistent_users': 0,
        'suspicious_users': 0,
        'suspicious_rows': 0,
    }
    for field in fields:
        preference_field = f'recipient__notification_preference__{field}'
        recent = Notification.objects.filter(kind=field).annotate(
            occurred_at=Coalesce('last_occurred_at', 'created_at'),
            preference_updated_at=F('recipient__notification_preference__updated_at'),
        ).filter(
            occurred_at__gte=start,
            occurred_at__lte=now,
            **{preference_field: False},
        )

        # 「与后关一致」和「偏好写入后仍出现」筛的是同一批行，原本却分成四次往返
        # （两次 count、两次 distinct count）。合成一次 aggregate 一次拿出来，口径不变：
        # 可疑仍然是「发生时间晚于整行偏好的最后写入时间」，仍然只是下界。
        stats = recent.aggregate(
            seen_rows=Count('id'),
            seen_users=Count('recipient', distinct=True),
            suspicious_rows=Count('id', filter=Q(occurred_at__gt=F('preference_updated_at'))),
            suspicious_users=Count(
                'recipient', distinct=True,
                filter=Q(occurred_at__gt=F('preference_updated_at')),
            ),
        )
        seen_rows = stats['seen_rows']
        seen_users = stats['seen_users']
        suspicious_rows = stats['suspicious_rows']
        suspicious_users = stats['suspicious_users']
        consistent_users = max(0, seen_users - suspicious_users)

        writer = SUMMARY_WRITERS.get(field, 'notifications.create_notification')
        rows.append({
            'field': field,
            'label': labels.get(field, field),
            'writer': writer,
            'path_label': '汇总写入' if field in SUMMARY_WRITERS else '统一写入',
            'off_users': off_counts['off_%s' % field],
            'seen_users': seen_users,
            'seen_rows': seen_rows,
            'consistent_users': consistent_users,
            'suspicious_users': suspicious_users,
            'suspicious_rows': suspicious_rows,
            'has_suspicious': suspicious_users > 0,
        })

        totals['off_seen_users'] += seen_users
        totals['consistent_users'] += consistent_users
        totals['suspicious_users'] += suspicious_users
        totals['suspicious_rows'] += suspicious_rows

    has_candidates = totals['off_seen_users'] > 0
    summary = (
        '「关着却仍收到过」按通知最近发生时间和偏好行最后写入时间拆开：'
        '不晚于最后写入的记为与后关一致，晚于最后写入的只是可疑下界。'
        'updated_at 是整行偏好的最后保存时间，不是逐开关关闭时间；'
        '后来改过别的开关会掩盖更早的异常，所以不报绕过率。'
    )
    return {
        'days': days,
        'field_total': len(fields),
        'summary': summary,
        'rows': rows,
        'coverage': {
            'preference_users': preference_total,
            'no_preference_users': no_preference_users,
        },
        'totals': totals,
        'has_data': True,
        'has_candidates': has_candidates,
    }

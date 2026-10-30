# -*- coding: utf-8 -*-
"""测试夹具共用的「今天结束时刻」参照点。

一批看板类测试需要一个「今天结束、且一定晚于夹具写入时刻」的参照点：夹具里的
``created_at`` / ``reviewed_at`` / ``matched_at`` 都是 ``auto_now_add`` 写进去的真实
当前时间，参照点一旦早于它，``created_at__lte=now`` 就会把周期内的记录整批排除。

原先每个模块各自写一遍 ``timezone.localtime().replace(hour=23, minute=59)``。这个
写法在本地时间 23:59:00–24:00:00 之间会早于夹具，于是落在这十分钟里的测试会集体
失败：``listings.test_meeting_incident_quality`` 在 CI 上连着两次这样红掉，六个断言
一起挂，而报错信息长得像计算错误，不像时钟问题。同一个坑当时还埋在另外七个模块里，
只是还没被踩到。

这里把修正收拢到一处。正常时段原样返回今天 23:59:00，行为和从前完全一致；只有落进
23:59:00–24:00:00 这一分钟时才改推当天的最后一微秒——仍然是「今天」，统计窗口不会
平移一天。
"""

from datetime import timedelta

from django.utils import timezone


def local_tonight(now=None):
    """今天 23:59，且保证严格晚于 ``now``（不传则取真实当前时间）。

    只有在当天最后一微秒那一瞬间做不到「既晚于此刻、又还在今天」——那种情况下只能
    推到次日的第一微秒，窗口整体平移一天。这个代价每 24 小时只出现一微秒。
    """
    now = now or timezone.now()
    local = timezone.localtime(now)
    reference = local.replace(hour=23, minute=59, second=0, microsecond=0)
    if reference <= local:
        reference = local.replace(hour=23, minute=59, second=59, microsecond=999999)
    if reference <= local:
        reference = local + timedelta(microseconds=1)
    return reference

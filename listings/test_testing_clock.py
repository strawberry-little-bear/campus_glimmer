# -*- coding: utf-8 -*-
"""为测试夹具的「今天结束时刻」参照点钉住口径。

这个参照点看着是个小事，但它坏过一次：CI 在 2026-10-30 的 23:59（UTC 15:59）前后
连着两次变红，`listings.test_meeting_incident_quality` 六个断言一起挂，报错信息是
``0 != 1``、``None != 0.0``、``False is not true``——长得像计算错误，实际是本地时间
走进 23:59:00-24:00:00 那一分钟后，参照点早于夹具的 ``auto_now_add`` 时间戳，
周期内的记录被 ``created_at__lte=now`` 整批排除。

同一个写法当时还埋在另外七个模块里，只是还没被踩到。现在统一走
``listings.testing_clock.local_tonight``，这里把它的两条不变量钉住：
参照点必须严格晚于给定时刻，且必须仍然落在「今天」。
"""

from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from .testing_clock import local_tonight


def at(hour, minute=0, second=0, microsecond=0):
    """今天本地某个时刻， aware 返回。"""
    local = timezone.localtime().replace(
        hour=hour, minute=minute, second=second, microsecond=microsecond,
    )
    return local


class LocalTonightTests(TestCase):
    def test_the_reference_point_is_never_behind_the_given_moment(self):
        """参照点必须严格晚于给定时刻，否则 auto_now_add 夹具会被整批排除。"""
        for moment in (
            at(0, 0, 0, 0),
            at(12, 0, 0, 0),
            at(23, 58, 59, 0),
            at(23, 59, 0, 0),
            at(23, 59, 1, 0),
            at(23, 59, 30, 0),
            at(23, 59, 59, 999999),
        ):
            self.assertGreater(
                local_tonight(moment), moment,
                '参照点没有晚于 %s' % moment,
            )

    def test_the_reference_point_stays_inside_today(self):
        """参照点必须仍然落在「今天」，否则统计窗口会整体平移一天。"""
        for moment in (at(0, 0, 1), at(12, 0), at(23, 59, 0), at(23, 59, 30),
                       at(23, 59, 59)):
            self.assertEqual(
                timezone.localdate(local_tonight(moment)),
                timezone.localdate(moment),
            )

    def test_the_last_microsecond_of_the_day_is_the_only_exception(self):
        """当天最后一微秒做不到「既晚于此刻、又还在今天」，只能推到次日第一微秒。

        这个代价每 24 小时只出现一微秒，比把窗口整体提前一天更容易接受。
        """
        moment = at(23, 59, 59, 999999)
        reference = local_tonight(moment)
        self.assertGreater(reference, moment)
        self.assertEqual(reference - moment, timedelta(microseconds=1))

    def test_outside_the_broken_window_it_is_still_exactly_2359(self):
        """正常时段行为和从前完全一致：就是今天 23:59:00，一秒都不多。"""
        expected = at(12, 0).replace(hour=23, minute=59, second=0, microsecond=0)
        self.assertEqual(local_tonight(at(12, 0)), expected)

    def test_without_an_argument_it_uses_the_real_clock(self):
        """不传参时按真实此刻走，且同样不会早于此刻。"""
        moment = timezone.now()
        self.assertGreater(local_tonight(), moment)

    def test_a_naive_moment_is_rejected_rather_than_silently_shifted(self):
        """传 naive 时间直接报错，不会静默按另一个时区解释。

        一个被静默按 UTC 解释的参照点，会让窗口整体平移八个小时，而报错信息看起来还像是统计错了。
        """
        naive = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        with self.assertRaises(ValueError):
            local_tonight(naive)

    def test_the_broken_window_is_only_one_second_wide(self):
        """23:59:00-24:00:00 这一分钟里只往后推一秒，不推到第二天。"""
        moment = at(23, 59, 30)
        self.assertEqual(
            local_tonight(moment),
            moment.replace(second=59, microsecond=999999),
        )

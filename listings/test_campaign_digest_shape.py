# -*- coding: utf-8 -*-
"""盯住「专题摘要的形状」报出来的每一个数。

这个模块读的是 ``CampaignRegistration.notify_updates`` /
``update_frequency`` 两个开关，和 ``CampaignUpdateMatch`` 上那三个时间戳。写入
路径在 ``campaign_updates.py``，读者此前只有 ``campaign_analytics`` 里的三个原始
计数，所以下面每一条测试钉的都是一个此前没人回答的问题：

- 加入和订阅是两次独立同意，报名数永远大于订阅数，分母只能算参与中的报名；
- 入队之后只有送达、取消、还挂着三种结局，取消一栏此前一个读者都没有；
- 绝对等待时长分不清每日和每周，必须除以各自频率的周期才能读出节奏；
- 此刻仍挂着的是右删失样本，只能报现状，不能替它猜一个结局。

这些测试同时是模块自己的边界说明：不输出用户名、商品标题或订单号，不加迁移、
不改频率选项、不取消任何命中、不补发任何摘要。
"""

from datetime import time as dt_time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campaign_digest_shape import (
    DIGEST_CYCLE_DAYS, MIN_SAMPLE, NOISE_POINTS, RHYTHM_BANDS,
    build_campaign_digest_shape,
)
from .models import (
    CampaignRegistration, CampaignUpdateMatch, CampusCampaign, Category, Item,
    NotificationPreference,
)


def _walk(value):
    """把一份看板结果摊平成纯标量的列表，用来查它有没有泄密。"""
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)
    else:
        yield value


class CampaignDigestShapeTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.category = Category.objects.create(name='专题摘要形状分类')
        self.seller = User.objects.create_user(
            username='digest-shape-seller', password='safe-password-123',
        )
        self.campaign = CampusCampaign.objects.create(
            title='摘要形状专题',
            slug='digest-shape-campaign',
            starts_at=self.now - timedelta(days=60),
            ends_at=self.now + timedelta(days=30),
        )
        self.user_index = 0

    # ------------------------------------------------------------------ 夹具

    def _user(self, prefix='digest-user'):
        self.user_index += 1
        return User.objects.create_user(
            username='%s-%d' % (prefix, self.user_index),
            password='safe-password-123',
        )

    def _registration(self, *, frequency='daily', notify=True, active=True,
                      user=None, campaign=None):
        return CampaignRegistration.objects.create(
            campaign=campaign or self.campaign,
            user=user or self._user(),
            notify_updates=notify,
            update_frequency=frequency,
            is_active=active,
            left_at=None if active else self.now - timedelta(days=2),
        )

    def _item(self, *, campaign=None, status='available'):
        self.item_index = getattr(self, 'item_index', 0) + 1
        return Item.objects.create(
            title='摘要形状商品 %d' % self.item_index,
            description='用于专题摘要形状测试',
            price='12.00',
            category=self.category,
            condition='良好',
            seller=self.seller,
            campaign=campaign if campaign is not None else self.campaign,
            status=status,
        )

    def _match(self, registration, *, matched_at, notified_at=None,
               cancelled_at=None, item=None):
        return CampaignUpdateMatch.objects.create(
            registration=registration,
            item=item or self._item(),
            matched_at=matched_at,
            notified_at=notified_at,
            cancelled_at=cancelled_at,
        )

    # ------------------------------------------------------------ 第一节

    def test_an_empty_library_says_the_chain_never_ran(self):
        shape = build_campaign_digest_shape(now=self.now)

        self.assertFalse(shape['has_data'])
        self.assertEqual(
            shape['summary'],
            '平台上还没有任何专题报名，专题摘要链路一次都没有被触发过。',
        )
        self.assertEqual(shape['frequency_rows'], [])
        self.assertEqual(shape['outcome_rows'], [])
        self.assertEqual(shape['rhythm_rows'], [])
        self.assertEqual(shape['pending_rows'], [])
        self.assertIsNone(shape['subscribe_rate'])
        self.assertIsNone(shape['oldest_wait_seconds'])
        self.assertEqual(shape['oldest_wait_label'], '\u2014')

    def test_joining_and_subscribing_are_two_separate_consents(self):
        self._registration(notify=True)
        self._registration(notify=True)
        self._registration(notify=False)

        shape = build_campaign_digest_shape(now=self.now)

        self.assertTrue(shape['has_data'])
        self.assertEqual(shape['active_count'], 3)
        self.assertEqual(shape['subscribed_count'], 2)
        self.assertEqual(shape['subscribe_rate'], 66.7)
        self.assertEqual(shape['left_count'], 0)
        self.assertIn('3 段参与中的报名里有 2 段开着专题摘要', shape['summary'])

    def test_left_registrations_stay_out_of_the_subscription_denominator(self):
        self._registration(notify=True)
        self._registration(notify=False)
        # 已退出的报名也开了摘要，但它不该把订阅率稀释掉。
        self._registration(notify=True, active=False)

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['active_count'], 2)
        self.assertEqual(shape['left_count'], 1)
        self.assertEqual(shape['subscribed_count'], 1)
        self.assertEqual(shape['subscribe_rate'], 50.0)
        self.assertIn('另有 1 段已退出、不计入分母', shape['summary'])
        # 第一节的每一行也只按参与中的报名算占比。
        for row in shape['frequency_rows']:
            self.assertEqual(
                sum(r['registration_count'] for r in shape['frequency_rows']),
                2,
            )

    def test_a_library_of_only_left_registrations_has_no_rate(self):
        self._registration(active=False, notify=True)

        shape = build_campaign_digest_shape(now=self.now)

        self.assertTrue(shape['has_data'])
        self.assertEqual(shape['active_count'], 0)
        self.assertEqual(shape['left_count'], 1)
        self.assertEqual(shape['subscribed_count'], 0)
        # 空分母报 None 不报 0：报 0 会让运营以为「有人看着却没一个人订阅」。
        self.assertIsNone(shape['subscribe_rate'])
        self.assertNotIn('订阅率', shape['summary'])

    def test_frequency_rows_keep_the_fixed_order_and_flag_outside_values(self):
        self._registration(frequency='weekly', notify=False)
        self._registration(frequency='daily', notify=True)
        self._registration(frequency='monthly', notify=True)

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(
            [row['key'] for row in shape['frequency_rows']],
            ['daily', 'weekly', 'monthly'],
        )
        outside = shape['frequency_rows'][-1]
        self.assertTrue(outside['is_other'])
        self.assertEqual(outside['label'], '其它')
        self.assertIsNone(outside['cycle_days'])
        for row in shape['frequency_rows'][:2]:
            self.assertFalse(row['is_other'])

    def test_cycle_days_come_from_the_sender_not_from_a_second_table(self):
        self.assertEqual(DIGEST_CYCLE_DAYS, {'daily': 1, 'weekly': 7})
        self._registration(frequency='daily')
        self._registration(frequency='weekly')

        shape = build_campaign_digest_shape(now=self.now)

        by_key = {row['key']: row for row in shape['frequency_rows']}
        self.assertEqual(by_key['daily']['cycle_days'], 1)
        self.assertEqual(by_key['weekly']['cycle_days'], 7)

    def test_the_three_reasons_a_subscription_cannot_send_are_separate(self):
        # 偏好行存在但全局开关关着：发送端第一道门就拦在这里。
        closed = self._registration(notify=True)
        NotificationPreference.objects.create(
            user=closed.user, campaign_update=False,
        )
        # 没有偏好行：发送端会补一行默认开启的，照样送达。
        self._registration(notify=True)
        # 偏好行存在且开着：这一行只是用来证明上一类没被多算。
        self._registration(notify=True)

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['subscribed_count'], 3)
        self.assertEqual(shape['subscribed_preference_off_count'], 1)
        self.assertEqual(shape['subscribed_no_preference_count'], 2)
        self.assertEqual(shape['subscribed_quiet_count'], 0)

    def test_quiet_hours_are_a_snapshot_of_right_now(self):
        quiet = self._registration(notify=True)
        NotificationPreference.objects.create(
            user=quiet.user,
            quiet_hours_enabled=True,
            quiet_hours_start=dt_time(0, 0),
            quiet_hours_end=dt_time(23, 59, 59, 999999),
        )
        self._registration(notify=True)

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['subscribed_quiet_count'], 1)
        row = shape['frequency_rows'][0]
        self.assertEqual(row['quiet_count'], 1)
        self.assertEqual(row['preference_off_count'], 0)

    # ------------------------------------------------------------ 第二节

    def test_outcomes_are_four_mutually_exclusive_buckets(self):
        daily = self._registration(frequency='daily')
        self._match(daily, matched_at=self.now - timedelta(days=3),
                    notified_at=self.now - timedelta(days=2))
        self._match(daily, matched_at=self.now - timedelta(days=3),
                    cancelled_at=self.now - timedelta(days=2))
        self._match(daily, matched_at=self.now - timedelta(days=3))

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['outcome_queued_count'], 3)
        self.assertEqual(shape['outcome_delivered_count'], 1)
        self.assertEqual(shape['outcome_cancelled_count'], 1)
        self.assertEqual(shape['outcome_pending_count'], 1)
        row = shape['outcome_rows'][0]
        self.assertEqual(row['queued_count'], 3)
        self.assertEqual(row['delivered_count'], 1)
        self.assertEqual(row['cancelled_count'], 1)
        self.assertEqual(row['pending_count'], 1)
        self.assertEqual(row['delivered_rate'], 33.3)
        self.assertEqual(row['cancelled_rate'], 33.3)

    def test_a_notified_match_stays_delivered_even_if_it_was_cancelled_later(self):
        daily = self._registration(frequency='daily')
        self._match(
            daily, matched_at=self.now - timedelta(days=5),
            notified_at=self.now - timedelta(days=4),
            cancelled_at=self.now - timedelta(days=1),
        )

        shape = build_campaign_digest_shape(now=self.now)

        # 取消只数从未送达的那些：送达过又取消的条目，结局已经定了。
        self.assertEqual(shape['outcome_delivered_count'], 1)
        self.assertEqual(shape['outcome_cancelled_count'], 0)
        self.assertEqual(shape['outcome_pending_count'], 0)

    def test_the_window_cuts_on_matched_at_not_on_notified_at(self):
        daily = self._registration(frequency='daily')
        # 四十天前入队、刚刚才送达：notified_at 落在窗口里，但它属于上一个窗口
        # 入队的条目，不算这一轮的入队。
        self._match(daily, matched_at=self.now - timedelta(days=40),
                    notified_at=self.now - timedelta(hours=1))
        # 三天前入队、还没送达：这才是这一轮窗口里的条目。
        self._match(daily, matched_at=self.now - timedelta(days=3))
        # 四十天前入队、一直挂着：窗口没切到它，第四节仍要数它。
        self._match(daily, matched_at=self.now - timedelta(days=40))

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['outcome_queued_count'], 1)
        self.assertEqual(shape['outcome_delivered_count'], 0)
        self.assertEqual(shape['outcome_pending_count'], 1)
        # 第四节不看窗口，所以挂着的是两条，不是窗口里那一条。
        self.assertEqual(shape['pending_total'], 2)

    def test_an_unknown_frequency_gets_its_own_row_in_the_outcome_table(self):
        odd = self._registration(frequency='monthly')
        self._match(odd, matched_at=self.now - timedelta(days=2))

        shape = build_campaign_digest_shape(now=self.now)

        row = shape['outcome_rows'][0]
        self.assertEqual(row['key'], 'monthly')
        self.assertTrue(row['is_other'])
        self.assertEqual(row['pending_count'], 1)

    # ------------------------------------------------------------ 第三节

    def test_rhythm_only_counts_matches_that_were_actually_delivered(self):
        daily = self._registration(frequency='daily')
        self._match(daily, matched_at=self.now - timedelta(days=5),
                    notified_at=self.now - timedelta(days=4))
        self._match(daily, matched_at=self.now - timedelta(days=5),
                    cancelled_at=self.now - timedelta(days=4))
        self._match(daily, matched_at=self.now - timedelta(days=5))

        shape = build_campaign_digest_shape(now=self.now)

        row = shape['rhythm_rows'][0]
        self.assertEqual(row['delivered_count'], 1)
        self.assertEqual(row['mean_wait_label'], '1.0 天')
        self.assertEqual(row['median_wait_label'], '1.0 天')
        self.assertEqual(row['p90_wait_label'], '1.0 天')

    def test_landing_exactly_on_the_cycle_counts_as_within(self):
        daily = self._registration(frequency='daily')
        # 恰好一天：踩着周期线送达的那一条是准点的，不该被推到下一档。
        self._match(daily, matched_at=self.now - timedelta(days=1),
                    notified_at=self.now)

        shape = build_campaign_digest_shape(now=self.now)

        row = shape['rhythm_rows'][0]
        self.assertEqual([band['count'] for band in row['bands']], [1, 0, 0, 0])
        self.assertEqual(row['within_share'], 100.0)

    def test_the_same_wait_is_a_different_rhythm_for_each_frequency(self):
        daily = self._registration(frequency='daily')
        weekly = self._registration(frequency='weekly')
        for registration in (daily, weekly):
            self._match(
                registration, matched_at=self.now - timedelta(days=3),
                notified_at=self.now,
            )

        shape = build_campaign_digest_shape(now=self.now)

        by_key = {row['key']: row for row in shape['rhythm_rows']}
        # 三天对每日是拖了三倍，对每周是提前四天：绝对时长分不清这件事。
        self.assertEqual([b['count'] for b in by_key['daily']['bands']], [0, 0, 1, 0])
        self.assertEqual([b['count'] for b in by_key['weekly']['bands']], [1, 0, 0, 0])
        self.assertEqual(by_key['daily']['within_share'], 0.0)
        self.assertEqual(by_key['weekly']['within_share'], 100.0)

    def test_bands_are_closed_on_the_left_and_cover_zero(self):
        daily = self._registration(frequency='daily')
        for ratio_days in (0, 1, 2, 4):
            self._match(
                daily, matched_at=self.now - timedelta(days=ratio_days),
                notified_at=self.now,
            )

        shape = build_campaign_digest_shape(now=self.now)

        row = shape['rhythm_rows'][0]
        # 0 与 1 天都在「周期内」，2 天进「一到两倍」，4 天进「两到四倍」。
        self.assertEqual([band['count'] for band in row['bands']], [2, 1, 1, 0])
        self.assertEqual(
            [band['label'] for band in row['bands']],
            [label for _limit, label, _hint in RHYTHM_BANDS],
        )

    def test_a_frequency_without_a_cycle_enters_no_band_at_all(self):
        odd = self._registration(frequency='monthly')
        self._match(odd, matched_at=self.now - timedelta(days=2),
                    notified_at=self.now)

        shape = build_campaign_digest_shape(now=self.now)

        row = shape['rhythm_rows'][0]
        self.assertEqual(row['cycle_days'], None)
        self.assertEqual([band['count'] for band in row['bands']], [0, 0, 0, 0])
        self.assertIsNone(row['within_share'])
        # 除不出倍数来，就不假装除出来了：绝不当成「已超周期」扫进去。
        self.assertEqual(sum(band['count'] for band in row['bands']), 0)

    def test_small_samples_are_flagged_rather_than_silently_read(self):
        daily = self._registration(frequency='daily')
        self._match(daily, matched_at=self.now - timedelta(days=1),
                    notified_at=self.now)
        self._match(daily, matched_at=self.now - timedelta(hours=6))

        shape = build_campaign_digest_shape(now=self.now)

        self.assertTrue(shape['rhythm_rows'][0]['is_small_sample'])
        self.assertTrue(shape['outcome_rows'][0]['is_small_sample'])
        self.assertTrue(shape['pending_rows'][0]['is_small_sample'])
        self.assertTrue(shape['frequency_rows'][0]['is_small_sample'])
        self.assertEqual(shape['min_sample'], MIN_SAMPLE)
        self.assertEqual(shape['noise_points'], NOISE_POINTS)

    # ------------------------------------------------------------ 第四节

    def test_pending_is_a_snapshot_of_everything_still_buffered(self):
        daily = self._registration(frequency='daily')
        # 窗口之前入队的也挂着，第四节要把它数进来。
        self._match(daily, matched_at=self.now - timedelta(days=45))
        self._match(daily, matched_at=self.now - timedelta(days=3))
        # 半天前入队的还在周期内。
        self._match(daily, matched_at=self.now - timedelta(hours=12))

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['pending_total'], 3)
        self.assertEqual(shape['outcome_pending_count'], 2)
        self.assertEqual(shape['pending_deliverable_count'], 3)
        row = shape['pending_rows'][0]
        self.assertEqual(row['pending_count'], 3)
        self.assertEqual(row['within_cycle_count'], 1)
        self.assertEqual(row['overdue_count'], 2)
        self.assertEqual(row['oldest_wait_label'], '45 天')

    def test_deliverable_is_recomputed_with_the_senders_own_boundary(self):
        # 发送端自己的四条边界，逐条拆掉一条，看这一条还算不算「仍可投递」。
        ended = CampusCampaign.objects.create(
            title='已结束的摘要专题', slug='digest-shape-ended',
            starts_at=self.now - timedelta(days=60),
            ends_at=self.now - timedelta(days=1),
        )
        left = self._registration(active=False)
        unsubscribed = self._registration(notify=False)
        closed = self._registration(campaign=ended)
        sold_item = self._item(status='sold')
        gone = self._registration()
        healthy = self._registration()

        self._match(left, matched_at=self.now - timedelta(days=1))
        self._match(unsubscribed, matched_at=self.now - timedelta(days=1))
        self._match(closed, matched_at=self.now - timedelta(days=1))
        self._match(gone, matched_at=self.now - timedelta(days=1), item=sold_item)
        self._match(healthy, matched_at=self.now - timedelta(days=1))

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['pending_total'], 5)
        self.assertEqual(shape['pending_deliverable_count'], 1)
        # 表外频率那一行也走同一条边界，不因为除不出周期就放过。
        odd = self._registration(frequency='monthly')
        self._match(odd, matched_at=self.now - timedelta(days=1))
        shape = build_campaign_digest_shape(now=self.now)
        self.assertEqual(shape['pending_total'], 6)
        self.assertEqual(shape['pending_deliverable_count'], 2)

    def test_a_pending_frequency_without_a_cycle_reports_no_cycle_split(self):
        odd = self._registration(frequency='monthly')
        self._match(odd, matched_at=self.now - timedelta(days=20))

        shape = build_campaign_digest_shape(now=self.now)

        row = shape['pending_rows'][0]
        self.assertEqual(row['pending_count'], 1)
        self.assertEqual(row['deliverable_count'], 1)
        self.assertIsNone(row['within_cycle_count'])
        self.assertIsNone(row['overdue_count'])

    def test_the_oldest_wait_is_the_longest_wait_still_buffered(self):
        daily = self._registration(frequency='daily')
        weekly = self._registration(frequency='weekly')
        self._match(daily, matched_at=self.now - timedelta(days=1))
        self._match(weekly, matched_at=self.now - timedelta(days=9))
        # 已送达的不算「仍挂着」，不该把最老一条抢走。
        self._match(daily, matched_at=self.now - timedelta(days=20),
                    notified_at=self.now - timedelta(days=19))

        shape = build_campaign_digest_shape(now=self.now)

        self.assertEqual(shape['pending_total'], 2)
        self.assertEqual(shape['oldest_wait_label'], '9 天')
        by_key = {row['key']: row for row in shape['pending_rows']}
        self.assertEqual(by_key['daily']['oldest_wait_label'], '1 天')
        self.assertEqual(by_key['weekly']['oldest_wait_label'], '9 天')

    # ------------------------------------------------------------ 边界与口径

    def test_an_unknown_period_falls_back_to_the_default_window(self):
        shape = build_campaign_digest_shape(days=13, now=self.now)

        self.assertEqual(shape['days'], 30)
        self.assertEqual(
            timezone.localtime(shape['period_start']).date(),
            timezone.localtime(self.now).date() - timedelta(days=29),
        )
        self.assertEqual(shape['period_end'], self.now)

    def test_every_period_choice_is_accepted(self):
        for days, _label in ((7, ''), (30, ''), (90, ''), (365, '')):
            self.assertEqual(build_campaign_digest_shape(days=days, now=self.now)['days'], days)

    def test_the_report_never_leaks_usernames_titles_or_order_numbers(self):
        buyer = self._user(prefix='digest-leak')
        daily = self._registration(user=buyer)
        item = self._item()
        self._match(daily, item=item, matched_at=self.now - timedelta(days=2))

        shape = build_campaign_digest_shape(now=self.now)
        leaked = [
            value for value in _walk(shape)
            if isinstance(value, str)
            and (buyer.username in value or item.title in value)
        ]

        self.assertEqual(leaked, [])

    def test_the_module_never_writes_anything(self):
        daily = self._registration(frequency='daily')
        item = self._item()
        match = self._match(daily, item=item, matched_at=self.now - timedelta(days=2))
        before = (
            CampaignRegistration.objects.count(),
            CampaignUpdateMatch.objects.count(),
            Item.objects.count(),
        )

        build_campaign_digest_shape(now=self.now)

        after = (
            CampaignRegistration.objects.count(),
            CampaignUpdateMatch.objects.count(),
            Item.objects.count(),
        )
        self.assertEqual(before, after)
        # 只读：挂在缓冲里的那条既没被送达，也没被取消。
        match.refresh_from_db()
        self.assertIsNone(match.notified_at)
        self.assertIsNone(match.cancelled_at)

    def test_the_summary_names_the_pending_pool_without_guessing_its_fate(self):
        daily = self._registration(frequency='daily')
        self._match(daily, matched_at=self.now - timedelta(days=3))

        shape = build_campaign_digest_shape(now=self.now)

        self.assertIn('窗口内入队 1 条', shape['summary'])
        self.assertIn('仍未送达 1', shape['summary'])
        self.assertIn('此刻仍挂着 1 条', shape['summary'])
        self.assertIn('最老一条已等 3 天', shape['summary'])

    def test_the_summary_says_nothing_about_rhythm_when_samples_are_thin(self):
        daily = self._registration(frequency='daily')
        self._match(daily, matched_at=self.now - timedelta(days=1),
                    notified_at=self.now)

        shape = build_campaign_digest_shape(now=self.now)

        self.assertIn('两个频率的已送达样本都不足 %d 条' % MIN_SAMPLE, shape['summary'])


class CampaignDigestShapePageTests(TestCase):
    """看板页面和导出要讲同一件事。"""

    def setUp(self):
        self.now = timezone.now()
        self.staff = User.objects.create_user(
            username='digest-shape-staff', password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.category = Category.objects.create(name='专题摘要形状页面分类')
        self.seller = User.objects.create_user(
            username='digest-shape-page-seller', password='safe-password-123',
        )
        self.campaign = CampusCampaign.objects.create(
            title='摘要形状页面专题', slug='digest-shape-page',
            starts_at=self.now - timedelta(days=60),
            ends_at=self.now + timedelta(days=30),
        )

    def _item(self, index):
        return Item.objects.create(
            title='摘要形状页面商品 %d' % index,
            description='用于页面与导出一致性测试',
            price='15.00', category=self.category, condition='良好',
            seller=self.seller, campaign=self.campaign, status='available',
        )

    def _seed(self):
        for index, frequency in enumerate(('daily', 'weekly')):
            user = User.objects.create_user(
                username='digest-page-user-%d' % index,
                password='safe-password-123',
            )
            registration = CampaignRegistration.objects.create(
                campaign=self.campaign, user=user, notify_updates=True,
                update_frequency=frequency,
            )
            # 报名与商品这对组合唯一，所以两行命中要挂在两个商品上。
            CampaignUpdateMatch.objects.create(
                registration=registration, item=self._item(index * 2 + 1),
                matched_at=self.now - timedelta(days=3),
                notified_at=self.now - timedelta(days=2),
            )
            CampaignUpdateMatch.objects.create(
                registration=registration, item=self._item(index * 2 + 2),
                matched_at=self.now - timedelta(days=2),
            )

    def test_the_panel_and_the_export_show_the_same_four_blocks(self):
        self._seed()

        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        html = page.content.decode('utf-8')
        text = export.content.decode('utf-8-sig')
        for heading in (
            '专题摘要的形状',
            '一、从加入到订阅',
            '二、缓冲池的三种结局',
            '三、从入队到送达，相对各自频率的周期',
            '四、当前仍挂着的',
        ):
            self.assertIn(heading, html)
            self.assertIn(heading, text)
        # 汇总里的四个关键数在两边都要出现。
        for number in ('2', '4'):
            self.assertIn(number, html)
        self.assertIn('此处只报告', text)

    def test_the_panel_collapses_to_one_sentence_when_the_chain_never_ran(self):
        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertIn(
            '平台上还没有任何专题报名，专题摘要链路一次都没有被触发过。',
            page.content.decode('utf-8'),
        )
        self.assertIn(
            '平台上还没有任何专题报名，专题摘要链路一次都没有被触发过。',
            export.content.decode('utf-8-sig'),
        )

    def test_the_fragment_endpoint_renders_the_same_panel(self):
        self._seed()

        response = self.client.get(
            reverse('operations_dashboard_fragment', args=('ops-campaigns',)),
            {'days': 30},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('专题摘要的形状', response.content.decode('utf-8'))
        self.assertIn('一、从加入到订阅', response.content.decode('utf-8'))

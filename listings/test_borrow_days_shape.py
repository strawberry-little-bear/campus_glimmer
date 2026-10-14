# -*- coding: utf-8 -*-
"""默认借用天数形状的测试。

这个模块读的字段有一个坑，也是它存在的理由：``Item.borrow_days`` 有写入路径、有显示
路径、没有任何统计路径。测试要守住的正是「读了之后没有顺手改掉什么」，以及四个口径边界。

**档位是固定的，不随数据移动。** ``BORROW_DAY_BANDS`` 的六个档位是表单上的原值
（1/3/7/14/30/90），不是分位数。测试必须断言「库里只有 3 天和 90 天两档时，六个档位
依然全列」——一旦有人把它改成分位数，这一条会红。

**分母是订单侧全量，不是升级侧。** 一笔从未升级过的借用必须进分母。测试要同时造「升级
过的」和「没升级过的」两笔，断言分母是 2 而不是 1。

**只统计已归还的订单。** 还挂在 ``borrowed`` 的订单是右删失样本，它的实际时长还不存在。
测试要断言它不进第二节的任何分母，而且被单独数出来。

**逾期按已归还的笔算，不按未闭环的算。** 一笔逾期的未闭环借用，它的逾期天数会随时间继
续增长。测试要断言开放订单不计入平均超期，只计入开放数。

**配置改了不追溯旧订单。** 一笔订单下单时商品配的是 7 天，之后商品被改成 90 天，第二
节读到的配置侧是 90 天（商品的现值），而 ``return_due_at`` 还是按下单时的 7 天算的。
测试要断言这个不对称被如实报出来，而不是被抹平。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .borrow_days_shape import (
    BORROW_DAY_BANDS, DEFAULT_BORROW_DAYS, DEFAULT_LOOKBACK_DAYS, MIN_BAND_SAMPLE,
    MIN_ITEM_SAMPLE, _band_label, build_borrow_days_shape,
)
from .models import BorrowReturnEscalation, CampusLocation, Category, Item, Order


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_item(seller, *, borrow_days, status='available', trade_mode='borrow', title=None):
    category, _ = Category.objects.get_or_create(name='测试分类')
    location, _ = CampusLocation.objects.get_or_create(name='测试地点')
    return Item.objects.create(
        title=title or f'测试商品 {borrow_days} 天',
        description='测试用',
        trade_mode=trade_mode,
        price=0,
        deposit_amount=10,
        borrow_days=borrow_days,
        category=category,
        location=location,
        seller=seller,
        condition='good',
        status=status,
    )


def make_order(item, buyer, *, status='returned', created_at, due_offset_days,
               returned_offset_days=None):
    """造一笔借用订单。

    ``created_at`` 传绝对时刻；``due_offset_days`` 与 ``returned_offset_days`` 是相对
    ``created_at`` 的偏移天数，让测试能精确控制「配了多少天」和「实际借了多久」。
    """
    order = Order.objects.create(
        item=item,
        buyer=buyer,
        seller=item.seller,
        agreed_price=0,
        deposit_amount=10,
        status=status,
        return_due_at=created_at + timedelta(days=due_offset_days),
        returned_at=(
            created_at + timedelta(days=returned_offset_days)
            if returned_offset_days is not None else None
        ),
    )
    # ``Order.created_at`` 是 auto_now_add，create() 里显式传会被覆盖，所以要事后再改。
    # 借用天数的口径完全建立在「下单时刻」上，这一列不真实，第二节和窗口就都无从测起。
    Order.objects.filter(pk=order.pk).update(created_at=created_at)
    return order


def add_escalation(order, *, level=1, resolved_at=None, last_escalated_at=None):
    return BorrowReturnEscalation.objects.create(
        order=order,
        escalation_level=level,
        escalation_count=1,
        last_escalated_at=last_escalated_at or order.created_at,
        resolved_at=resolved_at,
    )


class BorrowDaysShapeBandTests(TestCase):
    """档位本身：固定、互斥、全列。"""

    def test_bands_cover_the_whole_range_without_gaps(self):
        """六个档位左闭右开，合起来覆盖 1 天以上，没有落不进去的值。"""
        self.assertEqual(BORROW_DAY_BANDS[0][0], 1)
        self.assertIsNone(BORROW_DAY_BANDS[-1][1])
        for index, (lower, upper, _label) in enumerate(BORROW_DAY_BANDS):
            if upper is not None:
                self.assertEqual(BORROW_DAY_BANDS[index + 1][0], upper)
        # 表单上真实会出现的值各自落到预期的档。
        self.assertEqual(_band_label(1), '1-3 天')
        self.assertEqual(_band_label(3), '1-3 天')
        self.assertEqual(_band_label(4), '4-7 天')
        self.assertEqual(_band_label(7), '4-7 天')
        self.assertEqual(_band_label(8), '8-14 天')
        self.assertEqual(_band_label(14), '8-14 天')
        self.assertEqual(_band_label(15), '15-30 天')
        self.assertEqual(_band_label(30), '15-30 天')
        self.assertEqual(_band_label(31), '31-90 天')
        self.assertEqual(_band_label(90), '31-90 天')
        self.assertEqual(_band_label(91), '90 天以上')

    def test_unset_borrow_days_is_reported_not_silently_dropped(self):
        """``borrow_days`` 为空时报「未设置」，不当成 0 天。"""
        self.assertEqual(_band_label(None), '未设置')

    def test_zero_sample_bands_stay_in_the_table(self):
        """库里只有 3 天和 90 天两档，六个档位依然全列。

        0 样本档必须留在表里：「90 天上没有人」本身是结论，不是表格的缺口。这一条同时
        守住档位不随数据移动——一旦有人把档位改成分位数，空档会被自动裁掉。
        """
        now = timezone.now()
        seller = make_user('band-seller')
        make_item(seller, borrow_days=3)
        make_item(seller, borrow_days=90)

        result = build_borrow_days_shape(now=now)

        self.assertEqual(result['item_count'], 2)
        self.assertEqual(len(result['item_rows']), len(BORROW_DAY_BANDS))
        by_label = {row['label']: row for row in result['item_rows']}
        self.assertEqual(by_label['1-3 天']['item_count'], 1)
        self.assertEqual(by_label['31-90 天']['item_count'], 1)
        self.assertEqual(by_label['8-14 天']['item_count'], 0)
        self.assertEqual(by_label['8-14 天']['share'], 0.0)
        self.assertTrue(by_label['8-14 天']['is_small_sample'])

    def test_item_table_counts_every_borrow_item_not_just_borrowed_ones(self):
        """第一节的分母是全部借用商品，包括从没被借出去的。

        借用商品的配置分布和「被用过的期限分布」是两个问题。只读被借过的商品会让前者
        看起来像后者。
        """
        now = timezone.now()
        seller = make_user('never-borrowed-seller')
        make_item(seller, borrow_days=7, status='available')
        make_item(seller, borrow_days=30, status='expired')

        result = build_borrow_days_shape(now=now)

        self.assertEqual(result['item_count'], 2)
        self.assertEqual(result['in_stock_count'], 1)

    def test_non_borrow_items_are_out_of_scope(self):
        """出售与免费赠送商品不进本模块。"""
        now = timezone.now()
        seller = make_user('sale-seller')
        make_item(seller, borrow_days=7, trade_mode='sale')
        make_item(seller, borrow_days=7, trade_mode='free')

        result = build_borrow_days_shape(now=now)

        self.assertEqual(result['item_count'], 0)
        self.assertFalse(result['has_data'])
        self.assertIn('还没有限期借用商品', result['summary'])


class BorrowDaysShapeConfiguredVsActualTests(TestCase):
    """第二节：配的天数 vs 实际借了多久。"""

    def setUp(self):
        self.now = timezone.now()

    def test_returned_borrow_compares_its_band_to_the_configured_band(self):
        """配 7 天、3 天后归还，两列各记一格，且记为「不一致」。"""
        seller = make_user('cva-seller')
        buyer = make_user('cva-buyer')
        item = make_item(seller, borrow_days=7, status='reserved')
        make_order(
            item, buyer,
            created_at=self.now - timedelta(days=20),
            due_offset_days=7,
            returned_offset_days=3,
        )

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['configured_rows']}
        # 配置侧：这一笔配的是 7 天，落在 4-7 天档。
        self.assertEqual(by_label['4-7 天']['configured_count'], 1)
        self.assertEqual(by_label['4-7 天']['configured_share'], 100.0)
        # 实际侧：3 天后归还，落在 1-3 天档。两列各报各的，不是同一个数减来减去。
        self.assertEqual(by_label['1-3 天']['actual_count'], 1)
        self.assertEqual(by_label['1-3 天']['actual_share'], 100.0)
        self.assertEqual(by_label['1-3 天']['average_actual_days'], 3.0)
        self.assertEqual(by_label['4-7 天']['actual_count'], 0)
        self.assertIsNone(by_label['4-7 天']['average_actual_days'])
        self.assertEqual(result['matched_count'], 0)
        self.assertEqual(result['mismatch_count'], 1)

    def test_actual_matching_configuration_is_counted_as_matched(self):
        """配 7 天、7 天后归还，记为「一致」。"""
        seller = make_user('match-seller')
        buyer = make_user('match-buyer')
        item = make_item(seller, borrow_days=7, status='reserved')
        make_order(
            item, buyer,
            created_at=self.now - timedelta(days=20),
            due_offset_days=7,
            returned_offset_days=7,
        )

        result = build_borrow_days_shape(now=self.now)

        self.assertEqual(result['matched_count'], 1)
        self.assertEqual(result['mismatch_count'], 0)

    def test_borrow_still_in_flight_is_censored_not_shortened(self):
        """还挂在 borrowed 的订单不计入实际时长，单独数出来。

        借用还没结束，它的实际时长还不存在。把它算进分母会让每个档位的实际时长凭空变小。
        """
        seller = make_user('censored-seller')
        buyer = make_user('censored-buyer')
        item = make_item(seller, borrow_days=7, status='reserved')
        make_order(
            item, buyer, status='borrowed',
            created_at=self.now - timedelta(days=2),
            due_offset_days=7,
        )

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['configured_rows']}
        self.assertEqual(by_label['4-7 天']['configured_count'], 1)
        self.assertEqual(by_label['4-7 天']['actual_count'], 0)
        self.assertIsNone(by_label['4-7 天']['average_actual_days'])
        # 实际列的分母是「已归还的借用数」，这里一笔都没有，所以占比报 None 不报 0。
        self.assertIsNone(by_label['4-7 天']['actual_share'])
        self.assertEqual(result['censored_count'], 1)
        self.assertEqual(result['matched_count'] + result['mismatch_count'], 0)

    def test_reconfigured_item_keeps_the_old_deadline(self):
        """改商品的借用天数不追溯旧订单，两侧的不对称被如实报出来。

        下单时配的是 7 天，``return_due_at`` 按 7 天算；之后商品被改成 90 天。配置侧读
        到的是商品的现值 90 天，而归还截止时刻还是按下单时的 7 天算的。本模块不抹平这个
        不对称——它没有记录历史配置的表，只能把现值报出来。
        """
        seller = make_user('reconfig-seller')
        buyer = make_user('reconfig-buyer')
        item = make_item(seller, borrow_days=7, status='reserved')
        make_order(
            item, buyer,
            created_at=self.now - timedelta(days=20),
            due_offset_days=7,
            returned_offset_days=7,
        )
        # 下单之后才把期限改掉。
        item.borrow_days = 90
        item.save(update_fields=['borrow_days'])

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['configured_rows']}
        self.assertEqual(by_label['31-90 天']['configured_count'], 1)
        # 实际侧还是 7 天：实际时长是订单自己的事实，跟配置现值无关。
        self.assertEqual(by_label['4-7 天']['actual_count'], 1)
        self.assertEqual(by_label['4-7 天']['average_actual_days'], 7.0)
        self.assertEqual(result['mismatch_count'], 1)


class BorrowDaysShapeEscalationTests(TestCase):
    """第三节与第四节：借用天数 × 催收，逾期相对配置天数的比例。"""

    def setUp(self):
        self.now = timezone.now()

    def test_never_escalated_borrow_stays_in_the_denominator(self):
        """从未升级过的借用进分母。

        如果只列升级过的订单，每个档位的升级率都会是 100%，分层就失去了意义。这条和
        ``borrow_risk`` 的分母口径一致，两表才能并排读。
        """
        seller = make_user('denom-seller')
        buyer = make_user('denom-buyer')
        escalated_item = make_item(seller, borrow_days=7, status='reserved')
        calm_item = make_item(seller, borrow_days=7, status='reserved')
        escalated_order = make_order(
            escalated_item, buyer,
            created_at=self.now - timedelta(days=20),
            due_offset_days=7,
            returned_offset_days=10,
        )
        make_order(
            calm_item, buyer,
            created_at=self.now - timedelta(days=19),
            due_offset_days=7,
            returned_offset_days=7,
        )
        add_escalation(escalated_order, level=1, resolved_at=self.now - timedelta(days=18))

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['band_rows']}
        band = by_label['4-7 天']
        self.assertEqual(band['borrow_count'], 2)
        self.assertEqual(band['escalated_count'], 1)
        self.assertEqual(band['escalation_rate'], 50.0)

    def test_level_three_borrow_is_reported_per_band(self):
        """第三级催收按档计数，闭环时长从真正发生的那一级算起。"""
        seller = make_user('level3-seller')
        buyer = make_user('level3-buyer')
        item = make_item(seller, borrow_days=90, status='reserved')
        order = make_order(
            item, buyer,
            created_at=self.now - timedelta(days=30),
            due_offset_days=90,
            returned_offset_days=100,
        )
        add_escalation(
            order, level=3,
            last_escalated_at=self.now - timedelta(days=5),
            resolved_at=self.now - timedelta(days=2),
        )

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['band_rows']}
        band = by_label['31-90 天']
        self.assertEqual(band['level_three_count'], 1)
        self.assertEqual(band['level_three_rate'], 100.0)
        self.assertEqual(band['average_close_days'], 3.0)
        self.assertEqual(result['level_three_count'], 1)
        self.assertEqual(result['escalation_rate'], 100.0)

    def test_overdue_is_measured_as_a_ratio_of_the_configured_term(self):
        """逾期 6 天对 90 天的借用是 6.7%，对 7 天的借用是 85.7%。

        主口径是相对比例，只有它能让不同期限档的逾期放在一起比。绝对超期天数仍然单列，
        因为那是运营和借用者沟通时真的会用到的数。
        """
        seller = make_user('ratio-seller')
        buyer = make_user('ratio-buyer')
        short_item = make_item(seller, borrow_days=7, status='reserved')
        long_item = make_item(seller, borrow_days=90, status='reserved')
        short_order = make_order(
            short_item, buyer,
            created_at=self.now - timedelta(days=20),
            due_offset_days=7,
            returned_offset_days=13,
        )
        long_order = make_order(
            long_item, buyer,
            created_at=self.now - timedelta(days=30),
            due_offset_days=90,
            returned_offset_days=96,
        )
        for order in (short_order, long_order):
            add_escalation(order, level=1, resolved_at=order.returned_at)

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['band_rows']}
        self.assertEqual(by_label['4-7 天']['overdue_count'], 1)
        self.assertEqual(by_label['4-7 天']['average_overdue_days'], 6.0)
        self.assertEqual(by_label['4-7 天']['average_overdue_ratio'], 85.7)
        self.assertEqual(by_label['31-90 天']['overdue_count'], 1)
        self.assertEqual(by_label['31-90 天']['average_overdue_days'], 6.0)
        self.assertEqual(by_label['31-90 天']['average_overdue_ratio'], 6.7)
        # 同一档里两个口径的分母一致：都是该档已结束且逾期的借用。
        self.assertEqual(by_label['4-7 天']['overdue_count'], 1)

    def test_on_time_borrow_is_not_overdue(self):
        """按时归还不计入逾期，逾期率报 0.0 而不是 None。"""
        seller = make_user('ontime-seller')
        buyer = make_user('ontime-buyer')
        item = make_item(seller, borrow_days=7, status='reserved')
        make_order(
            item, buyer,
            created_at=self.now - timedelta(days=20),
            due_offset_days=7,
            returned_offset_days=5,
        )

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['band_rows']}
        band = by_label['4-7 天']
        self.assertEqual(band['overdue_count'], 0)
        self.assertEqual(band['overdue_rate'], 0.0)
        self.assertIsNone(band['average_overdue_days'])
        self.assertIsNone(band['average_overdue_ratio'])

    def test_open_overdue_borrow_does_not_drift_the_average(self):
        """未闭环的逾期借用只计入开放数，不进平均超期。

        它的逾期天数还在随时间增长，报进平均值会让这个数随读取时刻漂移。
        """
        seller = make_user('open-seller')
        buyer = make_user('open-buyer')
        item = make_item(seller, borrow_days=7, status='reserved')
        make_order(
            item, buyer, status='borrowed',
            created_at=self.now - timedelta(days=30),
            due_offset_days=7,
        )

        result = build_borrow_days_shape(now=self.now)

        by_label = {row['label']: row for row in result['band_rows']}
        band = by_label['4-7 天']
        self.assertEqual(band['open_count'], 0)
        self.assertEqual(band['overdue_count'], 0)
        self.assertIsNone(band['average_overdue_days'])

    def test_order_outside_the_window_is_not_counted(self):
        """窗口之外的订单不计入，但商品配置仍然计入第一节。"""
        seller = make_user('window-seller')
        buyer = make_user('window-buyer')
        item = make_item(seller, borrow_days=7, status='reserved')
        make_order(
            item, buyer,
            created_at=self.now - timedelta(days=DEFAULT_LOOKBACK_DAYS + 10),
            due_offset_days=7,
            returned_offset_days=8,
        )

        result = build_borrow_days_shape(now=self.now)

        self.assertEqual(result['borrow_count'], 0)
        self.assertEqual(result['item_count'], 1)
        self.assertIsNone(result['escalation_rate'])


class BorrowDaysShapeSummaryTests(TestCase):
    """第五节与总结句。"""

    def setUp(self):
        self.now = timezone.now()

    def test_default_borrow_days_share_is_reported(self):
        """默认值 7 天被用了多少单独报，并给出方向标记。

        它是运营唯一能一眼认出来的数，因为它就是表单上的占位符。样本不足时标记为
        ``False``，不硬给方向。
        """
        seller = make_user('default-seller')
        for index in range(MIN_ITEM_SAMPLE):
            make_item(seller, borrow_days=7 if index < 9 else 30, title=f'd{index}')

        result = build_borrow_days_shape(now=self.now)

        self.assertEqual(result['item_count'], MIN_ITEM_SAMPLE)
        self.assertEqual(result['default_borrow_days'], DEFAULT_BORROW_DAYS)
        self.assertEqual(result['default_share'], 75.0)
        self.assertTrue(result['default_share_has_sample'])
        self.assertIn('75.0%', result['summary'])

    def test_default_share_has_no_direction_below_the_threshold(self):
        """样本不足时不给方向，只报 None。"""
        seller = make_user('thin-seller')
        make_item(seller, borrow_days=7)

        result = build_borrow_days_shape(now=self.now)

        self.assertEqual(result['default_share'], 100.0)
        self.assertFalse(result['default_share_has_sample'])

    def test_summary_names_the_band_with_the_worst_level_three_rate(self):
        """总结句点名第三级催收率最高的档，且只点样本足够的档。"""
        seller = make_user('summary-seller')
        buyer = make_user('summary-buyer')
        for index in range(MIN_BAND_SAMPLE + 2):
            item = make_item(seller, borrow_days=90, title=f'long{index}')
            order = make_order(
                item, buyer,
                created_at=self.now - timedelta(days=30),
                due_offset_days=90,
                returned_offset_days=100,
            )
            if index == 0:
                add_escalation(order, level=3, resolved_at=self.now - timedelta(days=1))
        for index in range(MIN_BAND_SAMPLE + 2):
            item = make_item(seller, borrow_days=7, title=f'short{index}')
            make_order(
                item, buyer,
                created_at=self.now - timedelta(days=20),
                due_offset_days=7,
                returned_offset_days=7,
            )

        result = build_borrow_days_shape(now=self.now)

        self.assertTrue(result['has_sample'])
        self.assertIn('31-90 天', result['summary'])
        self.assertIn('第三级催收率最高', result['summary'])

    def test_empty_library_reports_none_not_zero(self):
        """空库时报 None 不报 0。0 会被读成「有，占比很小」，而这里的事实是分母不存在。"""
        result = build_borrow_days_shape(now=self.now)

        self.assertFalse(result['has_data'])
        self.assertIn('还没有限期借用商品', result['summary'])
        self.assertIsNone(result['escalation_rate'])
        self.assertIsNone(result['default_share'])
        for row in result['band_rows']:
            self.assertIsNone(row['escalation_rate'])
            self.assertIsNone(row['level_three_rate'])
            self.assertIsNone(row['average_overdue_ratio'])
        for row in result['configured_rows']:
            self.assertIsNone(row['configured_share'])
            self.assertIsNone(row['actual_share'])

    def test_band_rows_are_sorted_by_volume(self):
        """按档按借用量从多到少排，运营先看样本最多的档。"""
        seller = make_user('sort-seller')
        buyer = make_user('sort-buyer')
        for index in range(3):
            item = make_item(seller, borrow_days=90, title=f'l{index}')
            make_order(
                item, buyer,
                created_at=self.now - timedelta(days=30),
                due_offset_days=90,
                returned_offset_days=95,
            )
        for index in range(6):
            item = make_item(seller, borrow_days=7, title=f's{index}')
            make_order(
                item, buyer,
                created_at=self.now - timedelta(days=20),
                due_offset_days=7,
                returned_offset_days=8,
            )

        result = build_borrow_days_shape(now=self.now)

        counts = [row['borrow_count'] for row in result['band_rows']]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(result['band_rows'][0]['label'], '4-7 天')

    def test_module_never_reports_a_user_or_an_item_title(self):
        """不按用户排名、不输出用户名、不列商品名。"""
        seller = make_user('privacy-seller')
        buyer = make_user('privacy-buyer')
        item = make_item(seller, borrow_days=7, title='一本独特的书名')
        make_order(
            item, buyer,
            created_at=self.now - timedelta(days=20),
            due_offset_days=7,
            returned_offset_days=8,
        )

        result = build_borrow_days_shape(now=self.now)
        rendered = repr(result)

        self.assertNotIn('privacy-seller', rendered)
        self.assertNotIn('privacy-buyer', rendered)
        self.assertNotIn('一本独特的书名', rendered)
        self.assertNotIn('description', rendered)

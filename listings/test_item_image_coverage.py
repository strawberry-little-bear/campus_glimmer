# -*- coding: utf-8 -*-
"""为「商品图片配了多少张」钉住口径。

`ItemImage` 是站内唯一一条商品配图记录，产品侧已经在用它（订单安全卡判
`if not item.images.exists()`），运营侧从来没有数过。这个模块把它数出来，
只报告不改写。这些测试要钉住的是：

1. 分母只收当前在架的商品，已售出已预订已过期的不进分母。
2. 成交看 `completed` / `returned`，`cancelled` 单独一类。
3. 「有图 vs 无图」的成交率只做对照，样本不足或差值不满噪声线时不给方向。
4. 图片数分档只报形状，某一档样本不足时该档成交率标为样本不足。
5. 无图商品清单只带商品标题、分类、交易方式和举报数，不带任何用户名。
6. 不读图片内容、不打开文件、不按卖家聚合排名。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .item_image_coverage import (
    IMAGE_COUNT_BUCKETS, MIN_MEASURED_ITEMS, NO_IMAGE_LIST_LIMIT, NOISE_POINTS,
    build_item_image_coverage,
)
from .models import CampusLocation, Category, Item, ItemImage, Order, Report


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class ItemImageCoverageTests(TestCase):
    def setUp(self):
        self.seller = make_user('coverage-seller')
        self.buyer = make_user('coverage-buyer')
        self.category = Category.objects.create(name='配图测试分类')
        self.other_category = Category.objects.create(name='配图另一分类')
        self.location = CampusLocation.objects.create(name='配图测试地点')
        self._seq = 0

    def make_item(self, *, status='available', trade_mode='sale', category=None,
                  created_days_ago=3, seller=None):
        self._seq += 1
        item = Item.objects.create(
            title=f'配图测试商品 {self._seq}', description='用于图片覆盖统计',
            price='50.00', category=category or self.category, condition='良好',
            location=self.location, seller=seller or self.seller,
            trade_mode=trade_mode, status=status,
        )
        # `created_at` 是 auto_now_add，建行时拿到的是真实墙上时间。统计窗口按
        # `Item.created_at` 切，所以必须回填，否则商品落在窗口右端之外被整条丢掉。
        Item.objects.filter(pk=item.pk).update(
            created_at=timezone.now() - timedelta(days=created_days_ago),
        )
        item.refresh_from_db()
        return item

    def add_images(self, item, count):
        """给一件商品挂 `count` 张图。

        `ItemImage` 只有一个外键和一个 ImageField，测试不需要真文件，
        直接用 `image` 字段的名字占位即可——本模块只数行数，不打开文件。
        """
        for index in range(count):
            ItemImage.objects.create(
                item=item, image=f'items/test-{item.id}-{index}.jpg',
            )

    def make_order(self, item, *, status='completed', buyer=None):
        return Order.objects.create(
            item=item, buyer=buyer or self.buyer, seller=item.seller,
            meeting_location=self.location, agreed_price='50.00', status=status,
        )

    # --- 分母：只收在架商品 ----------------------------------------------

    def test_sold_items_are_not_in_the_denominator(self):
        """已售出的商品不进分母——它已经走完了自己的流程。

        图片补不补都改变不了结局，把它算进「有多少商品没图」会让这个数字永远虚高，
        而虚高的部分运营做不了任何事。
        """
        self.make_item(status='sold')
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['total'], 0)
        self.assertFalse(result['has_data'])

    def test_reserved_and_expired_items_are_not_in_the_denominator(self):
        self.make_item(status='reserved')
        self.make_item(status='expired')
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['total'], 0)

    def test_available_items_are_counted(self):
        self.make_item()
        self.make_item()
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['total'], 2)
        self.assertTrue(result['has_data'])

    def test_items_published_outside_the_window_are_excluded(self):
        """窗口按 `Item.created_at` 切，一件 40 天前发布的商品不进 30 天窗口。"""
        self.make_item(created_days_ago=40)
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['total'], 0)

    # --- 覆盖：有多少件一张图都没有 --------------------------------------

    def test_no_image_items_are_counted(self):
        with_image = self.make_item()
        self.add_images(with_image, 2)
        self.make_item()
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['total'], 2)
        self.assertEqual(result['coverage']['no_image_count'], 1)
        self.assertEqual(result['coverage']['no_image_rate'], 50.0)
        self.assertEqual(result['coverage']['with_image_count'], 1)

    def test_reported_no_image_items_are_counted_separately(self):
        """无图商品里有多少曾被举报过，只是一个间接线索。

        曾被举报过的商品，删图可能是被动的。平台没有能力从现有字段判断一个人为什么
        删图，所以只数存在，不把删图动机分成主动被动两类。
        """
        item = self.make_item()
        self.make_item()
        Report.objects.create(
            item=item, reporter=make_user('coverage-reporter'),
            reason='inappropriate', status='pending',
        )
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['no_image_count'], 2)
        self.assertEqual(result['coverage']['no_image_reported_count'], 1)

    def test_rejected_reports_do_not_count(self):
        """被驳回的举报不算「曾被举报过」——它没有被平台认可存在过。"""
        item = self.make_item()
        Report.objects.create(
            item=item, reporter=make_user('coverage-reporter'),
            reason='other', status='rejected',
        )
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['no_image_reported_count'], 0)

    # --- 成交对照：只做对照，不给因果 ------------------------------------

    def test_dealt_counts_completed_and_returned(self):
        """`completed` 和 `returned` 都算成交——借用走到 `returned` 是完整闭环。"""
        item = self.make_item()
        self.add_images(item, 1)
        self.make_order(item, status='completed')
        self.make_order(item, status='returned')
        row = self._by_label(self.result_by_trade_mode(item), '出售')
        self.assertEqual(row['order_count'], 2)
        self.assertEqual(row['dealt_count'], 2)
        self.assertEqual(row['dealt_rate'], 100.0)

    def _by_label(self, rows, label):
        return next(row for row in rows if row['label'] == label)

    def result_by_trade_mode(self, item):
        result = build_item_image_coverage(days=30)
        return result['by_trade_mode']['rows']

    def test_cancelled_is_its_own_bucket(self):
        """`cancelled` 单独一类，不算成交也不算未成交。

        它是一次真的失败，而且失败在下单之后，比「没有订单」更值得被看见。
        """
        item = self.make_item()
        self.add_images(item, 1)
        self.make_order(item, status='cancelled')
        row = self._by_label(self.result_by_trade_mode(item), '出售')
        self.assertEqual(row['order_count'], 1)
        self.assertEqual(row['dealt_count'], 0)
        self.assertEqual(row['dealt_rate'], 0)
        self.assertEqual(self.result_contrast(item)['no_image']['order_count'], 0)

    def result_contrast(self, item):
        return build_item_image_coverage(days=30)['contrast']

    def test_dealt_rate_denominator_is_orders_not_items(self):
        """成交率的分母是订单数，不是商品数。

        一件下过五笔单的商品在这个率里出现五次，而它确实产生了五笔交易——
        成交率回答的是「一笔交易走成了没有」。
        """
        item = self.make_item()
        self.add_images(item, 1)
        for _ in range(3):
            self.make_order(item, status='completed')
        for _ in range(2):
            self.make_order(item, status='cancelled')
        row = self._by_label(self.result_by_trade_mode(item), '出售')
        self.assertEqual(row['item_count'], 1)
        self.assertEqual(row['order_count'], 5)
        self.assertEqual(row['dealt_rate'], 60.0)

    def test_small_sample_flags_the_contrast(self):
        """两组各需至少 12 件才给方向，不足时只报数字。"""
        self.make_item()
        self.make_item()
        self.add_images(self.make_item(), 1)
        result = build_item_image_coverage(days=30)
        self.assertTrue(result['contrast']['is_measurable'] is False)
        self.assertFalse(result['contrast']['gap_is_meaningful'])
        self.assertTrue(result['coverage']['is_small_sample'])
        self.assertEqual(result['coverage']['min_measured_items'], MIN_MEASURED_ITEMS)

    def test_gap_below_the_noise_line_is_not_meaningful(self):
        """差值不满 8 个百分点视为噪声，不作解读。"""
        result = self._build_contrast_with_gap(0)
        self.assertTrue(result['is_measurable'])
        self.assertEqual(result['gap_points'], 0)
        self.assertFalse(result['gap_is_meaningful'])

    def _build_contrast_with_gap(self, gap_points):
        """构建一组对照数据，使有图组与无图组的成交率相差
        究底 gap_points 个百分点。

        成交率的分母是订单笔数而不是商品件数，所以这里给无图组每件商品
        同时建 5 笔订单，其中成交的比例控制在 100 - gap_points，这样
        不管 gap 取多少，两组都远远超过最小视测商品数，不会误命中小样本判定。
        有图组每件 1 笔订单全部成交，成交率 100%。
        """
        for _ in range(MIN_MEASURED_ITEMS):
            item = self.make_item()
            self.add_images(item, 1)
            self.make_order(item, status='completed')
        orders_per_item = 5
        dealt_orders = orders_per_item * (100 - int(gap_points)) // 100
        for _ in range(MIN_MEASURED_ITEMS):
            item = self.make_item()
            for index in range(orders_per_item):
                self.make_order(
                    item,
                    status='completed' if index < dealt_orders else 'cancelled',
                )
        return build_item_image_coverage(days=30)['contrast']

    def test_gap_above_the_noise_line_is_meaningful(self):
        result = self._build_contrast_with_gap(20)
        self.assertTrue(result['is_measurable'])
        self.assertEqual(result['gap_points'], 20.0)
        self.assertTrue(result['gap_is_meaningful'])

    # --- 图片数分档：只报形状，不报最优值 --------------------------------

    def test_image_count_buckets_cover_every_item(self):
        self.make_item()                        # 0 张
        one = self.make_item()
        self.add_images(one, 1)
        three = self.make_item()
        self.add_images(three, 3)
        five = self.make_item()
        self.add_images(five, 5)
        result = build_item_image_coverage(days=30)
        counts = {
            row['label']: row['item_count'] for row in result['image_counts']['rows']
        }
        self.assertEqual(counts, {
            '一张图都没有': 1, '1 张图': 1, '2-3 张图': 1, '4 张及以上': 1,
        })
        self.assertEqual(
            len(result['image_counts']['rows']), len(IMAGE_COUNT_BUCKETS),
        )

    def test_bucket_with_too_few_items_is_flagged(self):
        """某一档样本不足 12 件时，该档的成交率只报数字不给方向。"""
        item = self.make_item()
        self.add_images(item, 5)
        self.make_order(item, status='completed')
        result = build_item_image_coverage(days=30)
        top = next(
            row for row in result['image_counts']['rows'] if row['label'] == '4 张及以上'
        )
        self.assertTrue(top['is_small_sample'])
        self.assertEqual(top['item_count'], 1)

    def test_every_bucket_is_flagged_when_the_sample_is_thin(self):
        self.make_item()
        result = build_item_image_coverage(days=30)
        self.assertTrue(all(
            row['is_small_sample'] for row in result['image_counts']['rows']
        ))

    # --- 按交易方式与分类拆开 --------------------------------------------

    def test_conversion_is_split_by_trade_mode(self):
        """免费商品走申请队列而不是直接预约，不合并报。

        一份申请能不能被选中和图片的关系，与出售商品下单的关系不是一回事。
        合并会把两种体验平均成一个没有解释力的数。
        """
        self.add_images(self.make_item(trade_mode='sale'), 1)
        self.make_item(trade_mode='free')
        self.make_item(trade_mode='borrow')
        result = build_item_image_coverage(days=30)
        labels = {row['label']: row['item_count'] for row in result['by_trade_mode']['rows']}
        self.assertEqual(labels, {'出售': 1, '免费赠送': 1, '限期借用': 1})

    def test_breakdown_rows_are_sorted_by_size(self):
        """样本多的排在前面：运营要看的是规模，不是字母序。"""
        for _ in range(3):
            self.make_item(trade_mode='sale')
        self.make_item(trade_mode='free')
        result = build_item_image_coverage(days=30)
        rows = result['by_trade_mode']['rows']
        self.assertEqual(rows[0]['label'], '出售')
        self.assertEqual(rows[0]['item_count'], 3)

    def test_breakdown_carries_its_own_no_image_rate(self):
        item = self.make_item(trade_mode='sale')
        self.add_images(item, 1)
        self.make_item(trade_mode='sale')
        result = build_item_image_coverage(days=30)
        sale = self._by_label(result['by_trade_mode']['rows'], '出售')
        self.assertEqual(sale['item_count'], 2)
        self.assertEqual(sale['no_image_count'], 1)
        self.assertEqual(sale['no_image_rate'], 50.0)

    def test_category_breakdown_covers_every_category(self):
        self.make_item(category=self.category)
        self.make_item(category=self.other_category)
        result = build_item_image_coverage(days=30)
        labels = {row['label'] for row in result['by_category']['rows']}
        self.assertEqual(labels, {'配图测试分类', '配图另一分类'})

    # --- 无图商品清单 ----------------------------------------------------

    def test_no_image_list_carries_no_username(self):
        """清单只带商品标题、分类、交易方式和举报数，不带任何用户名。

        不报「谁最不爱传图」——那是一份指责清单，不是一份看板。
        """
        item = self.make_item()
        row = self._only_row(build_item_image_coverage(days=30))
        self.assertEqual(row.item, item)
        self.assertEqual(row.category_label, '配图测试分类')
        self.assertEqual(row.trade_mode_label, '出售')
        self.assertEqual(row.report_count, 0)

    def _only_row(self, result):
        rows = result['no_image_rows']['rows']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_items_with_images_are_not_in_the_no_image_list(self):
        item = self.make_item()
        self.add_images(item, 1)
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['no_image_rows']['row_total'], 0)
        self.assertEqual(result['no_image_rows']['rows'], [])

    def test_no_image_list_is_capped(self):
        """清单最多列 20 条。它是给「现在去处理」用的，不是给归档用的。"""
        for _ in range(NO_IMAGE_LIST_LIMIT + 5):
            self.make_item()
        result = build_item_image_coverage(days=30)
        self.assertEqual(len(result['no_image_rows']['rows']), NO_IMAGE_LIST_LIMIT)
        self.assertEqual(result['no_image_rows']['row_total'], NO_IMAGE_LIST_LIMIT + 5)
        self.assertEqual(result['no_image_rows']['row_limit'], NO_IMAGE_LIST_LIMIT)

    def test_no_image_list_puts_newest_first(self):
        """越新的排在越前面——越新的越可能是还没被任何曝光碰过的新品。"""
        old = self.make_item(created_days_ago=10)
        new = self.make_item(created_days_ago=1)
        rows = build_item_image_coverage(days=30)['no_image_rows']['rows']
        self.assertEqual([row.item for row in rows], [new, old])

    def test_no_image_list_reports_the_report_count(self):
        item = self.make_item()
        Report.objects.create(
            item=item, reporter=make_user('coverage-reporter'),
            reason='scam', status='pending',
        )
        row = self._only_row(build_item_image_coverage(days=30))
        self.assertEqual(row.report_count, 1)

    # --- 门槛与降级 ------------------------------------------------------

    def test_empty_period_reports_no_data(self):
        result = build_item_image_coverage(days=30)
        self.assertFalse(result['has_data'])
        self.assertIn('没有在架的新商品', result['summary'])
        self.assertEqual(result['coverage']['total'], 0)
        self.assertEqual(result['no_image_rows']['row_total'], 0)

    def test_small_sample_is_named_in_the_summary(self):
        self.make_item()
        summary = build_item_image_coverage(days=30)['summary']
        self.assertIn(str(MIN_MEASURED_ITEMS), summary)

    def test_invalid_days_falls_back_to_thirty(self):
        self.make_item(created_days_ago=5)
        result = build_item_image_coverage(days=3)
        self.assertEqual(result['period_days'], 30)

    def test_noise_line_is_reported_for_the_template(self):
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['noise_points'], NOISE_POINTS)
        self.assertEqual(result['min_measured_items'], MIN_MEASURED_ITEMS)
        self.assertEqual(
            len(result['image_count_labels']), len(IMAGE_COUNT_BUCKETS),
        )
        self.assertEqual(result['dealt_statuses'], ('completed', 'returned'))

    def test_summary_mentions_the_no_image_count(self):
        self.make_item()
        self.add_images(self.make_item(), 2)
        summary = build_item_image_coverage(days=30)['summary']
        self.assertIn('2 件在售商品', summary)
        self.assertIn('1 件一张图都没有', summary)
        self.assertIn('50.0%', summary)

    def test_report_reason_is_never_read(self):
        """不读举报理由——那可能是「描述与图片不一致」这样的自由文本。

        本模块只数存在。这条测试用一个会触发读取的举报理由，确认它不出现在任何输出里。
        """
        item = self.make_item()
        Report.objects.create(
            item=item, reporter=make_user('coverage-reporter'),
            reason='inappropriate', detail='商品描述与图片不一致，图上是另一台',
            status='pending',
        )
        result = build_item_image_coverage(days=30)
        self.assertEqual(result['coverage']['no_image_reported_count'], 1)
        self.assertNotIn('不一致', str(result))
# -*- coding: utf-8 -*-
"""为「用户建的心愿单分组」钉住口径。

`views.create_favorite_collection` 建分组，`views.update_favorite_collection` 把一条收藏移
进或移出分组。分组功能有完整的增删改查和 UI，而「分组」这个维度此前一条统计都没有。这个
模块把它们读出来只报告不改写。这些测试要钉住的是比数字本身更要紧的那些取舍：

1. **「先收藏后归类」报得了，前提是 `Favorite.grouped_at`。** 那一列加上之前，归类时刻在
   库里不存在，报告里连占位都不给；加上之后，当场 / 事后 / 从未 / 归过又取消四类分开报，
   分母只收归过类的收藏，宽限天数出现在面板上。
2. 同理不报「分组被改名的频率」（应用内没有改名路由，`updated_at` 只反映 admin 动过）和
   「删分组之后收藏去哪了」（删分组只走 admin，删完的收藏和没归过类的长得一样）。
3. 四类分母各自独立，不能混：当前全部收藏、当前全部分组、当前有收藏的用户、窗口内新建。
   建过分组的人占的分子是建过分组的人、分母是**有收藏的用户**，不是全站用户。
4. 「当前」是快照不是窗口：分组可被删（删完收藏变未分组）、收藏可被取消，所以窗口内的
   新建规模和当前快照是两件事，分开报。
5. 空分组要按 `FavoriteCollection.created_at` 拆「刚建」和「早就建了还空着」，占空分组的
   比例分母是空分组自己。
6. 分组名的计数天然是用户数（同一用户内名字唯一），只有被 `MIN_NAME_SAMPLE` 个以上用户
   用过的名字才进榜——这是隐私线不是样本线。
7. 空平台不报 0%，也不报 0 个以外的任何形状。
8. 只报告：不改任何一条 `Favorite.collection`、不删分组、不改分组名、不动表单校验。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import CampusLocation, Category, Favorite, FavoriteCollection, Item
from .favorite_collection_quality import (
    EMPTY_GROUP_GRACE_DAYS, INSTANT_GROUPING_GRACE_MINUTES, MIN_BAND_SAMPLE,
    MIN_NAME_SAMPLE, NOISE_POINTS, build_favorite_collection_quality,
)


class FavoriteCollectionQualityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='fav-group-user', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='fav-group-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='分组统计测试分类')
        self.location = CampusLocation.objects.create(
            name='分组统计测试地点', building='分组统计测试楼', is_public=True,
        )

    # --- 造数据的帮手 ---

    def make_item(self, title='分组统计测试商品', *, seller=None):
        return Item.objects.create(
            title=title, description='用于分组统计', price='30.00',
            category=self.category, location=self.location,
            condition='良好', seller=seller or self.user, status='available',
        )

    def make_favorite(self, *, user=None, item=None, collection=None, days_ago=1):
        """建一条收藏，`days_ago` 回填 `created_at`。

        `created_at` 是 `auto_now_add`，建行时拿到的是真实墙上时间，而统计窗口按它切，不
        回填会让这一行落在窗口右端之外。这一列写进去就不再动，所以窗口归属是干净的——它
        也是库里唯一能直接按时间切的收藏时间列。
        """
        favorite = Favorite.objects.create(
            user=user or self.user, item=item or self.make_item(),
            collection=collection,
        )
        created_at = timezone.now() - timedelta(days=days_ago)
        # 带了分组的收藏，模型会在创建那一刻写下 grouped_at。回填 created_at 时必须
        # 把它一起推回去，否则这一行读起来像「囤了一天才归类」——而它建行时是当场归
        # 类的。夹具必须产出自洽的行，否则测的是夹具不是实现。
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=created_at,
            grouped_at=created_at if collection is not None else None,
        )
        favorite.refresh_from_db()
        return favorite

    def make_collection(self, name='教材', *, user=None, days_ago=1):
        """建一个分组，`days_ago` 回填 `created_at`。"""
        collection = FavoriteCollection.objects.create(user=user or self.user, name=name)
        created_at = timezone.now() - timedelta(days=days_ago)
        FavoriteCollection.objects.filter(pk=collection.pk).update(
            created_at=created_at,
        )
        collection.refresh_from_db()
        return collection

    # --- 口径一：「先收藏后归类」报得了，前提是 Favorite.grouped_at ---

    def test_grouping_latency_is_published_in_four_kinds(self):
        """归类时刻现在在库里了，四类必须分开报，不能合成一个比例。

        `Favorite.grouped_at` 由模型在 `collection_id` 变化时自己写。这个测试钉住
        报告的形状：当场、事后、从未归类、归过类又取消，是四个数而不是一个率——合成
        一个率就会把「试过了又退回去」和「从来没试过」混成同一件事，而后者是比前者弱
        得多的信号。
        """
        collection = self.make_collection(name='宿舍')
        # 当场归类：收藏和归类在同一分钟内。
        self.make_favorite(collection=collection)
        # 从未归类。
        self.make_favorite()
        # 归过类又取消：collection_id 先真的变过一次（归进去），再变一次（移出
        # 来）。不能从「无分组」直接置「无分组」——NULL 到 NULL 不算变化，模型
        # 不会盖章，这一行就会落进「从未归类」，测的成了夹具不是实现。两步都盖
        # 了章，最后 grouped_at 停在移出来那一刻，把它压回 created_at 附近，这
        # 一行才读起来是「当场归了又当场退回去」；不压的话它以囤了一天的形状
        # 进来，delayed_total 会被它顶高，四类的数就全乱了。
        released = self.make_favorite()
        released.collection = collection
        released.save(update_fields=['collection'])
        released.collection = None
        released.save(update_fields=['collection'])
        # grouped_at 停在移出来那一刻：归进去是当场，退出来是 5 天后。这一行读
        # 起来是「当场归了，过几天又退回去」，于是它归进事后那一档，而不是又给
        # 当场加一条——当场/事后/归过又取消四类必须互斥，合成一个数就分不出
        # 「试过了又退回去」和「从来没试过」了。
        Favorite.objects.filter(pk=released.pk).update(
            grouped_at=released.created_at + timedelta(days=5),
        )

        report = build_favorite_collection_quality(days=30)

        latency = report['grouping_latency']
        self.assertEqual(latency['instant_total'], 1)
        self.assertEqual(latency['ungrouped_total'], 1)
        self.assertEqual(latency['released_total'], 1)
        # 归过类的一共两条：当场那条和归过又取消那条。后者归进去是当场、退出
        # 来是 5 天后，按最后一次分组变化算落在事后那一档——四类互斥，每条收
        # 藏只出现在一类里，合起来正好等于 grouped_ever。
        self.assertEqual(latency['grouped_ever_total'], 2)
        self.assertEqual(latency['delayed_total'], 1)
        self.assertEqual(latency['instant_total'] + latency['delayed_total'],
                         latency['grouped_ever_total'])
        # 宽限天数必须出现在面板上：运营读不出「当场」和「事后」的分界在哪，这两个数
        # 就没有意义。这个数没有理论依据，所以它不能是藏在代码里的魔法值。
        self.assertEqual(
            latency['grace_minutes'], INSTANT_GROUPING_GRACE_MINUTES,
        )
        self.assertEqual(
            report['metrics']['instant_grouping_grace_minutes'],
            INSTANT_GROUPING_GRACE_MINUTES,
        )

    def test_delayed_grouping_is_separated_from_instant(self):
        """囤了很久才归类的，不能算进「当场」。"""
        collection = self.make_collection(name='宿舍')
        favorite = self.make_favorite(days_ago=30)
        # 把 grouped_at 压到 created_at 之后 10 天：事后归类。
        Favorite.objects.filter(pk=favorite.pk).update(
            grouped_at=favorite.created_at + timedelta(days=10),
        )

        report = build_favorite_collection_quality(days=90)

        latency = report['grouping_latency']
        self.assertEqual(latency['instant_total'], 0)
        self.assertEqual(latency['delayed_total'], 1)
        self.assertEqual(latency['grouped_ever_total'], 1)

    def test_the_grace_boundary_is_inclusive(self):
        """正好卡在宽限边界上算当场，不是事后。

        判据是「距离小于等于宽限」。取等于而不是小于，是因为一个花了正好六十分钟
        的用户和一个花了五十九分钟的用户没有区别，把他们分到两类只会让边界附近的计
        数随机抖动。
        """
        collection = self.make_collection(name='宿舍')
        favorite = self.make_favorite()
        Favorite.objects.filter(pk=favorite.pk).update(
            grouped_at=favorite.created_at
            + timedelta(minutes=INSTANT_GROUPING_GRACE_MINUTES),
        )

        report = build_favorite_collection_quality(days=30)

        self.assertEqual(report['grouping_latency']['instant_total'], 1)
        self.assertEqual(report['grouping_latency']['delayed_total'], 0)

    def test_pre_migration_favorites_are_not_in_any_denominator(self):
        """迁移前就归了组的收藏，归类时刻不可知，四类都不算。

        加列之前归过类的收藏 `grouped_at` 是 NULL。它们既不是当场也不是事后，把它们
        算进分母，「事后归类」的占比会被一批不知道时刻的收藏稀释。数据不可知和样本不
        足是两件事：前者必须明确说「这个数不知道」。
        """
        collection = self.make_collection(name='宿舍')
        # 一条归了类但 grouped_at 为空（迁移前的样子）。
        Favorite.objects.filter(
            pk=self.make_favorite(collection=collection).pk,
        ).update(grouped_at=None)

        report = build_favorite_collection_quality(days=30)

        latency = report['grouping_latency']
        self.assertEqual(latency['grouped_ever_total'], 0)
        self.assertEqual(latency['instant_total'], 0)
        self.assertEqual(latency['delayed_total'], 0)
        self.assertEqual(latency['ungrouped_total'], 0)
        # 当前快照里它仍然是一条已归组的收藏——渗透那张表照实报它。
        self.assertEqual(report['penetration']['grouped_favorite_total'], 1)

    def test_grouping_latency_stays_out_of_the_old_tables(self):
        """新表是新增的，不是把旧表改出来的。

        渗透、规模、空分组、命名四张表一个字段都没动。如果有人把「当场归类率」塞进
        渗透表，这个测试会红——那两件事的分母不同（全部收藏 vs 归过类的收藏），混在
        一张表里会让运营以为有两个独立的信号。
        """
        self.make_favorite(collection=self.make_collection(name='宿舍'))

        report = build_favorite_collection_quality(days=30)

        for row in report['scale_rows']:
            self.assertNotIn('instant_total', row)
            self.assertNotIn('grouped_ever_total', row)
        for row in report['name_rows']:
            self.assertNotIn('instant_total', row)
            self.assertNotIn('grouped_ever_total', row)
        for key in ('instant_total', 'delayed_total', 'released_total'):
            self.assertNotIn(key, report['penetration'])
            self.assertNotIn(key, report['empty_rows'])

    def test_latency_note_names_the_boundary_and_does_not_act(self):
        """建议里要写出宽限天数，并且句尾落在「只报告」上。"""
        for index in range(MIN_BAND_SAMPLE):
            self.make_favorite(collection=self.make_collection(name=f'分组{index}'))

        report = build_favorite_collection_quality(days=30)

        notes = [note for note in report['recommendations']
                 if '分钟内当场归的' in note or '囤了一阵才整理' in note]
        self.assertTrue(notes, '当场 / 事后归类的形状没有进建议')
        self.assertIn(str(INSTANT_GROUPING_GRACE_MINUTES), notes[0])
        self.assertIn('只报告', notes[0])

    def test_a_recently_grouped_favorite_keeps_its_original_created_at(self):
        # 直接把这条边界钉成一条行为断言：一条 60 天前收藏的商品，昨天被移进分组，它在这
        # 个库里的全部痕迹就是 created_at = 60 天前、collection = 那个分组。窗口内新建的
        # 收藏读出来的 collection 是今天读到的值，不是它创建时的值。
        favorite = self.make_favorite(days_ago=60)
        collection = self.make_collection(name='后来才建的分组')
        favorite.collection = collection
        favorite.save(update_fields=['collection'])
        favorite.refresh_from_db()

        self.assertEqual(favorite.collection_id, collection.id)
        self.assertEqual(favorite.created_at.date(), (timezone.now() - timedelta(days=60)).date())

        report = build_favorite_collection_quality(days=30)
        # 这条收藏不在窗口内（60 天前建的），但它现在确实被归了类，所以计入当前快照的渗透。
        self.assertEqual(report['metrics']['window_favorite_count'], 0)
        self.assertEqual(report['penetration']['grouped_favorite_total'], 1)

    # --- 口径二：分母怎么切 ---

    def test_collector_share_uses_favoriters_as_denominator(self):
        # 「建过分组的人占多少」的分母是有收藏的用户，不是全站用户。没有收藏的人看不到
        # 分组功能，把他们算进分母会把渗透率压低，读成「分组没人用」。
        collector = User.objects.create_user(username='fav-group-collector')
        favoriter = User.objects.create_user(username='fav-group-favoriter')
        stranger = User.objects.create_user(username='fav-group-stranger')
        self.make_collection(name='教材', user=collector)
        self.make_favorite(user=collector)
        self.make_favorite(user=favoriter)

        report = build_favorite_collection_quality(days=30)

        self.assertEqual(report['penetration']['favoriter_count'], 2)
        self.assertEqual(report['penetration']['collector_count'], 1)
        self.assertEqual(report['penetration']['collector_share'], 50.0)
        self.assertEqual(report['penetration']['favoriter_only_count'], 1)
        # stranger 一条收藏都没有，他不进任何一个分母。
        self.assertNotIn(stranger.id, [collector.id, favoriter.id])

    def test_grouped_favorite_share_uses_all_favorites_as_denominator(self):
        # 「已归组的收藏占多少」的分母是全部收藏。未分组的收藏就是一条普通收藏，和分组
        # 功能无关——这个比例问的是「已经放进分组收纳盒的收藏有多少」。
        collection = self.make_collection(name='教材')
        self.make_favorite(item=self.make_item(title='归了类的'), collection=collection)
        self.make_favorite(item=self.make_item(title='没归类的'))
        self.make_favorite(item=self.make_item(title='也没归类的'))

        report = build_favorite_collection_quality(days=30)

        self.assertEqual(report['penetration']['favorite_total'], 3)
        self.assertEqual(report['penetration']['grouped_favorite_total'], 1)
        self.assertEqual(report['penetration']['grouped_favorite_share'], 33.3)

    def test_window_counts_are_separate_from_the_current_snapshot(self):
        # 「当前」是快照不是窗口：分组可以被删（删完收藏变未分组）、收藏可以被取消，所以
        # 窗口内的新建规模和当前快照是两件事，分开报、不放进同一张表。
        self.make_collection(name='窗口内建的', days_ago=2)
        self.make_collection(name='更早建的', days_ago=40)
        self.make_favorite(item=self.make_item(title='窗口内收藏的'), days_ago=2)
        self.make_favorite(item=self.make_item(title='更早收藏的'), days_ago=40)

        report = build_favorite_collection_quality(days=30)

        self.assertEqual(report['metrics']['window_collection_count'], 1)
        self.assertEqual(report['metrics']['window_favorite_count'], 1)
        self.assertEqual(report['metrics']['group_total'], 2)
        self.assertEqual(report['metrics']['favorite_total'], 2)

    def test_collector_share_never_exceeds_one_hundred_percent(self):
        # 建分组不要求有收藏：create_favorite_collection 只要求登录。所以一个人完全可以先
        # 建一个空分组、一条收藏都没有。这个比例的分子必须取「有收藏且建过分组的人」，不
        # 能是「建过分组的人」——否则分母（有收藏的用户）不含他、分子含他，占比会超过
        # 100%，报出一个数学上不可能的数。这一条是回归测试。
        self.make_collection(name='先建的空分组')    # 一条收藏都没有
        favoriter = User.objects.create_user(username='fav-group-plain')
        self.make_favorite(user=favoriter, item=self.make_item(title='只收藏'))

        report = build_favorite_collection_quality(days=30)
        penetration = report['penetration']

        self.assertEqual(penetration['favoriter_count'], 1)
        self.assertEqual(penetration['collector_count'], 1)
        self.assertEqual(penetration['collector_with_favorite_count'], 0)
        self.assertEqual(penetration['collector_share'], 0.0)
        self.assertEqual(penetration['collector_only_count'], 1)
        self.assertLessEqual(penetration['collector_share'], 100.0)

    def test_collector_only_users_are_reported_separately(self):
        # 建了分组却一条收藏都没有的人是另一批人：他们用了分组功能、但分组是空的。不该被渗
        # 透率吞掉，也不算「看不到分组功能」。单独报成绝对量。
        # 两个人都只建空分组、不收藏；第三条收藏归第三个用户，他才是「只收藏」的人。
        self.make_collection(name='空分组甲')
        self.make_collection(name='空分组乙', user=self.other)
        stranger = User.objects.create_user(username='fav-group-stranger-2')
        self.make_favorite(user=stranger, item=self.make_item(title='有收藏的'))

        report = build_favorite_collection_quality(days=30)
        penetration = report['penetration']

        self.assertEqual(penetration['collector_only_count'], 2)
        self.assertEqual(penetration['favoriter_only_count'], 1)
        self.assertEqual(penetration['collector_share'], 0.0)
        joined = ''.join(report['recommendations'])
        self.assertIn('一条收藏都没有', joined)

    def test_band_sample_and_name_sample_are_two_different_thresholds(self):
        # 大小档的样本线和名字榜的隐私线是两件事，取同一个数只是量级相同，含义必须分开：小
        # 样本档不给方向（样本线），低频名字不进榜（隐私线）。这里钉住两者在报告里各自暴
        # 露，且大小分布用的是样本线。
        for index in range(MIN_BAND_SAMPLE):
            collection = self.make_collection(name=f'装一条的 {index}')
            self.make_favorite(
                item=self.make_item(title=f'一条 {index}'), collection=collection,
            )
        # 只用一次的名字不该进榜，哪怕它满足大小档的样本量。
        for index in range(MIN_BAND_SAMPLE):
            self.make_collection(name=f'私密名字 {index}')

        report = build_favorite_collection_quality(days=30)

        self.assertIn('min_band_sample', report['metrics'])
        self.assertIn('min_name_sample', report['metrics'])
        labels = [row['label'] for row in report['name_rows']]
        self.assertNotIn('私密名字 0', labels)

    # --- 口径三：空分组分「刚建」和「早就建了还空着」---

    def test_empty_groups_are_split_by_grace_period(self):
        # 空分组有一个良性解释：几秒钟前刚建的，用户还没来得及往里放东西。所以按
        # FavoriteCollection.created_at 拆一刀，只有早于 grace 期的空分组才算真信号。
        fresh = self.make_collection(name='刚建的空分组', days_ago=1)
        stale = self.make_collection(name='早就空着的分组', days_ago=EMPTY_GROUP_GRACE_DAYS + 5)
        used = self.make_collection(name='有收藏的分组', days_ago=EMPTY_GROUP_GRACE_DAYS + 5)
        self.make_favorite(item=self.make_item(title='放进去了'), collection=used)

        report = build_favorite_collection_quality(days=30)
        empty = report['empty_rows']

        self.assertEqual(empty['group_total'], 3)
        self.assertEqual(empty['empty_total'], 2)
        self.assertEqual(empty['stale_empty_total'], 1)
        # 早就空着的占**空分组**的比例，分母是空分组自己，不是全部分组。
        self.assertEqual(empty['stale_share'], 50.0)
        self.assertEqual(empty['stale_user_count'], 1)
        self.assertEqual(empty['empty_share'], 66.7)
        self.assertEqual(empty['grace_days'], EMPTY_GROUP_GRACE_DAYS)

    def test_stale_share_has_no_denominator_when_there_are_no_empty_groups(self):
        # 没有空分组时这个比例不存在，不是 0。空平台报 0% 会被读成「早就空着的比例是
        # 零」，而真相是没有分母。
        self.make_favorite(collection=self.make_collection(name='有收藏的'))
        self.make_favorite(collection=self.make_collection(name='也有收藏的'))

        report = build_favorite_collection_quality(days=30)

        self.assertEqual(report['empty_rows']['empty_total'], 0)
        self.assertIsNone(report['empty_rows']['stale_share'])
        self.assertEqual(report['empty_rows']['empty_share'], 0.0)

    # --- 口径四：分组大小分布 ---

    def test_size_bands_count_groups_not_favorites(self):
        # 大小分布的分子分母都是分组，问的是「分出来一个分组的人往里面放了几条」。
        one = self.make_collection(name='装一条的')
        self.make_favorite(item=self.make_item(title='一'), collection=one)
        many = self.make_collection(name='装五条的')
        for index in range(5):
            self.make_favorite(
                item=self.make_item(title=f'五条之一 {index}'), collection=many,
            )
        self.make_collection(name='空的')

        report = build_favorite_collection_quality(days=30)
        rows = {row['label']: row for row in report['scale_rows']}

        self.assertEqual(rows['1 条']['group_count'], 1)
        self.assertEqual(rows['3-10 条']['group_count'], 1)
        self.assertEqual(rows['20 条以上']['group_count'], 0)
        # 空分组不在任何一档里：0 条不属于任何 SIZE_BANDS，所以档位行加不起来不等于分组总
        # 数。这是有意的——大小分布问的是「往分组里放了几条」，空分组没有「几条」可放。
        self.assertEqual(rows['20 条以上']['group_count'], 0)
        # 分母是全部分组（3 个），存在，所以这一档的占比是 0.0 不是 None。None 只留给分母
        # 为零的情形——那才是「这个数不存在」。
        self.assertEqual(rows['20 条以上']['share_of_groups'], 0.0)
        self.assertEqual(
            sum(row['group_count'] for row in report['scale_rows']), 2,
        )
        self.assertEqual(report['metrics']['group_total'], 3)

    def test_size_shape_stays_flat_when_the_spread_is_inside_the_noise_line(self):
        # 两个档的占比相差不满噪声线，只说分布平坦，不指方向。
        for index in range(MIN_NAME_SAMPLE):
            collection = self.make_collection(name=f'装一条的 {index}')
            self.make_favorite(
                item=self.make_item(title=f'一条 {index}'), collection=collection,
            )
        for index in range(MIN_NAME_SAMPLE):
            collection = self.make_collection(name=f'装两条的 {index}')
            for slot in range(2):
                self.make_favorite(
                    item=self.make_item(title=f'两条 {index}-{slot}'), collection=collection,
                )

        report = build_favorite_collection_quality(days=30)
        shape = report['size_shape']

        self.assertTrue(shape['has_comparison'])
        # 3 组对 3 组，两档占比都是 50%，相差 0 个百分点，落在噪声线内。
        self.assertEqual(shape['kind'], 'flat')
        self.assertEqual(shape['spread_points'], 0.0)
        self.assertLess(shape['spread_points'], NOISE_POINTS)
        self.assertIn('噪声', shape['summary'])

    def test_size_shape_is_skewed_when_one_band_dominates(self):
        # 一个档明显占多数时给出方向，并挂上替它说话的数字。
        for index in range(MIN_NAME_SAMPLE + 2):
            collection = self.make_collection(name=f'装一条的 {index}')
            self.make_favorite(
                item=self.make_item(title=f'一条 {index}'), collection=collection,
            )
        for index in range(MIN_NAME_SAMPLE):
            collection = self.make_collection(name=f'装五条的 {index}')
            for slot in range(5):
                self.make_favorite(
                    item=self.make_item(title=f'五条 {index}-{slot}'), collection=collection,
                )

        report = build_favorite_collection_quality(days=30)
        shape = report['size_shape']

        self.assertEqual(shape['kind'], 'skewed')
        self.assertGreaterEqual(shape['spread_points'], NOISE_POINTS)
        self.assertIn('1 条', shape['summary'])

    def test_size_shape_has_no_comparison_with_too_few_groups(self):
        # 分组数量太少时不给方向，只说读不出来。
        self.make_collection(name='只有一个')

        report = build_favorite_collection_quality(days=30)

        self.assertFalse(report['size_shape']['has_comparison'])
        self.assertIn('太少', report['size_shape']['summary'])

    # --- 口径五：分组名收敛 ---

    def test_name_counts_are_user_counts_not_group_counts(self):
        # 分组名在同一个用户内唯一（UniqueConstraint(fields=['user', 'name'])），所以一个
        # 名字的计数天然就是「用过这个名字的用户数」。同一个名字被同一个用户建两次只会留
        # 下一条（get_or_create），所以 group_count 和 user_count 在这里恰好相等——但写成
        # 用户数才说得清这个数是什么。
        for index in range(MIN_NAME_SAMPLE):
            user = User.objects.create_user(username=f'fav-group-namer-{index}')
            collection = self.make_collection(name='教材', user=user)
            self.make_favorite(
                user=user, item=self.make_item(title=f'教材收藏 {index}'),
                collection=collection,
            )

        report = build_favorite_collection_quality(days=30)
        row = report['name_rows'][0]

        self.assertEqual(row['label'], '教材')
        self.assertEqual(row['user_count'], MIN_NAME_SAMPLE)
        self.assertEqual(row['group_count'], MIN_NAME_SAMPLE)
        self.assertEqual(row['average_size'], 1.0)

    def test_names_used_by_too_few_users_are_not_published(self):
        # 这是隐私线不是样本线：一个只被一个人用过的分组名，报出来等于把某个人的个人命名
        # 公开展示。所以低频名字不进榜。
        lonely = User.objects.create_user(username='fav-group-lonely')
        self.make_collection(name='只有我一个人这么叫', user=lonely)
        for index in range(MIN_NAME_SAMPLE):
            user = User.objects.create_user(username=f'fav-group-popular-{index}')
            self.make_collection(name='教材', user=user)

        report = build_favorite_collection_quality(days=30)
        labels = [row['label'] for row in report['name_rows']]

        self.assertNotIn('只有我一个人这么叫', labels)
        self.assertIn('教材', labels)

    def test_name_rows_are_sorted_by_user_count(self):
        # 榜单按用户数排，运营第一眼看到的是最收敛的那个名字。
        for index in range(MIN_NAME_SAMPLE + 2):
            self.make_collection(name='教材', user=User.objects.create_user(
                username=f'fav-group-jc-{index}',
            ))
        for index in range(MIN_NAME_SAMPLE):
            self.make_collection(name='宿舍', user=User.objects.create_user(
                username=f'fav-group-ss-{index}',
            ))

        report = build_favorite_collection_quality(days=30)

        self.assertEqual(report['name_rows'][0]['label'], '教材')
        self.assertEqual(report['name_rows'][1]['label'], '宿舍')

    # --- 口径六：空平台 ---

    def test_empty_platform_reports_no_shares_at_all(self):
        # 空平台不报 0%：渗透、空分组率这些比例的分母为零，这个数不存在，不是 0。唯一允许
        # 出现的 0 是「当前一共有 0 个分组」这种绝对量。
        report = build_favorite_collection_quality(days=30)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['metrics']['group_total'], 0)
        self.assertIsNone(report['penetration']['grouped_favorite_share'])
        self.assertIsNone(report['penetration']['collector_share'])
        self.assertIsNone(report['empty_rows']['stale_share'])
        for row in report['scale_rows']:
            self.assertIsNone(row['share_of_groups'])
        self.assertEqual(report['name_rows'], [])
        self.assertFalse(report['size_shape']['has_comparison'])
        self.assertIn('还没有任何心愿单分组', report['recommendations'][0])

    def test_report_is_empty_when_no_group_exists_but_favorites_do(self):
        # 有人收藏但没人建分组：has_data 是 False（没有分组就没有分组的形状），但渗透率有分
        # 母，照报——「建过分组的人占有收藏用户的比例是 0%」在这里是一个真正的 0，因为分
        # 母（有收藏的用户）存在。
        self.make_favorite(item=self.make_item(title='只收藏不分组'))

        report = build_favorite_collection_quality(days=30)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['penetration']['favoriter_count'], 1)
        # 分母（有收藏的用户）存在，建过分组的人是 0 个，所以这是真正的 0%。
        self.assertEqual(report['penetration']['collector_share'], 0.0)
        # 分母（全部收藏）同样存在，已归组 0 条，所以也是 0.0 不是 None。None 只留给分母
        # 为零的情形。
        self.assertEqual(report['penetration']['grouped_favorite_share'], 0.0)

    # --- 口径七：只报告 ---

    def test_the_module_never_writes_anything(self):
        # 只报告：不改任何一条 Favorite.collection、不删分组、不改分组名、不建分组。跑完之
        # 后库里的每一行都得原样还在，一列都没被碰过。
        collection = self.make_collection(name='教材')
        favorite = self.make_favorite(item=self.make_item(title='会被读到'), collection=collection)
        ungrouped = self.make_favorite(item=self.make_item(title='不会被归类'))

        build_favorite_collection_quality(days=30)

        favorite.refresh_from_db()
        ungrouped.refresh_from_db()
        collection.refresh_from_db()
        self.assertEqual(favorite.collection_id, collection.id)
        self.assertIsNone(ungrouped.collection_id)
        self.assertEqual(collection.name, '教材')
        self.assertEqual(Favorite.objects.count(), 2)
        self.assertEqual(FavoriteCollection.objects.count(), 1)

    def test_days_outside_the_allowed_set_falls_back_to_thirty(self):
        # 周期不在可选集合里时回落到 30 天，和其余模块一致。
        self.make_collection(name='教材', days_ago=2)

        report = build_favorite_collection_quality(days=17)

        self.assertEqual(report['period_days'], 30)
        self.assertEqual(report['metrics']['window_collection_count'], 1)

    def test_recommendations_point_at_the_preset_name_gap(self):
        # 名字反复出现时报「站内缺一份预设分组名」，这条建议必须挂在替它说话的数字上。
        for index in range(MIN_NAME_SAMPLE):
            user = User.objects.create_user(username=f'fav-group-rec-{index}')
            self.make_collection(name='教材', user=user)
            self.make_favorite(user=user, item=self.make_item(title=f'教材 {index}'))

        report = build_favorite_collection_quality(days=30)

        joined = ''.join(report['recommendations'])
        self.assertIn('教材', joined)
        self.assertIn('预设', joined)
        self.assertIn('只报告', joined)

class FavoriteGroupedAtTests(TestCase):
    """`Favorite.grouped_at` 的写入路径：一个字段不够，两条路径都要接。

    `Favorite.collection` 有两条写入路径——`views.update_favorite_collection` 和
    Django admin 的 change 表单。只接视图层，后台每改一条收藏就在库里留一个假数据：
    `grouped_at` 停在 NULL，那条收藏会被读成「从未归类」，而它其实归过类。所以写入
    放在模型层。这些测试钉住的正是这一点。
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='grouped-at-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='归类时刻测试分类')
        self.location = CampusLocation.objects.create(
            name='归类时刻测试地点', building='归类时刻测试楼', is_public=True,
        )
        self.item = Item.objects.create(
            title='归类时刻测试商品', description='d', price='30.00',
            category=self.category, location=self.location,
            condition='良好', seller=self.user, status='available',
        )
        self.collection = FavoriteCollection.objects.create(
            user=self.user, name='教材',
        )

    def make_favorite(self, *, collection=None):
        return Favorite.objects.create(
            user=self.user, item=self.item, collection=collection,
        )

    def test_grouping_in_stamps_grouped_at(self):
        """把收藏归进分组，`grouped_at` 必须被写下来。"""
        favorite = self.make_favorite()
        self.assertIsNone(favorite.grouped_at)

        favorite.collection = self.collection
        favorite.save(update_fields=['collection'])

        favorite.refresh_from_db()
        self.assertIsNotNone(favorite.grouped_at)

    def test_releasing_a_group_also_stamps_grouped_at(self):
        """把收藏移出分组，同样要写 `grouped_at`。

        这一条最容易被漏：`update_favorite_collection` 支持把 `collection_id` 传
        空，也就是移出分组。如果只记「归进去」，这批人会被算进「从未归类」，而他们其
        实是「用过又不用了」——那是比「从未归类」更强的信号。字段的语义是「分组最后
        一次发生变化」，归进去和移出来都算。
        """
        favorite = self.make_favorite(collection=self.collection)
        Favorite.objects.filter(pk=favorite.pk).update(grouped_at=None)

        favorite.collection = None
        favorite.save(update_fields=['collection'])

        favorite.refresh_from_db()
        self.assertIsNotNone(favorite.grouped_at)
        self.assertIsNone(favorite.collection_id)

    def test_update_fields_is_extended_so_the_column_lands(self):
        """调用方只列 `['collection']` 时，`grouped_at` 照样要落库。

        `save(update_fields=[...])` 只写列出来的字段。不把 `grouped_at` 追加进去，
        视图那一行 `favorite.save(update_fields=['collection'])` 就会静默地不写它
        ——而视图层看不出任何异常。这是覆写 `save()` 的理由，也是它最容易出错的地方。
        """
        favorite = self.make_favorite()

        favorite.collection = self.collection
        favorite.save(update_fields=['collection'])

        favorite.refresh_from_db()
        self.assertIsNotNone(favorite.grouped_at)

    def test_an_unchanged_collection_does_not_move_grouped_at(self):
        """分组没变，`grouped_at` 不许动。

        判据是 `collection_id` 变了，不是 `collection` 实例变了。同一个分组被重新赋
        值，`collection_id` 不变，那确实没有发生变化——动它会让「事后归类」被误判成
        「当场归类」，而这个字段的全部价值就在于它只在这一种变化上动。
        """
        favorite = self.make_favorite(collection=self.collection)
        stamped = timezone.now() - timedelta(days=10)
        Favorite.objects.filter(pk=favorite.pk).update(grouped_at=stamped)

        favorite.collection = self.collection
        favorite.save(update_fields=['collection'])

        favorite.refresh_from_db()
        self.assertEqual(favorite.grouped_at, stamped)

    def test_a_plain_save_without_grouping_does_not_move_grouped_at(self):
        """一次和分组无关的保存，不许刷新 `grouped_at`。

        这一条守的是「不用 `auto_now`」那个决定。`auto_now` 会在每次 `save()` 上无
        条件刷新，那么将来谁给这个模型加一个字段、顺手保存一次，事后归类就会被推成
        当场归类。
        """
        favorite = self.make_favorite(collection=self.collection)
        stamped = timezone.now() - timedelta(days=10)
        Favorite.objects.filter(pk=favorite.pk).update(grouped_at=stamped)
        # 从库里重新加载，还原真实场景：admin 和 update_favorite_collection 都是
        # 先加载实例再保存，内存里的 grouped_at 就是库里的值。不 refresh 的话这次
        # 全量 save() 会把内存里的旧值写回——那是 Django 全量保存的既有行为，任何
        # 字段都如此，不是 grouped_at 特有的问题。
        favorite.refresh_from_db()

        favorite.save()

        favorite.refresh_from_db()
        self.assertEqual(favorite.grouped_at, stamped)

    def test_a_new_favorite_with_a_group_is_stamped_at_creation(self):
        """新建时就带了分组的，创建那一刻就是归类时刻。

        后台可以一次性建好一条已归组的收藏。没有这一条，那些行会带着 `collection`
        却读不出归类时刻，被算进「迁移前归组」那一类——而它们并不是。
        """
        favorite = Favorite.objects.create(
            user=self.user, item=self.item, collection=self.collection,
        )

        favorite.refresh_from_db()
        self.assertIsNotNone(favorite.grouped_at)

    def test_a_new_favorite_without_a_group_has_no_stamp(self):
        """新建时没有分组，`grouped_at` 必须是 NULL，不能是创建时刻。

        否则每一条收藏都会被读成「当场归类」，而它们压根没被归过类——这个字段就废了。
        """
        favorite = self.make_favorite()

        favorite.refresh_from_db()
        self.assertIsNone(favorite.grouped_at)

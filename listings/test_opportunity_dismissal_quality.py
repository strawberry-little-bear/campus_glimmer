# -*- coding: utf-8 -*-
"""为「用户按过不再显示的机会」钉住口径。

`opportunity_feed.build_opportunity_feed` 是站内唯一一条个性化推荐流，用户在机会流里点
「暂不展示」落一行 `OpportunityDismissal`，读路径只有机会流自己。这个模块把这些忽略读出
来只报告不改写。这些测试要钉住的是比数字本身更要紧的那些取舍：

1. **不报忽略率。** 分母（展示过的机会）不可得，所以报告里没有任何一个「忽略率」字
   段——连占位都不给。
2. 归期按 `created_at`，重复忽略的判据是 `expires_at` 和 `created_at` 的距离。
3. 按 `kind` 拆、按分数档拆、按理由拆，三处的分子分母都是忽略，报的是占比不是率。
4. 重复忽略是本模块唯一敢报出来的比例，因为它有自己的分母（窗口内全部忽略）。
5. 分数在忽略时刻重算，走两个匹配器自己的函数；今天匹配不上的行仍然计入规模。
6. 样本不足或占比相差不满噪声线时只报数字，不给方向。
7. 空平台不报 0%，也不报 0 条以外的任何形状。
8. 只报告：不改 `expires_at`、不删行、不动两个匹配器的任何常数。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .demand_matching import _match_demand
from .lost_found_matching import score_lost_found_posts
from .models import CampusLocation, Category, DemandPost, Item, LostFoundPost, OpportunityDismissal
from .opportunity_dismissal_quality import (
    MIN_BAND_SAMPLE, MIN_REASON_SAMPLE, NOISE_POINTS, SILENCE_DAYS,
    build_opportunity_dismissal_quality,
)


class OpportunityDismissalQualityTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='dismissal-seller', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='dismissal-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='忽略统计测试分类')
        self.location = CampusLocation.objects.create(
            name='忽略统计测试地点', building='忽略统计测试楼', is_public=True,
        )

    # --- 造数据的帮手 ---

    def make_item(self, title='忽略统计测试商品', *, seller=None, price='30.00'):
        return Item.objects.create(
            title=title, description='用于忽略统计', price=price,
            category=self.category, location=self.location,
            condition='良好', seller=seller or self.seller, status='available',
        )

    def make_demand(self, title='求购忽略统计测试商品', *, requester=None,
                    max_price='50.00'):
        return DemandPost.objects.create(
            requester=requester or self.other,
            title=title, description='希望找到一件',
            category=self.category, location=self.location,
            max_price=max_price, status='active',
        )

    def make_dismissal(self, *, kind='demand', target=None, days_ago=1, repeated=False,
                       seller=None):
        """建一行忽略记录，`repeated=True` 表示这个人按过不止一次。

        `expires_at` 和 `created_at` 都要回填：`created_at` 是 `auto_now_add`，建行时拿
        到的是真实墙上时间，而统计窗口按它切，不回填会让这一行落在窗口右端之外。重复忽
        略的判据是两列的距离，所以两列都要显式给出来——第一次按下时两列相差 30 天，第二
        次按下时 `expires_at` 被推到「第二次的时刻 + 30 天」，距离就落到 (30, 35] 区间
        里。
        """
        target = target or self.make_demand()
        filters = (
            {'demand': target, 'lost_found_post': None}
            if kind == 'demand'
            else {'demand': None, 'lost_found_post': target}
        )
        dismissal = OpportunityDismissal.objects.create(
            user=seller or self.seller, kind=kind,
            expires_at=timezone.now() + timedelta(days=SILENCE_DAYS), **filters,
        )
        created_at = timezone.now() - timedelta(days=days_ago)
        if repeated:
            # 第二次忽略发生在三天后：expires_at 被推到那一刻 + 30 天，
            # 两列距离变成 33 天，落在判据区间里。
            expires_at = created_at + timedelta(days=SILENCE_DAYS + 3)
        else:
            expires_at = created_at + timedelta(days=SILENCE_DAYS)
        OpportunityDismissal.objects.filter(pk=dismissal.pk).update(
            created_at=created_at, expires_at=expires_at,
        )
        dismissal.refresh_from_db()
        return dismissal

    def make_lost_found_pair(self):
        """建一对方向相反的失物招领记录，返回「别人的那条」作为忽略目标。"""
        mine = LostFoundPost.objects.create(
            reporter=self.seller, post_type='lost', title='遗失忽略统计测试物品',
            description='在测试地点附近遗失', category=self.category,
            location=self.location, occurred_at=timezone.now() - timedelta(hours=3),
            identifying_features='黑色', status='active',
        )
        theirs = LostFoundPost.objects.create(
            reporter=self.other, post_type='found', title='捡到忽略统计测试物品',
            description='在测试地点捡到', category=self.category,
            location=self.location, occurred_at=timezone.now() - timedelta(hours=2),
            identifying_features='黑色', status='active',
        )
        return mine, theirs

    # --- 分母：本模块不报忽略率 ---

    def test_the_report_never_publishes_a_dismissal_rate(self):
        # 分母（展示过的机会）不可得：OpportunityDismissal 只记忽略不记看过，机会流页面
        # 也不产生任何可反推的记录。所以报告里没有任何一个「率」字段，连占位都不给——
        # 报一个用页面构造当分母的估算率，会让运营以为自己量过，那比没有数字更糟。
        self.make_dismissal()

        report = build_opportunity_dismissal_quality(days=30)

        for key in ('dismissal_rate', 'ignore_rate', 'rate'):
            self.assertNotIn(key, report)
            self.assertNotIn(key, report['metrics'])
            self.assertNotIn(key, report['repeat_stats'])
        for row in report['kind_rows']:
            self.assertNotIn('dismissal_rate', row)
        for row in report['band_rows']:
            self.assertNotIn('dismissal_rate', row)
            self.assertNotIn('rate', row)
        for row in report['reason_rows']:
            self.assertNotIn('dismissal_rate', row)

    def test_band_and_reason_shares_are_shapes_not_rates(self):
        # 按分数档和按理由拆开的两处，分子分母都是忽略：share_of_kind 是「这一档占全部
        # 可打分忽略的比例」，不是「这一档的机会被忽略的比例」。差别在于后者需要一个
        # 展示过分母，而它拿不到。
        item = self.make_item()
        demand = self.make_demand()
        for index in range(MIN_BAND_SAMPLE + 1):
            seller = User.objects.create_user(username=f'band-seller-{index}')
            self.make_item(title=f'分数档商品 {index}', seller=seller)
            self.make_dismissal(target=demand, seller=seller)

        report = build_opportunity_dismissal_quality(days=30)
        band = next(
            row for row in report['band_rows']
            if row['kind'] == 'demand' and row['dismissal_count'] == MIN_BAND_SAMPLE + 1
        )
        self.assertEqual(band['share_of_kind'], 100.0)
        # 分数重算走 _match_demand 本身：分类 4 分、地点 2 分、价格 2 分、关键词 3 分，
        # 合计 11 分，落在 9-11 分档。
        self.assertEqual(band['label'], '9-11 分')
        self.assertEqual(band['average_score'], _match_demand(demand, item)[0])

    # --- 归期与规模 ---

    def test_dismissals_are_counted_by_created_at(self):
        # 归期按 created_at，这是第一次按下「不再显示」的时刻。一行 60 天前首次忽略的记
        # 录，即使 expires_at 还在未来也不算进 30 天窗口。
        self.make_dismissal(days_ago=60)
        self.make_dismissal(days_ago=2, target=self.make_demand(title='另一条求购'))

        report = build_opportunity_dismissal_quality(days=30)

        self.assertEqual(report['metrics']['dismissal_total'], 1)
        self.assertTrue(report['has_data'])

    def test_kind_rows_split_demand_from_lost_found(self):
        demand = self.make_demand()
        self.make_item()
        self.make_dismissal(target=demand)
        _, theirs = self.make_lost_found_pair()
        self.make_dismissal(kind='lost_found', target=theirs)

        report = build_opportunity_dismissal_quality(days=30)
        by_key = {row['key']: row for row in report['kind_rows']}

        self.assertEqual(by_key['demand']['dismissal_count'], 1)
        self.assertEqual(by_key['lost_found']['dismissal_count'], 1)
        self.assertEqual(by_key['demand']['user_count'], 1)
        self.assertEqual(by_key['demand']['per_user'], 1.0)

    def test_an_empty_campus_reports_nothing_rather_than_zero(self):
        # 一条忽略都没有的时候，占比、平均分、per_user 全都不存在，不是 0。空平台报 0%
        # 会让运营以为用户在大规模忽略机会，而真相是没人按过按钮。
        report = build_opportunity_dismissal_quality(days=30)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['metrics']['dismissal_total'], 0)
        self.assertIsNone(report['metrics']['repeat_share'])
        self.assertIsNone(report['repeat_stats']['repeat_share'])
        for row in report['kind_rows']:
            self.assertIsNone(row['per_user'])
        for row in report['band_rows']:
            self.assertIsNone(row['share_of_kind'])
            self.assertIsNone(row['average_score'])
        for shape in report['score_shape'].values():
            self.assertFalse(shape['has_comparison'])
        self.assertTrue(any(
            '没有任何忽略记录' in note for note in report['recommendations']
        ))

    # --- 重复忽略 ---

    def test_a_repeated_dismissal_is_counted_separately(self):
        # 重复忽略的判据是 expires_at 和 created_at 的距离：第一次按下时相差 30 天，第二
        # 次按下时 expires_at 被推到「第二次的时刻 + 30 天」，距离就落到 (30, 35] 里。
        demand = self.make_demand()
        self.make_dismissal(target=demand, repeated=True)

        report = build_opportunity_dismissal_quality(days=30)

        self.assertEqual(report['metrics']['repeated_count'], 1)
        self.assertEqual(report['repeat_stats']['repeat_share'], 100.0)
        self.assertEqual(report['repeat_stats']['user_count'], 1)
        self.assertEqual(
            report['repeat_stats']['by_kind']['demand']['repeated_count'], 1,
        )

    def test_a_single_dismissal_is_not_counted_as_repeated(self):
        # 只按过一次的行，两列距离就是 30 天整，不算重复忽略。把首次忽略误判成重复会让
        # 这个数恒等于忽略总数，第 4 问直接作废。
        self.make_dismissal()

        report = build_opportunity_dismissal_quality(days=30)

        self.assertEqual(report['metrics']['repeated_count'], 0)
        self.assertEqual(report['repeat_stats']['repeat_share'], 0.0)

    def test_a_dismissal_whose_silence_ran_out_long_ago_is_not_repeated(self):
        # 两列距离超过 35 天的行不算重复忽略：那更可能是数据里的一条老记录，而不是「第
        # 二次忽略发生在第一次之后的五天内」。判据的区间是 (30, 35]，见
        # REPEAT_WINDOW_TOLERANCE。
        demand = self.make_demand()
        dismissal = self.make_dismissal(target=demand)
        created_at = timezone.now() - timedelta(days=20)
        OpportunityDismissal.objects.filter(pk=dismissal.pk).update(
            created_at=created_at,
            expires_at=created_at + timedelta(days=SILENCE_DAYS + 40),
        )

        report = build_opportunity_dismissal_quality(days=30)

        self.assertEqual(report['metrics']['repeated_count'], 0)

    def test_repeat_share_is_the_only_rate_the_module_publishes(self):
        # 重复忽略占全部忽略的比例有自己的分母（窗口内全部忽略），所以它是本模块唯一敢
        # 报出来的比例。它回答的是「忽略有没有用」，而不是「有多少机会被忽略」。
        for index in range(4):
            self.make_item(title=f'重复忽略商品 {index}')
            self.make_dismissal(
                target=self.make_demand(title=f'求购 {index}'), repeated=(index < 2),
            )

        report = build_opportunity_dismissal_quality(days=30)

        self.assertEqual(report['repeat_stats']['repeated_count'], 2)
        self.assertEqual(report['repeat_stats']['repeat_share'], 50.0)
        # 只有 2 条重复忽略，够不上 MIN_REASON_SAMPLE：小样本时只报数字，不给「静默期
        # 对一部分人无效」这个方向——它会让人拿两条记录去改一个 30 天的常数。
        self.assertTrue(report['repeat_stats']['is_small_sample'])
        self.assertFalse(any(
            '静默期' in note for note in report['recommendations']
        ))

        # 凑够样本之后，方向才出现，而且必须挂在自己的分母上。注意门槛数的是**重复忽略
        # 本身**的条数，不是全部忽略的条数——所以这里要造出 12 条重复忽略。
        for index in range(MIN_REASON_SAMPLE):
            self.make_item(title=f'重复忽略商品 {index}')
            self.make_dismissal(
                target=self.make_demand(title=f'求购 {index}'), repeated=True,
            )

        report = build_opportunity_dismissal_quality(days=30)

        self.assertFalse(report['repeat_stats']['is_small_sample'])
        self.assertTrue(any(
            '静默期' in note for note in report['recommendations']
        ))

    # --- 分数与理由的重算 ---

    def test_scores_are_recomputed_with_the_matchers_themselves(self):
        # 分数走 _match_demand 和 score_lost_found_posts 自己的函数，不在运营模块里另写
        # 一套：两套打分逻辑算出两个分数，比没有分数更糟。
        demand = self.make_demand()
        item = self.make_item()
        self.make_dismissal(target=demand)
        mine, theirs = self.make_lost_found_pair()
        self.make_dismissal(kind='lost_found', target=theirs)

        report = build_opportunity_dismissal_quality(days=30)
        demand_band = next(
            row for row in report['band_rows']
            if row['kind'] == 'demand' and row['dismissal_count']
        )
        lost_band = next(
            row for row in report['band_rows']
            if row['kind'] == 'lost_found' and row['dismissal_count']
        )
        self.assertEqual(demand_band['average_score'], _match_demand(demand, item)[0])
        self.assertEqual(lost_band['average_score'], score_lost_found_posts(mine, theirs)[0])
        # 失物侧只认 30 分以上的机会，和机会流自己的门槛一致。
        self.assertGreaterEqual(lost_band['low'], 30)

    def test_reasons_come_from_the_matchers_too(self):
        # 理由同样重算：求购侧是 _match_demand 返回的第二值，失物侧是
        # score_lost_found_posts 返回的第二值拼起来的。
        demand = self.make_demand()
        self.make_item()
        self.make_dismissal(target=demand)
        _, theirs = self.make_lost_found_pair()
        self.make_dismissal(kind='lost_found', target=theirs)

        report = build_opportunity_dismissal_quality(days=30)
        by_kind = {}
        for row in report['reason_rows']:
            by_kind.setdefault(row['kind'], []).append(row)

        # 默认标题「忽略统计测试商品」和求购标题共享「忽略统计」这个关键词，所以理由里
        # 还多一条关键词命中。断言前缀，不断言整串——整串会随匹配器改关键词规则而变。
        self.assertTrue(
            by_kind['demand'][0]['label'].startswith('分类一致、地点一致、价格符合预算')
        )
        self.assertIn('物品分类一致', by_kind['lost_found'][0]['label'])

    def test_a_dismissal_whose_item_is_gone_still_counts_in_scale(self):
        # 今天已经没有在售商品能匹配上的行，进不了按分数档和按理由的切分，但仍然计入规
        # 模——它是「忽略之后把自己的商品下架了」的那一批，本身就是一个信号。
        demand = self.make_demand()
        self.make_dismissal(target=demand)
        self.make_item().delete()

        report = build_opportunity_dismissal_quality(days=30)

        self.assertEqual(report['metrics']['dismissal_total'], 1)
        self.assertEqual(report['metrics']['scored_total'], 0)
        self.assertEqual(report['metrics']['unscored_total'], 1)
        self.assertIn('已经匹配不上', report['summary'])
        for row in report['band_rows']:
            self.assertEqual(row['dismissal_count'], 0)
            self.assertIsNone(row['share_of_kind'])

    def test_the_recomputed_score_is_not_the_score_at_dismissal_time(self):
        # 边界：复算读的是今天的在售商品，不是忽略那一刻的商品。把商品下架再建一件不同
        # 分类的，分数就变了——所以面板上必须写清这是「今天再进池的分数」。
        demand = self.make_demand()
        self.make_item()
        self.make_dismissal(target=demand)
        # 把唯一那件在售商品换成另一个分类：分类不再一致，_match_demand 直接返回
        # None，这一行就落进「未匹配到具体依据」，按分数档的切分里一条都没有。
        Item.objects.all().delete()
        other_category = Category.objects.create(name='忽略统计另一个分类')
        Item.objects.create(
            title='换了分类的商品', description='用于忽略统计', price='30.00',
            category=other_category, location=self.location,
            condition='良好', seller=self.seller, status='available',
        )

        report = build_opportunity_dismissal_quality(days=30)

        # 分类不再一致，_match_demand 直接返回 None，这一行落到「未匹配到具体依据」。
        self.assertEqual(report['metrics']['scored_total'], 0)
        self.assertEqual(report['metrics']['unscored_total'], 1)
        for row in report['band_rows']:
            self.assertEqual(row['dismissal_count'], 0)
        # 理由那一侧同样一条都没有，但仍然计入规模。
        self.assertEqual(report['metrics']['dismissal_total'], 1)
        self.assertEqual(
            [row for row in report['reason_rows'] if row['dismissal_count']], [],
        )

    # --- 门槛与噪声 ---

    def test_a_band_with_too_few_samples_gets_no_verdict(self):
        demand = self.make_demand()
        self.make_item()
        self.make_dismissal(target=demand)

        report = build_opportunity_dismissal_quality(days=30)
        band = next(
            row for row in report['band_rows']
            if row['kind'] == 'demand' and row['dismissal_count']
        )

        self.assertTrue(band['is_small_sample'])
        self.assertLess(band['dismissal_count'], MIN_BAND_SAMPLE)
        for shape in report['score_shape'].values():
            self.assertFalse(shape['has_comparison'])
        # 只有一档够 12 条时，模块的原话是「达到 12 条忽略的分数档不足两个」：档间无从
        # 比较，就不指方向。
        self.assertIn(
            f'达到 {MIN_BAND_SAMPLE} 条忽略的分数档不足两个',
            report['score_shape']['demand']['summary'],
        )

    def test_bands_within_noise_are_not_separated(self):
        # 两个分数档的占比相差不满 NOISE_POINTS 个百分点，视为噪声。同一个量级的档之间
        # 的这点差别，运营看不出来、也动不了手。
        # 高分那条分类、地点、价格全部命中，是 11 分；低分那条把分类、地点、价格全部摘
        # 掉，只剩关键词「自行车」命中商品标题，拿 3 分，落在 2-4 分档。两边各
        # MIN_BAND_SAMPLE 条，占比就是 50:50，相差 0 个百分点，判成噪声。
        high_demand = self.make_demand(title='高分求购', max_price='50.00')
        low_demand = self.make_demand(title='求购一辆自行车', max_price=None)
        low_demand.category = None
        low_demand.save(update_fields=['category'])
        low_demand.location = None
        low_demand.save(update_fields=['location'])
        for index in range(MIN_BAND_SAMPLE):
            seller = User.objects.create_user(username=f'noise-low-{index}')
            self.make_item(title=f'低分自行车 {index}', seller=seller)
            self.make_dismissal(target=low_demand, seller=seller)
            self.make_dismissal(target=high_demand, seller=seller)

        report = build_opportunity_dismissal_quality(days=30)
        shape = report['score_shape']['demand']

        self.assertTrue(shape['has_comparison'])
        self.assertEqual(shape['kind'], 'flat')
        self.assertLess(shape['spread_points'], NOISE_POINTS)

    def test_a_skewed_band_is_named_with_its_share(self):
        # 占比明显偏向某一档时，结论要点出是哪一档、占比多少——这是唯一能指向具体改动的
        # 切分。两档都要凑够 MIN_BAND_SAMPLE：只有一档有量时档间无从比较，模块只报样本
        # 不足。高分档 24 条、低分档 12 条，占比 66.7 对 33.3，相差 33.4 个百分点。
        demand = self.make_demand()
        low_demand = self.make_demand(title='求购一辆自行车', max_price=None)
        low_demand.category = None
        low_demand.save(update_fields=['category'])
        low_demand.location = None
        low_demand.save(update_fields=['location'])
        for index in range(MIN_BAND_SAMPLE * 2):
            seller = User.objects.create_user(username=f'skew-{index}')
            self.make_item(title=f'偏斜商品 {index}', seller=seller)
            self.make_dismissal(target=demand, seller=seller)
        for index in range(MIN_BAND_SAMPLE):
            seller = User.objects.create_user(username=f'skew-low-{index}')
            self.make_item(title=f'偏斜自行车 {index}', seller=seller)
            self.make_dismissal(target=low_demand, seller=seller)

        report = build_opportunity_dismissal_quality(days=30)
        shape = report['score_shape']['demand']

        self.assertTrue(shape['has_comparison'])
        self.assertEqual(shape['kind'], 'skewed')
        self.assertEqual(shape['strongest_label'], '9-11 分')
        self.assertEqual(shape['strongest_share'], 66.7)
        self.assertEqual(shape['weakest_share'], 33.3)
        self.assertTrue(any(
            '9-11 分' in note for note in report['recommendations']
        ))

    def test_a_reason_with_too_few_samples_gets_no_verdict(self):
        demand = self.make_demand()
        self.make_item()
        self.make_dismissal(target=demand)

        report = build_opportunity_dismissal_quality(days=30)
        row = next(row for row in report['reason_rows'] if row['dismissal_count'])

        self.assertTrue(row['is_small_sample'])
        self.assertLess(row['dismissal_count'], MIN_REASON_SAMPLE)

    def test_reasons_are_grouped_by_text_not_by_user_or_item(self):
        # 按理由聚合，不按用户、不按商品：按用户是排名（隐私，也不是运营射程），按商品让
        # 被忽略一万次和一次不可比。同一个用户按两条不同求购、两条求购的分数档不同，理由
        # 也只有一行各自的。
        first = self.make_demand(title='求购甲')
        second = self.make_demand(title='求购乙')
        self.make_item()
        self.make_dismissal(target=first)
        self.make_dismissal(target=second)

        report = build_opportunity_dismissal_quality(days=30)
        demand_rows = [row for row in report['reason_rows'] if row['kind'] == 'demand']

        self.assertEqual(len(demand_rows), 1)
        self.assertEqual(demand_rows[0]['dismissal_count'], 2)
        self.assertEqual(demand_rows[0]['share_of_kind'], 100.0)

    # --- 只报告 ---

    def test_building_the_report_writes_nothing_back(self):
        # 只报告：不改 expires_at、不删行、不动静默期、不给任何人排名。
        demand = self.make_demand()
        dismissal = self.make_dismissal(target=demand, repeated=True)
        before = OpportunityDismissal.objects.get(pk=dismissal.pk)
        item = self.make_item()

        build_opportunity_dismissal_quality(days=30)

        after = OpportunityDismissal.objects.get(pk=dismissal.pk)
        self.assertEqual(after.expires_at, before.expires_at)
        self.assertEqual(after.created_at, before.created_at)
        self.assertEqual(after.kind, before.kind)
        self.assertEqual(OpportunityDismissal.objects.count(), 1)
        # 复算只读在售商品，不碰它们的状态。
        self.assertEqual(Item.objects.get(pk=item.pk).status, 'available')

    def test_the_report_never_ranks_users(self):
        # 不按用户聚合、不报「谁最爱按不再显示」——那是一份指责清单，不是一份看板。
        for index in range(3):
            seller = User.objects.create_user(username=f'rank-seller-{index}')
            self.make_item(title=f'排名商品 {index}', seller=seller)
            self.make_dismissal(
                target=self.make_demand(title=f'排名求购 {index}'), seller=seller,
            )

        report = build_opportunity_dismissal_quality(days=30)

        for key in ('user_rows', 'top_users', 'user_ranking'):
            self.assertNotIn(key, report)
        self.assertNotIn('username', str(report['kind_rows']))

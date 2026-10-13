# -*- coding: utf-8 -*-
"""为「推荐打分准不准」钉住口径。

`recommendations.py` 给每个登录用户算八件商品，用户点卡上的「想看看」或「不感兴趣」
落在 `RecommendationFeedback` 里。这个模块把这些表态读出来，只报告不改权重。测试要
钉住的是比数字本身更要紧的那些取舍：

1. 反馈率的分母是窗口内第一次被看见的推荐卡（`RecommendationImpression` 去重后的
   (用户, 商品) 对），不和反馈取交集——零反馈不等于满意。自本轮起这个分母真正落库
   了，此前只能拿 `BrowsingHistory` 反推（一件从搜索点进去的商品也占分母，却没有推
   荐卡可点），所以现在报出来的是测量而不是下界，但它仍是下界、只低不高，见
   `docs/recommendation-impression-design.md` 第 5.4 节。
2. 正负比例的分母只收有过反馈的推荐，两个分母不能混。
3. 理由按反馈时刻重算，复算走推荐服务同一个函数，不能在运营模块里另写一套。
4. 改口是一次改口不是两条记录，改口数单独统计。
5. 一条理由的样本不足门槛时只报数字，不给方向。
6. 空平台不报 0%：没有反馈的时候这个数字不存在，不是零。
7. 只报告，不改任何权重、不改任何理由文案。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import (
    BrowsingHistory, Category, CampusLocation, Favorite, Item, Order,
    RecommendationFeedback, RecommendationImpression,
)
from .recommendation_quality import (
    FOLLOWUP_WINDOW_DAYS, MIN_REASON_FEEDBACK, MIN_RECOMMENDATION_IMPRESSIONS,
    REASON_NOISE_POINTS, SCORE_BANDS, build_recommendation_quality,
)


class RecommendationQualityTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(
            username='quality-buyer', password='safe-password-123',
        )
        self.seller = User.objects.create_user(
            username='quality-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='推荐质量测试分类')
        self.location = CampusLocation.objects.create(
            name='推荐质量测试地点', building='推荐质量测试楼', is_public=True,
        )

    # 「这个商品没有交易地点」的哨兵。make_item 的 location 默认值是 None，而
    # None 又正好是「没有地点」的数据库值，所以不传和传 None 分不开——要建真的
    # 无地点商品必须显式传这个哨兵。
    NO_LOCATION = object()

    def make_item(self, title, *, category=None, location=None, favorite_count=0):
        item = Item.objects.create(
            title=title, description='用于推荐质量统计', price='20.00',
            category=category or self.category,
            # 哨兵在这里才落成数据库里的 NULL：调用方传 None 表示「用默认地点」，
            # 传 NO_LOCATION 才表示「这件商品真的没有地点」。
            location=(
                None if location is self.NO_LOCATION
                else (self.location if location is None else location)
            ),
            condition='良好', seller=self.seller, status='available',
        )
        for index in range(favorite_count):
            user = User.objects.create_user(
                username=f'{title}-collector-{index}',
            )
            Favorite.objects.create(user=user, item=item)
        return item

    def make_impression(self, item, *, reason_key='fresh', score=2.0, days_ago=0, user=None):
        """建一行首页推荐曝光：一张卡被渲染给了某个用户。

        `created_at` 是 auto_now_add，要给成窗口内的过去时刻只能建完用 `update()` 覆
        写。`position` 一律给 1：本模块不按位置报数（「第几位更容易被点」是另一个模块
        的事），位置这一列只是照 `SearchImpression` 的范式留着。
        """
        impression = RecommendationImpression.objects.create(
            user=user or self.buyer,
            item=item,
            reason_key=reason_key,
            score=score,
            position=1,
        )
        if days_ago:
            RecommendationImpression.objects.filter(pk=impression.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago),
            )
        return impression

    def make_feedback(self, item, *, action='interested', days_ago=1, switched=False):
        """建一条推荐反馈，`switched=True` 表示这个人改过口。

        `created_at` 是 auto_now_add、`updated_at` 是 auto_now，两个都不能在 create
        里给，只能建完用 `update()` 覆写。覆写时两列要给成**同一个时刻**：一次
        `create()` 里两列各取一次时钟，会差出毫秒级，而改口的判据带 60 秒容差
        （见 `recommendation_quality.SWITCH_TOLERANCE`），毫秒级的差必须消掉，
        否则每条首次反馈都会被误判成改口。
        """
        RecommendationFeedback.objects.create(
            user=self.buyer, item=item, action=action,
        )
        moment = timezone.now() - timedelta(days=days_ago)
        if switched:
            # 改口：首次表态在更早，最近一次表态在 moment，两列差出一天。
            RecommendationFeedback.objects.filter(
                user=self.buyer, item=item,
            ).update(created_at=moment - timedelta(days=1), updated_at=moment)
        else:
            RecommendationFeedback.objects.filter(
                user=self.buyer, item=item,
            ).update(created_at=moment, updated_at=moment)
        return RecommendationFeedback.objects.get(user=self.buyer, item=item)

    # --- 分母 ---

    def test_feedback_rate_denominator_counts_seen_not_recommended(self):
        # 一张卡被渲染了、用户也从没点按钮，它仍然占分母。反馈率的分母是「被看见
        # 过」：零反馈不是「用户满意」，是这套卡没有引起任何反应，两种情况在运营眼里
        # 完全不同。这是本轮换成曝光表之后最关键的一条——此前分母拿 BrowsingHistory
        # 反推，「渲染了没点开」的那部分整个不在库里。
        item = self.make_item('被看见但没表态')
        self.make_impression(item)

        report = build_recommendation_quality(days=30)
        self.assertEqual(report['feedback_loop']['viewed_recommendations'], 1)
        # 被看见了却没表态，反馈率是 0%——这个数存在，就是零。
        self.assertEqual(report['feedback_loop']['feedback_rate'], 0)

    def test_a_recommendation_that_was_never_shown_is_not_in_the_denominator(self):
        # 反过来：一条反馈若对应的商品在窗口内一次都没被渲染过，它没有分母，也不该
        # 把分母抬起来。用户从别处点进商品、没有推荐卡可点，然后表了态——分子分母
        # 说的不是同一件事。
        item = self.make_item('从搜索点进去的一件')
        self.make_feedback(item, action='interested')

        report = build_recommendation_quality(days=30)
        self.assertEqual(report['feedback_loop']['viewed_recommendations'], 0)
        # 分母是零，反馈率不存在——不报 0%，也不报一个拿反馈数当分母的 100%。
        self.assertIsNone(report['feedback_loop']['feedback_rate'])

    def test_positive_rate_denominator_only_counts_feedback(self):
        # 正负比例的分母只收有过反馈的推荐。拿展示数当分母会把「没人看」算成
        # 「看了不喜欢」——零反馈的商品会把比例推向 0。
        dismissed = self.make_item('被划掉的一件')
        interested = self.make_item('想看的一件')
        silent = self.make_item('没人表态的一件')
        self.make_feedback(dismissed, action='dismiss')
        self.make_feedback(interested, action='interested')
        # silent 有浏览、没有反馈：它进反馈率的分母，不进正负比例的分母。
        for item in (dismissed, interested, silent):
            self.make_impression(item)

        report = build_recommendation_quality(days=30)
        loop = report['feedback_loop']
        self.assertEqual(loop['viewed_recommendations'], 3)
        self.assertEqual(loop['feedback_total'], 2)
        self.assertEqual(loop['positive_rate'], 50)

    # --- 理由 ---

    def test_reason_rows_group_by_recomputed_reason(self):
        # 五句理由各自的正负比例按理由聚合：按用户聚合是排名，按商品聚合让被推一万次
        # 和一次不可比，而理由是打分的可解释输出，也是运营唯一能动手改的东西。
        # 收藏过同分类的商品，让推荐带上「与你收藏或交易过的分类相近」。
        preferred = self.make_item('收藏过的一件')
        Favorite.objects.create(user=self.buyer, item=preferred)
        # matched 不给地点：收藏来源自带地点，若这里也有地点，分类和地点就同时命中，
        # 理由会落到 category_and_location 上，这条测试要的 category 那一档就空了。
        matched = self.make_item('分类相近的一件', location=self.NO_LOCATION)
        # 「校园新上架」是兜底理由：分类、地点都不匹配，收藏数也是零。这里给一个
        # 用户从没表达过偏好的分类，让理由落到兜底那句上。
        other_category = Category.objects.create(name='推荐质量测试另一个分类')
        fresh = self.make_item(
            '全新上架的一件', category=other_category, location=self.NO_LOCATION,
        )

        for index in range(MIN_REASON_FEEDBACK):
            buyer = User.objects.create_user(username=f'cat-{index}')
            Favorite.objects.create(user=buyer, item=preferred)
            RecommendationFeedback.objects.create(user=buyer, item=matched, action='interested')
        for index in range(3):
            buyer = User.objects.create_user(username=f'fresh-{index}')
            RecommendationFeedback.objects.create(user=buyer, item=fresh, action='dismiss')

        report = build_recommendation_quality(days=30)
        by_key = {row['key']: row for row in report['reason_rows']}
        category_row = by_key['category']
        self.assertEqual(category_row['feedback_count'], MIN_REASON_FEEDBACK)
        self.assertEqual(category_row['positive_rate'], 100)
        fresh_row = by_key['fresh']
        self.assertEqual(fresh_row['feedback_count'], 3)
        self.assertEqual(fresh_row['positive_rate'], 0)

    def test_recomputed_reason_comes_from_the_service_itself(self):
        # 复算走的是推荐服务同一个函数。运营模块另写一套打分，两套逻辑算出两个理由，
        # 比没有理由更糟。这里用同一个输入直接调那个函数，和面板上的理由对。
        from .recommendations import recommendation_reason_key, recommendation_reason_label
        preferred = self.make_item('偏好来源')
        Favorite.objects.create(user=self.buyer, item=preferred)
        target = self.make_item('被复算的一件')

        self.make_feedback(target, action='interested')
        report = build_recommendation_quality(days=30)
        matched = [row for row in report['reason_rows'] if row['feedback_count']]
        self.assertEqual(len(matched), 1)

        # 只表过一次态，所以复算的 feedback_match 是 False：点击本身就是入集合的
        # 那个动作，「你标记过想看」那一刻还没生效。同一个函数、同一组输入，服务
        # 自己算也会得到这一句。
        expected = recommendation_reason_label(
            recommendation_reason_key(
                feedback_match=False, category_match=True, location_match=True,
                favorite_count=0,
            )
        )
        self.assertEqual(matched[0]['label'], expected)
        self.assertEqual(matched[0]['key'], 'category_and_location')

    def test_the_recomputed_reason_is_not_the_reason_at_recommendation_time(self):
        # 设计里写明的边界：复算还原的是点击那一刻的理由，不是现在的理由。用户点了
        # 「想看看」之后服务给出的理由会滑到「你标记过想看」，而 +10 分的反馈匹配在
        # 点击那一刻还没生效——点击本身就是入集合的那个动作。所以复算给出的仍是分类
        # 那句，和推荐当时的那一句一致。这条测试把两个时刻都钉住，免得后人以为复算
        # 能还原「用户现在会看到的理由」。
        preferred = self.make_item('偏好来源')
        Favorite.objects.create(user=self.buyer, item=preferred)
        target = self.make_item('被复算的一件')

        from .recommendations import RecommendationService
        before = {
            row.item.id: row.reason
            for row in RecommendationService(self.buyer).get(limit=50)
        }
        self.assertIn(target.id, before)
        # 这件商品同时命中分类与地点，理由落在 category_and_location 上。
        self.assertEqual(before[target.id], '符合你的分类与交易地点偏好')

        # 只表过一次态：点击那一刻 feedback_match 还没生效，复算给出的仍是分类与
        # 地点那句，和推荐当时的那一句一致。
        self.make_feedback(target, action='interested')
        report = build_recommendation_quality(days=30)
        matched = [row for row in report['reason_rows'] if row['feedback_count']]
        self.assertEqual(matched[0]['label'], '符合你的分类与交易地点偏好')

        # 服务在反馈之后给出的理由已经滑到了 feedback_match——两句话不同，正是复算
        # 的边界：它读的是按钮那一刻的输入，不是这一刻的输入。
        after = {
            row.item.id: row.reason
            for row in RecommendationService(self.buyer).get(limit=50)
        }
        self.assertEqual(after[target.id], '你标记过想看，优先为你保留')

    def test_switching_actions_is_counted_separately(self):
        # 先「想看」后「不感兴趣」在库里是一行 action='dismiss'。改口数是「推荐可能
        # 推得太频繁」最直接的证据，比任何比例都硬，所以单独统计。改过口的这批反馈
        # 第一次表态已经把商品写进了「想看看」集合，第二次点按钮时「你标记过想看」
        # 是生效的，所以它们应当落进 feedback_match 桶——+10 分这一档的过度推送信号
        # 就是从这里抓到的。
        for index in range(MIN_REASON_FEEDBACK):
            buyer = User.objects.create_user(username=f'switch-{index}')
            item = self.make_item(f'改口商品 {index}')
            RecommendationFeedback.objects.create(
                user=buyer, item=item, action='dismiss',
            )
            # 首次表态在一天前，改口在两天前——两列差出一天，跨过 60 秒容差。
            RecommendationFeedback.objects.filter(user=buyer, item=item).update(
                created_at=timezone.now() - timedelta(days=3),
                updated_at=timezone.now() - timedelta(days=2),
            )

        report = build_recommendation_quality(days=30)
        self.assertEqual(report['feedback_loop']['switched_total'], MIN_REASON_FEEDBACK)
        self.assertTrue(any(
            '改口' in note for note in report['recommendations']
        ))
        by_key = {row['key']: row for row in report['reason_rows']}
        self.assertEqual(by_key['feedback_match']['feedback_count'], MIN_REASON_FEEDBACK)

    # --- 门槛 ---

    def test_a_reason_with_too_few_samples_gets_no_verdict(self):
        # 一条理由只有三条反馈，其中两条「不感兴趣」：这个比例不是方向。
        for index in range(3):
            buyer = User.objects.create_user(username=f'thin-{index}')
            item = self.make_item(f'样本薄的商品 {index}')
            RecommendationFeedback.objects.create(user=buyer, item=item, action='dismiss')

        report = build_recommendation_quality(days=30)
        row = next(
            row for row in report['reason_rows'] if row['feedback_count'] == 3
        )
        self.assertTrue(row['is_small_sample'])
        self.assertEqual(row['positive_rate'], 0)
        # 样本不足时结论只说样本不够，不指向任何一句理由。
        self.assertIn('不足', report['measured']['summary'])
        self.assertFalse(report['measured']['has_comparison'])

    def test_reasons_within_noise_are_not_separated(self):
        # 两条理由的正负比例相差在噪声范围内，读不出哪一句在骗人。两条都得凑够
        # 样本门槛，否则结论只会说样本不足。
        # 第一条：分类匹配 + 「想看看」，正反馈 100%。
        preferred = self.make_item('噪声偏好来源')
        for index in range(MIN_REASON_FEEDBACK):
            buyer = User.objects.create_user(username=f'flat-a-{index}')
            Favorite.objects.create(user=buyer, item=preferred)
            RecommendationFeedback.objects.create(user=buyer, item=preferred, action='interested')
        # 第二条：兜底理由，正反馈 91.7%——与第一条的 100% 相差 8.3 个百分点，落在
        # REASON_NOISE_POINTS 之内。同一个量级的理由之间的这点差别，运营看不出来、
        # 也动不了手，所以不拿来指向任何一句理由。
        other_category = Category.objects.create(name='推荐质量噪声分类')
        for index in range(MIN_REASON_FEEDBACK):
            buyer = User.objects.create_user(username=f'flat-b-{index}')
            item = self.make_item(
                f'噪声商品 B {index}', category=other_category,
                location=self.NO_LOCATION,
            )
            action = 'interested' if index < 11 else 'dismiss'
            RecommendationFeedback.objects.create(user=buyer, item=item, action=action)

        report = build_recommendation_quality(days=30)
        self.assertTrue(report['measured']['has_comparison'])
        self.assertEqual(report['measured']['kind'], 'flat')
        self.assertLess(report['measured']['spread_points'], REASON_NOISE_POINTS)

    def test_the_weakest_reason_is_named_with_its_number(self):
        # 一条理由的负反馈占多数、样本也够，结论要点出是哪一句、比例多少——这是唯一
        # 能指向具体改动的切分。
        for index in range(MIN_REASON_FEEDBACK + 2):
            buyer = User.objects.create_user(username=f'weak-{index}')
            item = self.make_item(f'弱理由商品 {index}')
            RecommendationFeedback.objects.create(user=buyer, item=item, action='dismiss')
        for index in range(MIN_REASON_FEEDBACK + 2):
            buyer = User.objects.create_user(username=f'strong-{index}')
            other = User.objects.create_user(username=f'strong-pref-{index}')
            preferred = self.make_item(f'强理由商品 {index}')
            Favorite.objects.create(user=other, item=preferred)
            RecommendationFeedback.objects.create(user=buyer, item=preferred, action='interested')

        report = build_recommendation_quality(days=30)
        measured = report['measured']
        self.assertTrue(measured['has_comparison'])
        self.assertEqual(measured['weakest_label'], '校园新上架')
        self.assertEqual(measured['weakest_rate'], 0)
        self.assertTrue(any(
            '校园新上架' in note for note in report['recommendations']
        ))

    # --- 反馈之后的动作 ---

    def test_interested_followed_by_a_view_inside_the_window(self):
        # 「想看看」之后真的去看了：浏览落在反馈时刻之后、且在窗口内才算。
        item = self.make_item('想看看之后去看了')
        feedback = self.make_feedback(item, action='interested', days_ago=1)
        BrowsingHistory.objects.create(user=self.buyer, item=item)

        report = build_recommendation_quality(days=30)
        row = next(
            row for row in report['reason_rows'] if row['feedback_count']
        )
        self.assertEqual(row['followed_view_count'], 1)

    def test_a_view_before_the_feedback_does_not_count(self):
        # 先看后点不是「表态之后去看了」。浏览时刻必须晚于反馈时刻。
        item = self.make_item('先看后点的商品')
        BrowsingHistory.objects.create(user=self.buyer, item=item)
        self.make_feedback(item, action='interested', days_ago=1)
        BrowsingHistory.objects.filter(user=self.buyer, item=item).update(
            last_viewed_at=timezone.now() - timedelta(days=3),
        )

        report = build_recommendation_quality(days=30)
        row = next(
            row for row in report['reason_rows'] if row['feedback_count']
        )
        self.assertEqual(row['followed_view_count'], 0)

    def test_a_view_outside_the_followup_window_is_not_attributed(self):
        # 超过窗口的浏览无法归因：用户可能是在翻历史记录，不是在回应推荐卡。
        # days_ago 取 29 而不是 30：30 天前的反馈会掉出 30 天统计窗口，那一行根本
        # 不进报告，测的就不是归因窗口而是统计窗口了。
        item = self.make_item('窗口外又去看的商品')
        self.make_feedback(item, action='interested', days_ago=29)
        BrowsingHistory.objects.create(user=self.buyer, item=item)
        BrowsingHistory.objects.filter(user=self.buyer, item=item).update(
            last_viewed_at=timezone.now() - timedelta(days=FOLLOWUP_WINDOW_DAYS + 2),
        )

        report = build_recommendation_quality(days=30)
        row = next(
            row for row in report['reason_rows'] if row['feedback_count']
        )
        self.assertEqual(row['followed_view_count'], 0)

    def test_interested_followed_by_an_order_inside_the_window(self):
        item = self.make_item('想看看之后下单了')
        self.make_feedback(item, action='interested', days_ago=1)
        Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            agreed_price='20.00', status='pending',
        )

        report = build_recommendation_quality(days=30)
        row = next(
            row for row in report['reason_rows'] if row['feedback_count']
        )
        self.assertEqual(row['followed_order_count'], 1)

    # --- 空平台 ---

    def test_an_empty_campus_does_not_report_zero(self):
        # 一条反馈都没有的时候，反馈率和正负比例都不存在，不是 0%。空平台报 0% 会让
        # 运营以为用户在大量拒绝推荐。
        report = build_recommendation_quality(days=30)
        self.assertFalse(report['has_data'])
        self.assertIsNone(report['feedback_loop']['feedback_rate'])
        self.assertIsNone(report['feedback_loop']['positive_rate'])
        self.assertFalse(report['measured']['has_comparison'])

    # --- 曝光表：分母落库之后 ---

    def test_the_same_card_shown_twice_counts_once_in_the_denominator(self):
        # 一件商品在窗口内被渲染了两次，它只占一个分母。曝光表刻意不设 (用户, 商品)
        # 唯一约束——「看见」不该折叠，同一个人在不同时刻看见同一张卡是两次独立的看
        # 见；但算「第一次被看见」的那一刻要去重，否则分母重复计、分子不重复计，率会
        # 虚低。
        item = self.make_item('被渲染两次的一件')
        self.make_impression(item)
        self.make_impression(item)

        report = build_recommendation_quality(days=30)
        self.assertEqual(RecommendationImpression.objects.count(), 2)
        self.assertEqual(report['feedback_loop']['viewed_recommendations'], 1)

    def test_an_impression_outside_the_window_is_not_counted(self):
        # 窗口外被看见过、窗口内又被看见，分母只认窗口内那次。反过来，窗口内被看见、
        # 窗口外才表态，分子进不来（见下面那条），分母照算——两个方向都只数窗口内产生
        # 的行。
        item = self.make_item('窗口边界上的一件')
        self.make_impression(item, days_ago=40)
        self.make_impression(item)

        report = build_recommendation_quality(days=30)
        self.assertEqual(report['feedback_loop']['viewed_recommendations'], 1)

    def test_reason_impression_rows_report_the_reason_at_recommendation_time(self):
        # 本轮真正的新读数：按推荐当时的理由拆曝光。`reason_key` 跟着卡一起落库，
        # 所以这一层不受用户之后又收藏、又浏览的影响——它回答「当时挂这句话的卡，用户点了吗」，而 `reason_rows` 那一层回答「这句理由今天还成立吗」。两问不能混。
        preferred = self.make_item('偏好来源')
        Favorite.objects.create(user=self.buyer, item=preferred)
        # 24 个不同的人各看见一件：分母按 (用户, 商品) 对去重，同一个人反复看见同一
        # 件只算一次，要凑够门槛就得给不同的对。其中只有第一个人按了按钮——分子要求
        # 「这条反馈对应的 (用户, 商品) 在窗口内被看见过」，两边必须是同一对。
        clicked = None
        for index in range(MIN_RECOMMENDATION_IMPRESSIONS):
            viewer = User.objects.create_user(username=f'impression-viewer-{index}')
            matched = self.make_item(
                f'当时挂在分类那句的一件 {index}', location=self.NO_LOCATION,
            )
            self.make_impression(matched, reason_key='category', score=8.0, user=viewer)
            if index == 0:
                clicked = (viewer, matched)
        RecommendationFeedback.objects.create(
            user=clicked[0], item=clicked[1], action='interested',
        )

        report = build_recommendation_quality(days=30)
        by_key = {row['key']: row for row in report['reason_impression_rows']}
        row = by_key['category']
        self.assertEqual(row['shown_count'], MIN_RECOMMENDATION_IMPRESSIONS)
        self.assertEqual(row['feedback_count'], 1)
        self.assertEqual(row['feedback_rate'], round(1 / MIN_RECOMMENDATION_IMPRESSIONS * 100, 1))
        self.assertFalse(row['is_small_sample'])
        # 一次都没被渲染过的理由在这一层不存在，不是零。
        self.assertEqual(by_key['feedback_match']['shown_count'], 0)
        self.assertIsNone(by_key['feedback_match']['feedback_rate'])

    def test_a_reason_with_too_few_impressions_gets_no_direction(self):
        # 一句理由只被渲染了区区几张，反馈率只报数字、不给方向。取
        # MIN_RECOMMENDATION_IMPRESSIONS 而不是 MIN_REASON_FEEDBACK：前者是曝光门槛，
        # 后者是反馈门槛，复用一个会让「够不够样本」在两张表上变成同一个意思。
        item = self.make_item('只渲染了几张的一件')
        for _ in range(3):
            self.make_impression(item, reason_key='fresh', score=2.0)

        report = build_recommendation_quality(days=30)
        row = next(
            row for row in report['reason_impression_rows'] if row['shown_count']
        )
        self.assertTrue(row['is_small_sample'])
        self.assertNotIn('当时', ' '.join(report['recommendations']))

    def test_score_bands_report_what_the_scoring_pushed_today(self):
        # 按推荐当时的分数档拆曝光规模。档位边界照三个可解释的加分取（+10/+8/+5），
        # 不按分数等分——运营能动手改的就是这三个常数。
        fresh = self.make_item('只有新鲜度的一件')
        matched = self.make_item('分类匹配的一件', location=self.NO_LOCATION)
        for _ in range(5):
            self.make_impression(fresh, reason_key='fresh', score=2.0)
        for _ in range(7):
            self.make_impression(matched, reason_key='category', score=8.0)

        report = build_recommendation_quality(days=30)
        by_label = {row['label']: row['shown_count'] for row in report['score_bands']}
        # 8 分落在 [8, 10) 这一档，2 分落在 [0, 5) 这一档。
        self.assertEqual(by_label['8-10 分（分类匹配）'], 7)
        self.assertEqual(by_label['0-5 分（新鲜度与热度）'], 5)
        # 档位要覆盖到上界，否则最后一段会凭空消失。
        self.assertEqual(sum(row['shown_count'] for row in report['score_bands']), 12)

    def test_the_weakest_reason_at_recommendation_time_is_named(self):
        # 结论里要点出当时最弱的那句理由，且带的是推荐那一刻的分母——它比反馈时刻复
        # 算的理由更硬。样本不足时不点名。
        # 强理由每一张都被点了，弱理由一张都没被点：两句的反馈率相差 100 个百分点，远超
        # 噪声范围，最弱那一句必须被点出来。若两边都 100%，min() 取到哪一句全看遍历
        # 顺序，测的就不是口径而是字典序了。
        for index in range(MIN_RECOMMENDATION_IMPRESSIONS + 2):
            strong = self.make_item(f'强理由商品 {index}')
            weak = self.make_item(f'弱理由商品 {index}')
            self.make_impression(strong, reason_key='category', score=8.0)
            self.make_impression(weak, reason_key='fresh', score=2.0)
            self.make_feedback(strong, action='interested')

        report = build_recommendation_quality(days=30)
        self.assertTrue(any(
            '当时' in note and '校园新上架' in note
            for note in report['recommendations']
        ))

    def test_a_campus_with_only_impressions_still_has_data(self):
        # 曝光表先于反馈表积累（用户先看见、后才可能点按钮）。一个刚上线、只渲染过首
        # 页还没人按过按钮的周期，也要能在面板上读到「已经被看见多少张、其中零张被
        # 点」——报「没有数据」会让运营以为模块没在跑。
        item = self.make_item('只被看见过的一件')
        self.make_impression(item)

        report = build_recommendation_quality(days=30)
        self.assertTrue(report['has_data'])
        self.assertEqual(report['feedback_loop']['viewed_recommendations'], 1)
        self.assertEqual(report['feedback_loop']['feedback_rate'], 0)
        self.assertEqual(report['measured']['has_comparison'], False)

    def test_building_the_report_writes_no_impression(self):
        # 只报告：建报告本身不写任何一行曝光。写入点只有 views.home 一处，运营模块自己
        # 不产生分母，否则它量的就是它上周的输出。
        item = self.make_item('不会被写进去的一件')
        self.make_impression(item)

        build_recommendation_quality(days=30)

        self.assertEqual(RecommendationImpression.objects.count(), 1)

    # --- 只报告 ---

    def test_building_the_report_writes_nothing_back(self):
        # 只报告，不改权重、不改理由文案、不把负反馈的商品加进黑名单、不重算推荐。
        item = self.make_item('只读的一件')
        self.make_feedback(item, action='dismiss')
        before = RecommendationFeedback.objects.get(item=item).action

        build_recommendation_quality(days=30)

        self.assertEqual(RecommendationFeedback.objects.get(item=item).action, before)
        self.assertEqual(Item.objects.get(pk=item.pk).status, 'available')

# -*- coding: utf-8 -*-
"""首页渲染一次，就把「被看见」落一行。

`RecommendationImpression` 是 `recommendation_quality` 报反馈率的分母。上一轮之前分
母只能拿 `BrowsingHistory` 反推，一件从搜索点进去的商品也占分母、却没有推荐卡可点，
反馈率只能报下界；这一轮把「被看见」这件事本身落库。写入点只许成功、不许影响渲染：
`bulk_create` 挂了，首页照常 200，只是这一屏没有分母。

同时钉住本轮对业务代码的唯一改动——`Recommendation` 多带一个 `reason_key`：它不改变
首页渲染的任何输出（文案照旧走 `reason`），只是把 `get()` 里已经算出来的 key 一起带
出来，让曝光表能按理由归组。
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, CampusLocation, Item, RecommendationImpression


class RecommendationImpressionWriteTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(
            username='home-buyer', password='safe-password-123',
        )
        self.seller = User.objects.create_user(
            username='home-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='首页曝光测试分类')
        self.location = CampusLocation.objects.create(
            name='首页曝光测试地点', building='首页曝光测试楼', is_public=True,
        )

    def make_item(self, title):
        return Item.objects.create(
            title=title, description='用于首页曝光写入测试', price='20.00',
            category=self.category, location=self.location, condition='良好',
            seller=self.seller, status='available',
        )

    def test_rendering_the_home_page_writes_one_row_per_shown_card(self):
        # 首页「为你挑选」最多渲染 8 张卡，渲染几张就落几行。这里是登录用户 + 一件可
        # 推荐商品的最小情形：一张卡、一行。
        item = self.make_item('被推荐的一件')
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        rows = list(RecommendationImpression.objects.filter(user=self.buyer))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].item, item)
        # reason_key 与 score 跟着卡一起落库，这是本轮和机会流那次最大的区别：那边刻意
        # 不存，因为分数在忽略时刻要重算；这边 Recommendation 是 frozen dataclass，两
        # 样东西在渲染那一刻就已经算出来了，落库是零成本的。
        self.assertTrue(rows[0].reason_key)
        self.assertGreater(rows[0].score, 0)
        self.assertEqual(rows[0].position, 1)

    def test_anonymous_visitors_write_a_row_with_no_user(self):
        # 这一条是设计期间被代码纠正的。最初写的是「匿名用户不写」，依据是
        # `_load_preferences` 对匿名用户直接 return；读了 `get()` 才发现匿名用户得到的
        # 是全站在售商品按分排下来的前八件，模板也照常渲染（只把标题换成「校园里正在
        # 被关注」、把反馈按钮收起来）。所以匿名用户的看见必须落库，`user` 存 null——
        # 否则首页最大的那部分流量整块不在分母里，反馈率会变成一个只有登录用户才配被
        # 统计的数。
        item = self.make_item('匿名也看得见的一件')

        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        rows = list(RecommendationImpression.objects.all())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].item, item)
        self.assertIsNone(rows[0].user)

    def test_a_row_survives_its_user_being_deleted(self):
        # `user` 取 SET_NULL 而不是 CASCADE：账号注销之后那些看见仍然发生过，只是再也
        # 归不到一个具体的人。删了用户就把分母一起删掉，等于把已经发生过的事从统计里
        # 抹掉。
        item = self.make_item('用户会被删掉的一件')
        self.client.force_login(self.buyer)
        self.client.get(reverse('home'))
        self.assertEqual(RecommendationImpression.objects.count(), 1)

        self.buyer.delete()

        rows = list(RecommendationImpression.objects.all())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].user)
        self.assertEqual(rows[0].item, item)

    def test_no_recommendation_writes_no_row(self):
        # 一件在售商品都没有时推荐服务返回空列表，一次 insert 都不发。分母是零不是缺
        # 失：这个周期的反馈率是 None，见 recommendation_quality 的口径。
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RecommendationImpression.objects.count(), 0)

    def test_the_same_card_shown_twice_writes_two_rows(self):
        # 「看见」不该折叠：同一个人在不同时刻看见同一张卡是两次独立的看见。曝光表刻意
        # 不设 (user, item) 唯一约束，去重只发生在算「第一次被看见」的那一刻。
        item = self.make_item('被渲染两次的一件')
        self.client.force_login(self.buyer)

        self.client.get(reverse('home'))
        self.client.get(reverse('home'))

        self.assertEqual(
            RecommendationImpression.objects.filter(user=self.buyer, item=item).count(),
            2,
        )

    def test_a_broken_write_does_not_break_the_page(self):
        self.make_item('写了会挂的一件')
        self.client.force_login(self.buyer)

        with mock.patch.object(
            RecommendationImpression.objects, 'bulk_create',
            side_effect=RuntimeError('曝光表写不进去'),
        ):
            response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RecommendationImpression.objects.count(), 0)

    def test_rows_are_attached_to_the_viewing_user(self):
        # 分母按人算：每个人看见的是他自己的那八张卡，行必须挂在正在看首页的那个人身
        # 上，不能挂在商品卖家或别人身上。
        item = self.make_item('两个人都会看见的一件')
        other = User.objects.create_user(username='home-other')
        self.client.force_login(self.buyer)
        self.client.get(reverse('home'))
        self.client.force_login(other)
        self.client.get(reverse('home'))

        rows = RecommendationImpression.objects.all()
        self.assertEqual(rows.count(), 2)
        self.assertEqual({row.user_id for row in rows}, {self.buyer.id, other.id})
        self.assertEqual({row.item_id for row in rows}, {item.id})

    def test_the_reason_key_matches_what_the_page_rendered(self):
        # 落库的 reason_key 必须和首页渲染出来的那句话对得上：文案走 `reason`，key 走
        # `reason_key`，两者是同一句话的两种形态。若哪天 `get()` 只填了一个，这里会
        # 挂——而挂的原因正是「面板按它归组的那句话和用户看见的那句话不是一句」。
        item = self.make_item('理由要对得上的一件')
        self.client.force_login(self.buyer)
        response = self.client.get(reverse('home'))

        rendered = response.context['recommendations']
        self.assertEqual(len(rendered), 1)
        rows = RecommendationImpression.objects.filter(user=self.buyer)
        self.assertEqual(rows[0].reason_key, rendered[0].reason_key)
        self.assertEqual(rows[0].score, rendered[0].score)
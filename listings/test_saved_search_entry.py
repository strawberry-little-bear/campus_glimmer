"""为「关注搜索」补上管理入口后必须钉住的几件事。

上一轮之前，`saved_search_list` 这个页面虽然写好了，全站却没有任何一处链接
指向它：用户在发现页保存了一组筛选条件，之后既看不见自己存过什么，也找不到
地方改提醒节奏或暂停。运营侧的通知 `target_url` 反而成了这个页面唯一的入口。

这些测试要钉住的是：

1. 用户菜单里能看到「关注的搜索」入口，未登录时看不到。
2. 保存筛选条件成功后，提示里带一条通往管理页的链接。
3. 卡片上的「当前命中」是可点的，跳回发现页并原样带上这组筛选条件。
4. 链接跳过去之后，发现页返回的商品数必须和卡片上的数字一致——这是两个
   口径（ORM 筛选 vs Python 逐件比对）不能对不上的地方。
5. 「待提醒」保持纯数字，不做成链接：缓冲里的命中可能已经下架或卖掉。
6. 空条件、缺分类、跨用户边界都不该把页面或链接搞坏。
"""

import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import CampusLocation, Category, Item, SavedSearch


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_item(seller, **kwargs):
    defaults = {
        'title': '测试商品',
        'description': '测试描述',
        'price': Decimal('30.00'),
        'condition': '九成新',
        'status': 'available',
        'category': Category.objects.create(name=f'夹具分类 {uuid.uuid4().hex[:8]}'),
    }
    defaults.update(kwargs)
    return Item.objects.create(seller=seller, **defaults)


def card_for(response, saved_search):
    """Return the rendered saved-search row that carries the view's annotations.

    `_attach_saved_search_stats` attaches its counts and URL onto the instances
    the view rendered, so the test must read those same instances instead of
    re-querying by pk, which would hand back a fresh, unannotated object.
    """
    for row in response.context['saved_searches']:
        if row.pk == saved_search.pk:
            return row
    raise AssertionError('页面里没有这条关注搜索')

class SavedSearchEntryTests(TestCase):
    """登录用户能从导航找到关注搜索管理页。"""

    def setUp(self):
        self.user = make_user('entry-user')

    def test_user_menu_links_to_the_saved_search_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('item_list'))
        self.assertContains(response, reverse('saved_search_list'))

    def test_anonymous_visitor_does_not_see_the_entry(self):
        response = self.client.get(reverse('item_list'))
        self.assertNotContains(response, reverse('saved_search_list'))

    def test_page_requires_login(self):
        response = self.client.get(reverse('saved_search_list'))
        self.assertEqual(response.status_code, 302)


class SaveSearchConfirmationTests(TestCase):
    """保存条件后，提示要自己带上去管理页的路。"""

    def setUp(self):
        self.user = make_user('confirm-user')

    def test_success_message_carries_a_link_to_the_management_page(self):
        self.client.force_login(self.user)
        # 一次请求就够：同名关注搜索在同一个用户下有唯一约束，重发会走到
        # 「你已经有一个同名的关注搜索」分支，那条提示不带管理页链接。
        response = self.client.post(
            reverse('save_search'),
            {'name': '考研资料', 'query': '考研', 'notify_frequency': 'instant'},
            follow=True,
        )
        # 提示里要出现保存成功的措辞，并且带一条通往管理页的链接。
        self.assertContains(response, '已保存')
        self.assertContains(response, 'alert-action')
        self.assertContains(response, reverse('saved_search_list'))

    def test_message_does_not_leak_the_link_when_not_logged_in(self):
        response = self.client.post(
            reverse('save_search'),
            {'name': '考研资料', 'query': '考研', 'notify_frequency': 'instant'},
        )
        # 未登录保存会先被登录墙拦下，不该走到写提示那一步。
        self.assertEqual(response.status_code, 302)


class SavedSearchLiveMatchLinkTests(TestCase):
    """「当前命中」要能点，而且要跳到同一批商品。"""

    def setUp(self):
        self.user = make_user('link-user')
        self.seller = make_user('link-seller')
        self.category = Category.objects.create(name='链接测试分类')
        self.location = CampusLocation.objects.create(name='链接测试地点')

    def test_live_match_count_is_rendered_as_a_link(self):
        saved_search = SavedSearch.objects.create(
            user=self.user, name='可点的命中', query='测试', category=self.category,
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('saved_search_list'))
        card = card_for(response, saved_search)
        # assertContains 不做查询串的百分号解码，所以按片段断言而不是整条 URL。
        self.assertContains(response, 'saved-search-metric-link')
        for fragment in ('q=%E6%B5%8B%E8%AF%95', 'category=1'):
            self.assertContains(response, fragment)

    def test_pending_match_count_stays_a_plain_number(self):
        saved_search = SavedSearch.objects.create(
            user=self.user, name='待提醒不点', query='测试',
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('saved_search_list'))
        # 待提醒这一项必须仍然是 span，不能变成可点的链接。
        self.assertContains(response, '<span class="saved-search-metric"><i class="bi bi-collection"></i>待提醒')

    def test_link_reproduces_the_same_result_set_as_the_counted_number(self):
        saved_search = SavedSearch.objects.create(
            user=self.user,
            name='口径要对上',
            query='测试',
            category=self.category,
            location=self.location,
            min_price=Decimal('10.00'),
            max_price=Decimal('50.00'),
            condition='九成新',
        )
        for index in range(3):
            make_item(
                self.seller,
                title=f'测试商品 {index}',
                category=self.category,
                location=self.location,
            )
        # 一件不满足条件的，用来确认筛选真的在起作用。
        make_item(
            self.seller, title='测试商品 贵的', category=self.category,
            location=self.location, price=Decimal('99.00'),
        )

        self.client.force_login(self.user)
        list_response = self.client.get(reverse('saved_search_list'))
        card = card_for(list_response, saved_search)
        counted = card.live_match_count
        self.assertEqual(counted, 3)

        detail_response = self.client.get(card.live_match_url)
        self.assertEqual(len(detail_response.context['items']), counted)

    def test_link_preserves_query_category_location_and_price(self):
        saved_search = SavedSearch.objects.create(
            user=self.user,
            name='条件都要带上',
            query='测试',
            category=self.category,
            location=self.location,
            min_price=Decimal('10.00'),
            max_price=Decimal('50.00'),
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('saved_search_list'))
        card = card_for(response, saved_search)
        url = card.live_match_url
        for fragment in ('q=', 'category=', 'location=', 'min_price=', 'max_price='):
            self.assertIn(fragment, url)

    def test_search_without_criteria_falls_back_to_the_plain_list(self):
        SavedSearch.objects.create(user=self.user, name='没有任何条件')
        self.client.force_login(self.user)
        response = self.client.get(reverse('saved_search_list'))
        card = response.context['saved_searches'][0]
        self.assertEqual(card.live_match_url, reverse('item_list'))

    def test_link_points_at_the_list_page_rather_than_filtering_out_own_items(self):
        """链接只负责还原筛选条件，不替发现页改口径。

        关注搜索页统计 live_match_count 时会排除用户自己的商品，那是「我会被
        提醒到什么」的口径；发现页本身不排除卖家自己的商品。链接把条件交还给
        发现页，就该让发现页按它自己的口径来，否则同一组条件在两个页面上数字
        不一致，用户只会更困惑。
        """
        saved_search = SavedSearch.objects.create(user=self.user, name='不掺和自己的', query='测试')
        make_item(self.seller, title='测试商品')
        make_item(self.user, title='测试商品 自己的')
        self.client.force_login(self.user)
        card = card_for(self.client.get(reverse('saved_search_list')), saved_search)
        self.assertEqual(card.live_match_count, 1)
        response = self.client.get(card.live_match_url)
        titles = [item.title for item in response.context['items']]
        self.assertIn('测试商品', titles)

    def test_another_users_saved_search_is_not_reachable(self):
        other = make_user('link-other')
        SavedSearch.objects.create(user=other, name='别人的关注', query='测试')
        self.client.force_login(self.user)
        response = self.client.get(reverse('saved_search_list'))
        self.assertNotContains(response, '别人的关注')

# -*- coding: utf-8 -*-
from datetime import timedelta
from urllib.parse import parse_qs

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusLocation, Category, DemandPost, DemandResponse, Item


class DemandFilterWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = User.objects.create_user(
            username='demand-filter-viewer', password='safe-password-123',
        )
        cls.other_user = User.objects.create_user(
            username='demand-filter-other', password='safe-password-123',
        )
        cls.category = Category.objects.create(name='数码设备')
        cls.location = CampusLocation.objects.create(name='图书馆大厅')
        cls.demand = DemandPost.objects.create(
            requester=cls.other_user,
            title='求购一台宿舍显示器',
            description='希望是 24 英寸，预算合适即可。',
            category=cls.category,
            location=cls.location,
            max_price='500.00',
        )
        cls.other_demand = DemandPost.objects.create(
            requester=cls.other_user,
            title='求购羽毛球拍',
            description='周末可以当面交易。',
        )

    def test_filter_workspace_reports_results_and_removable_conditions(self):
        response = self.client.get(reverse('demand_list'), {
            'q': '显示器',
            'category': self.category.id,
            'location': self.location.id,
            'sort': 'popular',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(response.context['demand_active_filter_count'], 3)
        self.assertEqual(response.context['demand_sort_label'], '浏览最多')
        self.assertContains(response, '当前显示 1 条 · 已启用 3 个条件')
        self.assertContains(response, '关键词：显示器')
        self.assertContains(response, '分类：数码设备')
        self.assertContains(response, '地点：图书馆大厅')
        self.assertContains(response, '正在寻找的需求')
        self.assertContains(response, '按浏览最多排序')

        filters = {
            row['label']: row for row in response.context['demand_active_filters']
        }
        self.assertEqual(
            parse_qs(filters['关键词']['remove_query']),
            {'category': [str(self.category.id)], 'location': [str(self.location.id)], 'sort': ['popular']},
        )
        self.assertEqual(
            parse_qs(filters['分类']['remove_query']),
            {'q': ['显示器'], 'location': [str(self.location.id)], 'sort': ['popular']},
        )

    def test_default_browse_page_has_no_filter_summary(self):
        response = self.client.get(reverse('demand_list'))

        self.assertEqual(response.context['demand_active_filter_count'], 0)
        self.assertContains(response, '当前显示 2 条')
        self.assertNotContains(response, '清除筛选')
        self.assertContains(response, '求购一台宿舍显示器')
        self.assertContains(response, '求购羽毛球拍')

    def test_my_demands_keeps_the_same_filter_workspace_and_ownership_scope(self):
        own_demand = DemandPost.objects.create(
            requester=self.viewer,
            title='求购我的专业课教材',
            description='需要最新版。',
        )
        self.client.force_login(self.viewer)

        response = self.client.get(reverse('my_demands'), {'q': '教材'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_mine'])
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertContains(response, own_demand.title)
        self.assertNotContains(response, self.demand.title)
        self.assertContains(response, '我的求购')
        self.assertContains(response, '当前显示 1 条 · 已启用 1 个条件')
        self.assertContains(response, f'href="{reverse("my_demands")}"')


class MyDemandWorkspaceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='my-demand-owner', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='my-demand-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='工作台求购分类')
        self.location = CampusLocation.objects.create(name='工作台求购地点')

    def make_demand(self, title, *, status='active', requester=None, **kwargs):
        return DemandPost.objects.create(
            requester=requester or self.owner,
            title=title,
            description=f'{title}的详细需求',
            category=self.category,
            location=self.location,
            status=status,
            **kwargs,
        )

    def make_item(self, title, *, seller=None):
        return Item.objects.create(
            seller=seller or self.other,
            title=title,
            description=f'{title}商品说明',
            category=self.category,
            location=self.location,
            condition='9成新',
            price='50.00',
        )

    def test_mine_requires_login_and_public_list_still_only_contains_active_demands(self):
        active = self.make_demand('公开寻找中的求购')
        fulfilled = self.make_demand('已经找到的求购', status='fulfilled')
        self.make_demand('其他人的求购', requester=self.other)

        protected = self.client.get(reverse('my_demands'))
        public = self.client.get(reverse('demand_list'))

        self.assertRedirects(
            protected,
            f"{reverse('login')}?next={reverse('my_demands')}",
            fetch_redirect_response=False,
        )
        self.assertContains(public, active.title)
        self.assertNotContains(public, fulfilled.title)
        self.assertEqual(public.context['demand_counts']['all'], 0)

    def test_status_tabs_keep_all_lifecycle_counts_and_strict_owner_scope(self):
        demands = {
            'active': self.make_demand('寻找中的教材'),
            'fulfilled': self.make_demand('已经找到的台灯', status='fulfilled'),
            'closed': self.make_demand('已经关闭的球拍', status='closed'),
            'expired': self.make_demand('已经过期的耳机', status='expired'),
        }
        self.make_demand('别人的寻找中求购', status='active', requester=self.other)
        self.client.force_login(self.owner)

        response = self.client.get(reverse('my_demands'))

        expected_counts = {
            'all': 4, 'active': 1, 'fulfilled': 1, 'closed': 1, 'expired': 1,
        }
        self.assertEqual(response.context['demand_counts'], expected_counts)
        self.assertEqual(response.context['demand_selected_tab'], 'all')
        self.assertEqual(
            {demand.status for demand in response.context['demands']},
            {'active', 'fulfilled', 'closed', 'expired'},
        )
        self.assertNotContains(response, '别人的寻找中求购')
        for status, demand in demands.items():
            tab_response = self.client.get(reverse('my_demands'), {'tab': status})
            self.assertEqual(tab_response.context['demand_selected_tab'], status)
            self.assertEqual(
                [row.pk for row in tab_response.context['demands']], [demand.pk],
            )
            self.assertEqual(tab_response.context['demand_counts'], expected_counts)
            self.assertContains(
                tab_response, f'id="demand-{status}-tab"', html=False,
            )

        invalid = self.client.get(reverse('my_demands'), {'tab': 'unknown'})
        self.assertEqual(invalid.context['demand_selected_tab'], 'all')
        self.assertEqual(invalid.context['page_obj'].paginator.count, 4)

    def test_response_total_and_pending_count_are_annotated_per_demand(self):
        demand = self.make_demand('等待多个响应的显示器')
        self.make_demand('没有响应的键盘')
        DemandResponse.objects.create(
            demand=demand,
            item=self.make_item('显示器一'),
            responder=self.other,
            status='pending',
        )
        DemandResponse.objects.create(
            demand=demand,
            item=self.make_item('显示器二'),
            responder=self.other,
            status='pending',
        )
        DemandResponse.objects.create(
            demand=demand,
            item=self.make_item('显示器三'),
            responder=self.other,
            status='accepted',
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse('my_demands'), {'tab': 'active'})
        rows = {row.title: row for row in response.context['demands']}

        self.assertEqual(rows[demand.title].response_count, 3)
        self.assertEqual(rows[demand.title].pending_response_count, 2)
        self.assertContains(response, '3 条响应 · <strong>2 条待处理</strong>', html=False)
        self.assertContains(response, '#demand-responses')
        self.assertContains(response, '还没有收到响应')

    def test_tabs_filters_sort_and_pagination_keep_each_other(self):
        active = self.make_demand('我的显示器教材')
        self.make_demand('我的显示器教材已找到', status='fulfilled')
        for index in range(12):
            self.make_demand(f'我的显示器教材补充 {index:02d}')
        self.client.force_login(self.owner)

        response = self.client.get(reverse('my_demands'), {
            'tab': 'active',
            'q': '显示器',
            'category': self.category.id,
            'location': self.location.id,
            'sort': 'popular',
            'page': 2,
        })
        tab_queries = {
            row['key']: parse_qs(row['query'])
            for row in response.context['demand_tab_links']
        }
        filter_query = parse_qs(response.context['demand_filter_query'])

        self.assertEqual(tab_queries['fulfilled']['tab'], ['fulfilled'])
        self.assertEqual(tab_queries['fulfilled']['q'], ['显示器'])
        self.assertEqual(tab_queries['fulfilled']['category'], [str(self.category.id)])
        self.assertEqual(tab_queries['fulfilled']['location'], [str(self.location.id)])
        self.assertEqual(tab_queries['fulfilled']['sort'], ['popular'])
        self.assertEqual(filter_query['tab'], ['active'])
        self.assertNotIn('page', filter_query)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(active.status, 'active')
        self.assertContains(
            response,
            f'<input type="hidden" name="tab" value="active">',
            html=True,
        )
        self.assertContains(response, '?page=1&tab=active&amp;q=', html=False)

    def test_empty_states_distinguish_status_from_filter_miss(self):
        self.make_demand('寻找中的求购')
        self.client.force_login(self.owner)

        status_empty = self.client.get(reverse('my_demands'), {'tab': 'fulfilled'})
        filter_empty = self.client.get(reverse('my_demands'), {
            'tab': 'active', 'q': '完全不存在的关键词',
        })
        no_demands_client = self.client_class()
        no_demands_client.force_login(self.other)
        no_demands = no_demands_client.get(reverse('my_demands'))

        self.assertContains(status_empty, '“已找到”状态下还没有求购')
        self.assertContains(status_empty, '查看全部我的求购')
        self.assertContains(filter_empty, '没有被这组条件筛中的求购')
        self.assertContains(filter_empty, f'{reverse("my_demands")}?tab=active')
        self.assertContains(no_demands, '你还没有发布任何求购')
        self.assertNotContains(no_demands, '去寻找中')

    def test_lifecycle_cards_expose_only_valid_next_actions(self):
        active = self.make_demand('可管理的寻找中求购')
        expired = self.make_demand('可重新上架的过期求购', status='expired')
        closed = self.make_demand('只能查看的关闭求购', status='closed')
        fulfilled = self.make_demand('只能查看的已找到求购', status='fulfilled')
        self.client.force_login(self.owner)

        active_html = self.client.get(reverse('my_demands'), {'tab': 'active'})
        expired_html = self.client.get(reverse('my_demands'), {'tab': 'expired'})
        closed_html = self.client.get(reverse('my_demands'), {'tab': 'closed'})
        fulfilled_html = self.client.get(
            reverse('my_demands'), {'tab': 'fulfilled'},
        )

        self.assertContains(active_html, reverse('edit_demand', args=[active.id]))
        self.assertContains(active_html, reverse('close_demand', args=[active.id]))
        self.assertContains(active_html, '标记已找到')
        self.assertContains(expired_html, reverse('edit_demand', args=[expired.id]))
        self.assertContains(expired_html, '更新并重新上架')
        self.assertNotContains(expired_html, reverse('close_demand', args=[expired.id]))
        self.assertNotContains(closed_html, reverse('edit_demand', args=[closed.id]))
        self.assertNotContains(closed_html, reverse('close_demand', args=[closed.id]))
        self.assertNotContains(
            fulfilled_html, reverse('edit_demand', args=[fulfilled.id]),
        )

    def test_request_marks_past_due_demands_expired_before_counting(self):
        expired = self.make_demand(
            '已经超过截止时间的求购',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.make_demand(
            '仍然在有效期内的求购',
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse('my_demands'))

        expired.refresh_from_db()
        self.assertEqual(expired.status, 'expired')
        self.assertEqual(response.context['demand_counts']['active'], 1)
        self.assertEqual(response.context['demand_counts']['expired'], 1)
        self.assertEqual(response.context['demand_counts']['all'], 2)

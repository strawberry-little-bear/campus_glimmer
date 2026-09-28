from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import BrowsingHistory, CampusLocation, Category, DeliveryConfirmation, Favorite, Item, ItemAvailabilityWatch, Notification, NotificationPreference, Order, OrderDispute, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SearchQuery
from .analytics import build_operational_alerts, build_search_insights
from .order_maintenance import process_order_timeouts
from .recommendations import get_recommendations
from .notifications import create_notification
from .availability import notify_item_available


class ListingFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='safe-password-123')
        self.other_user = User.objects.create_user(username='bob', password='safe-password-123')
        self.category = Category.objects.create(name='数码', description='电子设备')
        self.location = CampusLocation.objects.create(
            name='图书馆东门', building='东校区', address='图书馆一层东侧',
        )
        self.item = Item.objects.create(
            title='便携键盘', description='适合宿舍使用', price='99.00',
            category=self.category, location=self.location, condition='9成新', seller=self.user,
        )

    def _complete_order(self):
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '周三晚课后见面'},
        )
        order = Order.objects.get(item=self.item)
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        status_url = reverse('update_order_status', args=[order.id])
        self.client.post(status_url, {'status': 'confirmed'})
        self.client.post(status_url, {'status': 'meeting'})
        self.client.post(reverse('confirm_delivery', args=[order.id]))
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(reverse('confirm_delivery', args=[order.id]))
        order.refresh_from_db()
        return order

    def test_home_and_listing_pages_render(self):
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)
        response = self.client.get(reverse('item_list'), {'q': '键盘', 'sort': 'price_asc'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '便携键盘')
        self.assertContains(response, '图书馆东门')

    def test_location_filter_only_returns_matching_items(self):
        another_location = CampusLocation.objects.create(name='南门快递站')
        Item.objects.create(
            title='宿舍台灯', description='暖光', price='39.00', category=self.category,
            location=another_location, condition='全新', seller=self.user,
        )
        response = self.client.get(reverse('item_list'), {'location': self.location.id})
        self.assertContains(response, '便携键盘')
        self.assertNotContains(response, '宿舍台灯')

    def test_authenticated_user_can_toggle_favorite(self):
        self.client.login(username='alice', password='safe-password-123')
        url = reverse('toggle_favorite', args=[self.item.id])
        response = self.client.post(url, {'next': reverse('item_detail', args=[self.item.id])})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Favorite.objects.filter(user=self.user, item=self.item).exists())
        self.client.post(url, {'next': reverse('item_detail', args=[self.item.id])})
        self.assertFalse(Favorite.objects.filter(user=self.user, item=self.item).exists())

    def test_user_can_submit_one_report_and_cannot_report_again(self):
        self.client.login(username='bob', password='safe-password-123')
        report_url = reverse('report_item', args=[self.item.id])
        response = self.client.post(report_url, {'reason': 'scam', 'detail': '商品描述与图片不一致'})
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertTrue(Report.objects.filter(item=self.item, reporter=self.other_user, status='pending').exists())
        response = self.client.get(report_url)
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertEqual(Report.objects.filter(item=self.item, reporter=self.other_user).count(), 1)

    def test_seller_cannot_report_own_item(self):
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('report_item', args=[self.item.id]))
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertFalse(Report.objects.filter(item=self.item, reporter=self.user).exists())
    def test_buyer_can_place_order_and_seller_can_complete_it(self):
        self.client.login(username='bob', password='safe-password-123')
        order_url = reverse('create_order', args=[self.item.id])
        response = self.client.post(order_url, {'meeting_location': self.location.id, 'buyer_note': '周三晚课后见面'})
        order = Order.objects.get(item=self.item)
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.item.refresh_from_db()
        self.assertEqual(order.status, 'pending')
        self.assertEqual(self.item.status, 'reserved')

        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        status_url = reverse('update_order_status', args=[order.id])
        self.client.post(status_url, {'status': 'confirmed'})
        self.client.post(status_url, {'status': 'meeting'})
        self.client.post(reverse('confirm_delivery', args=[order.id]))
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(reverse('confirm_delivery', args=[order.id]))
        order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        self.assertEqual(self.item.status, 'sold')
    def test_recommendations_prioritize_matching_category_and_location(self):
        books = Category.objects.create(name='书籍', description='教材资料')
        matching_item = Item.objects.create(
            title='高等数学教材', description='教材', price='45.00', category=self.category,
            location=self.location, condition='9成新', seller=self.other_user,
        )
        unrelated_item = Item.objects.create(
            title='考研资料', description='资料', price='30.00', category=books,
            condition='8成新', seller=self.other_user,
        )
        Favorite.objects.create(user=self.user, item=self.item)
        recommendations = get_recommendations(self.user, limit=2)
        self.assertEqual(recommendations[0].item, matching_item)
        self.assertNotIn(self.item, [recommendation.item for recommendation in recommendations])
        self.assertTrue(recommendations[0].reason)
    def test_health_check_returns_service_status(self):
        response = self.client.get(reverse('health_check'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
    def test_logged_in_item_view_creates_browsing_history(self):
        self.client.login(username='bob', password='safe-password-123')
        detail_url = reverse('item_detail', args=[self.item.id])
        self.client.get(detail_url)
        self.client.get(detail_url)
        record = BrowsingHistory.objects.get(user=self.other_user, item=self.item)
        self.assertEqual(record.view_count, 2)
        response = self.client.get(reverse('browsing_history'))
        self.assertContains(response, '便携键盘')


    def test_buyer_can_rate_seller_after_completed_order(self):
        order = self._complete_order()
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(
            reverse('rate_order', args=[order.id]),
            {'score': 5, 'comment': '卖家沟通顺畅，交付很准时。'},
        )
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        rating = Rating.objects.get(order=order, rater=self.other_user)
        self.assertEqual(rating.ratee, self.user)
        self.assertEqual(rating.score, 5)

    def test_user_cannot_rate_same_order_twice(self):
        order = self._complete_order()
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        rate_url = reverse('rate_order', args=[order.id])
        self.client.post(rate_url, {'score': 4, 'comment': '第一次评价'})
        self.client.post(rate_url, {'score': 1, 'comment': '重复提交'})
        self.assertEqual(Rating.objects.filter(order=order, rater=self.other_user).count(), 1)
        self.assertEqual(Rating.objects.get(order=order, rater=self.other_user).score, 4)

    def test_incomplete_order_cannot_be_rated(self):
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '等待确认'},
        )
        order = Order.objects.get(item=self.item)
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        response = self.client.post(reverse('rate_order', args=[order.id]), {'score': 5, 'comment': '不应提交'})
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.assertFalse(Rating.objects.filter(order=order).exists())

    def test_seller_rating_is_visible_on_item_detail(self):
        order = self._complete_order()
        Rating.objects.create(order=order, rater=self.other_user, ratee=self.user, score=5, comment='值得信赖')
        response = self.client.get(reverse('item_detail', args=[self.item.id]))
        self.assertContains(response, '5.0')
        self.assertContains(response, '来自 1 条交易评价')
        self.assertContains(response, '值得信赖')

    def test_order_tracks_status_history(self):
        order = self._complete_order()
        events = list(OrderEvent.objects.filter(order=order).order_by('created_at', 'id'))
        self.assertEqual([event.to_status for event in events], ['pending', 'confirmed', 'meeting', 'completed'])
        self.assertEqual(events[0].from_status, '')
        self.assertEqual(events[0].actor, self.other_user)
        self.assertEqual(events[-1].from_status, 'meeting')
        self.assertEqual(events[-1].actor, self.other_user)

    def test_order_detail_displays_status_history(self):
        order = self._complete_order()
        response = self.client.get(reverse('order_detail', args=[order.id]))
        self.assertContains(response, '买家发起交易预约')
        self.assertContains(response, '双方确认交易已完成')

    def test_new_order_notifies_seller_and_status_change_notifies_buyer(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '周三晚课后见面'},
        )
        order = Order.objects.get(item=self.item)
        seller_notice = Notification.objects.get(
            recipient=self.user, kind='order_created', order=order,
        )
        self.assertEqual(seller_notice.actor, self.other_user)
        self.assertFalse(seller_notice.is_read)

        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'confirmed'})
        buyer_notice = Notification.objects.get(
            recipient=self.other_user, kind='order_status', order=order,
        )
        self.assertIn('卖家已确认', buyer_notice.message)

    def test_rating_notifies_the_rated_user(self):
        order = self._complete_order()
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('rate_order', args=[order.id]),
            {'score': 5, 'comment': '交付顺利'},
        )
        notice = Notification.objects.get(
            recipient=self.user, kind='rating_received', order=order,
        )
        self.assertEqual(notice.actor, self.other_user)
        self.assertIn('5星', notice.message)

    def test_notification_preferences_default_to_enabled_and_can_suppress_kind(self):
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())
        created = create_notification(
            self.user,
            kind='comment_received',
            title='商品收到新的留言',
            message='有人留言了。',
        )
        self.assertIsNotNone(created)
        preference = NotificationPreference.objects.get(user=self.user)
        preference.comment_received = False
        preference.save(update_fields=['comment_received', 'updated_at'])
        suppressed = create_notification(
            self.user,
            kind='comment_received',
            title='第二条留言',
            message='这条不应写入通知中心。',
        )
        self.assertIsNone(suppressed)
        self.assertEqual(Notification.objects.filter(recipient=self.user, kind='comment_received').count(), 1)

    def test_notification_preferences_page_saves_only_current_user_settings(self):
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('notification_preferences'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '通知偏好')
        preference = NotificationPreference.objects.get(user=self.user)
        form_data = {field: 'on' for field in (
            'order_created', 'order_status', 'rating_received', 'message_received',
            'comment_received', 'saved_search_match', 'item_available', 'order_dispute',
            'order_expiring', 'order_expired', 'report_update',
        ) if field != 'message_received'}
        response = self.client.post(reverse('notification_preferences'), form_data)
        self.assertRedirects(response, reverse('notification_preferences'))
        preference.refresh_from_db()
        self.assertFalse(preference.message_received)
        self.assertTrue(preference.comment_received)
        self.assertFalse(NotificationPreference.objects.filter(user=self.other_user).exists())

    def test_notification_preferences_require_login(self):
        response = self.client.get(reverse('notification_preferences'))
        self.assertRedirects(response, f'{reverse("login")}?next={reverse("notification_preferences")}')
    def test_notification_center_can_mark_one_or_all_as_read(self):
        one = Notification.objects.create(
            recipient=self.user, kind='comment_received', title='商品收到新的留言',
            message='bob评论了你的商品。', target_url=reverse('item_detail', args=[self.item.id]),
        )
        Notification.objects.create(
            recipient=self.user, kind='order_status', title='订单状态更新',
            message='订单已确认。',
        )
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('notification_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '商品收到新的留言')
        self.assertContains(response, '标记所选为已读')
        self.assertContains(response, '2 条未读')
        self.assertEqual(response.context['notification_unread_total'], 2)
        kind_options = {
            option['value']: option for option in response.context['notification_kind_options']
        }
        self.assertEqual(kind_options['comment_received']['unread_count'], 1)
        self.assertEqual(kind_options['order_status']['unread_count'], 1)

        filtered = self.client.get(reverse('notification_list'), {'kind': 'comment_received', 'status': 'unread'})
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.context['notification_total'], 1)
        self.assertContains(filtered, '商品收到新的留言')
        self.assertEqual(filtered.context['notifications'][0].id, one.id)

        searched = self.client.get(reverse('notification_list'), {'q': '留言'})
        self.assertEqual(searched.context['notification_total'], 1)
        self.assertContains(searched, '已应用筛选条件')

        response = self.client.post(
            reverse('mark_notification_read', args=[one.id]),
            {'next': reverse('notification_list')},
        )
        self.assertRedirects(response, reverse('notification_list'))
        one.refresh_from_db()
        self.assertTrue(one.is_read)
        self.client.post(reverse('mark_all_notifications_read'))
        self.assertFalse(Notification.objects.filter(recipient=self.user, is_read=False).exists())

    def test_notification_center_can_bulk_mark_only_selected_owned_unread(self):
        first = Notification.objects.create(
            recipient=self.user, kind='comment_received', title='第一条未读', message='请查看。',
        )
        second = Notification.objects.create(
            recipient=self.user, kind='order_status', title='第二条未读', message='订单有变化。',
        )
        already_read = Notification.objects.create(
            recipient=self.user, kind='rating_received', title='已读通知', message='谢谢评价。', is_read=True,
        )
        someone_elses = Notification.objects.create(
            recipient=self.other_user, kind='comment_received', title='不属于当前用户', message='不能被越权修改。',
        )
        self.client.login(username='alice', password='safe-password-123')

        response = self.client.post(reverse('mark_selected_notifications_read'), {
            'notification_ids': [first.id, already_read.id, someone_elses.id, 'not-an-id'],
            'next': reverse('notification_list') + '?status=unread',
        })

        self.assertRedirects(response, reverse('notification_list') + '?status=unread')
        first.refresh_from_db()
        second.refresh_from_db()
        already_read.refresh_from_db()
        someone_elses.refresh_from_db()
        self.assertTrue(first.is_read)
        self.assertFalse(second.is_read)
        self.assertTrue(already_read.is_read)
        self.assertFalse(someone_elses.is_read)

    def test_notification_bulk_read_rejects_external_redirect(self):
        notification = Notification.objects.create(
            recipient=self.user, kind='comment_received', title='安全跳转', message='不能跳出站点。',
        )
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.post(reverse('mark_selected_notifications_read'), {
            'notification_ids': [notification.id], 'next': 'https://evil.example/phishing',
        })
        self.assertRedirects(response, reverse('notification_list'))
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_notification_filters_keep_query_when_paginating(self):
        Notification.objects.bulk_create([
            Notification(
                recipient=self.user,
                kind='comment_received',
                title=f'留言提醒 {index}',
                message='分页测试通知',
            )
            for index in range(21)
        ])
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('notification_list'), {
            'kind': 'comment_received', 'status': 'unread',
        })
        self.assertEqual(response.context['notification_total'], 21)
        self.assertEqual(response.context['notifications'].paginator.num_pages, 2)
        self.assertContains(response, 'kind=comment_received')
        self.assertContains(response, 'status=unread')

    def test_search_matches_category_location_and_condition(self):
        response = self.client.get(reverse('item_list'), {'q': '东校区'})
        self.assertContains(response, '便携键盘')

        response = self.client.get(reverse('item_list'), {'q': '9成新'})
        self.assertContains(response, '便携键盘')

        response = self.client.get(reverse('item_list'), {'q': '数码'})
        self.assertContains(response, '便携键盘')

    def test_search_filters_by_price_range(self):
        Item.objects.create(
            title='低价鼠标', description='备用鼠标', price='20.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        Item.objects.create(
            title='高价显示器', description='大屏显示器', price='260.00', category=self.category,
            location=self.location, condition='8成新', seller=self.other_user,
        )

        response = self.client.get(reverse('item_list'), {'min_price': '50', 'max_price': '120'})
        self.assertContains(response, '便携键盘')
        self.assertNotContains(response, '低价鼠标')
        self.assertNotContains(response, '高价显示器')

    def test_relevance_sort_prioritizes_exact_and_prefix_matches(self):
        exact = Item.objects.create(
            title='键盘', description='精确匹配', price='80.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        prefix = Item.objects.create(
            title='键盘套', description='前缀匹配', price='30.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        contains = Item.objects.create(
            title='无线键盘', description='包含匹配', price='60.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )

        response = self.client.get(reverse('item_list'), {'q': '键盘', 'sort': 'relevance'})
        result_ids = [item.id for item in response.context['items']]
        self.assertLess(result_ids.index(exact.id), result_ids.index(prefix.id))
        self.assertLess(result_ids.index(prefix.id), result_ids.index(contains.id))

    def test_search_query_records_filters_and_result_count(self):
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('item_list'), {
            'q': '键盘', 'condition': '9成新', 'location': self.location.id, 'min_price': '50', 'max_price': '120',
        })
        self.assertEqual(response.status_code, 200)
        record = SearchQuery.objects.get(user=self.user)
        self.assertEqual(record.query, '键盘')
        self.assertEqual(record.condition, '9成新')
        self.assertEqual(record.category, None)
        self.assertEqual(record.location, self.location)
        self.assertEqual(str(record.min_price), '50.00')
        self.assertEqual(str(record.max_price), '120.00')
        self.assertEqual(record.result_count, 1)

    def test_search_pagination_does_not_create_duplicate_records(self):
        for index in range(13):
            Item.objects.create(
                title=f'键盘配件 {index}', description='分页搜索商品', price='12.00',
                category=self.category, location=self.location, condition='全新', seller=self.other_user,
            )

        self.client.get(reverse('item_list'), {'q': '键盘'})
        self.client.get(reverse('item_list'), {'q': '键盘', 'page': 2})
        self.assertEqual(SearchQuery.objects.filter(query='键盘').count(), 1)

    def test_invalid_price_filters_are_ignored_safely(self):
        response = self.client.get(reverse('item_list'), {
            'q': '键盘', 'min_price': 'not-a-number', 'max_price': '-10',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '便携键盘')
        record = SearchQuery.objects.get(query='键盘')
        self.assertIsNone(record.min_price)
        self.assertIsNone(record.max_price)
    def test_operations_dashboard_is_restricted_to_staff(self):
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 403)

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '运营数据概览')

    def test_operations_dashboard_aggregates_search_and_supply_metrics(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        second_item = Item.objects.create(
            title='宿舍台灯', description='暖光台灯', price='39.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        SearchQuery.objects.create(query='键盘', condition='9成新', result_count=3)
        SearchQuery.objects.create(query='键盘', result_count=0)
        SearchQuery.objects.create(query='台灯', result_count=1)
        BrowsingHistory.objects.create(user=self.other_user, item=self.item)
        Favorite.objects.create(user=self.other_user, item=self.item)
        Order.objects.create(
            item=second_item, buyer=self.other_user, seller=self.user,
            meeting_location=self.location, agreed_price='39.00', status='completed',
        )

        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        metrics = response.context['metrics']
        self.assertEqual(metrics['new_items'], 2)
        self.assertEqual(metrics['detail_views'], 1)
        self.assertEqual(metrics['favorites'], 1)
        self.assertEqual(metrics['searches'], 3)
        self.assertEqual(metrics['zero_result_searches'], 1)
        self.assertEqual(metrics['orders'], 1)
        self.assertEqual(metrics['completed_orders'], 1)
        self.assertEqual(response.context['top_searches'][0]['query'], '键盘')
        self.assertEqual(response.context['category_stats'][0].new_count, 2)
        self.assertEqual(response.context['location_stats'][0].order_count, 1)
        self.assertEqual(response.context['conversion_funnel'][1]['rate'], 100.0)
        self.assertEqual(response.context['conversion_funnel'][2]['rate'], 100.0)
        self.assertContains(response, '用户行为转化漏斗')
        self.assertContains(response, '图书馆东门')

    def test_operations_dashboard_surfaces_actionable_alerts(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        for _ in range(3):
            SearchQuery.objects.create(query='投影仪', result_count=0)
        report_items = [self.item]
        report_items.extend(
            Item.objects.create(
                title=f'测试商品-{index}', description='测试', price='10.00',
                category=self.category, location=self.location, condition='全新', seller=self.user,
            )
            for index in (1, 2)
        )
        for report_item, reason in zip(report_items, ('scam', 'spam', 'inappropriate')):
            Report.objects.create(item=report_item, reporter=self.other_user, reason=reason)

        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        alert_keys = {alert['key'] for alert in response.context['operational_alerts']}
        self.assertIn('search_supply_gap', alert_keys)
        self.assertIn('report_backlog', alert_keys)
        self.assertContains(response, '运营提醒')
        self.assertContains(response, '搜索供给缺口')
        self.assertContains(response, '举报审核队列积压')

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertContains(export, '运营提醒')
        self.assertContains(export, '搜索供给缺口')

    def test_operational_alerts_detect_completed_order_drop(self):
        metrics = {
            'searches': 10, 'zero_result_searches': 0, 'zero_result_rate': 0,
            'pending_reports': 0,
        }
        comparisons = [{
            'key': 'completed_orders', 'current': 2, 'previous': 5,
        }]
        alerts = build_operational_alerts(metrics, comparisons)
        self.assertEqual([alert['key'] for alert in alerts], ['completed_order_drop'])
        self.assertEqual(alerts[0]['metric'], '-60.0%')
        self.assertEqual(alerts[0]['severity'], 'critical')

    def test_operations_dashboard_export_is_staff_only_and_contains_aggregates(self):
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard_export'), {'days': 7})
        self.assertEqual(response.status_code, 403)

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        SearchQuery.objects.create(query='键盘', result_count=2)
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard_export'), {'days': 7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('campus-glimmer-operations-7d.csv', response['Content-Disposition'])
        report = response.content.decode('utf-8-sig')
        self.assertIn('拾光校园运营数据导出', report)
        self.assertIn('核心指标,数值', report)
        self.assertIn('当前在售商品,1', report)
        self.assertIn('每日活动趋势', report)
        self.assertIn('图书馆东门', report)

    def test_operations_dashboard_respects_selected_period(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        old_search = SearchQuery.objects.create(query='旧搜索', result_count=0)
        SearchQuery.objects.filter(pk=old_search.pk).update(
            created_at=timezone.now() - timedelta(days=40),
        )
        SearchQuery.objects.create(query='近期搜索', result_count=2)

        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'), {'days': 7})
        self.assertEqual(response.context['period_days'], 7)
        self.assertEqual(response.context['metrics']['searches'], 1)
        self.assertContains(response, '近期搜索')
        self.assertNotContains(response, '旧搜索')

    def test_operations_dashboard_compares_with_previous_period(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        old_item = Item.objects.create(
            title='上一周期商品', description='用于周期对比', price='20.00',
            category=self.category, location=self.location, condition='全新', seller=self.user,
        )
        previous_time = timezone.now() - timedelta(days=10)
        Item.objects.filter(pk=old_item.pk).update(created_at=previous_time)
        SearchQuery.objects.create(query='当前搜索', result_count=2)
        old_search_one = SearchQuery.objects.create(query='历史搜索', result_count=1)
        old_search_two = SearchQuery.objects.create(query='历史搜索', result_count=0)
        SearchQuery.objects.filter(pk__in=[old_search_one.pk, old_search_two.pk]).update(created_at=previous_time)

        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'), {'days': 7})
        self.assertEqual(response.status_code, 200)
        comparisons = {row['key']: row for row in response.context['period_comparisons']}
        self.assertEqual(comparisons['new_items']['current'], 1)
        self.assertEqual(comparisons['new_items']['previous'], 1)
        self.assertEqual(comparisons['new_items']['change_display'], '持平')
        self.assertEqual(comparisons['searches']['current'], 1)
        self.assertEqual(comparisons['searches']['previous'], 2)
        self.assertEqual(comparisons['searches']['change_display'], '-50.0%')
        self.assertContains(response, '周期对比')

    def test_operations_dashboard_segments_active_users_by_behavior(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        SearchQuery.objects.create(user=self.user, query='键盘', result_count=1)
        BrowsingHistory.objects.create(user=self.other_user, item=self.item)
        Favorite.objects.create(user=self.other_user, item=self.item)

        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['metrics']['active_users'], 2)
        segments = {row['key']: row for row in response.context['activity_segments']}
        self.assertEqual(segments['supplier']['count'], 0)
        self.assertEqual(segments['explorer']['count'], 0)
        self.assertEqual(segments['light']['count'], 2)
        self.assertContains(response, '活跃用户分层')

    def test_operations_dashboard_reports_previous_new_user_retention(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        cohort_user = User.objects.create_user(username='cohort-user', password='safe-password-123')
        cohort_time = timezone.now() - timedelta(days=10)
        User.objects.filter(pk=cohort_user.pk).update(date_joined=cohort_time)
        SearchQuery.objects.create(user=cohort_user, query='回访搜索', result_count=1)

        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'), {'days': 7})
        self.assertEqual(response.status_code, 200)
        retention = response.context['user_retention']
        self.assertEqual(retention['cohort_size'], 1)
        self.assertEqual(retention['retained_users'], 1)
        self.assertEqual(retention['rate'], 100.0)
        self.assertContains(response, '用户回访率')

    def test_operations_dashboard_export_includes_activity_segments(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        SearchQuery.objects.create(user=self.user, query='键盘', result_count=1)
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        report = response.content.decode('utf-8-sig')
        self.assertIn('周期活跃用户,1', report)
        self.assertIn('活跃用户分层,人数,占活跃用户（%）,识别口径', report)
        self.assertIn('供给贡献者,0,0.0,周期内发布了多件商品', report)
        self.assertIn('用户回访,人数,比例（%）,口径说明', report)

    def test_operations_dashboard_export_includes_period_comparison(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard_export'), {'days': 7})
        self.assertEqual(response.status_code, 200)
        report = response.content.decode('utf-8-sig')
        self.assertIn('周期对比,当前周期,上一周期,变化', report)
        self.assertIn('新增商品,1,0,新增', report)

    def test_user_can_save_search_from_filtered_results(self):
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('item_list'), {
            'q': '键盘', 'condition': '9成新', 'location': self.location.id,
            'min_price': '50', 'max_price': '120',
        })
        self.assertTrue(response.context['has_search_criteria'])
        response = self.client.post(reverse('save_search'), {
            'name': '图书馆附近的键盘', 'query': '键盘', 'condition': '9成新',
            'location': self.location.id, 'min_price': '50', 'max_price': '120',
            'next': reverse('item_list'),
        })
        self.assertRedirects(response, reverse('item_list'))
        saved_search = SavedSearch.objects.get(user=self.user)
        self.assertEqual(saved_search.location, self.location)
        self.assertEqual(str(saved_search.min_price), '50.00')

    def test_new_matching_item_creates_one_aggregated_notification(self):
        SavedSearch.objects.create(user=self.other_user, name='键盘提醒', query='键盘')
        SavedSearch.objects.create(user=self.other_user, name='数码提醒', category=self.category)
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.post(reverse('new_item'), {
            'title': '宿舍键盘', 'description': '轻便好用', 'price': '39.00',
            'category': self.category.id, 'location': self.location.id, 'condition': '全新',
            'images-TOTAL_FORMS': '3', 'images-INITIAL_FORMS': '0',
            'images-MIN_NUM_FORMS': '0', 'images-MAX_NUM_FORMS': '5',
        })
        self.assertEqual(response.status_code, 302)
        notifications = Notification.objects.filter(
            recipient=self.other_user, kind='saved_search_match',
        )
        self.assertEqual(notifications.count(), 1)
        self.assertIn('宿舍键盘', notifications.first().message)
        self.assertIn('2 个关注条件', notifications.first().message)
        self.assertFalse(Notification.objects.filter(recipient=self.user, kind='saved_search_match').exists())

    def test_saved_search_can_be_paused_and_deleted(self):
        saved_search = SavedSearch.objects.create(user=self.user, name='价格提醒', max_price='100')
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('saved_search_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '价格提醒')
        response = self.client.post(reverse('toggle_saved_search', args=[saved_search.id]))
        self.assertRedirects(response, reverse('saved_search_list'))
        saved_search.refresh_from_db()
        self.assertFalse(saved_search.is_active)
        response = self.client.post(reverse('delete_saved_search', args=[saved_search.id]))
        self.assertRedirects(response, reverse('saved_search_list'))
        self.assertFalse(SavedSearch.objects.filter(pk=saved_search.id).exists())


    def test_delivery_confirmation_requires_both_parties(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '当面交付'},
        )
        order = Order.objects.get(item=self.item)
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'confirmed'})
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'meeting'})
        self.client.post(reverse('confirm_delivery', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'meeting')
        confirmation = DeliveryConfirmation.objects.get(order=order)
        self.assertIsNotNone(confirmation.seller_confirmed_at)
        self.assertIsNone(confirmation.buyer_confirmed_at)

        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(reverse('confirm_delivery', args=[order.id]))
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'sold')
        self.assertTrue(DeliveryConfirmation.objects.get(order=order).is_complete)

    def test_user_can_open_dispute_and_staff_can_resolve_it(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '需要核对商品'},
        )
        order = Order.objects.get(item=self.item)
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'confirmed'})
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(reverse('open_dispute', args=[order.id]), {
            'reason': 'mismatch', 'detail': '收到的商品与描述不一致。',
        })
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        dispute = OrderDispute.objects.get(order=order)
        self.assertEqual(dispute.opened_by, self.other_user)
        self.assertTrue(Notification.objects.filter(recipient=self.user, kind='order_dispute').exists())

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('dispute_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '交易争议处理')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]), {
            'status': 'resolved', 'resolution_note': '已核实并完成双方沟通。',
        })
        self.assertRedirects(response, reverse('dispute_list'))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.reviewer, self.user)
        self.assertTrue(Notification.objects.filter(recipient=self.other_user, kind='order_dispute').exists())

    def test_recommendation_feedback_closes_the_personalization_loop(self):
        second_item = Item.objects.create(
            title='宿舍台灯', description='暖光护眼', price='39.00',
            category=self.category, location=self.location, condition='全新', seller=self.user,
        )
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(
            reverse('recommendation_feedback', args=[self.item.id]),
            {'action': 'interested', 'next': reverse('home')},
        )
        self.assertRedirects(response, reverse('home'))
        feedback = RecommendationFeedback.objects.get(user=self.other_user, item=self.item)
        self.assertEqual(feedback.action, 'interested')

        response = self.client.post(
            reverse('recommendation_feedback', args=[self.item.id]),
            {'action': 'dismiss', 'next': reverse('home')},
        )
        self.assertRedirects(response, reverse('home'))
        feedback.refresh_from_db()
        self.assertEqual(feedback.action, 'dismiss')
        recommendations = get_recommendations(self.other_user, limit=10)
        self.assertNotIn(self.item.id, [recommendation.item.id for recommendation in recommendations])
        self.assertIn(second_item.id, [recommendation.item.id for recommendation in recommendations])

    def test_recommendation_feedback_rejects_unsupported_actions(self):
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(
            reverse('recommendation_feedback', args=[self.item.id]),
            {'action': 'boost', 'next': 'https://evil.example/steal'},
        )
        self.assertRedirects(response, reverse('home'))
        self.assertFalse(RecommendationFeedback.objects.filter(user=self.other_user, item=self.item).exists())

    def test_staff_can_review_report_and_notify_reporter(self):
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(reverse('report_item', args=[self.item.id]), {
            'reason': 'scam', 'detail': '商品描述与实际情况不一致。',
        })
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        report = Report.objects.get(item=self.item, reporter=self.other_user)

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('report_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '商品举报审核')
        self.assertContains(response, '疑似诈骗或虚假信息')
        response = self.client.post(reverse('review_report', args=[report.id]), {
            'status': 'resolved', 'review_note': '已核对商品信息，提醒发布者补充说明。',
        })
        self.assertRedirects(response, reverse('report_list'))
        report.refresh_from_db()
        self.assertEqual(report.status, 'resolved')
        self.assertEqual(report.reviewer, self.user)
        self.assertIsNotNone(report.reviewed_at)
        self.assertTrue(Notification.objects.filter(
            recipient=self.other_user, kind='report_update', item=self.item,
        ).exists())

    def test_search_insights_aggregate_demand_gaps_for_staff(self):
        SearchQuery.objects.create(user=self.other_user, query='台灯', result_count=0)
        SearchQuery.objects.create(user=self.other_user, query='台灯', result_count=0)
        SearchQuery.objects.create(user=self.user, query='台灯', result_count=2)
        SearchQuery.objects.create(user=self.other_user, query='键盘', result_count=4)

        dashboard = build_search_insights(30)
        self.assertEqual(dashboard['metrics']['searches'], 4)
        self.assertEqual(dashboard['metrics']['zero_result_searches'], 2)
        self.assertEqual(dashboard['metrics']['zero_result_rate'], 50.0)
        self.assertEqual(dashboard['term_rows'][0]['query'], '台灯')
        self.assertEqual(dashboard['term_rows'][0]['zero_result_rate'], 66.7)
        self.assertEqual(dashboard['gap_terms'][0]['query'], '台灯')

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('search_insights'), {'q': '台', 'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '搜索需求洞察')
        self.assertContains(response, '台灯')
        self.assertContains(response, '供给缺口')

    def test_search_insights_builds_hourly_and_weekday_rhythm(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        records = []
        for hour, result_count in ((9, 3), (9, 0), (21, 0)):
            record = SearchQuery.objects.create(
                user=self.other_user, query='台灯', result_count=result_count,
            )
            record.created_at = timezone.make_aware(
                datetime.combine(yesterday, time(hour=hour, minute=15)),
            )
            record.save(update_fields=['created_at'])
            records.append(record)

        dashboard = build_search_insights(30)
        rhythm = dashboard['search_rhythm']
        self.assertEqual(rhythm['peak_hour']['hour'], 9)
        self.assertEqual(rhythm['peak_hour']['count'], 2)
        self.assertEqual(rhythm['peak_weekday']['count'], 3)
        self.assertEqual(rhythm['hourly'][9]['zero_result_count'], 1)
        self.assertEqual(sum(slot['count'] for slot in rhythm['hourly']), 3)
        self.assertEqual(sum(day['count'] for day in rhythm['weekdays']), 3)

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('search_insights'))
        self.assertContains(response, '搜索时段趋势')
        self.assertContains(response, '星期分布')

    def test_search_insights_compares_previous_period_and_term_movement(self):
        today = timezone.localdate()
        current_date = today - timedelta(days=1)
        previous_date = today - timedelta(days=31)

        def create_search(query, result_count, created_date):
            record = SearchQuery.objects.create(
                user=self.other_user, query=query, result_count=result_count,
            )
            record.created_at = timezone.make_aware(
                datetime.combine(created_date, time(hour=10, minute=30)),
            )
            record.save(update_fields=['created_at'])

        for query, result_count in (('台灯', 2), ('台灯', 2), ('台灯', 2), ('键盘', 3)):
            create_search(query, result_count, current_date)
        for query, result_count in (('台灯', 0), ('书籍', 4), ('书籍', 4)):
            create_search(query, result_count, previous_date)

        comparison = build_search_insights(30)['period_comparison']
        self.assertEqual(comparison['current_searches'], 4)
        self.assertEqual(comparison['previous_searches'], 3)
        self.assertEqual(comparison['search_change']['change_display'], '+33.3%')
        self.assertEqual(comparison['zero_result_rate_delta'], -33.3)
        rising = {row['query']: row['delta'] for row in comparison['rising_terms']}
        falling = {row['query']: row['delta'] for row in comparison['falling_terms']}
        self.assertEqual(rising['台灯'], 2)
        self.assertEqual(rising['键盘'], 1)
        self.assertEqual(falling['书籍'], -2)

        filtered_comparison = build_search_insights(30, '台灯')['period_comparison']
        self.assertEqual(filtered_comparison['current_searches'], 3)
        self.assertEqual(filtered_comparison['previous_searches'], 1)
        self.assertEqual([row['query'] for row in filtered_comparison['falling_terms']], [])

    def test_staff_can_export_filtered_search_insights(self):
        SearchQuery.objects.create(user=self.other_user, query='台灯', result_count=0)
        SearchQuery.objects.create(user=self.other_user, query='键盘', result_count=3)
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.login(username='alice', password='safe-password-123')

        response = self.client.get(reverse('search_insights_export'), {'days': 7, 'q': '台灯'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename=campus-glimmer-search-insights-7d.csv', response['Content-Disposition'])
        report = response.content.decode('utf-8-sig')
        self.assertIn('拾光校园搜索需求洞察导出', report)
        self.assertIn('搜索词筛选,台灯', report)
        self.assertIn('台灯,1,1,100.0', report)
        self.assertNotIn('键盘,1,0', report)

    def test_search_insights_is_staff_only(self):
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.get(reverse('search_insights'))
        self.assertEqual(response.status_code, 403)

    def test_pending_order_gets_confirmation_deadline(self):
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '请卖家确认'},
        )
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(item=self.item)
        self.assertIsNotNone(order.confirmation_deadline)
        self.assertGreater(order.confirmation_deadline, timezone.now())
        self.assertIsNone(order.confirmation_reminder_sent_at)
        self.assertContains(self.client.get(reverse('order_detail', args=[order.id])), '确认截止')

    def test_order_timeout_reminds_once_and_releases_reservation(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '等待确认'},
        )
        order = Order.objects.get(item=self.item)
        base_time = timezone.now()
        order.confirmation_deadline = base_time + timedelta(hours=3)
        order.save(update_fields=['confirmation_deadline', 'updated_at'])

        result = process_order_timeouts(now=base_time, reminder_hours=6)
        self.assertEqual(result, {'expired': 0, 'reminded': 1})
        order.refresh_from_db()
        self.assertIsNotNone(order.confirmation_reminder_sent_at)
        self.assertEqual(Notification.objects.filter(order=order, kind='order_expiring').count(), 1)

        second_result = process_order_timeouts(now=base_time + timedelta(hours=1), reminder_hours=6)
        self.assertEqual(second_result, {'expired': 0, 'reminded': 0})
        self.assertEqual(Notification.objects.filter(order=order, kind='order_expiring').count(), 1)

    def test_order_timeout_cancels_order_reopens_item_and_notifies_both_sides(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '等待确认'},
        )
        order = Order.objects.get(item=self.item)
        expired_at = timezone.now()
        order.confirmation_deadline = expired_at - timedelta(minutes=1)
        order.save(update_fields=['confirmation_deadline', 'updated_at'])

        result = process_order_timeouts(now=expired_at)
        self.assertEqual(result, {'expired': 1, 'reminded': 0})
        order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(self.item.status, 'available')
        self.assertTrue(OrderEvent.objects.filter(order=order, to_status='cancelled', actor__isnull=True).exists())
        self.assertEqual(Notification.objects.filter(order=order, kind='order_expired').count(), 2)

        second_result = process_order_timeouts(now=expired_at + timedelta(hours=1))
        self.assertEqual(second_result, {'expired': 0, 'reminded': 0})
        self.assertEqual(Notification.objects.filter(order=order, kind='order_expired').count(), 2)


class AvailabilityWatchTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='seller', password='safe-password-123')
        self.watcher = User.objects.create_user(username='watcher', password='safe-password-123')
        self.category = Category.objects.create(name='教材')
        self.item = Item.objects.create(
            title='高等数学教材', description='课本', price='35.00',
            category=self.category, condition='八成新', seller=self.seller, status='sold',
        )

    def test_user_can_toggle_watch_for_unavailable_item_but_seller_cannot(self):
        self.client.login(username='watcher', password='safe-password-123')
        url = reverse('toggle_availability_watch', args=[self.item.id])
        response = self.client.post(url)
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertTrue(ItemAvailabilityWatch.objects.filter(user=self.watcher, item=self.item).exists())

        self.client.post(url)
        self.assertFalse(ItemAvailabilityWatch.objects.filter(user=self.watcher, item=self.item).exists())

        self.client.logout()
        self.client.login(username='seller', password='safe-password-123')
        self.client.post(url)
        self.assertFalse(ItemAvailabilityWatch.objects.filter(user=self.seller, item=self.item).exists())

    def test_reopening_item_notifies_watchers_and_consumes_watch(self):
        ItemAvailabilityWatch.objects.create(user=self.watcher, item=self.item)
        notified_count = notify_item_available(self.item)
        self.assertEqual(notified_count, 0)

        self.item.status = 'available'
        self.item.save(update_fields=['status', 'updated_at'])
        notified_count = notify_item_available(self.item, actor=self.seller)

        self.assertEqual(notified_count, 1)
        self.assertFalse(ItemAvailabilityWatch.objects.filter(user=self.watcher, item=self.item).exists())
        notification = Notification.objects.get(recipient=self.watcher, kind='item_available')
        self.assertEqual(notification.item, self.item)
        self.assertEqual(notification.target_url, reverse('item_detail', args=[self.item.id]))

        self.assertEqual(notify_item_available(self.item), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.watcher, kind='item_available').count(), 1)

    def test_disabled_preference_keeps_watch_without_creating_notification(self):
        ItemAvailabilityWatch.objects.create(user=self.watcher, item=self.item)
        NotificationPreference.objects.create(user=self.watcher, item_available=False)
        self.item.status = 'available'
        self.item.save(update_fields=['status', 'updated_at'])

        self.assertEqual(notify_item_available(self.item), 0)
        self.assertTrue(ItemAvailabilityWatch.objects.filter(user=self.watcher, item=self.item).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.watcher, kind='item_available').exists())

    def test_manual_reopen_notifies_watcher(self):
        ItemAvailabilityWatch.objects.create(user=self.watcher, item=self.item)
        self.client.login(username='seller', password='safe-password-123')
        response = self.client.post(
            reverse('mark_sold', args=[self.item.id]),
            {'status': 'available'},
        )
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertEqual(Notification.objects.filter(recipient=self.watcher, kind='item_available').count(), 1)

    def test_order_cancellation_releases_item_and_notifies_watcher(self):
        buyer = User.objects.create_user(username='buyer', password='safe-password-123')
        ItemAvailabilityWatch.objects.create(user=self.watcher, item=self.item)
        self.item.status = 'available'
        self.item.save(update_fields=['status', 'updated_at'])

        self.client.login(username='buyer', password='safe-password-123')
        self.client.post(reverse('create_order', args=[self.item.id]), {})
        order = Order.objects.get(item=self.item)
        self.client.logout()
        self.client.login(username='seller', password='safe-password-123')
        response = self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'cancelled'})

        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'available')
        self.assertEqual(Notification.objects.filter(recipient=self.watcher, kind='item_available').count(), 1)

    def test_order_timeout_releases_item_and_notifies_watcher(self):
        buyer = User.objects.create_user(username='timeout-buyer', password='safe-password-123')
        ItemAvailabilityWatch.objects.create(user=self.watcher, item=self.item)
        self.item.status = 'available'
        self.item.save(update_fields=['status', 'updated_at'])

        self.client.login(username='timeout-buyer', password='safe-password-123')
        self.client.post(reverse('create_order', args=[self.item.id]), {})
        order = Order.objects.get(item=self.item)
        expired_at = timezone.now()
        order.confirmation_deadline = expired_at - timedelta(minutes=1)
        order.save(update_fields=['confirmation_deadline', 'updated_at'])

        result = process_order_timeouts(now=expired_at)
        self.assertEqual(result, {'expired': 1, 'reminded': 0})
        self.assertEqual(Notification.objects.filter(recipient=self.watcher, kind='item_available').count(), 1)


class SearchSuggestionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='searcher', password='safe-password-123')
        self.category = Category.objects.create(name='数码配件')
        self.location = CampusLocation.objects.create(
            name='图书馆东门', building='东校区', address='图书馆一层东侧',
        )
        Item.objects.create(
            title='宿舍台灯', description='暖光护眼', price='39.00',
            category=self.category, location=self.location, seller=self.user,
        )

    def test_suggestions_combine_popular_queries_categories_and_locations(self):
        for _ in range(3):
            SearchQuery.objects.create(query='台灯')
        SearchQuery.objects.create(query='台灯罩')

        response = self.client.get(reverse('search_suggestions'), {'q': '台灯'})

        self.assertEqual(response.status_code, 200)
        suggestions = response.json()['suggestions']
        self.assertLessEqual(len(suggestions), 8)
        self.assertEqual(suggestions[0]['text'], '台灯')
        self.assertEqual(suggestions[0]['kind'], 'history')

        location_response = self.client.get(reverse('search_suggestions'), {'q': '图书'})
        self.assertIn({'text': '图书馆东门', 'kind': 'location', 'label': '交易地点', 'meta': '1 件在售'}, location_response.json()['suggestions'])

    def test_short_queries_return_empty_without_revealing_history(self):
        SearchQuery.objects.create(query='台灯')

        response = self.client.get(reverse('search_suggestions'), {'q': '台'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'suggestions': []})

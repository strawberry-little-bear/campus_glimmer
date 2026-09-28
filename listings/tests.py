from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import BrowsingHistory, CampusLocation, Category, DemandPost, DeliveryConfirmation, Favorite, Item, ItemAvailabilityWatch, MeetingAppointment, Notification, NotificationPreference, Order, OrderDispute, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SearchClick, SearchImpression, SearchQuery, SearchSynonym
from .analytics import build_operations_dashboard, build_operational_alerts, build_search_insights
from .order_maintenance import process_order_timeouts
from .order_workflow import OrderTransitionError, transition_order
from .recommendations import get_recommendations
from .reputation import build_seller_reputation
from .notifications import create_notification
from .availability import notify_item_available
from .demand_matching import notify_demand_matches
from chat_messages.models import PrivateMessage


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

    def test_order_workflow_rejects_buyer_confirm_and_keeps_order_unchanged(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '等待确认'},
        )
        order = Order.objects.get(item=self.item)

        with self.assertRaises(OrderTransitionError):
            transition_order(order_id=order.id, actor=self.other_user, target_status='confirmed')

        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')
        self.assertEqual(order.events.count(), 1)

    def test_order_workflow_locks_transition_and_records_actor(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '等待卖家确认'},
        )
        order = Order.objects.get(item=self.item)

        updated_order = transition_order(
            order_id=order.id,
            actor=self.user,
            target_status='confirmed',
        )

        self.assertEqual(updated_order.status, 'confirmed')
        event = OrderEvent.objects.filter(order=order, to_status='confirmed').get()
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.from_status, 'pending')
        self.assertTrue(Notification.objects.filter(
            recipient=self.other_user, order=order, kind='order_status',
        ).exists())

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

    def test_unread_notifications_are_aggregated_by_dedupe_key(self):
        first = create_notification(
            self.user, kind='comment_received', title='商品收到新的留言',
            message='有人留言了。', dedupe_key='comment:item:alice',
        )
        second = create_notification(
            self.user, kind='comment_received', title='商品收到新的留言',
            message='又有人留言了。', dedupe_key='comment:item:alice',
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.occurrence_count, 2)
        self.assertEqual(first.message, '又有人留言了。')

        first.is_read = True
        first.save(update_fields=['is_read'])
        third = create_notification(
            self.user, kind='comment_received', title='商品收到新的留言',
            message='已读后重新提醒。', dedupe_key='comment:item:alice',
        )
        self.assertNotEqual(third.pk, first.pk)
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 2)

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

    def test_search_expands_operator_managed_synonyms(self):
        SearchSynonym.objects.create(keyword='电脑', synonym='笔记本')
        laptop = Item.objects.create(
            title='轻薄笔记本', description='适合上课使用', price='1200.00',
            category=self.category, location=self.location, condition='九成新', seller=self.other_user,
        )
        response = self.client.get(reverse('item_list'), {'q': '电脑'})
        self.assertContains(response, laptop.title)
        self.assertContains(response, '已扩展匹配：笔记本')
        self.assertEqual(response.context['search_terms'], ['电脑', '笔记本'])

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

    def test_relevance_sort_uses_recent_search_click_feedback_within_same_text_rank(self):
        first = Item.objects.create(
            title='键盘保护套', description='先发布的前缀匹配', price='30.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        clicked = Item.objects.create(
            title='键盘收纳包', description='被更多用户点击的前缀匹配', price='60.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        search = SearchQuery.objects.create(query='键盘', result_count=2)
        SearchClick.objects.create(search_query=search, item=clicked, position=2)

        response = self.client.get(reverse('item_list'), {'q': '键盘', 'sort': 'relevance'})
        result_ids = [item.id for item in response.context['items']]
        self.assertLess(result_ids.index(clicked.id), result_ids.index(first.id))

    def test_relevance_sort_prefers_higher_click_through_rate_not_only_click_volume(self):
        low_ctr = Item.objects.create(
            title='键盘低转化', description='曝光很多但点击少', price='30.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        high_ctr = Item.objects.create(
            title='键盘高转化', description='曝光少但点击稳定', price='60.00', category=self.category,
            location=self.location, condition='全新', seller=self.other_user,
        )
        search = SearchQuery.objects.create(query='键盘', result_count=2)
        SearchImpression.objects.bulk_create([
            SearchImpression(search_query=search, item=low_ctr, position=1)
            for _ in range(10)
        ] + [SearchImpression(search_query=search, item=high_ctr, position=2)])
        SearchClick.objects.create(search_query=search, item=low_ctr, position=1)
        SearchClick.objects.create(search_query=search, item=high_ctr, position=2)

        response = self.client.get(reverse('item_list'), {'q': '键盘', 'sort': 'relevance'})
        result_ids = [item.id for item in response.context['items']]
        self.assertLess(result_ids.index(high_ctr.id), result_ids.index(low_ctr.id))

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

    def test_search_results_record_item_exposures_for_quality_analysis(self):
        response = self.client.get(reverse('item_list'), {'q': '键盘'})
        self.assertEqual(response.status_code, 200)
        record = SearchQuery.objects.get(query='键盘')
        exposure = SearchImpression.objects.get(search_query=record, item=self.item)
        self.assertEqual(exposure.position, 1)
        self.assertIsNone(exposure.user)

    def test_search_result_detail_records_click_context(self):
        response = self.client.get(reverse('item_list'), {'q': '键盘', 'sort': 'relevance'})
        self.assertEqual(response.status_code, 200)
        record = SearchQuery.objects.get(query='键盘')
        self.assertContains(response, f'search_id={record.id}')

        self.client.login(username='bob', password='safe-password-123')
        self.client.get(
            reverse('item_detail', args=[self.item.id]),
            {'search_id': record.id, 'position': 1},
        )
        click = SearchClick.objects.get(search_query=record, item=self.item)
        self.assertEqual(click.user, self.other_user)
        self.assertEqual(click.position, 1)

    def test_search_insights_reports_click_through_quality(self):
        record = SearchQuery.objects.create(
            user=self.user, query='键盘', result_count=3,
        )
        SearchClick.objects.create(
            search_query=record, item=self.item, user=self.other_user, position=1,
        )
        dashboard = build_search_insights(30)
        self.assertEqual(dashboard['metrics']['clicks'], 1)
        self.assertEqual(dashboard['metrics']['click_through_rate'], 100.0)
        self.assertEqual(dashboard['term_rows'][0]['click_count'], 1)
        self.assertEqual(dashboard['clicked_items'][0]['item_id'], self.item.id)
        self.assertEqual(dashboard['search_quality']['score'], 100)
        self.assertEqual(dashboard['search_quality']['level'], '健康')

    def test_search_quality_score_surfaces_supply_and_engagement_risks(self):
        for query, result_count in (
            ('显示器', 0), ('显示器', 0), ('显示器', 2), ('显示器', 1),
        ):
            SearchQuery.objects.create(query=query, result_count=result_count)

        dashboard = build_search_insights(30, query='显示器')

        self.assertEqual(dashboard['search_quality']['score'], 28)
        self.assertEqual(dashboard['search_quality']['level'], '重点关注')
        self.assertEqual(dashboard['search_quality']['supply_score'], 50)
        self.assertEqual(dashboard['search_quality']['engagement_score'], 0)
        self.assertTrue(any('同义词' in item for item in dashboard['search_quality']['recommendations']))

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
        self.assertEqual(metrics['searches_with_results'], 2)
        self.assertEqual(metrics['search_click_through_rate'], 0)
        self.assertEqual(metrics['search_quality_score'], 37)
        self.assertEqual(metrics['orders'], 1)
        self.assertEqual(metrics['completed_orders'], 1)
        self.assertEqual(response.context['top_searches'][0]['query'], '键盘')
        self.assertEqual(response.context['category_stats'][0].new_count, 2)
        self.assertEqual(response.context['location_stats'][0].order_count, 1)
        self.assertEqual(response.context['conversion_funnel'][1]['rate'], 100.0)
        self.assertEqual(response.context['conversion_funnel'][2]['rate'], 100.0)
        self.assertContains(response, '用户行为转化漏斗')
        self.assertContains(response, '图书馆东门')

    def test_operations_dashboard_reports_demand_match_reach(self):
        demand = DemandPost.objects.create(
            requester=self.other_user, title='运营求购', description='测试匹配统计',
            category=self.category, location=self.location,
        )
        Notification.objects.create(
            recipient=self.other_user, kind='demand_match', title='匹配提醒',
            message='发现商品', demand=demand, item=self.item, is_read=True,
        )
        Notification.objects.create(
            recipient=self.other_user, kind='demand_match', title='匹配提醒',
            message='发现商品', demand=demand, item=self.item, is_read=False,
        )
        dashboard = build_operations_dashboard(30)
        insights = dashboard['demand_match_insights']
        self.assertEqual(insights['notification_count'], 2)
        self.assertEqual(insights['demand_count'], 1)
        self.assertEqual(insights['item_count'], 1)
        self.assertEqual(insights['read_rate'], 50.0)
        self.assertEqual(dashboard['metrics']['demand_matches'], 2)
        self.assertEqual(dashboard['metrics']['demand_match_demands'], 1)

    def test_operations_dashboard_reports_notification_deduplication(self):
        Notification.objects.create(
            recipient=self.user,
            kind='message_received',
            title='收到私信',
            message='有人给你发来消息。',
            occurrence_count=3,
            is_read=False,
        )
        Notification.objects.create(
            recipient=self.user,
            kind='comment_received',
            title='收到留言',
            message='你的商品收到新的留言。',
            occurrence_count=1,
            is_read=True,
        )

        dashboard = build_operations_dashboard(30)
        insights = dashboard['notification_insights']
        self.assertEqual(insights['notification_rows'], 2)
        self.assertEqual(insights['notification_events'], 4)
        self.assertEqual(insights['compressed_events'], 2)
        self.assertEqual(insights['compression_rate'], 50.0)
        self.assertEqual(insights['unread_notifications'], 1)
        self.assertEqual(insights['unread_rate'], 50.0)
        self.assertEqual(insights['kind_rows'][0]['label'], '收到新私信')
        self.assertEqual(insights['kind_rows'][0]['event_count'], 3)

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'))
        self.assertContains(response, '通知触达与聚合')
        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertContains(export, '通知聚合压缩率')
    def test_operations_dashboard_reports_order_health_signals(self):
        overdue_item = Item.objects.create(
            title='待确认商品', description='测试超时预约', price='20.00',
            category=self.category, location=self.location, condition='全新', seller=self.user,
        )
        cancelled_item = Item.objects.create(
            title='取消商品', description='测试取消率', price='30.00',
            category=self.category, location=self.location, condition='全新', seller=self.user,
        )
        overdue_order = Order.objects.create(
            item=overdue_item, buyer=self.other_user, seller=self.user,
            meeting_location=self.location, agreed_price='20.00', status='pending',
            confirmation_deadline=timezone.now() - timedelta(hours=2),
        )
        cancelled_order = Order.objects.create(
            item=cancelled_item, buyer=self.other_user, seller=self.user,
            meeting_location=self.location, agreed_price='30.00', status='cancelled',
        )
        OrderEvent.objects.create(
            order=cancelled_order, actor=self.other_user,
            from_status='pending', to_status='cancelled', note='买家取消',
        )

        dashboard = build_operations_dashboard(30)
        health = dashboard['order_health']
        self.assertEqual(health['total_orders'], 2)
        self.assertEqual(health['cancelled_orders'], 1)
        self.assertEqual(health['overdue_pending_orders'], 1)
        self.assertEqual(health['cancellation_rate'], 50.0)
        self.assertEqual(health['risk_level'], 'critical')
        self.assertIn('超过卖家确认截止时间', health['risk_message'])

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'))
        self.assertContains(response, '交易健康度')
        self.assertContains(response, '需要立即跟进')
        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertContains(export, '交易健康度')
        self.assertContains(export, '超时待确认')


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

    def test_operational_alerts_detect_search_engagement_drop(self):
        metrics = {
            'searches': 12, 'zero_result_searches': 1, 'zero_result_rate': 8.3,
            'searches_with_results': 6, 'search_click_through_rate': 16.7,
            'pending_reports': 0,
        }

        alerts = build_operational_alerts(metrics, [])

        self.assertEqual([alert['key'] for alert in alerts], ['search_engagement_drop'])
        self.assertEqual(alerts[0]['metric'], '16.7%')
        self.assertEqual(alerts[0]['severity'], 'critical')

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


    def test_buyer_can_propose_meeting_and_seller_can_accept(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '希望当面验货'},
        )
        order = Order.objects.get(item=self.item)
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'confirmed'})

        start_at = timezone.now() + timedelta(hours=2)
        end_at = start_at + timedelta(hours=1)
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.post(reverse('propose_meeting', args=[order.id]), {
            'start_at': start_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'end_at': end_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'location': self.location.id,
        })
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        appointment = MeetingAppointment.objects.get(order=order)
        self.assertEqual(appointment.status, 'pending')
        self.assertEqual(appointment.proposed_by, self.other_user)
        self.assertTrue(Notification.objects.filter(
            recipient=self.user, order=order, title='收到新的交付时间安排',
        ).exists())

        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.post(reverse('respond_meeting', args=[order.id, 'accept']))
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        appointment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(appointment.status, 'confirmed')
        self.assertEqual(appointment.responded_by, self.user)
        self.assertEqual(order.status, 'meeting')
        self.assertTrue(OrderEvent.objects.filter(order=order, to_status='meeting', note='双方确认了交付时间安排').exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.other_user, order=order, title='交付时间安排已确认',
        ).exists())

    def test_meeting_proposal_can_be_declined_and_replaced(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(reverse('create_order', args=[self.item.id]), {})
        order = Order.objects.get(item=self.item)
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'confirmed'})
        start_at = timezone.now() + timedelta(days=1)
        end_at = start_at + timedelta(minutes=45)
        response = self.client.post(reverse('propose_meeting', args=[order.id]), {
            'start_at': start_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'end_at': end_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'location': self.location.id,
        })
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(reverse('respond_meeting', args=[order.id, 'decline']))
        appointment = MeetingAppointment.objects.get(order=order)
        self.assertEqual(appointment.status, 'declined')
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')

        replacement_start = timezone.now() + timedelta(days=2)
        replacement_end = replacement_start + timedelta(minutes=30)
        response = self.client.post(reverse('propose_meeting', args=[order.id]), {
            'start_at': replacement_start.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'end_at': replacement_end.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'location': self.location.id,
        })
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'pending')
        self.assertEqual(appointment.proposed_by, self.other_user)
        self.assertEqual(appointment.decline_reason, '')

    def test_cancelled_order_cannot_accept_pending_meeting(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(reverse('create_order', args=[self.item.id]), {})
        order = Order.objects.get(item=self.item)
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'confirmed'})
        start_at = timezone.now() + timedelta(days=1)
        end_at = start_at + timedelta(minutes=30)
        self.client.logout()
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(reverse('propose_meeting', args=[order.id]), {
            'start_at': start_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'end_at': end_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'location': self.location.id,
        })
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'cancelled'})
        response = self.client.post(reverse('respond_meeting', args=[order.id, 'accept']))
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        appointment = MeetingAppointment.objects.get(order=order)
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(appointment.status, 'pending')

    def test_meeting_proposal_rejects_past_or_oversized_window(self):
        self.client.login(username='bob', password='safe-password-123')
        self.client.post(reverse('create_order', args=[self.item.id]), {})
        order = Order.objects.get(item=self.item)
        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'confirmed'})
        start_at = timezone.now() + timedelta(minutes=5)
        end_at = start_at + timedelta(hours=13)
        response = self.client.post(reverse('propose_meeting', args=[order.id]), {
            'start_at': start_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'end_at': end_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%dT%H:%M'),
            'location': self.location.id,
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MeetingAppointment.objects.filter(order=order).exists())

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

    def test_search_insights_scores_category_and_location_supply_gaps(self):
        SearchQuery.objects.create(
            user=self.other_user, query='投影仪', category=self.category,
            location=self.location, result_count=0,
        )
        SearchQuery.objects.create(
            user=self.other_user, query='投影仪', category=self.category,
            location=self.location, result_count=0,
        )

        dashboard = build_search_insights(30)
        facet_gaps = dashboard['facet_supply_gaps']
        self.assertEqual(facet_gaps['total_facets'], 2)
        self.assertEqual(facet_gaps['gap_facets'], 2)
        self.assertEqual(facet_gaps['categories'][0]['category__name'], self.category.name)
        self.assertEqual(facet_gaps['categories'][0]['zero_result_rate'], 100.0)
        self.assertEqual(facet_gaps['categories'][0]['available_count'], 1)
        self.assertGreater(facet_gaps['categories'][0]['priority_score'], 0)
        self.assertEqual(facet_gaps['locations'][0]['location__name'], self.location.name)

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('search_insights'))
        self.assertContains(response, '分类与地点缺口')
        self.assertContains(response, self.category.name)
        self.assertContains(response, self.location.name)


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


class ActivityCenterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='activity-reader', password='safe-password-123')
        self.other_user = User.objects.create_user(username='activity-sender', password='safe-password-123')

    def test_activity_center_merges_notifications_and_received_messages(self):
        Notification.objects.create(
            recipient=self.user, kind='order_status', title='订单状态更新',
            message='你的订单已经确认。',
        )
        Notification.objects.create(
            recipient=self.user, kind='comment_received', title='新的商品留言',
            message='有人回复了你的商品。', is_read=True,
        )
        PrivateMessage.objects.create(
            sender=self.other_user, receiver=self.user, content='可以今晚当面交易吗？',
        )

        self.client.login(username='activity-reader', password='safe-password-123')
        response = self.client.get(reverse('activity_center'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['activity_total'], 3)
        self.assertEqual(response.context['activity_unread_total'], 2)
        self.assertEqual(response.context['activity_filtered_unread_count'], 2)
        self.assertEqual(
            {row['source'] for row in response.context['activity_rows']},
            {'notification', 'message'},
        )
        self.assertContains(response, '站内动态')
        self.assertContains(response, '可以今晚当面交易吗？')

        filtered = self.client.get(reverse('activity_center'), {'type': 'messages', 'q': '今晚'})
        self.assertEqual(filtered.context['activity_total'], 1)
        self.assertEqual(filtered.context['activity_rows'][0]['source'], 'message')

    def test_activity_center_marks_both_sources_read_and_single_message_read(self):
        notification = Notification.objects.create(
            recipient=self.user, kind='order_status', title='待处理通知', message='请处理。',
        )
        private_message = PrivateMessage.objects.create(
            sender=self.other_user, receiver=self.user, content='请回复我。',
        )
        self.client.login(username='activity-reader', password='safe-password-123')

        response = self.client.post(
            reverse('mark_message_read', args=[private_message.id]),
            {'next': reverse('activity_center')},
        )
        self.assertRedirects(response, reverse('activity_center'))
        private_message.refresh_from_db()
        self.assertTrue(private_message.is_read)
        self.assertFalse(notification.is_read)

        next_url = reverse('activity_center') + '?status=unread'
        response = self.client.post(
            reverse('mark_all_activity_read'), {'next': next_url},
        )
        self.assertRedirects(response, next_url)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_activity_center_requires_login(self):
        response = self.client.get(reverse('activity_center'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)



class DemandPostTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='demand-alice', password='safe-password-123')
        self.other_user = User.objects.create_user(username='demand-bob', password='safe-password-123')
        self.category = Category.objects.create(name='求购数码', description='电子设备')
        self.location = CampusLocation.objects.create(name='求购图书馆东门')
        Item.objects.create(
            title='便携键盘', description='适合宿舍使用', price='99.00',
            category=self.category, location=self.location, condition='9成新', seller=self.user,
        )

    def test_user_can_publish_filter_and_close_demand_post(self):
        self.client.login(username='demand-bob', password='safe-password-123')
        response = self.client.post(
            reverse('new_demand'),
            {
                'title': '求一台便携键盘',
                'description': '希望适合宿舍使用，成色良好。',
                'category': self.category.id,
                'location': self.location.id,
                'min_price': '50',
                'max_price': '120',
                'expires_at': (timezone.now() + timedelta(days=10)).strftime('%Y-%m-%dT%H:%M'),
            },
        )
        demand = DemandPost.objects.get(requester=self.other_user)
        self.assertRedirects(response, reverse('demand_detail', args=[demand.id]))
        response = self.client.get(reverse('demand_list'), {'q': '便携键盘', 'location': self.location.id})
        self.assertContains(response, '求一台便携键盘')
        response = self.client.get(reverse('demand_detail', args=[demand.id]))
        self.assertContains(response, '可能符合需求的商品')
        self.client.post(reverse('close_demand', args=[demand.id]), {'status': 'fulfilled'})
        demand.refresh_from_db()
        self.assertEqual(demand.status, 'fulfilled')

    def test_demand_form_rejects_invalid_budget(self):
        self.client.login(username='demand-bob', password='safe-password-123')
        response = self.client.post(
            reverse('new_demand'),
            {
                'title': '预算校验',
                'description': '测试预算范围。',
                'category': self.category.id,
                'min_price': '200',
                'max_price': '100',
                'expires_at': (timezone.now() + timedelta(days=10)).strftime('%Y-%m-%dT%H:%M'),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '最高预算不能低于最低预算')
        self.assertFalse(DemandPost.objects.filter(title='预算校验').exists())


class DemandMatchingNotificationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='match-seller', password='safe-password-123')
        self.requester = User.objects.create_user(username='match-requester', password='safe-password-123')
        self.other = User.objects.create_user(username='match-other', password='safe-password-123')
        self.category = Category.objects.create(name='匹配教材')
        self.location = CampusLocation.objects.create(name='匹配图书馆')
        self.demand = DemandPost.objects.create(
            requester=self.requester, title='高等数学教材', description='需要一本教材',
            category=self.category, location=self.location, min_price='20', max_price='80',
        )

    def test_new_item_notifies_matching_demand_and_is_idempotent(self):
        item = Item.objects.create(
            seller=self.seller, title='高等数学教材九成新', description='课本', price='50',
            category=self.category, location=self.location, condition='9成新',
        )

        self.assertEqual(notify_demand_matches(item), 1)
        notice = Notification.objects.get(recipient=self.requester, kind='demand_match')
        self.assertEqual(notice.item, item)
        self.assertEqual(notice.demand, self.demand)
        self.assertEqual(notice.target_url, reverse('demand_detail', args=[self.demand.id]))
        self.assertIn('价格符合预算', notice.message)

        self.assertEqual(notify_demand_matches(item), 0)
        self.assertEqual(Notification.objects.filter(
            recipient=self.requester, kind='demand_match',
        ).count(), 1)

    def test_publishing_item_triggers_matching_notification(self):
        self.client.login(username='match-seller', password='safe-password-123')
        response = self.client.post(reverse('new_item'), {
            'title': '高等数学教材九成新',
            'description': '适合备考使用',
            'price': '50',
            'category': self.category.id,
            'location': self.location.id,
            'condition': '9成新',
            'images-TOTAL_FORMS': '3',
            'images-INITIAL_FORMS': '0',
            'images-MIN_NUM_FORMS': '0',
            'images-MAX_NUM_FORMS': '5',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Notification.objects.filter(
            recipient=self.requester, kind='demand_match', item__title='高等数学教材九成新',
        ).exists())

    def test_mismatch_and_disabled_preference_do_not_notify(self):
        wrong_category = Category.objects.create(name='匹配家具')
        item = Item.objects.create(
            seller=self.seller, title='高等数学教材', description='课本', price='50',
            category=wrong_category, location=self.location, condition='9成新',
        )
        self.assertEqual(notify_demand_matches(item), 0)

        preference = NotificationPreference.objects.create(
            user=self.requester, demand_match=False,
        )
        item.category = self.category
        item.save(update_fields=['category', 'updated_at'])
        self.assertEqual(notify_demand_matches(item), 0)
        self.assertFalse(Notification.objects.filter(
            recipient=self.requester, kind='demand_match',
        ).exists())
        preference.refresh_from_db()
        self.assertFalse(preference.demand_match)


class SellerReputationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='reputation-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='reputation-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='信誉测试分类')
        self.location = CampusLocation.objects.create(name='信誉测试地点')

    def _item(self, title):
        return Item.objects.create(
            seller=self.seller, title=title, description='测试商品', price='20',
            category=self.category, location=self.location, condition='9成新',
        )

    def test_reputation_summary_explains_completion_and_public_ratings(self):
        completed_item = self._item('已完成商品')
        cancelled_item = self._item('已取消商品')
        completed_order = Order.objects.create(
            item=completed_item, buyer=self.buyer, seller=self.seller,
            agreed_price='20', status='completed',
        )
        Order.objects.create(
            item=cancelled_item, buyer=self.buyer, seller=self.seller,
            agreed_price='20', status='cancelled',
        )
        Rating.objects.create(
            order=completed_order, rater=self.buyer, ratee=self.seller, score=5, comment='顺利',
        )

        reputation = build_seller_reputation(self.seller)
        self.assertEqual(reputation['completed_orders'], 1)
        self.assertEqual(reputation['closed_orders'], 2)
        self.assertEqual(reputation['completion_rate'], 50.0)
        self.assertEqual(reputation['rating_average'], 5.0)
        self.assertIn('有完成交易记录', reputation['badges'])

        response = self.client.get(reverse('item_detail', args=[completed_item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '交易概览')
        self.assertContains(response, '完成交易')

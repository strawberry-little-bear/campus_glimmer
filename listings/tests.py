from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import BrowsingHistory, CampusLocation, Category, DeliveryConfirmation, Favorite, Item, Notification, Order, OrderDispute, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SearchQuery
from .analytics import build_search_insights
from .recommendations import get_recommendations


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
        self.assertContains(response, '2 条未读')

        response = self.client.post(
            reverse('mark_notification_read', args=[one.id]),
            {'next': reverse('notification_list')},
        )
        self.assertRedirects(response, reverse('notification_list'))
        one.refresh_from_db()
        self.assertTrue(one.is_read)
        self.client.post(reverse('mark_all_notifications_read'))
        self.assertFalse(Notification.objects.filter(recipient=self.user, is_read=False).exists())


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
        Order.objects.create(
            item=second_item, buyer=self.other_user, seller=self.user,
            meeting_location=self.location, agreed_price='39.00', status='completed',
        )

        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        metrics = response.context['metrics']
        self.assertEqual(metrics['new_items'], 2)
        self.assertEqual(metrics['searches'], 3)
        self.assertEqual(metrics['zero_result_searches'], 1)
        self.assertEqual(metrics['orders'], 1)
        self.assertEqual(metrics['completed_orders'], 1)
        self.assertEqual(response.context['top_searches'][0]['query'], '键盘')
        self.assertEqual(response.context['category_stats'][0].new_count, 2)
        self.assertEqual(response.context['location_stats'][0].order_count, 1)
        self.assertContains(response, '图书馆东门')

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

    def test_search_insights_is_staff_only(self):
        self.client.login(username='bob', password='safe-password-123')
        response = self.client.get(reverse('search_insights'))
        self.assertEqual(response.status_code, 403)

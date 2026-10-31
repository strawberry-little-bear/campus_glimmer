# -*- coding: utf-8 -*-
"""订单状态机与「重新有货」提醒的直接口径测试。

订单状态只在两个地方被改写：``transition_order``（买卖双方在订单详情页点的按钮）
和 ``confirm_delivery``（双方交付确认之后落到 borrowed / completed）。后者由
``tests.py`` 与 ``test_order_stage_flow.py`` 从页面层覆盖；前者此前一条直接测试都
没有——它只被 ``update_order_status`` 视图调用，于是状态机的每一条规则都只能穿过
HTTP 间接验证：发一个 POST、看一条 flash 消息。那样能测出「按钮点了页面没炸」，
测不出「谁在什么状态下能走到哪一步」，因为视图把 ``OrderTransitionError`` 的消息
原样显示给用户，非法流转与合法流转在页面上都是「回到订单详情页」。

这份测试直接调用 ``transition_order``，把三件事钉住：

* ``ORDER_TRANSITIONS`` 必须覆盖 ``Order.STATUS_CHOICES`` 的每一个状态。新增一个
  状态却忘了登记，``.get(status, set())`` 会静默把它当成终态——不报错，只是永远
  走不下去，而这种沉默在页面上表现为「按钮不见了」，查起来要回头翻这张表。
* 谁能推动订单：卖家沿正链推进，买家只能取消。这两条不对称规则是业务约定。
* 取消的副作用：商品回到在售，一次性有货关注被唤醒并消耗掉。唤醒只发生在商品
  原本不在售的时候，否则同一批关注会被反复通知。

``notify_item_available`` 一并在这里直接测。它是一次性关注唯一的消费者，而
「通知发出去之后关注才删」这条顺序意味着：偏好关着的用户会一直留着那条关注。
这不是缺陷——用户从没收到过提醒，关注留着，等他重新打开开关还能再响一次；
但它是隐含约定，值得写下来而不是留给下一个人猜。
"""

from django.contrib.auth.models import User
from django.test import TestCase

from .availability import notify_item_available
from .models import (
    Category, CampusLocation, Item, ItemAvailabilityWatch, Notification,
    NotificationPreference, Order, OrderEvent,
)
from .order_workflow import ORDER_TRANSITIONS, OrderTransitionError, transition_order


class OrderTransitionTableTests(TestCase):
    """状态表自己的形状：新增状态不能悄悄变成永远走不通的终态。"""

    def test_every_order_status_has_a_row_in_the_table(self):
        declared = {value for value, _label in Order.STATUS_CHOICES}
        self.assertEqual(
            sorted(declared - set(ORDER_TRANSITIONS)), [],
            '这些状态没有登记合法去向，会被 .get() 静默当成终态',
        )

    def test_the_table_declares_nothing_the_model_does_not_know(self):
        declared = {value for value, _label in Order.STATUS_CHOICES}
        self.assertEqual(
            sorted(set(ORDER_TRANSITIONS) - declared), [],
            '状态表里出现了模型没有的状态',
        )

    def test_no_status_can_transition_to_itself(self):
        loops = sorted(status for status, targets in ORDER_TRANSITIONS.items() if status in targets)
        self.assertEqual(loops, [], '自环会让同一笔订单被无限次推进')


class OrderTransitionTests(TestCase):
    """一次状态流转该写什么、不该写什么。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='wf-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='wf-buyer', password='safe-password-123',
        )
        self.stranger = User.objects.create_user(
            username='wf-stranger', password='safe-password-123',
        )
        self.category = Category.objects.create(name='状态机测试分类')
        self.location = CampusLocation.objects.create(name='状态机测试地点')

    def make_item(self, *, title='状态机测试商品', status='reserved', trade_mode='sale'):
        return Item.objects.create(
            title=title,
            description='用于验证订单状态流转。',
            category=self.category,
            location=self.location,
            seller=self.seller,
            price=10,
            trade_mode=trade_mode,
            status=status,
        )

    def make_order(self, *, status='pending', item=None):
        return Order.objects.create(
            item=item or self.make_item(),
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.location,
            agreed_price=10,
            status=status,
        )

    # --- 谁能在什么状态下走到哪一步 -------------------------------------

    def test_the_seller_walks_the_forward_chain(self):
        order = self.make_order(status='pending')
        order = transition_order(order_id=order.id, actor=self.seller, target_status='confirmed')
        self.assertEqual(order.status, 'confirmed')
        order = transition_order(order_id=order.id, actor=self.seller, target_status='meeting')
        self.assertEqual(order.status, 'meeting')

    def test_the_buyer_can_cancel_from_every_open_status(self):
        for status in ('pending', 'confirmed', 'meeting'):
            with self.subTest(status=status):
                order = self.make_order(status=status)
                order = transition_order(
                    order_id=order.id, actor=self.buyer, target_status='cancelled',
                )
                self.assertEqual(order.status, 'cancelled')

    def test_the_seller_can_cancel_from_every_open_status(self):
        for status in ('pending', 'confirmed', 'meeting'):
            with self.subTest(status=status):
                order = self.make_order(status=status)
                order = transition_order(
                    order_id=order.id, actor=self.seller, target_status='cancelled',
                )
                self.assertEqual(order.status, 'cancelled')

    def test_a_stranger_cannot_move_the_order(self):
        order = self.make_order(status='pending')
        with self.assertRaises(OrderTransitionError) as caught:
            transition_order(order_id=order.id, actor=self.stranger, target_status='confirmed')
        self.assertIn('权限', str(caught.exception))

    def test_the_buyer_cannot_push_the_order_forward(self):
        # 买家只有取消权。推进是卖家的事：买家点「确认」会让一笔没人应允的订单
        # 直接进入待交付，那是把卖家的确认义务跳过去了。
        order = self.make_order(status='pending')
        with self.assertRaises(OrderTransitionError):
            transition_order(order_id=order.id, actor=self.buyer, target_status='confirmed')

    def test_the_seller_cannot_walk_the_order_backwards(self):
        order = self.make_order(status='meeting')
        with self.assertRaises(OrderTransitionError):
            transition_order(order_id=order.id, actor=self.seller, target_status='confirmed')

    def test_every_terminal_status_rejects_every_target(self):
        targets = ('pending', 'confirmed', 'meeting', 'completed', 'cancelled', 'borrowed', 'returned')
        for status in ('completed', 'borrowed', 'returned', 'cancelled'):
            for target in targets:
                with self.subTest(status=status, target=target):
                    order = self.make_order(status=status)
                    with self.assertRaises(OrderTransitionError):
                        transition_order(
                            order_id=order.id, actor=self.seller, target_status=target,
                        )

    def test_an_unknown_target_status_is_rejected(self):
        order = self.make_order(status='pending')
        with self.assertRaises(OrderTransitionError):
            transition_order(order_id=order.id, actor=self.seller, target_status='Teleported')

    # --- 被拒的流转什么都不写 -------------------------------------------

    def test_a_rejected_transition_writes_nothing(self):
        order = self.make_order(status='meeting')
        before = order.updated_at
        with self.assertRaises(OrderTransitionError):
            transition_order(order_id=order.id, actor=self.seller, target_status='confirmed')
        order.refresh_from_db()
        self.assertEqual(order.status, 'meeting')
        self.assertEqual(order.updated_at, before)
        self.assertEqual(OrderEvent.objects.filter(order=order).count(), 0)
        self.assertEqual(Notification.objects.filter(order=order).count(), 0)

    # --- 成功的流转写什么 -------------------------------------------------

    def test_each_transition_leaves_exactly_one_event(self):
        order = self.make_order(status='pending')
        transition_order(order_id=order.id, actor=self.seller, target_status='confirmed')
        transition_order(order_id=order.id, actor=self.seller, target_status='meeting')
        events = list(OrderEvent.objects.filter(order=order).order_by('created_at', 'id'))
        self.assertEqual(
            [(event.from_status, event.to_status) for event in events],
            [('pending', 'confirmed'), ('confirmed', 'meeting')],
        )
        self.assertEqual([event.actor for event in events], [self.seller, self.seller])
        self.assertTrue(all(event.note for event in events), '每条事件都要带一句人话说明')

    def test_only_the_other_party_is_notified(self):
        order = self.make_order(status='pending')
        transition_order(order_id=order.id, actor=self.seller, target_status='confirmed')
        notes = list(Notification.objects.filter(order=order))
        self.assertEqual([note.recipient for note in notes], [self.buyer])
        self.assertEqual(notes[0].kind, 'order_status')

    def test_the_actor_is_never_notified_about_their_own_action(self):
        order = self.make_order(status='pending')
        transition_order(order_id=order.id, actor=self.buyer, target_status='cancelled')
        notes = list(Notification.objects.filter(order=order))
        self.assertEqual([note.recipient for note in notes], [self.seller])
        self.assertNotIn(self.buyer, [note.recipient for note in notes])

    def test_a_closed_order_status_preference_suppresses_the_notification(self):
        NotificationPreference.objects.create(user=self.buyer, order_status=False)
        order = self.make_order(status='pending')
        transition_order(order_id=order.id, actor=self.seller, target_status='confirmed')
        self.assertEqual(Notification.objects.filter(order=order).count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed', '通知被关掉不该拦住状态本身')

    # --- 取消的副作用 -----------------------------------------------------

    def test_cancel_puts_the_item_back_on_sale(self):
        item = self.make_item(status='reserved')
        order = self.make_order(status='pending', item=item)
        transition_order(order_id=order.id, actor=self.buyer, target_status='cancelled')
        item.refresh_from_db()
        self.assertEqual(item.status, 'available')

    def test_advancing_the_order_never_touches_the_item(self):
        item = self.make_item(status='reserved')
        order = self.make_order(status='pending', item=item)
        transition_order(order_id=order.id, actor=self.seller, target_status='confirmed')
        item.refresh_from_db()
        self.assertEqual(item.status, 'reserved')

    def test_cancel_wakes_the_one_shot_availability_watchers(self):
        item = self.make_item(status='reserved')
        order = self.make_order(status='pending', item=item)
        watcher = User.objects.create_user(
            username='wf-watcher', password='safe-password-123',
        )
        ItemAvailabilityWatch.objects.create(user=watcher, item=item)
        transition_order(order_id=order.id, actor=self.buyer, target_status='cancelled')
        self.assertEqual(
            Notification.objects.filter(recipient=watcher, kind='item_available').count(), 1,
        )
        self.assertFalse(
            ItemAvailabilityWatch.objects.filter(user=watcher, item=item).exists(),
            '一次性关注必须在送达之后消失，否则下一次有货会重复通知',
        )

    def test_cancel_on_an_item_that_never_left_the_shelf_wakes_nobody(self):
        # 商品本来就在售：取消一笔挂在在售商品上的订单，不构成「重新有货」。
        # 不设这道闸门，同一批关注会被后续每一笔取消反复唤醒。
        item = self.make_item(status='available')
        order = self.make_order(status='pending', item=item)
        watcher = User.objects.create_user(
            username='wf-watcher-idle', password='safe-password-123',
        )
        ItemAvailabilityWatch.objects.create(user=watcher, item=item)
        transition_order(order_id=order.id, actor=self.buyer, target_status='cancelled')
        self.assertEqual(Notification.objects.filter(kind='item_available').count(), 0)
        self.assertTrue(
            ItemAvailabilityWatch.objects.filter(user=watcher, item=item).exists(),
        )

    def test_a_closed_item_available_preference_keeps_the_watch(self):
        item = self.make_item(status='reserved')
        order = self.make_order(status='pending', item=item)
        watcher = User.objects.create_user(
            username='wf-watcher-muted', password='safe-password-123',
        )
        NotificationPreference.objects.create(user=watcher, item_available=False)
        ItemAvailabilityWatch.objects.create(user=watcher, item=item)
        transition_order(order_id=order.id, actor=self.buyer, target_status='cancelled')
        self.assertEqual(Notification.objects.filter(recipient=watcher).count(), 0)
        self.assertTrue(
            ItemAvailabilityWatch.objects.filter(user=watcher, item=item).exists(),
            '没送出去的提醒不该把关注消耗掉',
        )


class NotifyItemAvailableTests(TestCase):
    """一次性有货关注唯一的消费者。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='na-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='有货提醒测试分类')
        self.location = CampusLocation.objects.create(name='有货提醒测试地点')

    def make_item(self, *, title='有货提醒测试商品', status='available'):
        return Item.objects.create(
            title=title,
            description='用于验证重新有货提醒。',
            category=self.category,
            location=self.location,
            seller=self.seller,
            price=10,
            trade_mode='sale',
            status=status,
        )

    def test_an_item_that_is_not_on_sale_notifies_nobody(self):
        for status in ('reserved', 'sold', 'expired'):
            with self.subTest(status=status):
                item = self.make_item(title=f'不在售商品 {status}', status=status)
                watcher = User.objects.create_user(
                    username='na-watcher-%s' % status, password='safe-password-123',
                )
                ItemAvailabilityWatch.objects.create(user=watcher, item=item)
                self.assertEqual(notify_item_available(item), 0)
                self.assertTrue(
                    ItemAvailabilityWatch.objects.filter(user=watcher, item=item).exists(),
                    '没货的时候不该消耗关注',
                )

    def test_watchers_are_notified_once_and_consumed(self):
        item = self.make_item()
        first = User.objects.create_user(username='na-first', password='safe-password-123')
        second = User.objects.create_user(username='na-second', password='safe-password-123')
        ItemAvailabilityWatch.objects.create(user=first, item=item)
        ItemAvailabilityWatch.objects.create(user=second, item=item)
        self.assertEqual(notify_item_available(item), 2)
        self.assertFalse(ItemAvailabilityWatch.objects.filter(item=item).exists())
        notes = list(
            Notification.objects.filter(kind='item_available').order_by('recipient__username')
        )
        self.assertEqual([note.recipient for note in notes], [first, second])

    def test_only_this_items_watchers_are_touched(self):
        mine = self.make_item(title='我的在售商品')
        other = self.make_item(title='别人的在售商品')
        mine_watcher = User.objects.create_user(username='na-mine', password='safe-password-123')
        other_watcher = User.objects.create_user(username='na-other', password='safe-password-123')
        ItemAvailabilityWatch.objects.create(user=mine_watcher, item=mine)
        ItemAvailabilityWatch.objects.create(user=other_watcher, item=other)
        self.assertEqual(notify_item_available(mine), 1)
        self.assertTrue(
            ItemAvailabilityWatch.objects.filter(user=other_watcher, item=other).exists(),
        )
        self.assertEqual(
            Notification.objects.filter(recipient=other_watcher, kind='item_available').count(), 0,
        )

    def test_a_second_call_notifies_nobody(self):
        item = self.make_item()
        watcher = User.objects.create_user(username='na-once', password='safe-password-123')
        ItemAvailabilityWatch.objects.create(user=watcher, item=item)
        self.assertEqual(notify_item_available(item), 1)
        self.assertEqual(notify_item_available(item), 0)

    def test_the_actor_never_notifies_themselves(self):
        # 卖家自己把商品改回在售时，他自己就是 actor。这条关注留给下一次真正
        # 由别人促成的上架，比当场删掉更符合「等它有货」的本意。
        item = self.make_item()
        watcher = User.objects.create_user(username='na-self', password='safe-password-123')
        ItemAvailabilityWatch.objects.create(user=watcher, item=item)
        self.assertEqual(notify_item_available(item, actor=watcher), 0)
        self.assertTrue(ItemAvailabilityWatch.objects.filter(user=watcher, item=item).exists())

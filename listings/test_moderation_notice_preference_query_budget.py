# -*- coding: utf-8 -*-
"""给「内容审核结果通知」的查询预算钉住数字，并核对每个口径。

上一轮把这个模块从 16 次查询降到 10 次，靠的是三件事，每一件都有对应的测试：

1. **免打扰不再逐用户查。** `_fetch_quiet_authors` 过去对每个作者调用一次
   `quiet_hours_active`，作者数就是查询数。现在 `quiet_hours_for_users` 一次取回全部
   偏好行，时段判断复用同一个 `quiet_window_active`。这里用逐用户的
   `quiet_hours_active` 当基准，核对批量版给出同一个集合。
2. **发言行为不再按组各查一遍。** `_build_activity_contrast` 过去对「关组」「开组」
   各调一次 `_fetch_author_activities`，同样三个表跑两遍；而 `_build_records` 早就为
   同一批人、同一个窗口取好了 `author_activities`，那个字段此前取了却没人读。
3. **偏好四格不再搬全表。** 过去把全表偏好行和全站用户主键都读进内存再数；
   现在只向数据库要两个计数，前两格从记录里现算。

测试全部用 Python 重算作基准，不依赖被改动的 ORM 聚合本身——否则改错了聚合也会绿。
"""

from datetime import timedelta
from datetime import time as dt_time

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from chat_messages.models import Comment, ModerationEvent, PrivateMessage

from .moderation_notice_preference import (
    MIN_PREFERENCE_USERS, build_moderation_notice_preference,
)
from .models import Category, Item, Notification, NotificationPreference
from .notifications import quiet_hours_active

# 一次面板构建的查询预算。构成是固定的：审核记录 1、通知 1、偏好 1、
# 批量免打扰 1、三类发言行为 3、偏好四格的两个计数 2、对照已读率 1。
# 这些查询一次都不随作者数或用户数增加——这正是本次改动要守住的性质。
PANEL_QUERY_BUDGET = 10

# 被复核的作者数。取到两组都过 `MIN_PREFERENCE_USERS`，好让「按组拆发言行为」
# 这条路径真的被走到，而不是被空组短路掉。
SWITCH_OFF_AUTHORS = 12
SWITCH_ON_AUTHORS = 12
NO_PREFERENCE_AUTHORS = 6
UNREVIEWED_USERS = 10

# 参照点统一走 testing_clock：本地时间落到 23:59:00-24:00:00 时，各模块自己写的
# 23:59:00 会早于夹具的 auto_now_add 时间戳，把周期内记录整批排除。
from .testing_clock import local_tonight


def stamp(instance, **fields):
    """把 `auto_now_add` 的创建时间写回夹具指定的值。"""
    if fields:
        type(instance).objects.filter(pk=instance.pk).update(**fields)
        instance.refresh_from_db()
    return instance


class QueryBudgetTests(TestCase):
    """查询数不随作者数、用户数增长。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='查询预算分类')
        self.users = [
            User.objects.create_user(username='qb%02d' % i, password='x')
            for i in range(
                SWITCH_OFF_AUTHORS + SWITCH_ON_AUTHORS + NO_PREFERENCE_AUTHORS
                + UNREVIEWED_USERS,
            )
        ]
        self.off_ids = list(range(0, SWITCH_OFF_AUTHORS))
        self.on_ids = list(range(SWITCH_OFF_AUTHORS, SWITCH_OFF_AUTHORS + SWITCH_ON_AUTHORS))
        self.nopref_ids = list(range(
            SWITCH_OFF_AUTHORS + SWITCH_ON_AUTHORS,
            SWITCH_OFF_AUTHORS + SWITCH_ON_AUTHORS + NO_PREFERENCE_AUTHORS,
        ))
        self.unreviewed_ids = list(range(
            SWITCH_OFF_AUTHORS + SWITCH_ON_AUTHORS + NO_PREFERENCE_AUTHORS,
            len(self.users),
        ))
        for index in self.off_ids + self.unreviewed_ids[:4]:
            self.set_preference(self.users[index], moderation_update=False)
        for index in self.on_ids:
            self.set_preference(self.users[index], moderation_update=True)
        # 免打扰时段包住此刻，且窗口不跨零点：这几个人在「开着」的前提下
        # 会被归到免打扰一档，而不是开关一档。右端点取 23:59:59 而不是 23:59，
        # 是因为夹具的参照点就是今天 23:59:00——写成 23:59 会把此刻正好排在外面，
        # 于是这一档永远是空的，测试看着绿其实什么都没测。
        for index in self.on_ids[:3]:
            self.set_preference(
                self.users[index], quiet_hours_enabled=True,
                quiet_hours_start=dt_time(0, 0), quiet_hours_end=dt_time(23, 59, 59),
            )
        self.reviewer = self.users[-1]

    def set_preference(self, user, **kwargs):
        preference, _ = NotificationPreference.objects.get_or_create(user=user)
        for field, value in kwargs.items():
            setattr(preference, field, value)
        preference.save(update_fields=list(kwargs))
        return preference

    def make_event(self, author, *, status='confirmed', reviewer=None,
                   reviewed_at=None):
        event = ModerationEvent.objects.create(
            channel='comment', author=author,
            content='正文里可能有电话。', matched_terms='外部链接',
            risk_score=20, risk_level='low', status='pending',
        )
        return stamp(event, status=status, reviewed_by=reviewer or self.reviewer,
                     reviewed_at=reviewed_at or (self.now - timedelta(days=1)))

    def make_notice(self, recipient, *, is_read=False, created_at=None):
        notice = Notification.objects.create(
            recipient=recipient, kind='moderation_update',
            title='内容审核结果', message='你提交的内容已完成复核。',
        )
        fields = {}
        if is_read:
            fields['is_read'] = True
            fields['read_at'] = created_at or self.now
        if created_at is not None:
            fields['created_at'] = created_at
        return stamp(notice, **fields)

    def make_item(self, seller, index):
        return Item.objects.create(
            title='查询预算商品 %d' % index, description='查询预算测试用',
            price='10.00', category=self.category, condition='良好', seller=seller,
        )

    def seed_cohort(self):
        """把三组作者、若干边界记录和三类发言行为都摆进窗口里。"""
        for index in self.off_ids + self.on_ids + self.nopref_ids:
            self.make_event(self.users[index])
        # 同一个作者两条记录：偏好四格和发言行为都按人去重，不能按记录数。
        self.make_event(self.users[self.off_ids[0]])
        # 作者缺失、仍在待复核、复核时间在窗口外，三种都不该进分母。
        self.make_event(None)
        self.make_event(self.users[self.on_ids[0]], status='pending',
                        reviewer=None, reviewed_at=None)
        self.make_event(self.users[self.nopref_ids[0]],
                        reviewed_at=self.now - timedelta(days=45))
        # 通知：只有一部分作者收到了。开组里**前三个正落在免打扰时段**，不能给他们
        # 通知——一旦有通知行，`_classify_reach` 第一道判断就返回 `delivered`，
        # 免打扰那一档永远是空的，等于没测。
        for index in self.off_ids[:4] + self.on_ids[3:7]:
            self.make_notice(self.users[index], is_read=(index % 2 == 0))
        # 三类发言行为，两组都覆盖到。
        item = self.make_item(self.users[0], 0)
        for index in self.off_ids[:4] + self.on_ids[:4]:
            stamp(Comment.objects.create(
                item=item, author=self.users[index], content='留言内容'),
                created_at=self.now - timedelta(days=2))
        for index in self.off_ids[4:8] + self.on_ids[4:8]:
            stamp(PrivateMessage.objects.create(
                sender=self.users[index], receiver=self.reviewer, content='私信'),
                created_at=self.now - timedelta(days=2))
        for index in self.off_ids[8:] + self.on_ids[8:]:
            self.make_item(self.users[index], index)
        # 窗口外的发言不算。
        stamp(Comment.objects.create(item=item, author=self.users[self.on_ids[0]],
                                     content='窗口外留言'),
              created_at=self.now - timedelta(days=40))

    def test_the_panel_stays_inside_its_query_budget(self):
        self.seed_cohort()
        with CaptureQueriesContext(connection) as ctx:
            build_moderation_notice_preference(days=30, now=self.now)
        self.assertLessEqual(
            len(ctx.captured_queries), PANEL_QUERY_BUDGET,
            '面板查询数超出预算：\n' + '\n'.join(
                q['sql'][:160] for q in ctx.captured_queries),
        )

    def test_the_query_count_does_not_grow_with_the_cohort(self):
        """再塞一批作者进去，查询数必须原地不动。

        改之前这条会失败：免打扰是逐用户查的，作者数翻倍查询数就跟着翻倍。
        """
        self.seed_cohort()
        with CaptureQueriesContext(connection) as ctx:
            build_moderation_notice_preference(days=30, now=self.now)
        before = len(ctx.captured_queries)

        extra = [
            User.objects.create_user(username='qb-extra-%d' % i, password='x')
            for i in range(24)
        ]
        for user in extra[:8]:
            self.set_preference(user, moderation_update=False)
        for user in extra[8:]:
            self.set_preference(user, moderation_update=True)
        for user in extra:
            self.make_event(user)

        with CaptureQueriesContext(connection) as ctx:
            build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(before, len(ctx.captured_queries))

    def test_no_query_touches_the_preference_table_per_author(self):
        """免打扰那一节一次查询都不许多查。"""
        self.seed_cohort()
        with CaptureQueriesContext(connection) as ctx:
            build_moderation_notice_preference(days=30, now=self.now)
        preference_queries = [
            q for q in ctx.captured_queries
            if 'listings_notificationpreference' in q['sql']
        ]
        # 偏好表只该被碰三次：`_fetch_preferences`、批量免打扰、四格的总计数。
        self.assertEqual(len(preference_queries), 3)


class EquivalenceTests(QueryBudgetTests):
    """每个口径都和一份独立重算对得上。"""

    def reviewed_ids(self):
        start = self.module_period_start()
        return {
            author_id for author_id in ModerationEvent.objects.filter(
                status__in=('confirmed', 'dismissed'),
                reviewed_at__gte=start, reviewed_at__lte=self.now,
            ).values_list('author_id', flat=True) if author_id
        }

    def module_period_start(self):
        from .moderation_notice_preference import _period_start
        return _period_start(30, timezone.localtime(self.now).date())

    def test_the_preference_grid_matches_a_plain_recount(self):
        from . import moderation_notice_preference as module
        self.seed_cohort()
        records = module._build_records(30, self.now)
        grid = module._build_preference_grid(records)

        # 基准：把全表偏好行和全站用户都搬进内存，逐个判断一遍。
        # 这正是被改掉的算法，留着它当对照。
        reviewed = self.reviewed_ids()
        switch_off = set(NotificationPreference.objects.filter(
            moderation_update=False,
        ).values_list('user_id', flat=True))
        everyone = set(User.objects.values_list('id', flat=True))
        expected = {
            'reviewed_switch_on': 0, 'reviewed_switch_off': 0,
            'unreviewed_switch_on': 0, 'unreviewed_switch_off': 0,
        }
        for user_id in everyone:
            switch_on = user_id not in switch_off
            if user_id in reviewed:
                expected['reviewed_switch_on' if switch_on else 'reviewed_switch_off'] += 1
            else:
                expected['unreviewed_switch_on' if switch_on else 'unreviewed_switch_off'] += 1
        self.assertEqual(grid, expected)

    def test_the_activity_contrast_matches_a_plain_recount(self):
        from . import moderation_notice_preference as module
        self.seed_cohort()
        records = module._build_records(30, self.now)
        result = module._build_activity_contrast(records)

        reviewed = self.reviewed_ids()
        switch_off = {
            record.event['author_id'] for record in records
            if record.event['author_id'] and record.preference is not None
            and not record.preference['moderation_update']
        }
        switch_on = reviewed - switch_off
        start = self.module_period_start()

        # 基准：两组人各查一遍三张表——也就是被改掉的算法。
        def activities(author_ids):
            found = {user_id: set() for user_id in author_ids}
            for user_id in Comment.objects.filter(
                author_id__in=author_ids, created_at__gte=start,
                created_at__lte=self.now,
            ).values_list('author_id', flat=True):
                found.setdefault(user_id, set()).add('comment')
            for user_id in PrivateMessage.objects.filter(
                sender_id__in=author_ids, created_at__gte=start,
                created_at__lte=self.now,
            ).values_list('sender_id', flat=True):
                found.setdefault(user_id, set()).add('message')
            for user_id in Item.objects.filter(
                seller_id__in=author_ids, created_at__gte=start,
                created_at__lte=self.now,
            ).values_list('seller_id', flat=True):
                found.setdefault(user_id, set()).add('item')
            return found

        off_acts = activities(switch_off)
        on_acts = activities(switch_on)
        expected_rows = []
        for kind, label in module.ACTIVITY_KINDS:
            off_count = sum(1 for acts in off_acts.values() if kind in acts)
            on_count = sum(1 for acts in on_acts.values() if kind in acts)
            off_total, on_total = len(off_acts), len(on_acts)
            comparable = (
                off_total >= MIN_PREFERENCE_USERS and on_total >= MIN_PREFERENCE_USERS
            )
            if comparable:
                direction = (
                    'switch_off_higher' if off_count > on_count
                    else 'switch_on_higher' if on_count > off_count
                    else 'same'
                )
            else:
                direction = None
            expected_rows.append({
                'kind': kind, 'label': label,
                'off_count': off_count, 'off_total': off_total,
                'on_count': on_count, 'on_total': on_total,
                'off_rate': module._percent_or_none(off_count, off_total),
                'on_rate': module._percent_or_none(on_count, on_total),
                'direction': direction, 'is_comparable': comparable,
            })
        self.assertEqual(result['rows'], expected_rows)
        self.assertEqual(result['off_total'], len(switch_off))
        self.assertEqual(result['on_total'], len(switch_on))
        # 两组都过了门槛，这个用例才真的测到了「合并两次查询」那条路。
        self.assertTrue(result['rows'][0]['is_comparable'])

    def test_the_baseline_read_rate_matches_two_plain_counts(self):
        from . import moderation_notice_preference as module
        self.seed_cohort()
        baseline = module._build_baseline_read_rate(30, self.now)
        start = self.module_period_start()
        sent = Notification.objects.filter(
            created_at__gte=start, created_at__lte=self.now,
        ).count()
        read = Notification.objects.filter(
            created_at__gte=start, created_at__lte=self.now, is_read=True,
        ).count()
        self.assertEqual(baseline, {
            'sent_count': sent,
            'read_count': read,
            'read_rate': module._percent_or_none(read, sent),
        })

    def test_the_batched_quiet_hours_match_the_single_user_lookup(self):
        """批量免打扰和逐用户查必须给出同一个集合。"""
        from . import moderation_notice_preference as module
        self.seed_cohort()
        records = module._build_records(30, self.now)
        author_ids = {
            record.event['author_id'] for record in records
            if record.event['author_id']
        }
        batched = module._fetch_quiet_authors(author_ids, self.now)
        expected = {
            user.id for user in User.objects.filter(id__in=author_ids)
            if quiet_hours_active(user, now=self.now)
        }
        self.assertEqual(batched, expected)
        # 这个夹具里必须真的有人在免打扰时段里，否则这条断言是空转。
        self.assertTrue(batched)
        # 触达那一节必须真的把这些人归到「免打扰」一档，而不是落回「开关关着」。
        result = build_moderation_notice_preference(days=30, now=self.now)
        quiet_total = sum(row['quiet_hours'] for row in result['reach_rows'])
        self.assertTrue(quiet_total)

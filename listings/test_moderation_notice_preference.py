# -*- coding: utf-8 -*-
"""为「内容审核结果通知：开关、触达与沉默」钉住口径。

`moderation_update`（内容审核结果）是站内 27 种通知里唯一一条**平台自己说话**的通知。
它的读取路径此前只有两处：`forms.py:40` 的表单字段和 `notifications.py:66` 的
`getattr(preference, kind, True)`。一个开关被表单收集、被写入路径读取，从来没被统计过。

`notification_response`、`notification_aggregation_quality`、`notification_snooze_return`、
`notification_kind_coverage` 四个模块都在读 `Notification` 表，没有一个读过
`NotificationPreference` 的一行值；`moderation_rule_quality` 的终点是
`ModerationEvent.status`。**复核结论发出去之后，接收端发生了什么，五个模块一处都没接上。**

本模块把它们接上，只报告不改写。这些测试要钉住的是：

1. 「没收到通知」必须拆成四种成因，不合成一个未送达率——合成之后，这个数会随
   「复核人是不是内容作者」这种人极少的情况抖动，而那和用户有没有关开关毫无关系；
2. 免打扰只压制徽章式提醒，通知照样落库，所以免打扰时段的作者其实**收到了**通知；
3. 已读率按复核结论拆——确认违规和误判放行是两条情绪上完全不同的消息；
4. 偏好四格只报人数，不报任何两格之间的比值——后两格的人没被复核过内容，
   他们关不关这个开关和「收到过审核结果」没有因果关系；
5. 关掉之后还在不在社区里说话，只报三档方向，不给百分比差；
6. 不按任何用户维度排名，不报任何用户名；
7. 一个字符的自由文本都不读——`ModerationEvent.content` 和通知正文都不读；
8. 不改 `create_notification`、不改复核写入路径、不给任何人自动打开开关。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from chat_messages.models import Comment, ModerationEvent, PrivateMessage

from .moderation_notice_preference import (
    MIN_BUCKET_SAMPLE, MIN_PREFERENCE_USERS, PREFERENCE_FIELD,
    build_moderation_notice_preference,
)
from .models import Category, Item, Notification, NotificationPreference

# 参照点统一走 testing_clock：本地时间落到 23:59:00-24:00:00 时，各模块自己写的
# 23:59:00 会早于夹具的 auto_now_add 时间戳，把周期内记录整批排除。
from .testing_clock import local_tonight


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_item(seller, title, category):
    return Item.objects.create(
        title=title, description='用于内容审核结果通知测试',
        price='10.00', category=category, condition='良好', seller=seller,
    )


def make_event(author, *, reviewer=None, status='confirmed', reviewed_at=None,
               channel='comment', item=None):
    """建一条审核记录，再把自动时间列写回夹具指定的值。

    ``created_at`` 是 ``auto_now_add``，创建时传值会被忽略，所以建完之后再 update
    写回。否则所有记录都落在「现在」，统计窗口读不出来。
    """
    event = ModerationEvent.objects.create(
        channel=channel, author=author, item=item,
        content='这条内容被规则拦下来了，正文可能是电话或微信。',
        matched_terms='外部链接', risk_score=20, risk_level='low',
        status='pending',
    )
    fields = {}
    if status in ('confirmed', 'dismissed'):
        fields['status'] = status
        fields['reviewed_by'] = reviewer
        fields['reviewed_at'] = reviewed_at or timezone.now()
    if fields:
        ModerationEvent.objects.filter(pk=event.pk).update(**fields)
        event.refresh_from_db()
    return event


def make_notice(recipient, *, is_read=False, created_at=None):
    """建一条 `moderation_update` 通知，再把创建时间写回夹具指定的值。"""
    notice = Notification.objects.create(
        recipient=recipient, kind='moderation_update',
        title='内容审核结果', message='你提交的内容已完成复核。',
    )
    fields = {}
    if is_read:
        fields['is_read'] = True
        fields['read_at'] = created_at or timezone.now()
    if created_at is not None:
        fields['created_at'] = created_at
    if fields:
        Notification.objects.filter(pk=notice.pk).update(**fields)
        notice.refresh_from_db()
    return notice


def set_preference(user, **kwargs):
    """把某个用户的偏好行写成夹具要的样子，返回这一行。"""
    preference, _ = NotificationPreference.objects.get_or_create(user=user)
    for field, value in kwargs.items():
        setattr(preference, field, value)
    preference.save(update_fields=list(kwargs))
    return preference


class EmptyAndWindowTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='审核通知空态分类')
        self.author = make_user('moderation-notice-empty-author')
        self.reviewer = make_user('moderation-notice-empty-reviewer')

    def test_an_empty_period_reports_no_data_rather_than_zero(self):
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertFalse(result['has_data'])
        self.assertEqual(result['outcome']['reviewed_count'], 0)
        # 空分母报 None 不报 0：0 会被读成「一条都没送到」，而事实是「没有复核完的记录」。
        self.assertIsNone(result['outcome']['delivered_rate'])
        self.assertIsNone(result['outcome']['switch_off_rate'])
        self.assertIsNone(result['baseline_read_rate']['read_rate'])
        self.assertFalse(result['activity']['has_data'])
        self.assertIn('周期内没有被复核完', result['summary'])

    def test_an_event_reviewed_before_the_window_is_excluded(self):
        make_event(self.author, reviewer=self.reviewer,
                   reviewed_at=self.now - timedelta(days=45))
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertFalse(result['has_data'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        result = build_moderation_notice_preference(days=999, now=self.now)
        self.assertEqual(result['period_days'], 30)

    def test_pending_events_are_right_censored(self):
        """还挂在 pending 的审核记录是右删失样本：复核还没发生，谈不上通知发没发。"""
        make_event(self.author, status='pending')
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertFalse(result['has_data'])
        self.assertEqual(result['outcome']['reviewed_count'], 0)

    def test_a_reviewed_event_without_an_author_is_counted_separately(self):
        """作者注销之后 author 是 SET_NULL，没有作者的复核结论谈不上送到谁眼前。"""
        make_event(None, reviewer=self.reviewer)
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertTrue(result['has_data'])
        self.assertEqual(result['outcome']['missing_author_count'], 1)
        self.assertEqual(result['outcome']['delivered_count'], 0)


class ReachTests(TestCase):
    """触达：这条结论送到作者眼前了没有，没送到的话是哪种没送到。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='审核通知触达分类')
        self.reviewer = make_user('moderation-notice-reach-reviewer')
        self.item = make_item(self.reviewer, '触达测试商品', self.category)

    def test_a_delivered_notice_is_reported_as_delivered(self):
        author = make_user('reach-delivered-author')
        make_event(author, reviewer=self.reviewer, item=self.item)
        make_notice(author)
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(result['outcome']['delivered_count'], 1)
        self.assertEqual(result['outcome']['delivered_rate'], 100.0)
        self.assertEqual(result['outcome']['switch_off_count'], 0)

    def test_a_switch_turned_off_is_reported_as_switch_off_not_undelivered(self):
        """关掉开关是用户主动选的，和免打扰、和作者本人不是一回事。"""
        author = make_user('reach-switch-off-author')
        set_preference(author, **{PREFERENCE_FIELD: False})
        make_event(author, reviewer=self.reviewer, item=self.item)
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(result['outcome']['switch_off_count'], 1)
        self.assertEqual(result['outcome']['delivered_count'], 0)
        # 分母是 1 条复核完的记录，这个数存在，就是 0——不是「分母不存在」的 None。
        # 空分母报 None 只在一条都没复核完的时候出现，见空态那条测试。
        self.assertEqual(result['outcome']['delivered_rate'], 0.0)
        self.assertEqual(result['outcome']['switch_off_rate'], 100.0)

    def test_a_self_authored_event_is_reported_as_its_own_band(self):
        """复核人是内容作者本人：`create_notification` 第一行的 `actor.pk == recipient.pk`
        短路。这一档人数极少，混进「开关关着」会让未送达率随复核人身份抖动。"""
        author = make_user('reach-self-authored')
        set_preference(author, **{PREFERENCE_FIELD: True})
        make_event(author, reviewer=author, item=self.item)
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(result['outcome']['switch_off_count'], 0)
        rows = {row['decision']: row for row in result['reach_rows']}
        self.assertEqual(rows['confirmed']['self_authored'], 1)
        self.assertEqual(rows['confirmed']['delivered'], 0)

    def test_quiet_hours_only_counts_when_the_switch_is_on_and_no_notice_exists(self):
        """免打扰只压制徽章式提醒，通知照样落库。

        所以一个正在免打扰时段里的作者，只要通知行建出来了，就**收到了**——
        这一档只在「开关开着、通知却没发出来」时才用来解释原因。
        """
        author = make_user('reach-quiet-author')
        set_preference(author, **{PREFERENCE_FIELD: True})
        make_event(author, reviewer=self.reviewer, item=self.item)
        # 通知照样落库：免打扰不该把它算成没收到。
        make_notice(author)
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(result['outcome']['delivered_count'], 1)
        rows = {row['decision']: row for row in result['reach_rows']}
        self.assertEqual(rows['confirmed']['quiet_hours'], 0)

    def test_reach_is_split_by_decision(self):
        """触达按复核结论拆：确认违规和误判放行是两条情绪上完全不同的消息。"""
        confirmed_author = make_user('reach-confirmed-author')
        dismissed_author = make_user('reach-dismissed-author')
        make_event(confirmed_author, reviewer=self.reviewer, status='confirmed',
                   item=self.item)
        make_event(dismissed_author, reviewer=self.reviewer, status='dismissed',
                   item=self.item)
        make_notice(confirmed_author)
        result = build_moderation_notice_preference(days=30, now=self.now)
        rows = {row['decision']: row for row in result['reach_rows']}
        self.assertEqual(rows['confirmed']['delivered'], 1)
        self.assertEqual(rows['dismissed']['delivered'], 0)
        # 中文标签，不出现英文原值：两个 dismissed 在站内是相反的意思，
        # 靠字段名分不开，模板和导出必须用标签。
        self.assertEqual(rows['confirmed']['label'], '确认违规')
        self.assertEqual(rows['dismissed']['label'], '误判放行')


class ReadRateTests(TestCase):
    """通知发出去之后，读没读。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='审核通知已读分类')
        self.reviewer = make_user('moderation-notice-read-reviewer')
        self.item = make_item(self.reviewer, '已读测试商品', self.category)

    def test_read_rate_is_split_by_decision(self):
        """确认违规和误判放行的已读率没有理由相同。"""
        confirmed_author = make_user('read-confirmed-author')
        dismissed_author = make_user('read-dismissed-author')
        make_event(confirmed_author, reviewer=self.reviewer, status='confirmed',
                   item=self.item)
        make_event(dismissed_author, reviewer=self.reviewer, status='dismissed',
                   item=self.item)
        make_notice(confirmed_author, is_read=True)
        make_notice(dismissed_author, is_read=False)
        result = build_moderation_notice_preference(days=30, now=self.now)
        rows = {row['decision']: row for row in result['read_rows']}
        self.assertEqual(rows['confirmed']['read_count'], 1)
        self.assertEqual(rows['confirmed']['unread_count'], 0)
        self.assertEqual(rows['confirmed']['read_rate'], 100.0)
        self.assertEqual(rows['dismissed']['read_count'], 0)
        self.assertEqual(rows['dismissed']['unread_count'], 1)
        self.assertEqual(rows['dismissed']['read_rate'], 0.0)

    def test_a_baseline_read_rate_covers_all_kinds(self):
        """同期全部通知的整体已读率作对照分母——少了它，单看一类的已读率
        读不出「这一类是不是特别没人看」。"""
        author = make_user('read-baseline-author')
        other = make_user('read-baseline-other')
        make_event(author, reviewer=self.reviewer, item=self.item)
        make_notice(author, is_read=True)
        # 另一种通知，已读。它该进对照分母，不该进这一类的分子。
        Notification.objects.create(
            recipient=other, kind='order_status', title='订单更新',
            message='你的订单有新的状态。', is_read=True,
        )
        result = build_moderation_notice_preference(days=30, now=self.now)
        baseline = result['baseline_read_rate']
        self.assertEqual(baseline['sent_count'], 2)
        self.assertEqual(baseline['read_count'], 2)
        self.assertEqual(baseline['read_rate'], 100.0)

    def test_an_empty_baseline_reports_none_rather_than_zero(self):
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(result['baseline_read_rate']['sent_count'], 0)
        self.assertIsNone(result['baseline_read_rate']['read_rate'])


class PreferenceGridTests(TestCase):
    """偏好四格：只报人数，不报任何两格之间的比值。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='审核通知偏好分类')
        self.reviewer = make_user('moderation-notice-grid-reviewer')
        self.item = make_item(self.reviewer, '偏好测试商品', self.category)

    def test_the_grid_counts_four_cells_without_any_ratio(self):
        reviewed_on = make_user('grid-reviewed-on')
        reviewed_off = make_user('grid-reviewed-off')
        make_user('grid-unreviewed-on')
        make_user('grid-unreviewed-off')
        set_preference(reviewed_on, **{PREFERENCE_FIELD: True})
        set_preference(reviewed_off, **{PREFERENCE_FIELD: False})
        make_event(reviewed_on, reviewer=self.reviewer, item=self.item)
        make_event(reviewed_off, reviewer=self.reviewer, item=self.item)

        result = build_moderation_notice_preference(days=30, now=self.now)
        grid = result['preference_grid']
        self.assertEqual(grid['reviewed_switch_on'], 1)
        self.assertEqual(grid['reviewed_switch_off'], 1)
        # 三个从没建过偏好行的人都算开着：两个路人加 setUp 里的复核人，全落第三格。
        self.assertEqual(grid['unreviewed_switch_on'], 3)
        self.assertEqual(grid['unreviewed_switch_off'], 0)
        self.assertEqual(sum(grid.values()), 5)
        # 四格之外不报任何比值：后两格的人没被复核过内容，他们关不关这个开关
        # 和「收到过审核结果」没有因果关系。
        self.assertEqual(
            sorted(grid), [
                'reviewed_switch_off', 'reviewed_switch_on',
                'unreviewed_switch_off', 'unreviewed_switch_on',
            ],
        )

    def test_a_user_without_a_preference_row_counts_as_switch_on(self):
        """没有偏好行的用户算「开着」。

        `NotificationPreference` 不是注册时建的，是第一次进设置页或第一次收到通知时
        由 `get_or_create` 现建的。一个从没进过设置页的用户库里没有这一行，而
        `create_notification` 对这种人照样发通知——`get_or_create` 建出默认全开的一行。
        只数库里的行会把这群人整群漏掉，而他们恰恰是全站的大多数。
        """
        reviewed = make_user('grid-no-row-reviewed')
        stranger = make_user('grid-no-row-stranger')
        # 两个人都不建偏好行。
        make_event(reviewed, reviewer=self.reviewer, item=self.item)

        result = build_moderation_notice_preference(days=30, now=self.now)
        grid = result['preference_grid']
        self.assertEqual(grid['reviewed_switch_on'], 1)
        self.assertEqual(grid['reviewed_switch_off'], 0)
        # 三个人都没有偏好行：被复核的那个进第一格，stranger 与 setUp 里的复核人进第三格。
        self.assertEqual(grid['unreviewed_switch_on'], 2)
        self.assertEqual(grid['unreviewed_switch_off'], 0)
        self.assertEqual(sum(grid.values()), 3)

    def test_the_grid_counts_every_user_not_only_reviewed_ones(self):
        """四格覆盖全站用户，不只周期内被复核过的那几个。

        只报被复核的人，「没内容被复核」两格永远是 0，读者会以为全站每个人都
        被复核过——那是把观测范围当成了总体。
        """
        author = make_user('grid-wide-author')
        stranger = make_user('grid-wide-stranger')
        set_preference(author, **{PREFERENCE_FIELD: True})
        set_preference(stranger, **{PREFERENCE_FIELD: False})
        make_event(author, reviewer=self.reviewer, item=self.item)

        result = build_moderation_notice_preference(days=30, now=self.now)
        grid = result['preference_grid']
        self.assertEqual(grid['reviewed_switch_on'], 1)
        self.assertEqual(grid['reviewed_switch_off'], 0)
        # 复核人没有偏好行，算开着，且他没被复核过内容，落第三格。
        # 显式关着的 stranger 落第四格——它证明「关着」这一格不是恒为 0。
        self.assertEqual(grid['unreviewed_switch_on'], 1)
        self.assertEqual(grid['unreviewed_switch_off'], 1)
        self.assertEqual(sum(grid.values()), 3)


class ActivityContrastTests(TestCase):
    """关掉之后还在不在社区里说话：只报形状，不作因果。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='审核通知发言分类')
        self.reviewer = make_user('moderation-notice-activity-reviewer')
        self.item = make_item(self.reviewer, '发言测试商品', self.category)

    def test_an_empty_period_reports_no_direction(self):
        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertFalse(result['activity']['has_data'])
        self.assertEqual(result['activity']['rows'], [])

    def test_activities_are_reported_as_presence_not_counts(self):
        """只报「有没有」，不报条数，不报内容。"""
        author = make_user('activity-presence-author')
        set_preference(author, **{PREFERENCE_FIELD: False})
        make_event(author, reviewer=self.reviewer, item=self.item)
        Comment.objects.create(item=self.item, author=author, content='我还在说话。')
        Comment.objects.create(item=self.item, author=author, content='第二条。')
        PrivateMessage.objects.create(
            sender=author, receiver=self.reviewer, content='私信内容',
        )

        result = build_moderation_notice_preference(days=30, now=self.now)
        rows = {row['kind']: row for row in result['activity']['rows']}
        self.assertEqual(rows['comment']['off_count'], 1)
        self.assertEqual(rows['message']['off_count'], 1)
        # 两个键都在，但没有任何一个键上出现 2——条数不该被报出来。
        self.assertNotIn('off_comment_count', rows['comment'])
        self.assertNotIn('off_message_count', rows['message'])

    def test_only_reviewed_users_enter_the_contrast(self):
        """没被复核过内容的人关不关这个开关，和审核结果通知没有因果关系，
        混进来只会把两群都稀释掉。"""
        reviewed = make_user('activity-reviewed')
        stranger = make_user('activity-stranger')
        set_preference(reviewed, **{PREFERENCE_FIELD: False})
        set_preference(stranger, **{PREFERENCE_FIELD: True})
        Comment.objects.create(item=self.item, author=stranger, content='路人发言。')
        make_event(reviewed, reviewer=self.reviewer, item=self.item)

        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(result['activity']['off_total'], 1)
        self.assertEqual(result['activity']['on_total'], 0)

    def test_a_reviewed_user_without_a_preference_row_lands_in_the_on_group(self):
        """没建过偏好行的被复核用户归进「开着的一组」。

        他们没进过设置页，而 `create_notification` 对这种人照样发通知。归进「关掉的
        一组」会把两组都弄错：关掉的一组会凭空多出从没做过这个选择的人。
        """
        reviewed = make_user('activity-no-row-reviewed')
        make_event(reviewed, reviewer=self.reviewer, item=self.item)
        Comment.objects.create(item=self.item, author=reviewed, content='没建过偏好行。')

        result = build_moderation_notice_preference(days=30, now=self.now)
        self.assertEqual(result['activity']['on_total'], 1)
        self.assertEqual(result['activity']['off_total'], 0)
        rows = {row['kind']: row for row in result['activity']['rows']}
        self.assertEqual(rows['comment']['on_count'], 1)

    def test_no_direction_is_given_below_the_sample_threshold(self):
        """样本不足就只报人数，不给方向：几条样本上的百分比不是方向。"""
        for index in range(3):
            author = make_user(f'activity-small-{index}')
            set_preference(author, **{PREFERENCE_FIELD: False})
            make_event(author, reviewer=self.reviewer, item=self.item)
            Comment.objects.create(item=self.item, author=author, content='还在说话。')
        # 开着的一组铺到门槛以上，关掉的一组故意留在门槛以下。
        for index in range(MIN_PREFERENCE_USERS):
            author = make_user(f'activity-large-{index}')
            set_preference(author, **{PREFERENCE_FIELD: True})
            make_event(author, reviewer=self.reviewer, item=self.item)

        result = build_moderation_notice_preference(days=30, now=self.now)
        rows = {row['kind']: row for row in result['activity']['rows']}
        self.assertFalse(rows['comment']['is_comparable'])
        self.assertIsNone(rows['comment']['direction'])

    def test_direction_is_one_of_three_bands_and_never_a_percentage_gap(self):
        """方向只允许三档，不给百分比差——一个 3 个百分点和一个 30 个百分点
        在看板上长一样，给的指引却完全相反。"""
        for index in range(MIN_PREFERENCE_USERS):
            author = make_user(f'activity-off-{index}')
            set_preference(author, **{PREFERENCE_FIELD: False})
            make_event(author, reviewer=self.reviewer, item=self.item)
            Comment.objects.create(item=self.item, author=author, content='关掉的人还在说话。')
        for index in range(MIN_PREFERENCE_USERS):
            author = make_user(f'activity-on-{index}')
            set_preference(author, **{PREFERENCE_FIELD: True})
            make_event(author, reviewer=self.reviewer, item=self.item)

        result = build_moderation_notice_preference(days=30, now=self.now)
        rows = {row['kind']: row for row in result['activity']['rows']}
        self.assertTrue(rows['comment']['is_comparable'])
        self.assertEqual(rows['comment']['direction'], 'switch_off_higher')
        # 三档之外没有第四个值。message 和 item 两档两组都没人发言，落在 'same'。
        self.assertEqual(rows['message']['direction'], 'same')
        self.assertEqual(rows['item']['direction'], 'same')
        self.assertTrue(
            {row['direction'] for row in result['activity']['rows']
             if row['direction']} <= {'switch_off_higher', 'switch_on_higher', 'same'},
        )
        self.assertNotIn('gap_points', rows['comment'])
        self.assertNotIn('rate_gap', rows['comment'])


class DisciplineTests(TestCase):
    """不排名、不报用户名、不读自由文本、不改写入路径。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='审核通知纪律分类')
        self.reviewer = make_user('moderation-notice-discipline-reviewer')
        self.item = make_item(self.reviewer, '纪律测试商品', self.category)

    def test_no_username_ever_appears_in_the_result(self):
        """关掉一个通知偏好是用户的私事，做成排行榜就是把设置页变成公开表态。"""
        for index in range(4):
            author = make_user(f'discipline-author-{index}')
            set_preference(author, **{PREFERENCE_FIELD: index % 2 == 0})
            make_event(author, reviewer=self.reviewer, item=self.item)
            make_notice(author, is_read=index % 2 == 0)
            Comment.objects.create(item=self.item, author=author, content='纪律测试发言。')

        result = build_moderation_notice_preference(days=30, now=self.now)
        blob = repr(result)
        for index in range(4):
            self.assertNotIn(f'discipline-author-{index}', blob)
        self.assertNotIn('username', blob)

    def test_no_free_text_is_read_from_the_event_or_the_notice(self):
        """`ModerationEvent.content` 是被拦截的原文，`Notification.title` / `message`
        是发给用户的话，三处都可能写着电话、微信、楼栋号。"""
        author = make_user('discipline-free-text')
        make_event(author, reviewer=self.reviewer, item=self.item)
        make_notice(author)
        result = build_moderation_notice_preference(days=30, now=self.now)
        blob = repr(result)
        self.assertNotIn('这条内容被规则拦下来了', blob)
        self.assertNotIn('你提交的内容已完成复核', blob)

    def test_the_module_never_writes_to_the_preference_or_the_event(self):
        """只报告。开关是用户自己划的那一格，模块不替任何人改。"""
        author = make_user('discipline-read-only')
        preference = set_preference(author, **{PREFERENCE_FIELD: False})
        event = make_event(author, reviewer=self.reviewer, item=self.item)
        before = (preference.moderation_update, event.status, event.reviewed_at)

        build_moderation_notice_preference(days=30, now=self.now)

        preference.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(
            (preference.moderation_update, event.status, event.reviewed_at), before,
        )

    def test_the_module_does_not_create_notifications(self):
        """不给任何人自动打开开关，也不补发任何一条通知。"""
        author = make_user('discipline-no-backfill')
        set_preference(author, **{PREFERENCE_FIELD: False})
        make_event(author, reviewer=self.reviewer, item=self.item)
        before = Notification.objects.count()

        build_moderation_notice_preference(days=30, now=self.now)

        self.assertEqual(Notification.objects.count(), before)


class ThresholdTests(TestCase):
    """门槛。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='审核通知门槛分类')

    def test_the_shared_bucket_threshold_is_twelve(self):
        """全站统一的「12 条」——低于这个数，一条样本就是一个百分点。"""
        self.assertEqual(MIN_BUCKET_SAMPLE, 12)
        self.assertEqual(MIN_PREFERENCE_USERS, 12)

    def test_small_sample_buckets_are_flagged(self):
        reviewer = make_user('threshold-reviewer')
        item = make_item(reviewer, '门槛测试商品', self.category)
        author = make_user('threshold-author')
        make_event(author, reviewer=reviewer, item=item)
        make_notice(author)
        result = build_moderation_notice_preference(days=30, now=self.now)
        rows = {row['decision']: row for row in result['reach_rows']}
        self.assertTrue(rows['confirmed']['is_small_sample'])

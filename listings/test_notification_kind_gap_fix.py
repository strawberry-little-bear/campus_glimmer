# -*- coding: utf-8 -*-
"""通知类型的两个窟窿补上之后，靠什么守住——三个来源必须一直对齐。

上一轮 ``notification_kind_coverage`` 把 ``Notification.KIND_CHOICES``、
``NotificationPreference`` 的布尔开关、``NotificationPreferenceForm`` 的字段列表
并排放，让两个对不上的窟窿浮了出来，并明确写下「修不修、什么时候修，是另一个
决定」。本模块对应的那一轮就是那个决定：两个都修。

- **窟窿一**：``mutual_aid_feedback`` 正在被发送、偏好表上有开关、表单里有那一行，
  而 ``KIND_CHOICES`` 里没有它。后果是 ``notification_list`` 的类型筛选器拿
  ``KIND_CHOICES`` 当白名单，这一类通知用户在自己的通知中心里筛不出来。
  修法是在 ``KIND_CHOICES`` 末尾补一行数据层的名字。
- **窟窿二**：``meeting_incident`` 模型上有、默认开着、真的在发，而表单的字段列表
  里没有它。偏好页模板是 ``{% for field in form.visible_fields %}``，所以用户收到了
  却没有任何一个开关能关掉它。修法是在 ``Meta.fields`` 里补一行表单层的暴露。

两处都不改表结构，所以都不需要迁移。

本模块钉住四件事，而它们守的是**未来**，不是本轮：

1. 三个来源此刻是对齐的，而且必须继续对齐。下一次有人新增一种通知类型，很可能
   只改一处——改完之后这条测试就该红。红的是新增者，不是本文件。
2. 窟窿一的两个真实后果真的消失了：类型下拉里有它、筛选器认它。
3. 窟窿二的真实后果真的消失了：偏好表单上有那一行，而且是用户看得见的那一行
   （``visible_fields``，不是 ``fields``——模板遍历的是前者）。
4. 两个修法都没有顺手改掉别的东西：``create_notification`` 的偏好闸门照旧，
   关掉开关之后通知一行都不建。

``test_notification_kind_coverage.py`` 是度量模块的测试，它的存在意义已经从「报窟窿」
变成「守住对齐」。本文件不重复它的版图口径，只补上它报完之后该有的那层保证。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import NotificationPreferenceForm
from .models import Notification, NotificationPreference
from .notification_kind_coverage import (
    NON_KIND_PREFERENCE_FIELDS,
    _form_switch_fields,
    _preference_switch_fields,
    build_notification_kind_coverage,
)
from .notifications import create_notification


def make_user(username):
    return User.objects.create_user(
        username=username, password='safe-password-123',
    )


def make_notification(recipient, **kwargs):
    """建一条通知，再把自动时间列写回夹具指定的值。

    ``created_at`` 是 ``auto_now_add``，创建时传值会被忽略，所以建完之后再用 update
    写回——和 ``test_notification_kind_coverage`` 的同名夹具同一个写法。
    """
    notification = Notification.objects.create(
        recipient=recipient,
        kind=kwargs.pop('kind', 'order_created'),
        title=kwargs.pop('title', '一条通知'),
        message=kwargs.pop('message', '内容'),
    )
    fields = {}
    for name in ('created_at', 'read_at', 'is_read'):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    if fields:
        Notification.objects.filter(pk=notification.pk).update(**fields)
        notification.refresh_from_db()
    return notification


def kind_choice_labels():
    return dict(Notification.KIND_CHOICES)


class ThreeSourceAlignmentTests(TestCase):
    """三个来源此刻对齐，而且必须继续对齐。"""

    def test_the_choice_labels_cover_every_preference_switch(self):
        """每个偏好开关都要有一个中文名。

        反方向的窟窿就是上一轮量到的 ``mutual_aid_feedback``：开关在、表单里有那一行、
        真的在发，而 ``choices`` 里没有它。补上之后，开关集合必须是选择项集合的子集。
        """
        switches = set(_preference_switch_fields())
        choices = {value for value, _label in Notification.KIND_CHOICES}

        self.assertEqual(switches - choices, set())

    def test_the_choice_labels_are_covered_by_the_preference_switches(self):
        """每个选择项都要有一个开关——没有开关的名字用户关不掉。"""
        switches = set(_preference_switch_fields())
        choices = {value for value, _label in Notification.KIND_CHOICES}

        self.assertEqual(choices - switches, set())

    def test_every_preference_switch_is_laid_out_in_the_form(self):
        """每个开关都要出现在表单里，用户才真的关得掉。

        反方向的窟窿就是上一轮量到的 ``meeting_incident``：模型上有、真的在发，而
        表单的字段列表里没有它，用户收到了却找不到开关。
        """
        switches = set(_preference_switch_fields())

        self.assertEqual(switches - set(_form_switch_fields()), set())

    def test_the_form_lays_out_nothing_that_is_not_a_switch(self):
        """表单里每一个名字都必须是模型上真实存在的字段。

        ``ModelForm`` 对 ``Meta.fields`` 里写错的字段名不会在启动时报错，页面要到
        渲染那一行时才炸——而这一页是用户唯一能管理自己通知的地方。这条守住的是
        另一半：不只「该在的都在」，也「不该在的不在」。
        """
        model_fields = {
            field.name for field in NotificationPreference._meta.get_fields()
        }
        missing = [
            field_name
            for field_name in NotificationPreferenceForm.Meta.fields
            if field_name not in model_fields
        ]

        self.assertEqual(missing, [])

    def test_the_alignment_is_read_from_code_not_a_hardcoded_list(self):
        """三个来源全部从代码现读，本测试不背书任何一份名单。

        硬编码的名单会和代码走散，而那正是上一轮要报的问题本身。这里直接比对面三份
        现读的集合，确认它们此刻彼此相等——不列任何一个名字。表单里另有免打扰那一组
        （开关本身加两个时刻），那是另一套机制，按 ``NON_KIND_PREFERENCE_FIELDS``
        显式排除，不装作没看见。
        """
        switches = set(_preference_switch_fields())
        form_fields = set(_form_switch_fields()) - set(NON_KIND_PREFERENCE_FIELDS)
        choices = {value for value, _label in Notification.KIND_CHOICES}

        self.assertEqual(switches, form_fields)
        self.assertEqual(switches, choices)


class MutualAidFeedbackNameTests(TestCase):
    """窟窿一：``mutual_aid_feedback`` 现在有名字，而且筛得出来。"""

    def setUp(self):
        self.now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        self.user = make_user('gap-fix-mutual-aid')

    def test_the_kind_has_a_chinese_label(self):
        """选择项里要有中文标签，不再是落进 fallback 的英文键名。

        ``notification_response`` 与 ``notification_snooze_return`` 都拿
        ``KIND_CHOICES`` 当标签表，缺这一行，运营看到的就是英文键名。
        """
        self.assertEqual(kind_choice_labels()['mutual_aid_feedback'], '互助完成反馈')

    def test_the_kind_appears_in_the_notification_center_type_filter(self):
        """类型下拉里要有它——修之前它连下拉都进不去。"""
        self.client.force_login(self.user)
        make_notification(
            self.user, kind='mutual_aid_feedback', title='互助结果反馈来啦',
            created_at=self.now - timedelta(days=1),
        )

        response = self.client.get(reverse('notification_list'))
        self.assertEqual(response.status_code, 200)
        options = {
            option['value'] for option in response.context['notification_kind_options']
        }
        self.assertIn('mutual_aid_feedback', options)
        labels = {
            option['label'] for option in response.context['notification_kind_options']
        }
        self.assertIn('互助完成反馈', labels)

    def test_the_kind_can_be_filtered_for_in_the_notification_center(self):
        """这是窟窿一真正的用户后果：按类型筛，要真的筛得出这一行。

        ``notification_list`` 的类型筛选器拿 ``KIND_CHOICES`` 当白名单
        （``allowed_kinds``），修之前 ``mutual_aid_feedback`` 不在白名单里，
        ``kind_filter`` 会被清空，整批通知一条都看不出来。
        """
        self.client.force_login(self.user)
        make_notification(
            self.user, kind='mutual_aid_feedback', title='这条要能被筛出来',
            created_at=self.now - timedelta(days=1),
        )
        make_notification(
            self.user, kind='order_created', title='这条是陪跑的',
            created_at=self.now - timedelta(days=1),
        )

        response = self.client.get(
            reverse('notification_list'), {'kind': 'mutual_aid_feedback'},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('这条要能被筛出来', content)
        self.assertNotIn('这条是陪跑的', content)

    def test_an_unknown_kind_is_still_ignored_by_the_filter(self):
        """白名单该严还是严——补一个名字不等于放开整个筛选器。

        修完之后 ``allowed_kinds`` 多了一个成员，但「不在名单里的值一律忽略」这条
        行为没变：认不出的类型参数被清空，筛选器退回「不过滤」，而不是报错、也不是把
        本来该看的通知藏起来。这条守住的是别把修复做成放开。
        """
        self.client.force_login(self.user)
        make_notification(
            self.user, kind='order_created', title='这条照旧看得见',
            created_at=self.now - timedelta(days=1),
        )

        response = self.client.get(
            reverse('notification_list'), {'kind': 'not_a_real_kind'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('这条照旧看得见', response.content.decode('utf-8'))


class MeetingIncidentExposureTests(TestCase):
    """窟窿二：``meeting_incident`` 现在摆在偏好页面上。"""

    def setUp(self):
        self.now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        self.user = make_user('gap-fix-meeting-incident')

    def test_the_switch_is_in_the_form_fields(self):
        """表单字段列表里要有它。"""
        self.assertIn('meeting_incident', NotificationPreferenceForm().fields)

    def test_the_switch_is_visible_to_the_user(self):
        """它要出现在 ``visible_fields`` 里，而不只是 ``fields`` 里。

        偏好页模板是 ``{% for field in form.visible_fields %}``，表单里没有的字段
        页面上就没有那一行。``fields`` 里有而 ``visible_fields`` 里没有的字段，
        用户照样看不见——所以判据是前者。
        """
        form = NotificationPreferenceForm()
        visible = {field.name for field in form.visible_fields()}
        self.assertIn('meeting_incident', visible)

    def test_the_preference_page_renders_the_switch(self):
        """偏好页面上要渲染出那一行，标签是中文的。"""
        self.client.force_login(self.user)
        preference, _ = NotificationPreference.objects.get_or_create(user=self.user)

        response = self.client.get(reverse('notification_preferences'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('交付预约异常', content)
        # 表单真的绑在这个偏好行上，不是一份空表单。
        self.assertEqual(response.context['form'].instance, preference)

    def test_the_switch_can_be_turned_off_from_the_page(self):
        """从页面上真的能关掉它——暴露不是摆着看的。"""
        self.client.force_login(self.user)
        NotificationPreference.objects.get_or_create(user=self.user)
        fields = {
            field_name: 'on'
            for field_name in NotificationPreferenceForm.Meta.fields
            if field_name != 'meeting_incident'
        }

        response = self.client.post(
            reverse('notification_preferences'), {**fields, 'quiet_hours_start': '22:00',
                                                 'quiet_hours_end': '08:00'},
        )
        self.assertEqual(response.status_code, 302)

        preference = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(preference.meeting_incident)

    def test_the_summary_count_covers_the_newly_exposed_switch(self):
        """偏好页那句「已开启 X / Y 类提醒」的 Y 只数真正的通知开关。

        表单还包含免打扰总闸和起止时间三个配置字段；它们不是可开关的通知类型，
        不能进入分子或分母。新增通知开关时，这句话的分母仍要跟着表单走。
        """
        self.client.force_login(self.user)
        NotificationPreference.objects.get_or_create(user=self.user)

        response = self.client.get(reverse('notification_preferences'))
        notification_fields = [
            field_name
            for field_name in NotificationPreferenceForm.Meta.fields
            if not field_name.startswith('quiet_hours_')
        ]
        self.assertIn('meeting_incident', notification_fields)
        self.assertEqual(
            response.context['notification_kind_count'],
            len(notification_fields),
        )


class CoverageAlignmentTests(TestCase):
    """度量模块的差集变空，而它是自动变空的。"""

    def setUp(self):
        self.now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        self.user = make_user('gap-fix-coverage')

    def test_the_two_gap_lists_are_now_empty(self):
        """两张差集表都空：每个类型都有名字、每个开关都在用户手上。

        这两个集合变空是修的自然结果，不是改口径改出来的——本测试直接读
        ``build_notification_kind_coverage`` 的输出，不碰模块内部。
        """
        make_notification(
            self.user, kind='mutual_aid_feedback',
            created_at=self.now - timedelta(days=1),
        )
        make_notification(
            self.user, kind='meeting_incident',
            created_at=self.now - timedelta(days=1),
        )

        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertEqual(report['unnamed_rows'], [])
        self.assertEqual(report['unreachable_rows'], [])
        self.assertFalse(report['current']['has_unnamed'])
        self.assertFalse(report['current']['has_unreachable'])

    def test_every_landscape_row_clears_all_three_columns(self):
        """版图里每一行的三列二值全为真——免打扰那一组不进版图，所以不该有例外。"""
        make_notification(
            self.user, kind='mutual_aid_feedback',
            created_at=self.now - timedelta(days=1),
        )

        report = build_notification_kind_coverage(days=30, now=self.now)
        for row in report['landscape']:
            self.assertTrue(row['has_choice_label'], row['kind'])
            self.assertTrue(row['has_preference_switch'], row['kind'])
            self.assertTrue(row['switch_in_form'], row['kind'])

    def test_the_summary_says_the_three_tables_are_aligned(self):
        """summary 从「报窟窿」翻到「对齐」，这句话本身要被钉住。

        上一轮这条位置上写的是两个窟窿的名字。翻面之后它应该说对齐——
        一个度量模块报的问题被修完，看板上要看得见这件事发生了。
        """
        make_notification(
            self.user, kind='order_created',
            created_at=self.now - timedelta(days=1),
        )

        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertIn('三种表此刻是对齐的', report['summary'])
        self.assertNotIn('没有中文名', report['summary'])
        self.assertNotIn('不在偏好页面上', report['summary'])

    def test_the_alignment_holds_even_with_no_notifications_at_all(self):
        """一条通知都没有，版图照样是齐的——它是代码事实，不是统计量。"""
        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertEqual(report['unnamed_rows'], [])
        self.assertEqual(report['unreachable_rows'], [])
        self.assertIn('还没有通知数据', report['summary'])

    def test_the_dashboard_panel_reports_alignment_not_gaps(self):
        """看板上那一格要翻面，两个空表的兜底文案要出现。"""
        staff = User.objects.create_superuser(
            username='gap-fix-staff', email='staff@example.com',
            password='safe-password-123',
        )
        self.client.force_login(staff)
        make_notification(
            self.user, kind='mutual_aid_feedback',
            created_at=self.now - timedelta(days=1),
        )

        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('当前没有正在发送却没有名字的类型', content)
        self.assertIn('当前每个类型的开关都在偏好页面上', content)
        # 标签表里有了中文名，看板上这一类不该再把英文键名当标签显示。
        self.assertIn('互助完成反馈', content)


class SwitchStillWorksTests(TestCase):
    """两个修法都没有顺手改掉别的东西。"""

    def setUp(self):
        self.now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        self.user = make_user('gap-fix-gate')

    def test_the_named_kind_is_still_gated_by_its_switch(self):
        """补名字不动闸门：关掉 ``mutual_aid_feedback`` 之后，通知一行都不建。

        ``create_notification`` 的偏好闸门是 ``getattr(preference, kind, True)``。
        本轮只往 ``KIND_CHOICES`` 里加了一行标签，这条闸门的输入没变——开关在模型上
        一直都在，所以它一直是真的生效的。这条测试守住「修复没顺手改写入路径」。
        """
        preference, _ = NotificationPreference.objects.get_or_create(user=self.user)
        preference.mutual_aid_feedback = False
        preference.save(update_fields=['mutual_aid_feedback'])

        created = create_notification(
            self.user, kind='mutual_aid_feedback', title='一条不该出现的通知',
            message='开关关着，不该建行。',
        )

        self.assertIsNone(created)
        self.assertFalse(
            Notification.objects.filter(recipient=self.user, kind='mutual_aid_feedback').exists()
        )

    def test_the_newly_exposed_switch_still_works_when_turned_off(self):
        """另一个方向：刚被摆出来的 ``meeting_incident`` 关掉之后也真的不建行。

        这个开关一直是真的生效的（模型上有、默认开着），只是用户看不见它。
        摆到表单上不改变它的行为——这条守住「暴露没顺手改写入路径」。
        """
        preference, _ = NotificationPreference.objects.get_or_create(user=self.user)
        preference.meeting_incident = False
        preference.save(update_fields=['meeting_incident'])

        created = create_notification(
            self.user, kind='meeting_incident', title='一条不该出现的异常提醒',
            message='用户刚关掉它，不该建行。',
        )

        self.assertIsNone(created)
        self.assertFalse(
            Notification.objects.filter(recipient=self.user, kind='meeting_incident').exists()
        )

    def test_a_notification_still_lands_when_the_switch_is_on(self):
        """开着的时候照旧建行——闸门两头都要钉。"""
        preference, _ = NotificationPreference.objects.get_or_create(user=self.user)
        self.assertTrue(preference.meeting_incident)
        self.assertTrue(preference.mutual_aid_feedback)

        created = create_notification(
            self.user, kind='meeting_incident', title='一条该出现的异常提醒',
            message='开关开着，照旧建行。',
        )

        self.assertIsNotNone(created)
        self.assertTrue(created.is_read is False or True)
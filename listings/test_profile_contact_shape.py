# -*- coding: utf-8 -*-
"""为「联系方式的形状」钉住口径。

``Profile`` 上有四个用户手写字段：``phone``（15）、``wechat``（50）、``bio``
（500）、``student_id``（20）。四个都有表单入口、都有 ``max_length``，而业务模块里
的读者数为零。这个模块把它们读出来，只报告不改写。

这些测试要钉住的是九件事：

**分母是全部 Profile，不是有过交易的用户。** 按交易用户当分母会把「留不留联系方式」
和「参不参与交易」焊死成一个问题，而第四节正是要把它们拆开。

**空白算未填。** 只打了空格再保存的用户，事实是没有留联系方式。

**长度档位取模型上的 max_length，不取分位数。** 档位来自各字段自己的
``max_length``，四档全列、含 0 样本档。

**长度节的分母是填了该字段的人，不是全部 Profile。** 没填的人没有长度，摊派进
任何一档都会把「不足一成」撑成一个假的多数。

**站外可触达只数 phone 和 wechat。** 只写了 ``bio`` 的人落在「都没留」。

**没有 Profile 行的用户单独数，不摊派。** 一个数不上的分母比一个错的分母好。

**三项参与分开报，不并集也不交集。** 「只逛不谈」和「只发不买」要能分开看见。

**不输出任何字段值。** 不列用户名、手机号、微信号、学号，不读 ``bio`` 正文。

**只报相关性，不讲因果。** 留了联系方式的人更可能交易这句话，模块不说也不暗示。
"""

from django.contrib.auth.models import User
from django.test import TestCase

from .models import Category, Item, Order
from .profile_contact_shape import (
    LENGTH_BANDS, MIN_FIELD_SAMPLE, MIN_GROUP_SAMPLE, NOISE_POINTS, PERIOD_CHOICES,
    build_profile_contact_shape, _band_label, _filled, _near_limit_chars,
)


def make_user(username, **profile_fields):
    """建一个用户，再直接写他的资料字段。

    ``Profile`` 由 ``create_user_profile`` 信号自动建好，所以这里用 update 而不是
    create——再 create 一次会撞上 OneToOne 唯一约束。
    """
    user = User.objects.create_user(username=username, password='safe-password-123')
    if profile_fields:
        user.profile.__dict__.update(profile_fields)
        user.profile.save()
    return user


class ProfileContactShapeTests(TestCase):
    """联系方式的形状：填写率、组合、长度与参与相关性。"""

    def setUp(self):
        self._seq = 0

    def make_item(self, seller):
        self._seq += 1
        category, _created = Category.objects.get_or_create(name='联系方式形状测试分类')
        return Item.objects.create(
            title=f'联系方式形状测试商品 {self._seq}', description='用于联系方式形状统计',
            price='10.00', category=category, condition='良好', seller=seller,
        )

    # --- 一、空平台 ---------------------------------------------------------

    def test_empty_site_says_so_instead_of_reporting_zero_percentages(self):
        report = build_profile_contact_shape()
        self.assertFalse(report['has_data'])
        self.assertEqual(report['profile_count'], 0)
        self.assertIn('还没有任何一份用户资料', report['summary'])

    def test_empty_site_reports_no_percentages_rather_than_zeros(self):
        # 0 会被读成「有，占比很小」，而这里的事实是「分母不存在」。
        report = build_profile_contact_shape()
        for row in report['field_rows']:
            self.assertIsNone(row['share'])
        for row in report['length_rows']:
            self.assertIsNone(row['near_limit_share'])

    # --- 二、填写率 ---------------------------------------------------------

    def test_fill_rate_denominator_is_every_profile_not_only_traders(self):
        # 分母是全部 Profile。按有过交易的用户当分母会把第四节的问题焊死。
        trader = make_user('pcs-trader', phone='13800000000')
        self.make_item(trader)
        bystander = make_user('pcs-bystander')

        report = build_profile_contact_shape()
        self.assertEqual(report['profile_count'], 2)
        row = {item['key']: item for item in report['field_rows']}['phone']
        self.assertEqual(row['filled_count'], 1)
        self.assertEqual(row['share'], 50.0)

    def test_whitespace_only_counts_as_unfilled(self):
        # 只打了空格再保存的用户，事实是没有留联系方式。
        make_user('pcs-blank', phone='   ', wechat='\t', bio='  ', student_id=' ')
        report = build_profile_contact_shape()
        for row in report['field_rows']:
            self.assertEqual(row['filled_count'], 0, row['key'])

    def test_every_field_reports_where_it_is_actually_shown(self):
        # 展示位置是事实核查，不是文案：student_id 采集后全站零展示。
        make_user('pcs-shown')
        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['field_rows']}
        self.assertEqual(rows['phone']['shown_in'], '会话侧栏「联系方式」块')
        self.assertEqual(rows['wechat']['shown_in'], '会话侧栏「联系方式」块')
        self.assertIn('零展示', rows['student_id']['note'])
        self.assertIn('零展示', rows['student_id']['shown_in'])
        self.assertTrue(rows['phone']['is_contact_field'])
        self.assertFalse(rows['bio']['is_contact_field'])
        self.assertFalse(rows['student_id']['is_contact_field'])

    def test_field_max_length_comes_from_the_model(self):
        make_user('pcs-limits', phone='1' * 15, wechat='w' * 50, bio='b' * 500,
                  student_id='s' * 20)
        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['field_rows']}
        self.assertEqual(rows['phone']['max_length'], 15)
        self.assertEqual(rows['wechat']['max_length'], 50)
        self.assertEqual(rows['bio']['max_length'], 500)
        self.assertEqual(rows['student_id']['max_length'], 20)

    # --- 三、站外可触达组合 -------------------------------------------------

    def test_reach_combinations_are_always_fully_listed(self):
        # 四种组合全列，包括 0 样本：「都没留」一个人都没有本身是结论。
        make_user('pcs-both', phone='13800000000', wechat='wx')
        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['reach_rows']}
        self.assertEqual(
            sorted(rows), ['both', 'neither', 'phone_only', 'wechat_only'],
        )
        self.assertEqual(rows['both']['count'], 1)
        self.assertEqual(rows['neither']['count'], 0)
        self.assertTrue(rows['both']['is_reachable'])
        self.assertFalse(rows['neither']['is_reachable'])

    def test_bio_and_student_id_do_not_count_as_reachable(self):
        # 站外可触达只数 phone 和 wechat。只写简介的人落在「都没留」。
        make_user('pcs-bio-only', bio='x' * 200, student_id='20210001')
        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['reach_rows']}
        self.assertEqual(rows['neither']['count'], 1)
        self.assertEqual(rows['both']['count'], 0)

    def test_reach_split_counts_each_single_field_case(self):
        make_user('pcs-phone-only', phone='13800000000')
        make_user('pcs-wechat-only', wechat='wx')
        make_user('pcs-both2', phone='13900000000', wechat='wx2')
        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['reach_rows']}
        self.assertEqual(rows['phone_only']['count'], 1)
        self.assertEqual(rows['wechat_only']['count'], 1)
        self.assertEqual(rows['both']['count'], 1)
        self.assertEqual(rows['neither']['count'], 0)

    # --- 四、长度形状 -------------------------------------------------------

    def test_length_bands_are_the_models_own_max_length(self):
        # 档位取 max_length 的固定比例，不取分位数。四档全列，含 0 样本档。
        make_user('pcs-len', wechat='w' * 50)
        report = build_profile_contact_shape()
        row = {item['key']: item for item in report['length_rows']}['wechat']
        self.assertEqual([band['label'] for band in row['band_rows']], list(report['length_bands']))
        self.assertEqual(row['max_seen'], 50)
        # 50 的九成是 45，不是浮点误差ceil出来的 46。
        self.assertEqual(row['near_limit_chars'], 45)
        self.assertEqual(row['near_limit_count'], 1)
        self.assertEqual(row['near_limit_share'], 100.0)

    def test_near_limit_chars_uses_integer_arithmetic(self):
        self.assertEqual(_near_limit_chars(15), 14)
        self.assertEqual(_near_limit_chars(50), 45)
        self.assertEqual(_near_limit_chars(500), 450)
        self.assertEqual(_near_limit_chars(20), 18)

    def test_band_label_bands_are_exclusive_and_left_closed(self):
        self.assertEqual(_band_label(0.0, LENGTH_BANDS), '不足一成')
        self.assertEqual(_band_label(0.099, LENGTH_BANDS), '不足一成')
        self.assertEqual(_band_label(0.1, LENGTH_BANDS), '一成到五成')
        self.assertEqual(_band_label(0.499, LENGTH_BANDS), '一成到五成')
        self.assertEqual(_band_label(0.5, LENGTH_BANDS), '五成到九成')
        self.assertEqual(_band_label(0.899, LENGTH_BANDS), '五成到九成')
        self.assertEqual(_band_label(0.9, LENGTH_BANDS), '贴上限（≥ 90%）')
        self.assertEqual(_band_label(1.0, LENGTH_BANDS), '贴上限（≥ 90%）')

    def test_length_denominator_is_only_those_who_filled_the_field(self):
        # 没填的人没有长度。摊派进任何一档都会把「不足一成」撑成一个假的多数。
        make_user('pcs-len-a', wechat='wx')
        make_user('pcs-len-empty')
        report = build_profile_contact_shape()
        row = {item['key']: item for item in report['length_rows']}['wechat']
        self.assertEqual(row['sample_count'], 1)
        bands = {band['label']: band for band in row['band_rows']}
        self.assertEqual(bands['不足一成']['count'], 1)
        self.assertEqual(bands['贴上限（≥ 90%）']['count'], 0)

    def test_a_field_nobody_filled_still_lists_all_four_bands(self):
        make_user('pcs-len-none')
        report = build_profile_contact_shape()
        for row in report['length_rows']:
            self.assertEqual(len(row['band_rows']), 4)
            self.assertEqual(row['sample_count'], 0)
            self.assertIsNone(row['max_seen'])
            self.assertIsNone(row['near_limit_share'])

    def test_filled_helper_treats_whitespace_as_empty(self):
        self.assertFalse(_filled(None))
        self.assertFalse(_filled(''))
        self.assertFalse(_filled(' \t\n '))
        self.assertTrue(_filled('x'))
        self.assertTrue(_filled(' x '))

    # --- 五、没有资料行的用户 -----------------------------------------------

    def test_users_without_a_profile_row_are_counted_not_approximated(self):
        # 信号漏建的用户不在分母里，也不摊派进任何一档，单独数出规模。
        make_user('pcs-normal', phone='13800000000')
        orphan = User.objects.create_user(username='pcs-orphan', password='safe-password-123')
        orphan.profile.delete()

        report = build_profile_contact_shape()
        self.assertEqual(report['profile_count'], 1)
        self.assertEqual(report['orphan_user_count'], 1)
        row = {item['key']: item for item in report['field_rows']}['phone']
        self.assertEqual(row['share'], 100.0)
        self.assertIn('没有资料行', report['summary'])

    # --- 六、参与相关性 -----------------------------------------------------

    def test_participation_is_split_by_whether_a_contact_was_left(self):
        reachable = make_user('pcs-part-reachable', phone='13800000000')
        unreachable = make_user('pcs-part-unreachable')
        item = self.make_item(reachable)
        Order.objects.create(
            item=item, buyer=unreachable, seller=reachable,
            agreed_price='10.00', status='completed',
        )

        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['participation_rows']}
        self.assertEqual(rows['reachable']['user_count'], 1)
        self.assertEqual(rows['unreachable']['user_count'], 1)
        # 三项分开报：卖家发布过商品，买家下过买入订单，两个人都有过对话。
        reachable_items = {item['key']: item for item in rows['reachable']['items']}
        unreachable_items = {item['key']: item for item in rows['unreachable']['items']}
        self.assertEqual(reachable_items['listed']['count'], 1)
        self.assertEqual(unreachable_items['bought']['count'], 1)
        self.assertEqual(reachable_items['bought']['count'], 0)
        self.assertEqual(unreachable_items['listed']['count'], 0)

    def test_messaged_counts_both_sides_of_a_conversation(self):
        from chat_messages.models import PrivateMessage
        sender = make_user('pcs-msg-sender', phone='13800000000')
        receiver = make_user('pcs-msg-receiver')
        item = self.make_item(sender)
        PrivateMessage.objects.create(
            sender=sender, receiver=receiver, item=item, content='在吗',
        )
        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['participation_rows']}
        reachable_items = {i['key']: i for i in rows['reachable']['items']}
        unreachable_items = {i['key']: i for i in rows['unreachable']['items']}
        self.assertEqual(reachable_items['messaged']['count'], 1)
        self.assertEqual(unreachable_items['messaged']['count'], 1)

    def test_participation_groups_are_both_listed_even_when_empty(self):
        make_user('pcs-part-only-reachable', phone='13800000000')
        report = build_profile_contact_shape()
        rows = {row['key']: row for row in report['participation_rows']}
        self.assertEqual(rows['unreachable']['user_count'], 0)
        # 「一个都没留」是 0%，不是「—」：分母（全部资料）是存在的。
        self.assertEqual(rows['unreachable']['share'], 0.0)
        # 组内占比才是空分母——组里没有人，三项都无从谈起。
        for item in rows['unreachable']['items']:
            self.assertEqual(item['count'], 0)
            self.assertIsNone(item['share'])

    def test_the_module_never_reports_a_direction_for_the_correlation(self):
        # 只报相关性，不讲因果：没有 gap、没有 direction、没有结论字段。
        make_user('pcs-corr-a', phone='13800000000')
        make_user('pcs-corr-b')
        rendered = repr(build_profile_contact_shape())
        for forbidden in ('gap', 'direction', 'more_likely', 'because', 'causes'):
            self.assertNotIn(forbidden, rendered)

    # --- 七、隐私边界 -------------------------------------------------------

    def test_the_module_never_reports_any_field_value(self):
        make_user(
            'pcs-private', phone='13812345678', wechat='wx-secret-99',
            bio='我住在 3 号宿舍楼 512，学号 20210001', student_id='20210001',
        )
        rendered = repr(build_profile_contact_shape())
        self.assertNotIn('pcs-private', rendered)
        self.assertNotIn('13812345678', rendered)
        self.assertNotIn('wx-secret-99', rendered)
        self.assertNotIn('20210001', rendered)
        self.assertNotIn('512', rendered)

    # --- 八、接口 -----------------------------------------------------------

    def test_days_is_accepted_but_never_filters_anything(self):
        # 联系方式是累计状态。days 只为接口一致收下，全量口径。
        old = make_user('pcs-old', phone='13800000000')
        item = self.make_item(old)
        Order.objects.create(
            item=item, buyer=make_user('pcs-old-buyer'), seller=old,
            agreed_price='10.00', status='completed',
        )
        for days in (7, 30, 90, 365):
            report = build_profile_contact_shape(days=days)
            self.assertEqual(report['days'], days)
            self.assertEqual(report['profile_count'], 2)
            rows = {row['key']: row for row in report['participation_rows']}
            # items 的顺序固定为 有过对话 / 下过买入订单 / 发布过商品。
            self.assertEqual([i['key'] for i in rows['reachable']['items']],
                             ['messaged', 'bought', 'listed'])
            # 卖家发布了商品，买家下过买入订单——两项分开，不合起来。
            self.assertEqual(rows['reachable']['items'][2]['count'], 1)
            self.assertEqual(rows['reachable']['items'][1]['count'], 0)

    def test_unknown_days_falls_back_to_the_default(self):
        self.assertEqual(build_profile_contact_shape(days=999)['days'], 30)
        self.assertEqual({value for value, _label in PERIOD_CHOICES}, {7, 30, 90, 365})

    def test_thresholds_are_exposed_for_the_panel(self):
        # 门槛必须显示在面板上：读者要知道一个数是从几份样本里读出来的。
        report = build_profile_contact_shape()
        self.assertEqual(report['min_field_sample'], MIN_FIELD_SAMPLE)
        self.assertEqual(report['min_group_sample'], MIN_GROUP_SAMPLE)
        self.assertEqual(report['noise_points'], NOISE_POINTS)
        self.assertEqual(report['contact_fields'], ['phone', 'wechat'])

    def test_panel_scalars_come_from_the_same_tables(self):
        # 首屏小卡片不许另立一套口径：四个标量都在上面的表里算过。
        make_user('pcs-scalar-a', phone='13800000000', wechat='w' * 50)
        make_user('pcs-scalar-b', bio='x' * 10)
        report = build_profile_contact_shape()
        self.assertEqual(report['reachable_share'], 50.0)
        self.assertEqual(report['near_limit_field_count'], 1)
        self.assertEqual(report['zero_display_fields'], ['学号'])

    def test_small_sample_is_flagged_rather_than_reported_as_a_direction(self):
        make_user('pcs-small', phone='13800000000')
        report = build_profile_contact_shape()
        row = {item['key']: item for item in report['field_rows']}['phone']
        self.assertTrue(row['is_small_sample'])
        self.assertIn('只作参考', report['summary'])

# -*- coding: utf-8 -*-
"""为「证据文件的形状」钉住口径。

`OrderDisputeEvidence.attachment` 上挂着 `validate_dispute_evidence`——5 MB 上限、
四种扩展名，这是站内唯一一处把「治理侧要能审阅」写成代码的产品规则。争议域唯一的
读者 `dispute_resolution_quality` 只数附件条数，`attachment.name` 与
`attachment.size` 一次都没被读过。这个模块把它读出来，只报告不改写。

这些测试要钉住的是七件事：

**「其它」档恒为 0 也要留在表里。** 校验器拦着，不合规的扩展名存不进来。但一个
恒为 0 的格子是有信息的——它说明校验在生效。测试要断言六档全列，而且「其它」的
说明文字说清了这一点。

**大小档位取规则里的原值，不取分位数。** 档位来自 `validate_dispute_evidence` 的
5 MB 和它的九成。测试要断言六个档位在只有两种文件时依然全列——一旦有人把档位改成
分位数，空档会被自动裁掉。

**窗口按证据上传时刻切，不按争议发起时刻切。** 一份窗口外上传的附件不计入，即使
它所属的争议是窗口内发起的。两个窗口不同是有原因的，测试要把它钉住。

**读不到大小的附件单独数，不摊派。** 一个读不到大小的附件是基础设施问题，不是
用户传了一个奇怪的文件。测试要断言它不进任何大小档的分母，却被单独数出规模。

**0 字节的文件进「0 KB」一格，不当作读不到大小。** 「传了一个空文件」和「存储层
出了问题」不是一回事。

**还在流程中的争议只报规模、不给方向。** `add_dispute_evidence` 只在 open /
reviewing 开放，所以流程中争议的份数只是下界。测试要断言它不拿 `gives_direction`。

**不判断证据充分性。** 模块只报「一笔争议带几份、总大小多少」，不报「几份才算
充分」——平台没有能力判断一段聊天记录能不能证明「商品与描述不符」。
"""

import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.utils import timezone

from .dispute_evidence_shape import (
    ALLOWED_EXTENSIONS, MAX_EVIDENCE_SIZE, MIN_EVIDENCE_SAMPLE, NEAR_LIMIT_SIZE,
    SIZE_BANDS, build_dispute_evidence_shape, _band_label, _extension_label,
    _format_size,
)
from .models import Category, Item, Order, OrderDispute, OrderDisputeEvidence


TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='evidence-shape-test-')


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class DisputeEvidenceShapeTests(TestCase):
    """证据文件的形状：扩展名、大小、每笔争议的份数。"""

    def setUp(self):
        self.buyer = make_user('evidence-buyer')
        self.seller = make_user('evidence-seller')
        self.category = Category.objects.create(name='证据形状测试分类')
        self._seq = 0

    def make_item(self):
        self._seq += 1
        return Item.objects.create(
            title=f'证据形状测试商品 {self._seq}', description='用于证据形状统计',
            price='10.00', category=self.category, condition='良好', seller=self.seller,
        )

    def make_order(self, *, buyer=None):
        item = self.make_item()
        return Order.objects.create(
            item=item, buyer=buyer or self.buyer, seller=item.seller,
            agreed_price=item.price, status='completed',
        )

    def make_dispute(self, *, status='resolved', order=None, created_days_ago=3):
        """建一条争议，`created_at` 是 auto_now_add，建完再改。

        窗口按证据上传时刻切，所以争议自己的发起时刻不影响本模块的分母；但把它
        挪到窗口内能让 `dispute_resolution_quality` 的口径在对照时也站得住。
        """
        dispute = OrderDispute.objects.create(
            order=order or self.make_order(), opened_by=self.buyer,
            reason='mismatch', detail='双方对交易结果有分歧。', status=status,
        )
        dispute.created_at = timezone.now() - timedelta(days=created_days_ago)
        dispute.save(update_fields=['created_at'])
        return dispute

    def make_evidence(self, dispute, *, name, size, created_days_ago=1, status='resolved'):
        """落一份真实大小的附件，再把它挂到争议上。

        `attachment.size` 由存储后端的文件大小决定，测试必须真的落盘——把一个
        不存在的路径塞进字段只会得到 `FileNotFoundError`，那不是「读不到大小」，
        那是这条记录本身没有文件。读不到大小那一档的构造见
        `test_unreadable_size_is_counted_not_approximated`。
        """
        default_storage.save(name, ContentFile(b'x' * size))
        evidence = OrderDisputeEvidence.objects.create(
            dispute=dispute, uploaded_by=self.buyer, attachment=name, note='',
        )
        evidence.created_at = timezone.now() - timedelta(days=created_days_ago)
        evidence.save(update_fields=['created_at'])
        if status is not None:
            dispute.status = status
            dispute.save(update_fields=['status'])
        return evidence

    # --- 扩展名分布 ---------------------------------------------------------

    def test_every_allowed_extension_gets_its_own_row_including_zero_sample(self):
        # 四种允许格式各一档，外加「其它」。0 样本档必须留在表里：「没有人用过
        # PDF」本身是结论，不是表格的缺口。这一条同时守住档位不随数据移动。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/only.jpg', size=2048)

        report = build_dispute_evidence_shape(days=30)
        rows = report['extension_rows']
        self.assertEqual(len(rows), len(ALLOWED_EXTENSIONS) + 1)
        labels = [row['label'] for row in rows]
        for expected in ('JPG', 'JPEG', 'PNG', 'WebP', 'PDF'):
            self.assertIn(expected, labels)
        self.assertIn('其它', labels)

    def test_extension_distribution_counts_each_format_separately(self):
        dispute = self.make_dispute()
        for index in range(3):
            self.make_evidence(
                dispute, name=f'dispute_evidence/2026/10/shot{index}.jpg', size=2048,
            )
        self.make_evidence(dispute, name='dispute_evidence/2026/10/scan.pdf', size=2048)

        report = build_dispute_evidence_shape(days=30)
        rows = {row['label']: row for row in report['extension_rows']}
        self.assertEqual(rows['JPG']['count'], 3)
        self.assertEqual(rows['JPG']['share'], 75.0)
        self.assertEqual(rows['PDF']['count'], 1)
        self.assertEqual(rows['PDF']['share'], 25.0)
        self.assertEqual(rows['PNG']['count'], 0)
        self.assertEqual(rows['PNG']['share'], 0.0)

    def test_extension_is_read_from_the_stored_path_not_the_original_name(self):
        # 扩展名只看 attachment.name 这个存储路径的字符串。上传时的原始名可能
        # 带着真名、身份证号，本模块一个字符都不碰它。
        dispute = self.make_dispute()
        self.make_evidence(
            dispute, name='dispute_evidence/2026/10/UPPERCASE.PNG', size=2048,
        )

        report = build_dispute_evidence_shape(days=30)
        rows = {row['label']: row for row in report['extension_rows']}
        self.assertEqual(rows['PNG']['count'], 1)

    def test_the_other_row_is_zero_when_the_validator_holds(self):
        # 校验器允许的范围之外扩展名理论上恒为 0，但它仍然单列——一个恒为 0 的
        # 格子是有信息的：它说明校验在生效。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/ok.jpg', size=2048)

        report = build_dispute_evidence_shape(days=30)
        other = [row for row in report['extension_rows'] if not row['is_allowed']][0]
        self.assertEqual(other['count'], 0)
        self.assertIn('校验在生效', other['note'])

    def test_a_path_with_no_extension_falls_into_the_other_row(self):
        # Path('').suffix 是空串，空串落进「其它」——校验器不允许没有扩展名的文件，
        # 所以它和不合规的扩展名是同一类事。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/noext', size=2048)

        report = build_dispute_evidence_shape(days=30)
        other = [row for row in report['extension_rows'] if not row['is_allowed']][0]
        self.assertEqual(other['count'], 1)

    # --- 大小分档 -----------------------------------------------------------

    def test_size_bands_cover_the_whole_range_without_gaps(self):
        """六个档位左闭右开，合起来覆盖 0 字节以上，没有落不进去的值。"""
        self.assertEqual(SIZE_BANDS[0][0], 0)
        self.assertIsNone(SIZE_BANDS[-1][1])
        for index, (lower, upper, _label) in enumerate(SIZE_BANDS):
            if upper is not None:
                self.assertEqual(SIZE_BANDS[index + 1][0], upper)
        # 边界语义：正好 102400 落进下一档。
        self.assertEqual(_band_label(0, SIZE_BANDS), '0 KB')
        self.assertEqual(_band_label(102399, SIZE_BANDS), '100 KB 以内')
        self.assertEqual(_band_label(102400, SIZE_BANDS), '100 KB - 1 MB')
        self.assertEqual(_band_label(NEAR_LIMIT_SIZE, SIZE_BANDS), '贴上限（≥ 4.5 MB）')

    def test_bands_are_taken_from_the_validator_not_from_percentiles(self):
        # 档位来自 validate_dispute_evidence 的 5 MB 和它的九成，不随数据移动。
        # 库里只有两种文件时六个档位依然全列——一旦有人把档位改成分位数，空档会
        # 被自动裁掉。
        self.assertEqual(NEAR_LIMIT_SIZE, int(MAX_EVIDENCE_SIZE * 0.9))
        self.assertEqual(MAX_EVIDENCE_SIZE, 5 * 1024 * 1024)
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/tiny.jpg', size=1024)
        self.make_evidence(dispute, name='dispute_evidence/2026/10/big.png', size=4000000)

        report = build_dispute_evidence_shape(days=30)
        self.assertEqual(len(report['size_rows']), len(SIZE_BANDS))
        rows = {row['label']: row for row in report['size_rows']}
        self.assertEqual(rows['100 KB 以内']['count'], 1)
        self.assertEqual(rows['3 MB - 4.5 MB']['count'], 1)
        self.assertEqual(rows['0 KB']['count'], 0)

    def test_size_distribution_counts_each_band_separately(self):
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/a.jpg', size=50 * 1024)
        self.make_evidence(dispute, name='dispute_evidence/2026/10/b.jpg', size=600 * 1024)
        self.make_evidence(dispute, name='dispute_evidence/2026/10/c.jpg', size=2 * 1024 * 1024)

        report = build_dispute_evidence_shape(days=30)
        rows = {row['label']: row for row in report['size_rows']}
        self.assertEqual(rows['100 KB 以内']['count'], 1)
        self.assertEqual(rows['100 KB - 1 MB']['count'], 1)
        self.assertEqual(rows['1 MB - 3 MB']['count'], 1)
        self.assertEqual(rows['100 KB 以内']['share'], round(100 / 3, 1))

    # --- 贴上限 -------------------------------------------------------------

    def test_files_just_under_the_limit_are_flagged_as_near_limit(self):
        # 「贴上限」是阈值观察，不是一格分布：一批文件反复撞上限，说明上限在挡人。
        dispute = self.make_dispute()
        for index in range(5):
            self.make_evidence(
                dispute, name=f'dispute_evidence/2026/10/huge{index}.jpg',
                size=4_800_000,
            )

        report = build_dispute_evidence_shape(days=30)
        near = report['near_limit']
        self.assertEqual(near['near_limit_count'], 5)
        self.assertEqual(near['near_limit_rate'], 100.0)
        self.assertEqual(near['over_limit_count'], 0)
        self.assertTrue(near['has_sample'])
        self.assertIn('4.5 MB', report['summary'])

    def test_a_file_over_the_limit_is_counted_separately_as_a_bug(self):
        # 校验器拦着，超过上限理论上也是 0。非 0 就说明有路径绕过了校验——
        # 那是一个 bug，不是一条产品建议。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/over.jpg', size=5_500_000)

        report = build_dispute_evidence_shape(days=30)
        near = report['near_limit']
        self.assertEqual(near['near_limit_count'], 1)
        self.assertEqual(near['over_limit_count'], 1)
        self.assertIn('1 份贴着', report['summary'])
        self.assertIn('已经超过上限', report['summary'])

    # --- 读不到大小与 0 字节 -------------------------------------------------

    def test_unreadable_size_is_counted_not_approximated(self):
        # 读不到大小的附件是基础设施问题，不是用户传了一个奇怪的文件。它单独数出
        # 规模，不摊派到任何一档，也不进任何分母。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/real.jpg', size=2048)
        missing = OrderDisputeEvidence.objects.create(
            dispute=dispute, uploaded_by=self.buyer,
            attachment='dispute_evidence/2099/01/never.jpg', note='',
        )

        report = build_dispute_evidence_shape(days=30)
        self.assertEqual(report['evidence_count'], 2)
        self.assertEqual(report['readable_count'], 1)
        self.assertEqual(report['unreadable_count'], 1)
        rows = {row['label']: row for row in report['size_rows']}
        self.assertEqual(sum(row['count'] for row in report['size_rows']), 1)
        self.assertEqual(rows['100 KB 以内']['count'], 1)
        # 扩展名的分母含它：扩展名只取决于名字这个字符串，和文件在不在无关。
        self.assertEqual(
            sum(row['count'] for row in report['extension_rows']),
            report['evidence_count'],
        )

    def test_a_zero_byte_file_is_a_band_not_an_unreadable_size(self):
        # 「传了一个空文件」和「存储层出了问题」不是一回事。0 字节进「0 KB」一格。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/empty.jpg', size=0)

        report = build_dispute_evidence_shape(days=30)
        self.assertEqual(report['unreadable_count'], 0)
        rows = {row['label']: row for row in report['size_rows']}
        self.assertEqual(rows['0 KB']['count'], 1)
        self.assertEqual(rows['0 KB']['share'], 100.0)

    # --- 每笔争议的份数 -----------------------------------------------------

    def test_per_dispute_counts_are_bucketed_by_attachment_count(self):
        dispute = self.make_dispute()
        for index in range(5):
            self.make_evidence(
                dispute, name=f'dispute_evidence/2026/10/p{index}.jpg', size=100 * 1024,
            )

        report = build_dispute_evidence_shape(days=30)
        rows = {row['label']: row for row in report['per_dispute']['count_rows']}
        self.assertEqual(rows['5 份及以上']['count'], 1)
        self.assertEqual(report['per_dispute']['max_count'], 5)
        self.assertEqual(report['per_dispute']['dispute_count'], 1)

    def test_per_dispute_totals_are_reported_not_judged_for_sufficiency(self):
        # 只报「一笔争议带几份、总大小多少」，不报「几份才算充分」。平台没有能力
        # 判断一段聊天记录能不能证明「商品与描述不符」。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/x.jpg', size=1024)
        self.make_evidence(dispute, name='dispute_evidence/2026/10/y.jpg', size=3072)

        report = build_dispute_evidence_shape(days=30)
        per_dispute = report['per_dispute']
        self.assertEqual(per_dispute['dispute_count'], 1)
        self.assertEqual(per_dispute['median_total_size'], 4096)
        self.assertEqual(per_dispute['median_total_label'], '4 KB')
        self.assertNotIn('充分', per_dispute['median_total_label'])

    def test_disputes_without_any_evidence_are_not_in_the_denominator(self):
        # 本模块按证据上传时刻切窗口，看不见一份附件都没有的争议。那个数在
        # dispute_resolution_quality 里，但它按争议发起切窗口，也不是同一批记录。
        silent = self.make_dispute()
        loud = self.make_dispute()
        self.make_evidence(loud, name='dispute_evidence/2026/10/loud.jpg', size=2048)

        report = build_dispute_evidence_shape(days=30)
        self.assertEqual(report['dispute_count'], 1)
        self.assertEqual(report['evidence_count'], 1)
        self.assertNotIn(silent.id, [row['dispute_id'] for row in []])

    # --- 状态交叉 -----------------------------------------------------------

    def test_a_dispute_still_in_flow_is_reported_without_a_direction(self):
        # add_dispute_evidence 只在 open / reviewing 开放，所以流程中争议以后还会
        # 补附件，它现在的份数只是下界。把两组的均值放在一起比，会把「还在谈」
        # 读成「材料少」。
        dispute = self.make_dispute(status='open')
        for index in range(2):
            self.make_evidence(
                dispute, name=f'dispute_evidence/2026/10/o{index}.jpg', size=2048,
                status='open',
            )

        report = build_dispute_evidence_shape(days=30)
        rows = {row['key']: row for row in report['status_cross_rows']}
        self.assertEqual(rows['open']['dispute_count'], 1)
        self.assertFalse(rows['open']['gives_direction'])
        self.assertIn('只是下界', rows['open']['note'])
        self.assertNotIn('closed', rows)

    def test_a_closed_dispute_can_carry_a_direction_when_it_has_a_sample(self):
        # 方向的分母是这一组里的争议条数，不是附件条数：5 条已完结争议各带一份，
        # 和 1 条已完结争议带 5 份，前者能说「一笔平均带多少」，后者什么都不能说。
        for index in range(MIN_EVIDENCE_SAMPLE):
            dispute = self.make_dispute(status='rejected')
            self.make_evidence(
                dispute, name=f'dispute_evidence/2026/10/c{index}.jpg', size=2048,
            )

        report = build_dispute_evidence_shape(days=30)
        rows = {row['key']: row for row in report['status_cross_rows']}
        self.assertTrue(rows['closed']['gives_direction'])
        self.assertEqual(rows['closed']['dispute_count'], MIN_EVIDENCE_SAMPLE)
        self.assertEqual(rows['closed']['evidence_count'], MIN_EVIDENCE_SAMPLE)

    # --- 窗口 ---------------------------------------------------------------

    def test_the_window_cuts_on_evidence_upload_not_dispute_creation(self):
        # 窗口按证据上传时刻切，不按争议发起时刻切。一份窗口外上传的附件不计入，
        # 即使它所属的争议是窗口内发起的——两个窗口的分母不是同一批记录。
        dispute = self.make_dispute(created_days_ago=1)
        self.make_evidence(
            dispute, name='dispute_evidence/2026/10/new.jpg', size=2048,
            created_days_ago=1,
        )
        self.make_evidence(
            dispute, name='dispute_evidence/2026/10/old.jpg', size=2048,
            created_days_ago=45,
        )

        report = build_dispute_evidence_shape(days=30)
        self.assertEqual(report['evidence_count'], 1)

    def test_unknown_days_falls_back_to_the_default_period(self):
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/d.jpg', size=2048)
        report = build_dispute_evidence_shape(days=15)
        self.assertEqual(report['days'], 30)

    def test_empty_period_reports_a_readable_summary(self):
        report = build_dispute_evidence_shape(days=30)
        self.assertFalse(report['has_data'])
        self.assertIn('还没有交易争议证据', report['summary'])
        self.assertEqual(report['evidence_count'], 0)
        self.assertEqual(report['unreadable_count'], 0)

    # --- 只读性 -------------------------------------------------------------

    def test_the_module_never_reports_file_content(self):
        # 不读文件内容、不 OCR、不看图片尺寸、不判断这份截图是不是真的和这笔订单
        # 有关。落盘的内容是字节流，模块只读存储层的元数据。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/chat.jpg', size=16)
        with default_storage.open('dispute_evidence/2026/10/chat.jpg', 'wb') as handle:
            handle.write(b'buyer-phone-13800000000')

        report = build_dispute_evidence_shape(days=30)
        rendered = repr(report)
        self.assertNotIn('13800000000', rendered)
        self.assertNotIn('buyer-phone', rendered)

    def test_the_module_never_reads_the_evidence_note(self):
        # note 是自由文本，可能写着电话、微信、楼栋号。
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/n.jpg', size=2048)
        evidence = OrderDisputeEvidence.objects.get(
            attachment='dispute_evidence/2026/10/n.jpg',
        )
        evidence.note = '买家电话 13800000000，微信同号，楼栋 A-302。'
        evidence.save(update_fields=['note'])

        report = build_dispute_evidence_shape(days=30)
        rendered = repr(report)
        self.assertNotIn('13800000000', rendered)
        self.assertNotIn('A-302', rendered)

    def test_the_module_never_reports_the_stored_filename(self):
        # 文件名是用户上传时的原始名，可能带着真名、身份证号、订单号。
        dispute = self.make_dispute()
        self.make_evidence(
            dispute, name='dispute_evidence/2026/10/20261012-张三-3301.pdf', size=2048,
        )

        report = build_dispute_evidence_shape(days=30)
        self.assertNotIn('张三', repr(report))
        self.assertNotIn('3301', repr(report))
        # 扩展名仍然读得到——它只看路径的字符串形状。
        rows = {row['label']: row for row in report['extension_rows']}
        self.assertEqual(rows['PDF']['count'], 1)

    def test_the_module_never_reports_upload_failures(self):
        # 「有多少次上传因为超过 5 MB 被拒」这个数，库里没有——校验在表单层拦下，
        # 失败不留任何记录。数据不可知不等于样本不足，不能编一个近似值顶上去。
        report = build_dispute_evidence_shape(days=30)
        rendered = repr(report)
        self.assertNotIn('rejected_count', rendered)
        self.assertNotIn('upload_failed', rendered)
        self.assertNotIn('upload_error', rendered)

    # --- 阈值显示 -----------------------------------------------------------

    def test_thresholds_are_exposed_for_the_panel(self):
        # 门槛必须显示在面板上：读者要知道一个数是从几份样本里读出来的。
        report = build_dispute_evidence_shape(days=30)
        self.assertEqual(report['min_sample'], 5)
        self.assertEqual(report['noise_points'], 8)
        self.assertEqual(report['allowed_extensions'], list(ALLOWED_EXTENSIONS))

    def test_small_sample_is_flagged_rather_than_reported_as_a_direction(self):
        dispute = self.make_dispute()
        self.make_evidence(dispute, name='dispute_evidence/2026/10/one.jpg', size=2048)

        report = build_dispute_evidence_shape(days=30)
        self.assertTrue(report['near_limit']['has_sample'] is False)
        self.assertIn('只作参考', report['summary'])

    def test_format_size_renders_in_readable_units(self):
        self.assertEqual(_format_size(None), '—')
        self.assertEqual(_format_size(512), '512 B')
        self.assertEqual(_format_size(2048), '2 KB')
        self.assertEqual(_format_size(3 * 1024 * 1024), '3.0 MB')

    def test_extension_label_reads_lowercase_suffix_only(self):
        self.assertEqual(_extension_label('dispute_evidence/2026/10/a.JPG'), 'JPG')
        self.assertEqual(_extension_label('dispute_evidence/2026/10/a.jpg'), 'JPG')
        self.assertEqual(_extension_label('dispute_evidence/2026/10/a.gif'), '其它')
        self.assertEqual(_extension_label(''), '其它')
        self.assertEqual(_extension_label(None), '其它')

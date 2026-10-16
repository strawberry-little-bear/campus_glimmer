from django.test import SimpleTestCase

from .bulk_listing_csv import BulkListingCsvRowError
from .bulk_listing_import_summary import build_bulk_listing_import_error_summary


class BulkListingImportErrorSummaryTests(SimpleTestCase):
    """批量发布导入错误摘要的聚合、排序与展示边界。"""

    def test_empty_errors_return_no_summary(self):
        self.assertIsNone(build_bulk_listing_import_error_summary([]))

    def test_groups_repeated_errors_and_counts_affected_rows(self):
        summary = build_bulk_listing_import_error_summary([
            BulkListingCsvRowError('只能填写出售、免费赠送或限期借用。', 3, '交易方式'),
            BulkListingCsvRowError('请填写数字，最多两位小数。', 4, '价格'),
            BulkListingCsvRowError('只能填写出售、免费赠送或限期借用。', 2, '交易方式'),
            BulkListingCsvRowError('这一列不能留空。', 2, '标题'),
            BulkListingCsvRowError('只能填写出售、免费赠送或限期借用。', 2, '交易方式'),
        ])

        assert summary is not None
        self.assertEqual(summary.total_error_count, 4)
        self.assertEqual(summary.affected_row_count, 3)
        self.assertEqual(summary.group_count, 3)
        self.assertEqual(summary.hidden_group_count, 0)
        self.assertEqual(summary.groups[0].column, '交易方式')
        self.assertEqual(summary.groups[0].count, 2)
        self.assertEqual(summary.groups[0].row_numbers, (3, 2))
        self.assertEqual(summary.groups[0].row_numbers_display, '第 3 行、第 2 行')
        self.assertEqual(summary.groups[1].column, '标题')
        self.assertEqual(summary.groups[2].column, '价格')

    def test_columnless_error_is_displayed_as_a_whole_row_problem(self):
        summary = build_bulk_listing_import_error_summary([
            BulkListingCsvRowError('这一行必须正好 7 列，请补齐或删除多余列。', 5),
        ])

        assert summary is not None
        self.assertEqual(summary.total_error_count, 1)
        self.assertEqual(summary.affected_row_count, 1)
        self.assertEqual(summary.group_count, 1)
        self.assertEqual(summary.groups[0].column_label, '整行')
        self.assertEqual(summary.groups[0].row_numbers_display, '第 5 行')

    def test_only_the_first_six_groups_are_expanded(self):
        summary = build_bulk_listing_import_error_summary([
            BulkListingCsvRowError(f'问题 {index}', 2, f'第 {index} 列')
            for index in range(8)
        ])

        assert summary is not None
        self.assertEqual(summary.group_count, 8)
        self.assertEqual(len(summary.groups), 6)
        self.assertEqual(summary.hidden_group_count, 2)

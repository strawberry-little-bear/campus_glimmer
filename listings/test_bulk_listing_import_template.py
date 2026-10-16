from io import BytesIO
from zipfile import ZipFile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from .bulk_listing_csv import BULK_LISTING_IMPORT_HEADERS, TRADE_MODE_LABELS, parse_bulk_listing_csv
from .bulk_listing_template import build_bulk_listing_import_template
from .bulk_listing_xlsx import parse_bulk_listing_xlsx


class BulkListingImportTemplateTests(TestCase):
    """批量发布 CSV / XLSX 模板的生成与下载边界。"""

    @classmethod
    def setUpTestData(cls):
        cls.seller = User.objects.create_user(
            username='template-seller', password='safe-password-123',
        )

    def test_csv_download_uses_exact_header_and_safe_headers(self):
        self.client.force_login(self.seller)
        page = self.client.get(reverse('bulk_create_items'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(
            page,
            reverse('bulk_listing_import_template', args=['csv']),
        )

        response = self.client.get(
            reverse('bulk_listing_import_template', args=['csv']),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertEqual(
            response['Content-Disposition'],
            'attachment; filename="campus-glimmer-bulk-listing-template.csv"',
        )
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertFalse(response.content.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(
            response.content.decode('utf-8').splitlines(),
            [','.join(BULK_LISTING_IMPORT_HEADERS)],
        )

    def test_xlsx_download_contains_one_guarded_worksheet(self):
        self.client.force_login(self.seller)

        response = self.client.get(
            reverse('bulk_listing_import_template', args=['xlsx']),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'spreadsheetml.sheet', response['Content-Type'],
        )
        self.assertEqual(
            response['Content-Disposition'],
            'attachment; filename="campus-glimmer-bulk-listing-template.xlsx"',
        )
        self.assertEqual(response['Cache-Control'], 'no-store')

        with ZipFile(BytesIO(response.content)) as archive:
            self.assertIsNone(archive.testzip())
        workbook = load_workbook(BytesIO(response.content), data_only=False)
        self.assertEqual(workbook.sheetnames, ['商品导入'])
        worksheet = workbook.active
        self.assertEqual(
            tuple(cell.value for cell in worksheet[1]),
            BULK_LISTING_IMPORT_HEADERS,
        )
        self.assertEqual(worksheet.freeze_panes, 'A2')
        self.assertEqual(worksheet.max_row, 1)
        self.assertTrue(all(cell.data_type == 's' for cell in worksheet[1]))

        validations = {
            str(validation.sqref): validation
            for validation in worksheet.data_validations.dataValidation
        }
        expected = {
            'A2:A21': (
                'textLength', 'between', '1', '200', '标题',
                '填写商品后标题需为 1–200 个字符。',
            ),
            'C2:C21': (
                'list', None, f'"{",".join(TRADE_MODE_LABELS)}"', None,
                '交易方式', f'只能填写：{",".join(TRADE_MODE_LABELS)}',
            ),
            'D2:D21': (
                'decimal', 'between', '0', '99999999.99', '价格',
                '填写价格时需为 0–99,999,999.99 的数字。',
            ),
            'E2:E21': (
                'decimal', 'between', '0', '99999999.99', '押金',
                '填写押金时需为 0–99,999,999.99 的数字。',
            ),
            'F2:F21': (
                'whole', 'between', '1', '90', '借用天数',
                '填写借用天数时需为 1–90 的整数。',
            ),
            'G2:G21': (
                'textLength', 'between', '1', '100', '成色',
                '填写商品后成色需为 1–100 个字符。',
            ),
        }
        self.assertEqual(set(validations), set(expected))
        for cell_range, attributes in expected.items():
            with self.subTest(cell_range=cell_range):
                validation = validations[cell_range]
                validation_type, operator, formula1, formula2, title, error = attributes
                self.assertEqual(validation.type, validation_type)
                self.assertEqual(validation.operator, operator)
                self.assertEqual(validation.formula1, formula1)
                self.assertEqual(validation.formula2, formula2)
                self.assertTrue(validation.allow_blank)
                self.assertTrue(validation.showErrorMessage)
                self.assertEqual(validation.errorTitle, title)
                self.assertEqual(validation.error, error)

    def test_generated_templates_still_reach_the_empty_row_guard(self):
        csv_template = build_bulk_listing_import_template('csv')
        csv_result = parse_bulk_listing_csv(SimpleUploadedFile(
            csv_template.filename,
            csv_template.content,
            content_type='text/csv',
        ))
        self.assertEqual(csv_result.file_error, '')
        self.assertEqual(len(csv_result.row_errors), 1)
        self.assertIn('CSV 中至少需要一行非空商品', csv_result.row_errors[0].message)
        self.assertEqual(csv_result.rows, [])

        xlsx_template = build_bulk_listing_import_template('xlsx')
        xlsx_result = parse_bulk_listing_xlsx(SimpleUploadedFile(
            xlsx_template.filename,
            xlsx_template.content,
            content_type=(
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        ))
        self.assertEqual(xlsx_result.file_error, '')
        self.assertEqual(len(xlsx_result.row_errors), 1)
        self.assertIn('至少需要一行非空商品', xlsx_result.row_errors[0].message)
        self.assertEqual(xlsx_result.rows, [])

    def test_unknown_format_and_anonymous_download_are_rejected(self):
        self.client.force_login(self.seller)
        unknown = self.client.get(
            reverse('bulk_listing_import_template', args=['xls']),
        )
        self.assertEqual(unknown.status_code, 404)
        self.client.logout()

        response = self.client.get(
            reverse('bulk_listing_import_template', args=['csv']),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

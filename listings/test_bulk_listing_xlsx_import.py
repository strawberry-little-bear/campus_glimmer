from datetime import date
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

from .models import CampusCampaign, CampusLocation, Category, Item

XLSX_HEADER = ('标题', '描述', '交易方式', '价格', '押金', '借用天数', '成色')
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def make_xlsx(rows, *, header=XLSX_HEADER, second_sheet_rows=None):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = '商品清单'
    worksheet.append(list(header))
    for row in rows:
        worksheet.append(list(row))
    if second_sheet_rows is not None:
        second = workbook.create_sheet('不要读取')
        second.append(['wrong header'])
        for row in second_sheet_rows:
            second.append(list(row))
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


class BulkListingXlsxImportTests(TestCase):
    """毕业季批量发布 XLSX 导入预览的服务端行为。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='xlsx-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='毕业季闲置')
        self.location = CampusLocation.objects.create(name='东门快递点')
        now = timezone.now()
        self.campaign = CampusCampaign.objects.create(
            title='毕业季清仓', slug='graduation-xlsx',
            starts_at=now - timezone.timedelta(days=1),
            ends_at=now + timezone.timedelta(days=30), is_active=True,
        )
        self.import_url = reverse('bulk_create_items_import')
        self.bulk_url = reverse('bulk_create_items')

    def _upload(self, content, *, filename='graduation.xlsx'):
        upload = SimpleUploadedFile(filename, content, content_type=XLSX_MIME)
        return self.client.post(self.import_url, {'import_file': upload})

    def _row_payload(self, rows, *, token=''):
        payload = {
            'category': self.category.id,
            'location': self.location.id,
            'campaign': self.campaign.id,
            'expires_at': '',
            'form-TOTAL_FORMS': str(len(rows)),
            'form-INITIAL_FORMS': str(len(rows)),
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '20',
            'bulk_import_token': token,
        }
        for index, row in enumerate(rows):
            payload.update({
                f'form-{index}-title': row[0],
                f'form-{index}-description': row[1],
                f'form-{index}-trade_mode': row[2],
                f'form-{index}-price': row[3],
                f'form-{index}-deposit_amount': row[4],
                f'form-{index}-borrow_days': row[5],
                f'form-{index}-condition': row[6],
                f'form-{index}-DELETE': False,
            })
        return payload

    def test_parses_first_worksheet_numbers_into_preview_without_writing(self):
        self.client.force_login(self.seller)

        with (patch('listings.views.notify_saved_search_matches') as saved_mock,
              patch('listings.views.notify_demand_matches') as demand_mock):
            response = self._upload(make_xlsx([
                [None] * 7,
                ['数据结构教材', '考研笔记齐全', '出售', 18.5, None, None, '良好'],
                ['折叠桌', '搬离宿舍时自提', '限期借用', None, 20, 14, '轻微使用痕迹'],
            ], second_sheet_rows=[['错误行', '不应读取', '出售', 1, None, None, '良好']]))

        self.assertEqual(response.status_code, 302)
        preview = self.client.get(response['Location'])
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, '导入文件已解析出 <strong>2</strong> 件商品')
        self.assertContains(preview, '内容还没有发布')
        self.assertContains(preview, 'value="18.5"')
        self.assertContains(preview, 'value="20"')
        self.assertContains(preview, 'value="14"')
        self.assertNotContains(preview, '错误行')
        self.assertFalse(Item.objects.exists())
        self.assertEqual(saved_mock.call_count, 0)
        self.assertEqual(demand_mock.call_count, 0)

    def test_publishes_xlsx_preview_through_existing_bulk_validation(self):
        self.client.force_login(self.seller)
        upload = self._upload(make_xlsx([
            ['数据结构教材', '考研笔记齐全', '出售', 18.5, None, None, '良好'],
            ['折叠桌', '搬离宿舍时自提', '限期借用', None, 20, 14, '轻微使用痕迹'],
        ]))
        preview = self.client.get(upload['Location'])
        token = preview.context['import_token']

        with (patch('listings.views.notify_saved_search_matches') as saved_mock,
              patch('listings.views.notify_demand_matches') as demand_mock):
            response = self.client.post(self.bulk_url, self._row_payload([
                ['数据结构教材', '考研笔记齐全', 'sale', '18.5', '', '', '良好'],
                ['折叠桌', '搬离宿舍时自提', 'borrow', '', '20', '14', '轻微使用痕迹'],
            ], token=token))

        self.assertRedirects(response, reverse('bulk_listing_image_queue'))
        self.assertEqual(Item.objects.count(), 2)
        self.assertEqual(saved_mock.call_count, 2)
        self.assertEqual(demand_mock.call_count, 2)
        self.assertNotIn('listings:bulk_listing_csv_import', self.client.session)

    def test_rejects_formula_date_and_boolean_cells_without_conversion(self):
        self.client.force_login(self.seller)

        response = self._upload(make_xlsx([
            ['台灯', '宿舍使用一学期', '出售', '=25', None, None, '良好'],
            ['日历', '过期样品', '出售', date(2026, 1, 1), None, None, '旧'],
            [True, '布尔标题不应变成文字', '出售', 5, None, None, '一般'],
            [CellRichText('富文本', TextBlock(InlineFont(b=True), '标题')), '样式不应参与导入', '出售', 5, None, None, '一般'],
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Item.objects.exists())
        self.assertContains(response, '第 2 行「价格」：这一格必须是文本或数字')
        self.assertContains(response, '第 3 行「价格」：这一格必须是文本或数字')
        self.assertContains(response, '第 4 行「标题」：这一格必须是文本或数字')
        self.assertContains(response, '第 5 行「标题」：这一格必须是文本或数字')
        self.assertNotContains(response, 'value="25"')
        self.assertNotContains(response, 'value="2026-01-01"')

    def test_rejects_wrong_header_even_when_second_sheet_is_valid(self):
        self.client.force_login(self.seller)

        response = self._upload(make_xlsx(
            [['台灯', '宿舍用一学期', '出售', 25, None, None, '良好']],
            header=('title', 'description', 'trade', 'price', 'deposit', 'days', 'condition'),
        ))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'XLSX 第一个工作表表头必须固定为：标题,描述,交易方式,价格,押金,借用天数,成色')
        self.assertFalse(Item.objects.exists())

    def test_rejects_more_than_twenty_non_empty_rows(self):
        self.client.force_login(self.seller)

        response = self._upload(make_xlsx([
            [f'毕业清仓商品 {index}', '批量导入边界测试', '出售', 5, None, None, '良好']
            for index in range(21)
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'XLSX 商品行超过 20 行，请分批导入。')
        self.assertFalse(Item.objects.exists())

    def test_rejects_invalid_zip_oversized_file_and_wrong_extension(self):
        self.client.force_login(self.seller)

        invalid_buffer = BytesIO()
        with ZipFile(invalid_buffer, 'w') as archive:
            archive.writestr('not-a-workbook.txt', 'not an xlsx')
        invalid = self._upload(invalid_buffer.getvalue())
        oversized = self._upload(b'0' * (256 * 1024 + 1))
        wrong_extension = self._upload(
            '标题,描述\r\n'.encode('utf-8'), filename='graduation.txt',
        )

        self.assertContains(invalid, 'XLSX 格式无法解析')
        self.assertContains(oversized, 'XLSX 文件不能超过 256 KB')
        self.assertContains(wrong_extension, '仅支持 .csv 或 .xlsx 文件')
        self.assertFalse(Item.objects.exists())

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusCampaign, CampusLocation, Category, Item

CSV_HEADER = '标题,描述,交易方式,价格,押金,借用天数,成色'


def make_csv(rows, *, bom=False):
    content = StringIO(newline='')
    content.write(CSV_HEADER + '\r\n')
    for row in rows:
        content.write(','.join(row) + '\r\n')
    payload = content.getvalue().encode('utf-8')
    if bom:
        payload = b'\xef\xbb\xbf' + payload
    return payload


class BulkListingCsvImportTests(TestCase):
    """毕业季批量发布 CSV 导入预览的服务端行为。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='csv-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='毕业季闲置')
        self.location = CampusLocation.objects.create(name='东门快递点')
        now = timezone.now()
        self.campaign = CampusCampaign.objects.create(
            title='毕业季清仓',
            slug='graduation-csv',
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=30),
            is_active=True,
        )
        self.import_url = reverse('bulk_create_items_import')
        self.bulk_url = reverse('bulk_create_items')

    def _upload(self, content, *, filename='graduation.csv'):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile(
            filename, content, content_type='text/csv',
        )
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

    def test_anonymous_upload_and_preview_are_protected(self):
        upload = self._upload(make_csv([['台灯', '宿舍用一学期', '出售', '25.00', '', '', '良好']]))
        preview = self.client.get(
            reverse('bulk_create_items_import_preview', args=['not-a-token']),
        )

        self.assertEqual(upload.status_code, 302)
        self.assertIn('login', upload['Location'])
        self.assertEqual(preview.status_code, 302)
        self.assertIn('login', preview['Location'])

    def test_parses_bom_utf8_csv_into_editable_preview_without_writing(self):
        self.client.force_login(self.seller)

        with (patch('listings.views.notify_saved_search_matches') as saved_mock,
              patch('listings.views.notify_demand_matches') as demand_mock):
            response = self._upload(make_csv([
                ['数据结构教材', '考研笔记齐全', '出售', '18.50', '', '', '良好'],
                ['台灯', '宿舍使用一学期', '免费赠送', '', '', '', '九成新'],
                ['折叠桌', '搬离宿舍时自提', '限期借用', '', '20.00', '14', '轻微使用痕迹'],
            ], bom=True))

        self.assertEqual(response.status_code, 302)
        preview_url = response['Location']
        preview = self.client.get(preview_url)

        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, '导入文件已解析出 <strong>3</strong> 件商品')
        self.assertContains(preview, '内容还没有发布')
        self.assertContains(preview, '数据结构教材')
        self.assertContains(preview, '免费赠送')
        self.assertFalse(Item.objects.exists())
        self.assertEqual(saved_mock.call_count, 0)
        self.assertEqual(demand_mock.call_count, 0)

    def test_publishes_preview_rows_through_existing_bulk_validation(self):
        self.client.force_login(self.seller)
        upload = self._upload(make_csv([
            ['数据结构教材', '考研笔记齐全', '出售', '18.50', '', '', '良好'],
            ['折叠桌', '搬离宿舍时自提', '限期借用', '', '20.00', '14', '轻微使用痕迹'],
        ]))
        preview_url = upload['Location']
        preview = self.client.get(preview_url)
        token = preview.context['import_token']

        with (patch('listings.views.notify_saved_search_matches') as saved_mock,
              patch('listings.views.notify_demand_matches') as demand_mock):
            response = self.client.post(self.bulk_url, self._row_payload([
                ['数据结构教材', '考研笔记齐全', 'sale', '18.50', '', '', '良好'],
                ['折叠桌', '搬离宿舍时自提', 'borrow', '', '20.00', '14', '轻微使用痕迹'],
            ], token=token))

        self.assertRedirects(response, reverse('bulk_listing_image_queue'))
        self.assertEqual(Item.objects.count(), 2)
        self.assertEqual(saved_mock.call_count, 2)
        self.assertEqual(demand_mock.call_count, 2)
        self.assertNotIn('listings:bulk_listing_csv_import', self.client.session)

    def test_reports_row_and_column_errors_and_keeps_recoverable_text(self):
        self.client.force_login(self.seller)

        response = self._upload(make_csv([
            ['台灯', '宿舍使用一学期', '出出售', '25.00', '', '', '良好'],
            ['折叠桌', '搬离宿舍时自提', '出售', 'abc', '-1', '999', '轻微使用痕迹'],
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Item.objects.exists())
        self.assertContains(response, '第 2 行「交易方式」')
        self.assertContains(response, '第 3 行「价格」')
        self.assertContains(response, '第 3 行「押金」')
        self.assertContains(response, '第 3 行「借用天数」')
        self.assertContains(response, 'value="台灯"')
        self.assertContains(response, 'value="折叠桌"')

    def test_rejects_wrong_header_without_generating_rows(self):
        self.client.force_login(self.seller)

        response = self._upload('title,description\r\n台灯,宿舍用一学期\r\n'.encode('utf-8'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CSV 表头必须固定为：标题,描述,交易方式,价格,押金,借用天数,成色')
        self.assertFalse(Item.objects.exists())

    def test_rejects_non_utf8_and_oversized_files(self):
        self.client.force_login(self.seller)

        encoding = self._upload('标题,描述\r\n'.encode('gbk'))
        oversized = self._upload(b'0' * (128 * 1024 + 1))

        self.assertContains(encoding, 'CSV 必须使用 UTF-8 编码保存')
        self.assertContains(oversized, 'CSV 文件不能超过 128 KB')

    def test_rejects_more_than_twenty_non_empty_rows(self):
        self.client.force_login(self.seller)

        response = self._upload(make_csv([
            [f'毕业清仓商品 {index}', '批量导入边界测试', '出售', '5.00', '', '', '良好']
            for index in range(21)
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CSV 商品行超过 20 行，请分批导入。')
        self.assertFalse(Item.objects.exists())

    def test_expired_token_redirects_to_manual_bulk_page_without_deleting_draft(self):
        self.client.force_login(self.seller)
        upload = self._upload(make_csv([
            ['台灯', '宿舍使用一学期', '出售', '25.00', '', '', '良好'],
        ]))
        valid_url = upload['Location']

        invalid = self.client.get(
            reverse('bulk_create_items_import_preview', args=['expired-token']),
        )
        valid = self.client.get(valid_url)

        self.assertRedirects(invalid, self.bulk_url)
        followed = self.client.get(
            reverse('bulk_create_items_import_preview', args=['expired-token']), follow=True,
        )
        self.assertContains(followed, '这份导入预览已失效')
        self.assertEqual(valid.status_code, 200)
        self.assertContains(valid, '导入文件已解析出 <strong>1</strong> 件商品')

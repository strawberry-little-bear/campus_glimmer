# -*- coding: utf-8 -*-
"""图片展示契约：列表位懒加载、详情主图立即加载、srcset 必配 sizes、宽高缺失不输出无效属性。

两份设计文档把页面口径写到了具体属性：docs/item-image-display-metadata-design.md
要求商品卡片、搜索、表格、公开档案、私信预览、详情缩略图条和同类商品输出内在宽高，
非首屏语义明显的列表图片输出 loading="lazy" 与 decoding="async"，详情页主图保持
立即加载，无图占位图只补异步解码、不假装商品图片尺寸，宽高缺失时模板不输出无效属性；
docs/item-responsive-image-variants-design.md 进一步要求列表与搜索卡片输出
srcset 与 sizes，并明确「没有完整候选宽时只输出单 src，不拼不完整的 srcset」。

这些口径此前只被两处页面断言过：listings/test_item_thumbnails.py 管商品列表和
详情主图，listings/test_lost_found_image.py 管失物列表和失物详情。设计文档点名的其余
位置——我的商品表格、订单表格、详情缩略图条、同类商品、公开主页小图、私信预览、
失物发布表单——一条断言都没有。而 ItemImage 与 LostFoundImage 的宽高字段全部可空，
原图读取失败、回填中断都会留下空值，此时无条件输出会把 None 直接写进 width 属性。

静态扫模板负责覆盖没有数据、别人视角这些渲染不到的分支；渲染态负责证明条件守卫
真有数据时确实呐出属性、宽高缺失时确实没呐。
"""

import re
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from listings.models import (
    CampusLocation, Category, Item, ItemImage, LostFoundImage, LostFoundPost,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / 'templates'

IMG_TAG = re.compile(r'<img\b[^>]*>', re.I)
ATTRIBUTE = re.compile(
    r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')
EXPRESSION = re.compile(r'\{\{\s*(.+?)\s*\}\}')
IF_CONDITION = re.compile(r'\{%\s*if\s+([^%]+?)\s*%\}')
PLACEHOLDER_MARKER = 'no-image.png'
# 渲染输出里 whitenoise manifest 会把占位图变成带哈希的 URL，
# 所以渲染态断言只能匹配文件名前缀，不能依赖完整文件名。
RENDERED_PLACEHOLDER_MARKER = 'no-image'
LIST_MARKER = 'list_image_url'
HERO_MARKER = 'detail_image_url'


def photo_upload(name='photo.png', width=1600, height=1000):
    """一张纯色 PNG：结果硬定、成本低、确实可解码。"""
    image = Image.new('RGB', (width, height), '#58a')
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


def attributes(tag):
    found = {}
    for match in ATTRIBUTE.finditer(tag):
        found[match.group(1).lower()] = match.group(2).strip('"\'')
    return found


def role(tag, attrs):
    """把 <img> 分成商品图、无图占位图、与本契约无关的三类。"""
    if 'data-image-preview' in attrs:
        # 发布表单里的客户端预览，src 由 JS 在运行时改写，不参与服务端口径。
        return 'preview'
    src = attrs.get('src', '')
    if PLACEHOLDER_MARKER in src:
        return 'placeholder'
    if LIST_MARKER in src or HERO_MARKER in src:
        return 'item-image'
    return 'other'


def template_images():
    """产出 (相对路径, 行号, 标签原文, 属性, 角色)。"""
    for path in sorted(TEMPLATES_DIR.rglob('*.html')):
        text = path.read_text(encoding='utf-8')
        for match in IMG_TAG.finditer(text):
            tag = match.group(0)
            attrs = attributes(tag)
            yield (
                str(path.relative_to(PROJECT_ROOT)).replace('\\', '/'),
                text.count('\n', 0, match.start()) + 1,
                tag,
                attrs,
                role(tag, attrs),
            )


def rendered_images(response):
    """从渲染结果里抽出所有 <img> 的属性。"""
    content = response.content.decode('utf-8')
    return [attributes(match.group(0)) for match in IMG_TAG.finditer(content)]


class ItemImageDisplayContractTests(SimpleTestCase):
    """静态扫模板：契约要覆盖到没有数据、别人视角这些渲染不到的分支。"""

    @staticmethod
    def _of(kind):
        return [row for row in template_images() if row[4] == kind]

    def test_the_contract_covers_every_documented_position(self):
        """契约不能空转：设计文档点名的位置都得真的在输出商品图。"""
        templates = {row[0] for row in self._of('item-image')}
        for expected in (
            'templates/listings/_item_card.html',
            'templates/listings/_my_item_row.html',
            'templates/listings/item_detail.html',
            'templates/listings/my_orders.html',
            'templates/accounts/public_profile.html',
            'templates/chat_messages/send_message.html',
            'templates/listings/lost_found_list.html',
            'templates/listings/lost_found_detail.html',
            'templates/listings/lost_found_form.html',
        ):
            self.assertIn(
                expected, templates,
                '%s 里已经找不到商品图了。上面这些位置都是'
                ' item-image-display-metadata-design.md 和'
                ' item-responsive-image-variants-design.md 点名要求输出宽高、'
                '懒加载和异步解码的页面；模板被改名或'
                '移走之后，静态扫描会安静地少扫一个页面。' % expected)
        self.assertGreaterEqual(
            len(self._of('placeholder')), 3,
            '无图占位图不到 3 处。占位图的口径是「不假装商品'
            '图片尺寸」，扫不到它们，那条断言就是空转。')

    def test_item_images_reserve_space_only_when_metadata_exists(self):
        """宽高必须带 {% if %} 守卫：缺失时整个属性都不出现，而不是渲染出 width="None"。

        ItemImage 与 LostFoundImage 的四个宽高字段全部可空；设计文档
        item-image-display-metadata-design.md 的目标 5 写的就是「宽高缺失时模板
        不输出无效属性」。
        """
        problems = []
        for path, line, tag, _attrs, _kind in self._of('item-image'):
            for name in ('width', 'height'):
                match = re.search(
                    r'\b%s\s*=\s*"\{\{\s*(.+?)\s*\}\}"' % name, tag)
                if match is None:
                    continue
                expression = match.group(1).strip()
                # 守卫条件里写的是 _width，同一个 {% if %} 还带着 _height，
                # 所以按去掉后缀的家族前缀比对，而不是逐字比对整个表达式。
                family = re.sub(r'_width$|_height$', '', expression)
                guarded = any(
                    family in condition
                    for condition in IF_CONDITION.findall(tag)
                )
                if not guarded:
                    problems.append(
                        '%s:%d 的 %s="%s" 没有条件守卫'
                        % (path, line, name, expression))
        self.assertEqual(
            problems, [],
            '商品图的宽高必须带 {% if %} 守卫。ItemImage 与 LostFoundImage 的'
            '\u5bbd\u9ad8\u5b57\u6bb5\u5168\u90e8\u53ef\u7a7a\uff0c\u539f\u56fe\u8bfb\u53d6\u5931\u8d25\u3001\u56de\u586b\u4e2d\u65ad\u90fd\u4f1a\u7559\u4e0b\u7a7a\u503c\uff1b'
            '\u8bbe\u8ba1\u6587\u6863\u8981\u6c42\u300c\u5bbd\u9ad8\u7f3a\u5931\u65f6\u4e0d\u8f93\u51fa\u65e0\u6548\u5c5e\u6027\u300d\uff0c'
            '\u65e0\u6761\u4ef6\u8f93\u51fa\u4f1a\u628a None \u76f4\u63a5\u5199\u8fdb width \u5c5e\u6027\u3002\n'
            + '\n'.join(problems))

    def test_item_images_decode_asynchronously(self):
        problems = [
            '%s:%d \u7f3a decoding="async"' % (path, line)
            for path, line, tag, attrs, _kind in self._of('item-image')
            if attrs.get('decoding') != 'async'
        ]
        self.assertEqual(
            problems, [],
            '\u5546\u54c1\u56fe\u8981\u5e26 decoding="async"\u3002\u89e3\u7801\u653e\u5230\u4e3b\u7ebf\u7a0b\u4e4b\u5916\uff0c'
            '\u5217\u8868\u9875\u56fe\u591a\u7684\u65f6\u5019\u9996\u5c4f\u624d\u4e0d\u4f1a\u88ab\u4e00\u5c4f\u56fe\u7247\u7684\u89e3\u7801\u5361\u4f4f\u3002\n'
            + '\n'.join(problems))

    def test_list_positions_lazy_load_and_heroes_stay_eager(self):
        """\u5217\u8868\u4f4d\u61d2\u52a0\u8f7d\uff0c\u8be6\u60c5\u4e3b\u56fe\u7acb\u5373\u52a0\u8f7d\u3002

        \u4e24\u8fb9\u90fd\u4e0d\u80fd\u677e\uff1b\u5217\u8868\u56fe\u7247\u5168\u90e8\u7acb\u5373\u89e3\u7801\u4f1a\u8ba9\u9996\u5c4f\u5916\u7684\u56fe\u62a2\u5360\u7f51\u7edc\u548c\u89e3\u7801\u8d44\u6e90\uff0c
        \u800c\u7ed9\u4e3b\u56fe\u52a0 loading="lazy" \u4f1a\u628a\u7528\u6237\u6b63\u5728\u770b\u7684\u90a3\u5f20\u56fe\u63a8\u540e\u3002
        """
        problems = []
        for path, line, tag, attrs, _kind in self._of('item-image'):
            src = attrs.get('src', '')
            lazy = attrs.get('loading') == 'lazy'
            if LIST_MARKER in src and not lazy:
                problems.append('%s:%d \u662f\u5217\u8868\u4f4d\u56fe\u7247\uff0c\u7f3a loading="lazy"' % (path, line))
            if HERO_MARKER in src and lazy:
                problems.append('%s:%d \u662f\u8be6\u60c5\u4e3b\u56fe\uff0c\u4e0d\u80fd\u52a0 loading="lazy"' % (path, line))
        self.assertEqual(
            problems, [],
            '\u5217\u8868\u4f4d\u5546\u54c1\u56fe\u8981\u5e26 loading="lazy"\uff0c\u8be6\u60c5\u4e3b\u56fe\u8981\u4fdd\u6301\u7acb\u5373\u52a0\u8f7d\u3002'
            '\u4e24\u8fb9\u90fd\u4e0d\u80fd\u677e\uff1b\u8bbe\u8ba1\u6587\u6863 item-image-display-metadata-design.md \u5199\u7684\u662f'
            '\u300c\u8be6\u60c5\u9875\u4e3b\u56fe\u4fdd\u6301\u7acb\u5373\u52a0\u8f7d\uff0c\u4e0d\u6dfb\u52a0 loading=\"lazy\"\uff0c'
            '\u907f\u514d\u5f71\u54cd\u7528\u6237\u6b63\u5728\u67e5\u770b\u7684\u5546\u54c1\u4e3b\u4f53\u300d\u3002\n'
            + '\n'.join(problems))

    def test_srcset_never_ships_without_a_sizes_hint(self):
        problems = [
            '%s:%d \u6709 srcset \u5374\u6ca1\u6709 sizes' % (path, line)
            for path, line, tag, attrs, _kind in template_images()
            if 'srcset' in attrs and 'sizes' not in attrs
        ]
        self.assertEqual(
            problems, [],
            'srcset \u5fc5\u987b\u914d sizes\u3002\u6ca1\u6709 sizes \u65f6\u6d4f\u89c8\u5668\u9ed8\u8ba4\u6309 sizes="100vw" \u9009\u5019\uff0c'
            '\u5361\u7247\u53ea\u663e\u793a\u51e0\u767e\u50cf\u7d20\u4e5f\u4f1a\u53d6\u5230\u6700\u5927\u7684\u5019\u9009\uff0c'
            '\u6070\u6070\u662f\u8fd9\u4e24\u4efd\u8bbe\u8ba1\u6587\u6863\u60f3\u907f\u514d\u7684\u90a3\u79cd\u7ed3\u679c\u3002\n'
            + '\n'.join(problems))

    def test_placeholders_never_fake_item_dimensions(self):
        problems = []
        for path, line, tag, attrs, _kind in self._of('placeholder'):
            for name in ('width', 'height'):
                if name in attrs:
                    problems.append('%s:%d \u7684\u5360\u4f4d\u56fe\u5047\u88c5\u4e86 %s' % (path, line, name))
        self.assertEqual(
            problems, [],
            '\u65e0\u56fe\u5360\u4f4d\u56fe\u4e0d\u80fd\u5e26 width / height\u3002\u8bbe\u8ba1\u6587\u6863\u5199\u7684\u662f\u300c\u65e0\u56fe\u5360\u4f4d\u56fe'
            '\u53ea\u8865\u5145\u5f02\u6b65\u89e3\u7801\uff0c\u4e0d\u5047\u88c5\u5546\u54c1\u56fe\u7247\u5c3a\u5bf8\u300d\u2014\u2014\u5360\u4f4d\u56fe\u662f\u7ed9\u6240\u6709\u5546\u54c1'
            '\u5171\u7528\u7684\u540c\u4e00\u5f20 png\uff0c\u5199\u6b7b\u5c3a\u5bf8\u53ea\u4f1a\u8ba9\u771f\u56fe\u51fa\u73b0\u65f6\u6392\u7248\u548c\u5360\u4f4d\u56fe\u4e0d\u4e00\u81f4\u3002\n'
            + '\n'.join(problems))


class ItemImageDisplayRenderedTests(TestCase):
    """\u6e32\u67d3\u6001\uff1a\u8bc1\u660e\u6761\u4ef6\u5b88\u536b\u771f\u6709\u6570\u636e\u65f6\u5450\u51fa\u5c5e\u6027\u3001\u5bbd\u9ad8\u7f3a\u5931\u65f6\u786e\u5b9e\u6ca1\u5450\u3002"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='display-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='\u5c55\u793a\u5951\u7ea6\u5206\u7c7b')
        self.location = CampusLocation.objects.create(name='\u5c55\u793a\u5951\u7ea6\u5730\u70b9')
        self.item = Item.objects.create(
            title='\u5c55\u793a\u5951\u7ea6\u5546\u54c1', description='\u7528\u4e8e\u56fe\u7247\u5c55\u793a\u5951\u7ea6',
            price='30.00', category=self.category, condition='\u4e5d\u6210\u65b0',
            location=self.location, seller=self.seller,
        )
        self.photo = ItemImage.objects.create(
            item=self.item, image=photo_upload(name='display-cover.png'),
        )
        self.second = ItemImage.objects.create(
            item=self.item, image=photo_upload(name='display-second.png'),
        )
        self.related = Item.objects.create(
            title='\u5c55\u793a\u5951\u7ea6\u540c\u7c7b\u5546\u54c1', description='\u7528\u4e8e\u540c\u7c7b\u5546\u54c1\u56fe\u7247\u5951\u7ea6',
            price='18.00', category=self.category, condition='\u516b\u6210\u65b0',
            location=self.location, seller=self.seller,
        )
        self.related_photo = ItemImage.objects.create(
            item=self.related, image=photo_upload(name='display-related.png'),
        )

    def test_my_items_rows_reserve_space_and_lazy_load(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        matches = [
            attrs for attrs in rendered_images(response)
            if attrs.get('src') == self.photo.list_image_url
        ]
        self.assertEqual(
            len(matches), 1,
            '\u6211\u7684\u5546\u54c1\u8868\u683c\u91cc\u5e94\u8be5\u6b63\u597d\u51fa\u73b0\u4e00\u6b21\u8fd9\u4ef6\u5546\u54c1\u7684\u7f29\u7565\u56fe\u3002'
            'item-image-display-metadata-design.md \u628a\u300c\u6211\u7684\u5546\u54c1\u300d\u5217\u8fdb'
            '\u4e86\u8981\u6c42\u8f93\u51fa\u5c0f\u56fe URL \u4e0e\u5bbd\u9ad8\u7684\u4f4d\u7f6e\u3002')
        self.assertEqual(matches[0].get('width'), '480')
        self.assertEqual(matches[0].get('height'), '300')
        self.assertEqual(matches[0].get('loading'), 'lazy')
        self.assertEqual(matches[0].get('decoding'), 'async')

    def test_my_items_rows_stay_dimensionless_when_metadata_is_missing(self):
        """\u5bbd\u9ad8\u7f3a\u5931\u65f6\u6574\u4e2a\u5c5e\u6027\u90fd\u4e0d\u51fa\u73b0\uff0c\u56fe\u7247\u4ecd\u6309\u539f\u8def\u5f84\u56de\u9000\u5c55\u793a\u3002"""
        ItemImage.objects.filter(pk=self.photo.pk).update(
            thumbnail_width=None, thumbnail_height=None,
            image_width=None, image_height=None,
        )
        self.client.force_login(self.seller)
        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertNotIn('width="None"', content)
        self.assertNotIn('height="None"', content)
        self.assertIn(
            'src="%s"' % self.photo.list_image_url, content,
            '\u5bbd\u9ad8\u7f3a\u5931\u4e0d\u8be5\u628a\u56fe\u7247\u81ea\u5df1\u4e5f\u5e26\u8d70\uff1b\u56de\u9000\u5230\u5355 src \u5c31\u597d\u3002')
        matches = [
            attrs for attrs in rendered_images(response)
            if attrs.get('src') == self.photo.list_image_url
        ]
        self.assertEqual(len(matches), 1)
        self.assertNotIn('width', matches[0])
        self.assertNotIn('height', matches[0])

    def test_placeholder_rows_never_fake_item_dimensions(self):
        bare = Item.objects.create(
            title='\u5c55\u793a\u5951\u7ea6\u65e0\u56fe\u5546\u54c1', description='\u6ca1\u6709\u56fe\u7247\u7684\u5546\u54c1',
            price='12.00', category=self.category, condition='\u4e5d\u6210\u65b0',
            location=self.location, seller=self.seller,
        )
        self.client.force_login(self.seller)
        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        placeholders = [
            attrs for attrs in rendered_images(response)
            if RENDERED_PLACEHOLDER_MARKER in attrs.get('src', '')
        ]
        self.assertTrue(
            placeholders,
            '\u65b0\u5efa\u4e86\u4e00\u4ef6\u6ca1\u6709\u56fe\u7247\u7684\u5546\u54c1\uff0c\u6211\u7684\u5546\u54c1\u9875\u5374\u6ca1\u51fa\u73b0\u5360\u4f4d\u56fe\u3002')
        for attrs in placeholders:
            self.assertNotIn('width', attrs, '\u5360\u4f4d\u56fe\u4e0d\u5f97\u5047\u88c5\u5546\u54c1\u56fe\u7247\u5c3a\u5bf8\u3002')
            self.assertNotIn('height', attrs, '\u5360\u4f4d\u56fe\u4e0d\u5f97\u5047\u88c5\u5546\u54c1\u56fe\u7247\u5c3a\u5bf8\u3002')
            self.assertEqual(attrs.get('decoding'), 'async')

    def test_detail_thumbnails_and_related_items_lazy_load_while_the_hero_stays_eager(self):
        response = self.client.get(reverse('item_detail', args=[self.item.id]))

        self.assertEqual(response.status_code, 200)
        images = rendered_images(response)
        heroes = [attrs for attrs in images if attrs.get('id') == 'mainProductImage']
        self.assertEqual(len(heroes), 1, '\u8be6\u60c5\u9875\u53ea\u5e94\u8be5\u6709\u4e00\u5f20\u4e3b\u56fe\u3002')
        self.assertEqual(heroes[0].get('width'), '960')
        self.assertEqual(heroes[0].get('height'), '600')
        self.assertEqual(heroes[0].get('decoding'), 'async')
        self.assertNotIn(
            'loading', heroes[0],
            '\u8be6\u60c5\u4e3b\u56fe\u5fc5\u987b\u7acb\u5373\u52a0\u8f7d\u3002\u7ed9\u5b83\u52a0 loading="lazy" \u4f1a\u628a\u7528\u6237'
            '\u6b63\u5728\u770b\u7684\u90a3\u5f20\u56fe\u63a8\u5230\u89c6\u53e3\u4e4b\u540e\u3002')

        list_sources = {
            self.photo.list_image_url,
            self.second.list_image_url,
            self.related_photo.list_image_url,
        }
        lazy_rows = [
            attrs for attrs in images
            if attrs.get('src') in list_sources
        ]
        self.assertEqual(
            len(lazy_rows), 3,
            '\u8be6\u60c5\u9875\u4e0a\u5e94\u8be5\u6709\u4e24\u5f20\u7f29\u7565\u56fe\u548c\u4e00\u5f20\u540c\u7c7b\u5546\u54c1\u5c0f\u56fe\u3002'
            'item-image-display-metadata-design.md \u628a\u8be6\u60c5\u7f29\u7565\u56fe\u6761\u548c\u540c\u7c7b\u5546\u54c1'
            '\u90fd\u5217\u8fdb\u4e86\u8981\u6c42\u8f93\u51fa\u5bbd\u9ad8\u7684\u4f4d\u7f6e\u3002')
        for attrs in lazy_rows:
            self.assertEqual(attrs.get('width'), '480')
            self.assertEqual(attrs.get('height'), '300')
            self.assertEqual(attrs.get('loading'), 'lazy')
            self.assertEqual(attrs.get('decoding'), 'async')

    def _lost_found_post(self):
        return LostFoundPost.objects.create(
            reporter=self.seller, post_type='found',
            title='\u5c55\u793a\u5951\u7ea6\u5931\u7269', description='\u7528\u4e8e\u5931\u7269\u8868\u5355\u56fe\u7247\u5951\u7ea6',
            category=self.category, location=self.location,
            occurred_at=timezone.now() - timedelta(hours=2),
            expires_at=timezone.now() + timedelta(days=5),
        )

    def test_the_lost_found_editor_reserves_space_when_metadata_exists(self):
        post = self._lost_found_post()
        photo = LostFoundImage.objects.create(
            post=post, image=photo_upload(name='lost-editor.png'),
        )
        self.client.force_login(self.seller)
        response = self.client.get(reverse('edit_lost_found', args=[post.id]))

        self.assertEqual(response.status_code, 200)
        matches = [
            attrs for attrs in rendered_images(response)
            if attrs.get('src') == photo.list_image_url
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].get('width'), '480')
        self.assertEqual(matches[0].get('height'), '300')
        self.assertEqual(matches[0].get('loading'), 'lazy')
        self.assertEqual(matches[0].get('decoding'), 'async')

    def test_the_lost_found_editor_does_not_render_invalid_dimensions(self):
        """\u5bbd\u9ad8\u7f3a\u5931\u65f6\u4e0d\u5f97\u628a None \u5199\u8fdb width \u5c5e\u6027\u3002

        \u8fd9\u91cc\u662f\u771f\u6b63\u7684\u7f3a\u53e3\uff1a\u5931\u7269\u53d1\u5e03\u8868\u5355\u91cc\u90a3\u5f20\u5f53\u524d\u7167\u7247\u7684\u5bbd\u9ad8\u662f\u65e0\u6761\u4ef6\u8f93\u51fa\u7684\uff0c
        LostFoundImage \u7684\u5bbd\u9ad8\u5b57\u6bb5\u53ef\u7a7a\uff0c\u4e00\u65e6\u4e3a\u7a7a\u5c31\u4f1a\u6e32\u67d3\u51fa width="None"\u3002
        """
        post = self._lost_found_post()
        photo = LostFoundImage.objects.create(
            post=post, image=photo_upload(name='lost-editor-bare.png'),
        )
        LostFoundImage.objects.filter(pk=photo.pk).update(
            thumbnail_width=None, thumbnail_height=None,
            image_width=None, image_height=None,
        )
        self.client.force_login(self.seller)
        response = self.client.get(reverse('edit_lost_found', args=[post.id]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertNotIn(
            'width="None"', content,
            '\u5931\u7269\u53d1\u5e03\u8868\u5355\u628a None \u5199\u8fdb\u4e86 width \u5c5e\u6027\u3002LostFoundImage \u7684\u5bbd\u9ad8\u5b57\u6bb5'
            '\u53ef\u7a7a\uff0c\u539f\u56fe\u8bfb\u53d6\u5931\u8d25\u3001\u56de\u586b\u4e2d\u65ad\u90fd\u4f1a\u7559\u4e0b\u7a7a\u503c\uff1b'
            'item-image-display-metadata-design.md \u7684\u76ee\u6807 5 \u8981\u6c42\u300c\u5bbd\u9ad8\u7f3a\u5931\u65f6\u6a21\u677f'
            '\u4e0d\u8f93\u51fa\u65e0\u6548\u5c5e\u6027\u300d\u3002')
        self.assertNotIn('height="None"', content)
        self.assertIn(
            'src="%s"' % photo.list_image_url, content,
            '\u5bbd\u9ad8\u7f3a\u5931\u4e0d\u8be5\u628a\u7167\u7247\u81ea\u5df1\u4e5f\u5e26\u8d70\uff1b\u56de\u9000\u5230\u5355 src \u5c31\u597d\u3002')

# -*- coding: utf-8 -*-
"""页内跳转目标必须让出吸顶导航的高度。

吸顶导航高 84px，任何页内跳转——skip-link、表单错误摘要里的字段链接、运营看板
的 chip、专题商品分页的锚点——只要落点没有预留空间，目标元素就会正好停在导航底
下：用户点了链接，却看不到自己跳到了哪里。

这件事反复踩过三次，每次都是「页面不报错、只有截图才看得出来」：

1. 运营看板的 38 个分区。分区是 ``<section class="ops-section">``，加了
   ``.ops-jump`` 吸顶跳转条之后忘了给 scroll-margin-top，点 chip 跳下去只见
   导航不见标题。搜索洞察的 15 个分区是同一套机制，一起补的。
2. 专题详情页的商品区分页。``?page=2#campaign-items`` 翻页后回到页首，商品区
   标题被导航盖住。
3. 表单错误摘要。``includes/form_error_summary.html`` 被 17 个模板 include，
   校验失败后每条错误都是指向字段 id 的链接，点下去输入框整个在导航底下——
   而这恰恰是用户唯一需要看见的东西。

于是把避让量收成一个常量：104px = 84px 导航 + 20px 间隙。全站跳转目标共用这一
个数，由这个文件逐条盯住：

* StaticAnchorReservationTests 扫静态 ``href="#x"``（含 ``?page=2#x`` 形式）和
  ``pagination_anchor='x'``，要求定义该 id 的元素带上能命中 scroll-margin-top
  的类名或 id 选择器。
* DynamicHashLinkTests 把「运行时才拼出 hash」的模板收进一张登记表，新增一个
  动态 hash 链接必须在这里说明落点凭什么安全，否则测试直接失败。
* FormWidgetReservationTests 把三个 app 的表单全部真的渲染一遍，收集控件上真实
  出现的类名，要求每个控件都带着会避让的类——错误摘要指向的就是这些 id。

刻意不做的两件事：错误页（``errors/base_error.html``）不进检查，它没有吸顶导航；
``.ops-section`` 用 140px 而不是 104px，因为它还要避开自己上方那条吸顶跳转条，
所以这里只要求「不小于 104px」，不要求「等于 104px」。
"""

import io
import os
import re
from pathlib import Path

from django import forms
from django.forms import BaseForm
from django.test import SimpleTestCase, TestCase

from listings.operations_nav import OPERATIONS_DASHBOARD_SECTIONS

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = Path(PROJECT_ROOT) / 'templates'
STYLESHEET = os.path.join(PROJECT_ROOT, 'static', 'css', 'style.css')

NAV_HEIGHT_PX = 84
GAP_PX = 20
SCROLL_MARGIN_PX = NAV_HEIGHT_PX + GAP_PX
ROOT_FONT_SIZE_PX = 16

HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)
TEMPLATE_SYNTAX = re.compile(r'\{%[^%]*%\}|\{\{[^}]*\}\}')
TAG_OPEN = re.compile(r'<[a-zA-Z][^<>]*>')
ATTR = re.compile(r'([a-zA-Z-]+)="([^"]*)"')
CSS_RULE = re.compile(r'([^{}]+)\{([^{}]*)\}')
SCROLL_MARGIN = re.compile(r'scroll-margin-top\s*:\s*([0-9.]+)(px|rem)')
HREF = re.compile(r'href="([^"]*)"')
PAGINATION_ANCHOR = re.compile(r"pagination_anchor='([^']+)'")
DYNAMIC_HASH_HREF = re.compile(r'href="[^"]*#[^"]*\{\{[^}]*\}\}[^"]*"')
HTML_CLASS = re.compile(r'class="([^"]*)"')

# 错误页没有吸顶导航：它用 error-header，不是 .site-nav，跳过去本来就在文档顶部，
# 再预留 104px 只会在页首留一片空白。
NON_STICKY_PAGES = frozenset({'errors/base_error.html'})

# 运行时才拼出 hash 的模板。新增一条动态 hash 链接必须登记在这里，写清落点是什
# 么、凭什么安全；没登记的直接让测试失败。
DYNAMIC_HASH_PAGES = {
    'listings/operations_dashboard.html':
        '落点是 38 个 <section class="ops-section">，由 .ops-section 规则避让'
        '（140px，因为还要避开上方那条吸顶跳转条）',
    'includes/form_error_summary.html':
        '落点是字段 id，由 FormWidgetReservationTests 渲染全部表单盯住',
    'listings/_personal_queue_pagination.html':
        '落点是调用方用 pagination_anchor 传进来的字面量，全站只有 '
        'my_campaigns.html 传了 campaign-history，由 id 选择器避让',
}


def _read(path):
    return io.open(path, encoding='utf-8').read()


def _without_comments(text):
    """把注释换成等长空白，行号和列号都不变。"""
    return HTML_COMMENT.sub(lambda match: re.sub(r'[^\n]', ' ', match.group(0)), text)


def _css_reservations():
    """收集样式表里已经预留了吸顶导航空间的 id 和类名。"""
    covered_ids, covered_classes = set(), set()
    stylesheet = CSS_COMMENT.sub(' ', _read(STYLESHEET))
    for rule in CSS_RULE.finditer(stylesheet):
        declaration = SCROLL_MARGIN.search(rule.group(2))
        if declaration is None:
            continue
        pixels = float(declaration.group(1))
        if declaration.group(2) == 'rem':
            pixels *= ROOT_FONT_SIZE_PX
        if pixels < SCROLL_MARGIN_PX:
            continue
        for raw_selector in rule.group(1).split(','):
            selector = ' '.join(raw_selector.split())
            if selector.startswith('@'):
                # @media 之类的前缀：CSS_RULE 会把整段媒体查询吃进选择器里。
                continue
            # 只认最简单的选择器——一个 id 或一个类。后代、伪类、属性选择器在这里
            # 一律不算覆盖：宁可误报让人来补，也不要漏放一个盖在导航底下的落点。
            if re.fullmatch(r'#[\w-]+', selector):
                covered_ids.add(selector[1:])
            elif re.fullmatch(r'\.[\w-]+', selector):
                covered_classes.add(selector[1:])
    return covered_ids, covered_classes


def _id_occurrences(text):
    """返回 {id: [(类名列表, 行号)]}，只收字面量 id。"""
    found = {}
    for tag in TAG_OPEN.finditer(_without_comments(text)):
        attrs = dict(ATTR.findall(tag.group(0)))
        node_id = attrs.get('id', '').strip()
        if not node_id or TEMPLATE_SYNTAX.search(node_id):
            continue
        line = text.count('\n', 0, tag.start()) + 1
        found.setdefault(node_id, []).append((attrs.get('class', '').split(), line))
    return found


def _templates():
    return sorted(TEMPLATES_DIR.rglob('*.html'))


def _class_tokens(html):
    return {token for match in HTML_CLASS.finditer(html) for token in match.group(1).split()}


def _reserved(node_id, class_names, covered_ids, covered_classes):
    """这个元素是否已经被某条 scroll-margin-top 规则覆盖到。"""
    if node_id in covered_ids:
        return True
    return bool(set(class_names) & covered_classes)


class StaticAnchorReservationTests(SimpleTestCase):
    """静态写死的页内锚点，落点必须预留吸顶导航空间。"""

    def test_every_static_anchor_target_reserves_room(self):
        covered_ids, covered_classes = _css_reservations()
        definitions = {}
        for path in _templates():
            relative = path.relative_to(TEMPLATES_DIR).as_posix()
            for node_id, occurrences in _id_occurrences(_read(path)).items():
                definitions.setdefault(node_id, []).append((relative, occurrences))

        problems = []
        for path in _templates():
            relative = path.relative_to(TEMPLATES_DIR).as_posix()
            text = _without_comments(_read(path))
            anchors = set()
            for match in HREF.finditer(text):
                href = match.group(1)
                if '#' not in href:
                    continue
                # rsplit 一次就同时吃下 "#x" 和 "?page=2#x" 两种写法。
                fragment = href.rsplit('#', 1)[1]
                if fragment and not TEMPLATE_SYNTAX.search(fragment):
                    anchors.add(fragment)
            anchors.update(PAGINATION_ANCHOR.findall(text))

            for anchor in sorted(anchors):
                targets = definitions.get(anchor)
                if not targets:
                    problems.append('%s 跳到 #%s，但全站没有一个元素带这个 id' % (relative, anchor))
                    continue
                for target_relative, occurrences in targets:
                    if target_relative in NON_STICKY_PAGES:
                        continue
                    for class_names, line in occurrences:
                        if _reserved(anchor, class_names, covered_ids, covered_classes):
                            continue
                        problems.append(
                            '%s 跳到 #%s，落点在 %s:%d（class="%s"），'
                            '没有 scroll-margin-top，会被 84px 的吸顶导航盖住'
                            % (relative, anchor, target_relative, line, ' '.join(class_names)),
                        )

        self.assertEqual(
            problems, [],
            '以下页内跳转的落点没有让出吸顶导航的高度：\n'
            + '\n'.join('  ' + item for item in problems),
        )

    def test_the_reserved_baseline_is_actually_declared(self):
        # 反方向：把常量改大了却没人跟着改样式表，上面那条会全站报警，报错信息很
        # 难定位。这里先把「基准值确实存在于样式表里」钉住。
        covered_ids, covered_classes = _css_reservations()
        self.assertIn('page-shell', covered_classes, '基础页面容器没有预留避让空间')
        self.assertIn('ops-section', covered_classes, '运营/搜索洞察分区没有预留避让空间')
        for field_class in ('form-control', 'form-select', 'form-check-input', 'form-range'):
            self.assertIn(field_class, covered_classes, '表单控件 %s 没有预留避让空间' % field_class)
        for anchor in ('campaign-items', 'campaign-history', 'submit-lead'):
            self.assertIn(anchor, covered_ids, '锚点 #%s 没有预留避让空间' % anchor)


class DynamicHashLinkTests(SimpleTestCase):
    """运行时才拼出 hash 的链接，必须登记在案。"""

    def _pages_with_dynamic_hash(self):
        return {
            path.relative_to(TEMPLATES_DIR).as_posix()
            for path in _templates()
            if DYNAMIC_HASH_HREF.search(_without_comments(_read(path)))
        }

    def test_every_runtime_hash_link_is_registered(self):
        unregistered = sorted(self._pages_with_dynamic_hash() - set(DYNAMIC_HASH_PAGES))

        self.assertEqual(
            unregistered, [],
            '以下模板在运行时才拼出页内 hash，没有登记落点：\n'
            + '\n'.join('  ' + item for item in unregistered)
            + '\n把它的落点和避让方式写进 DYNAMIC_HASH_PAGES，再补对应断言。',
        )

    def test_the_registry_has_no_stale_entries(self):
        stale = sorted(set(DYNAMIC_HASH_PAGES) - self._pages_with_dynamic_hash())

        self.assertEqual(stale, [], '登记表里有模板已经不再拼 hash：' + ', '.join(stale))

    def test_dashboard_jump_targets_reserve_room(self):
        covered_ids, covered_classes = _css_reservations()
        source = _read(os.path.join(TEMPLATES_DIR, 'listings', 'operations_dashboard.html'))
        sections = _id_occurrences(source)

        problems = []
        anchors = [
            anchor for _group, items in OPERATIONS_DASHBOARD_SECTIONS
            for anchor, _label in items
        ]
        for anchor in sorted(anchors):
            occurrences = sections.get(anchor)
            if not occurrences:
                problems.append('跳转条登记了 #%s，模板里却没有这个分区' % anchor)
                continue
            for class_names, line in occurrences:
                if _reserved(anchor, class_names, covered_ids, covered_classes):
                    continue
                problems.append(
                    '#%s（operations_dashboard.html:%d，class="%s"）没有 scroll-margin-top，'
                    '点 chip 跳下去分区标题会被吸顶导航盖住'
                    % (anchor, line, ' '.join(class_names)),
                )

        self.assertEqual(
            problems, [],
            '运营看板的分区落点没有让出吸顶导航的高度：\n'
            + '\n'.join('  ' + item for item in problems),
        )


class FormWidgetReservationTests(TestCase):
    """表单控件的类名必须让它避开吸顶导航。

    表单错误摘要里每条错误都是指向字段 id 的链接，用户点下去就是要改那个输入框。
    控件上没有一个带 scroll-margin-top 的类，输入框就整个停在导航底下。
    """

    def _form_classes(self):
        # 延迟导入：测试模块在 app 注册完成前就会被 Django 加载。
        from accounts import forms as accounts_forms
        from chat_messages import forms as chat_messages_forms
        from listings import forms as listings_forms

        return (listings_forms, accounts_forms, chat_messages_forms)

    def _form_classes_by_module(self):
        classes = []
        for module in self._form_classes():
            for name in sorted(dir(module)):
                candidate = getattr(module, name)
                if not isinstance(candidate, type) or not issubclass(candidate, BaseForm):
                    continue
                if candidate.__module__ != module.__name__:
                    continue
                classes.append((module.__name__, name, candidate))
        return classes

    def test_every_form_control_reserves_room_for_the_sticky_nav(self):
        covered_ids, covered_classes = _css_reservations()
        unrenderable, uncovered = [], []

        for module_name, class_name, form_class in self._form_classes_by_module():
            try:
                form = form_class()
            except Exception as error:
                # 需要位置参数的表单没法在测试里裸构造，点名让人来补默认值。
                unrenderable.append('%s.%s：%s: %s' % (
                    module_name, class_name, type(error).__name__, error))
                continue
            for field_name, field in form.fields.items():
                tokens = _class_tokens(str(field.widget.render(field_name, field.initial)))
                if not tokens & covered_classes:
                    uncovered.append('%s.%s.%s -> %s' % (
                        module_name, class_name, field_name,
                        ' '.join(sorted(tokens)) or '无 class'))

        self.assertEqual(
            uncovered, [],
            '以下表单控件没有带任何会避让吸顶导航的类名，错误摘要点过去会被导航盖住：\n'
            + '\n'.join('  ' + item for item in uncovered),
        )
        self.assertEqual(
            unrenderable, [],
            '以下表单无法在测试里裸构造，给它们补默认值，或在本文件里显式登记：\n'
            + '\n'.join('  ' + item for item in unrenderable),
        )

    def test_select_widgets_render_as_bootstrap_selects(self):
        # 顺手钉住一个真实的不一致：SavedSearchForm.notify_frequency 是下拉框，却
        # 因为字段写在表单体里、没走 Meta.widgets，挂成了 form-control——没有下拉
        # 箭头，用户看不出来这里能点开。现在 _style_fields 按控件类型给默认类。
        problems = []
        for module_name, class_name, form_class in self._form_classes_by_module():
            try:
                form = form_class()
            except Exception:
                continue
            for field_name, field in form.fields.items():
                if not isinstance(field.widget, forms.Select):
                    continue
                tokens = _class_tokens(str(field.widget.render(field_name, field.initial)))
                if 'form-select' not in tokens:
                    problems.append('%s.%s.%s -> %s' % (
                        module_name, class_name, field_name, ' '.join(sorted(tokens))))

        self.assertEqual(
            problems, [],
            '以下下拉框没有用 form-select，渲染出来没有下拉箭头：\n'
            + '\n'.join('  ' + item for item in problems),
        )

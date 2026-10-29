"""页面语义守卫：标题层级、表单标签、可访问名称、表格名称、分页名称、图片替代文本。

这一层以前靠浏览器审计兜。浏览器审计有两个绕不开的代价：一是要起真实服务、
装驱动、逐页访问，跑一轮以小时计；二是它只看得到当前数据恰好渲染出来的分支，
没数据的空状态、别人的视角、只有发布者看得见的区块，全都测不到。

于是真实缺陷会以很低的概率漏过去：商品详情页用 h6 写「商品描述」，在首页到
h1 之后直接跳了五级；卡片标题用 h3，让整个列表页从 h1 跳到 h3；八处空状态
提示用 h3，读屏软件里跟同级正文标题混在一起。这些问题在页面上看不出来，
在 DOM 里一目了然。

这里用 Django test client 渲染同一批页面，再配一个 stdlib 的轻量 DOM 做同样的
检查。没有新依赖，跑起来是秒级，可以挂在每次提交上。

六类检查各有分工：

1. TemplateHeadingOrderTests 只读模板文件，不渲染。它能在没有数据、没有请求的
   情况下发现标题跳级，覆盖浏览器审计够不到的分支（空状态、他人视角）。
2. RenderedPageSemanticsTests 用一套演示数据把主要页面都渲染一遍，检查表单
   控件有没有标签、可交互元素有没有可访问名称、图片有没有 alt，以及 id 引用
   是否完整。前两项只有在真实 DOM 里才查得出来，静态扫描给不出答案。
3. TouchTargetTests 盯导航链接的触控尺寸。鼠标点得中的东西手指不一定点得中，
   而导航是唯一出现在每一页上的交互元素。
4. TableAccessibleNameTests 扫模板源，要求每张 <table> 都有可访问名称。三百多
   张表里运营看板占了大半，且大量挂在 {% if has_data %} 分支上，渲染态检查只能
   点亮演示数据恰好命中的一百多张，只有静态扫才覆盖得到空状态和他人视角。
5. PaginationAccessibleNameTests 扫模板源，要求每个分页链接都读得出名字、
   当前页带 aria-current="page"。分页按钮长得都一样——一个图标或一串数字，
   同页出现多个分页器时更是完全无法分辨；而它只在数据够多页时才渲染出来，
   演示数据不特意造够页数就照不到，只能静态扫。
6. GridFirstCellTests 约束窄首列网格的首格内容。第一轨写成
   42px / 72px / 89.6px 这类固定长度时，首格自然是图标、时间戳、序号这类一两个字的东西；
   模板长大后有人往同一行塞九字标签，轨道放不下就折行，行高从 20px 涨到 59px。
   浏览器审计要逐页逐宽度量才抓得到，这里改成扫样式表 + 模板源。

第 2 类里的 id 引用完整性是后来补的，起因是一次真实的线上缺陷：Django 的
BoundField.aria_describedby 会自动拼出 `id_<字段名>_helptext` /
`id_<字段名>_error`，而模板把 help_text 和错误渲染成了不带 id 的元素，于是
全站几十个表单的帮助文字和错误提示对读屏软件完全不可见——页面上看不出来，
DOM 里一抓一个准。重复 id 更隐蔽：HTMLParser 用「不存在才记」的方式收集
by_id，第二次出现会被静默折叠，上面几类检查对它完全免疫。
"""

import re
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from chat_messages.models import PrivateMessage
from listings.models import (
    CampusCampaign,
    CampusLocation,
    Category,
    DemandPost,
    DemandResponse,
    GiftApplication,
    Item,
    LostFoundLead,
    LostFoundPost,
    MeetingAppointment,
    MeetingIncident,
    Order,
    OrderDispute,
    Report,
    SavedSearch,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / 'templates'
STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'

# 自闭合标签，HTMLParser 不会为它们调用 handle_endtag。
VOID_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
})
# 内容不进入可访问名称计算的容器。
OPAQUE_TAGS = frozenset({'script', 'style', 'template', 'noscript', 'svg'})
HEADING_TAGS = frozenset({'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})
CONTROL_TAGS = frozenset({'input', 'select', 'textarea'})
INTERACTIVE_TAGS = frozenset({'a', 'button', 'summary'})
# 这些 input 不承担需要朗读出来的输入语义，跳过标签检查。
LABEL_EXEMPT_TYPES = frozenset({'hidden', 'submit', 'button', 'reset', 'image'})


class _Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs
        self.children = []
        self.parent = parent


class _TreeBuilder(HTMLParser):
    """把 HTML 摊成一棵够用的树。

    Django 模板渲染出来的 HTML 是良构的，但页面里嵌的内联脚本不是。
    handle_endtag 因此按「从栈顶往回找第一个同名标签」处理，遇到脚本里的
    字面 </div> 也不会把整棵树掀翻。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node('#root', {}, None)
        self.stack = [self.root]
        self.by_id = {}
        self.labelled_ids = set()
        # 每一次出现都记下来，不折叠。by_id 故意只留第一个，重复 id 会被它
        # 悄悄吃掉，而重复 id 是真实缺陷：label for / aria-labelledby /
        # document.getElementById 全都只命中第一个。
        self.id_occurrences = []

    def _record_id(self, node):
        node_id = node.attrs.get('id')
        if node_id:
            self.id_occurrences.append((node_id, node.tag))
            if node_id not in self.by_id:
                self.by_id[node_id] = node

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag == 'label' and node.attrs.get('for'):
            self.labelled_ids.add(node.attrs['for'])
        self._record_id(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        self._record_id(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)
def _parse(html):
    builder = _TreeBuilder()
    builder.feed(html)
    return builder


def _text_of(node):
    parts = []

    def walk(current):
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in OPAQUE_TAGS:
                walk(child)

    walk(node)
    return re.sub(r'\s+', ' ', ''.join(parts)).strip()


def _iter_nodes(node, tags=None):
    for child in node.children:
        if isinstance(child, str) or child.tag in OPAQUE_TAGS:
            continue
        if tags is None or child.tag in tags:
            yield child
        yield from _iter_nodes(child, tags)


def _nav_current_labels(html):
    """Return the labels marked as the current page inside the global nav."""
    builder = _parse(html)
    nav = next(
        (node for node in _iter_nodes(builder.root, {'nav'})
         if 'site-nav' in (node.attrs.get('class') or '').split()),
        None,
    )
    if nav is None:
        return []
    return [
        _text_of(link)
        for link in _iter_nodes(nav, {'a'})
        if link.attrs.get('aria-current') == 'page'
    ]


def _is_hidden(node):
    attrs = node.attrs
    if 'hidden' in attrs:
        return True
    if (attrs.get('aria-hidden') or '').lower() == 'true':
        return True
    if (attrs.get('type') or '').lower() == 'hidden':
        return True
    parent = node.parent
    while parent is not None and parent.tag != '#root':
        if parent.tag in OPAQUE_TAGS or _is_hidden(parent):
            return True
        parent = parent.parent
    return False


def _has_label(node, labelled_ids):
    if node.attrs.get('aria-label', '').strip():
        return True
    if node.attrs.get('aria-labelledby', '').strip():
        return True
    if node.attrs.get('id') and node.attrs['id'] in labelled_ids:
        return True
    parent = node.parent
    while parent is not None and parent.tag != '#root':
        if parent.tag == 'label':
            return True
        parent = parent.parent
    return False


def _accessible_name(node, by_id):
    label = node.attrs.get('aria-label', '').strip()
    if label:
        return label
    labelled_by = node.attrs.get('aria-labelledby', '').strip()
    if labelled_by:
        parts = [_text_of(by_id[i]) for i in labelled_by.split() if i in by_id]
        joined = ' '.join(part for part in parts if part).strip()
        if joined:
            return joined
    text = _text_of(node)
    if text:
        return text
    title = node.attrs.get('title', '').strip()
    if title:
        return title
    for image in _iter_nodes(node, {'img'}):
        alt = image.attrs.get('alt', '').strip()
        if alt:
            return alt
    return node.attrs.get('value', '').strip()


# 这些属性的值是一个或多个 id，指向文档里必须真实存在的元素。
IDREF_ATTRS = (
    'for', 'aria-labelledby', 'aria-describedby', 'aria-controls',
    'aria-owns', 'aria-details', 'aria-errormessage', 'aria-flowto',
)


def _id_reference_problems(html):
    """返回 (重复 id, 悬空或空的 id 引用) 两组问题。

    两类都是「页面不报错、只有辅助技术受害」的缺陷：

    - 重复 id：第二个同名元素对 label for / aria-labelledby / JS 查询都不存在，
      用户点标签聚焦到的是另一个控件。
    - 悬空引用：for / aria-* 指向一个不存在的 id。Django 的
      BoundField.aria_describedby 会自动拼 `id_<字段名>_helptext` 和
      `id_<字段名>_error`，模板只要把帮助文字、错误提示渲染成不带 id 的元素，
      引用就全悬空——帮助文字和错误提示对读屏软件直接消失。
    - 空引用：for="" 这种。Django 的 RadioSelect.id_for_label 刻意返回空串，
      模板无条件写 for="{{ field.id_for_label }}" 就会得到 for=""。
    """
    builder = _parse(html)

    occurrences = {}
    for node_id, tag in builder.id_occurrences:
        occurrences.setdefault(node_id, []).append(tag)
    duplicates = [
        'id %s 出现 %d 次（%s）' % (node_id, len(tags), ','.join(tags))
        for node_id, tags in sorted(occurrences.items()) if len(tags) > 1
    ]

    dangling = []
    for node in _iter_nodes(builder.root):
        for attr in IDREF_ATTRS:
            if attr not in node.attrs:
                continue
            raw = node.attrs.get(attr, '') or ''
            if not raw.strip():
                dangling.append('<%s %s=""> 是空引用' % (node.tag, attr))
                continue
            for ref in raw.split():
                if ref and ref not in builder.by_id:
                    dangling.append('<%s %s=%r> 指向不存在的 id %s' % (
                        node.tag, attr, raw, ref))
    return duplicates, dangling


def _heading_problems(html):
    """按文档顺序走一遍标题，返回所有跳级的可读描述。"""
    builder = _parse(html)
    seen = []
    for heading in _iter_nodes(builder.root, HEADING_TAGS):
        if _is_hidden(heading):
            continue
        seen.append((int(heading.tag[1]), _text_of(heading)[:40]))

    problems = []
    for index, (level, text) in enumerate(seen):
        if index == 0:
            if level != 1:
                problems.append('第一个标题是 h%d（%s）' % (level, text))
        elif level > seen[index - 1][0] + 1:
            problems.append('h%d 跳到 h%d（%s）' % (seen[index - 1][0], level, text))
    return problems


def _semantics_problems(html):
    """返回 (表单缺标签, 无可访问名称, 图片缺 alt) 三组问题。"""
    builder = _parse(html)
    unlabelled = []
    for control in _iter_nodes(builder.root, CONTROL_TAGS):
        if _is_hidden(control):
            continue
        if (control.attrs.get('type') or '').lower() in LABEL_EXEMPT_TYPES:
            continue
        if not _has_label(control, builder.labelled_ids):
            unlabelled.append('<%s name=%s id=%s>' % (
                control.tag,
                control.attrs.get('name'),
                control.attrs.get('id'),
            ))

    nameless = []
    for element in _iter_nodes(builder.root, INTERACTIVE_TAGS):
        if _is_hidden(element):
            continue
        if element.tag == 'a' and 'href' not in element.attrs:
            continue
        if not _accessible_name(element, builder.by_id):
            nameless.append('<%s class=%s href=%s>' % (
                element.tag,
                element.attrs.get('class', ''),
                element.attrs.get('href', '')[:40],
            ))

    altless = []
    for image in _iter_nodes(builder.root, {'img'}):
        if _is_hidden(image) or 'alt' in image.attrs:
            continue
        altless.append('<img src=%s>' % image.attrs.get('src', '')[-40:])
    return unlabelled, nameless, altless
class TemplateHeadingOrderTests(SimpleTestCase):
    """模板里的标题必须逐级递进，不允许跳级。

    静态扫描而不渲染，是为了覆盖浏览器审计够不到的分支：空状态、只有发布者
    看得见的区块、需要特定权限才出现的区域。这些分支平时没有数据，浏览器里
    根本不会出现，一旦上线配上数据就成了残废页面。

    文件名以 `_` 开头的是分区片段，由父页面在 h1 之后 include，自身从 h2 起
    是正确结构，因此不参与检查。
    """

    HEADING = re.compile(r'<h([1-6])\b')
    HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
    # 同时吃开标签和闭标签，用来验证两者配得上。
    HEADING_TOKEN = re.compile(r'<h([1-6])\b[^>]*>|</h([1-6])>', re.I)

    def _levels(self, template):
        text = self.HTML_COMMENT.sub(' ', template.read_text(encoding='utf-8'))
        return [int(match.group(1)) for match in self.HEADING.finditer(text)]

    def test_page_templates_do_not_skip_heading_levels(self):
        offenders = []
        for template in sorted(TEMPLATES_DIR.rglob('*.html')):
            if template.name.startswith('_'):
                continue
            levels = self._levels(template)
            relative = template.relative_to(TEMPLATES_DIR).as_posix()
            for index, level in enumerate(levels):
                if index == 0:
                    if level != 1:
                        offenders.append('%s：第一个标题是 h%d' % (relative, level))
                elif level > levels[index - 1] + 1:
                    offenders.append('%s：h%d 跳到 h%d' % (
                        relative, levels[index - 1], level))

        self.assertEqual(
            offenders, [],
            '以下模板的标题层级有跳级，读屏软件里会丢失结构：\n'
            + '\n'.join('  ' + item for item in offenders),
        )

    def test_heading_tags_are_properly_paired(self):
        """标题的开标签和闭标签必须配得上。

        真实踩过：把 `<h6 class="section-label">商品描述</h6>` 改成 h2 时只换了
        开标签，闭标签留在 h6。上面的层级扫描只数开标签，渲染态检查又依赖浏览器
        容错——两条守卫都看不见，页面照样正常显示，只有看源码才发现。
        标签不配对还会让阅读顺序依赖浏览器的猜测，不能指望它一直猜对。
        """
        offenders = []
        for template in sorted(TEMPLATES_DIR.rglob('*.html')):
            relative = template.relative_to(TEMPLATES_DIR).as_posix()
            text = self.HTML_COMMENT.sub(' ', template.read_text(encoding='utf-8'))
            stack = []
            for match in self.HEADING_TOKEN.finditer(text):
                if match.group(1):
                    stack.append(int(match.group(1)))
                    continue
                closing = int(match.group(2))
                if not stack:
                    offenders.append('%s：多余的 </h%d>' % (relative, closing))
                elif stack[-1] != closing:
                    offenders.append('%s：<h%d> 被 </h%d> 闭合' % (
                        relative, stack[-1], closing))
                    stack.pop()
                else:
                    stack.pop()
            for level in stack:
                offenders.append('%s：<h%d> 没有闭合' % (relative, level))

        self.assertEqual(
            offenders, [],
            '以下模板的标题标签没有正确配对：\n'
            + '\n'.join('  ' + item for item in offenders),
        )

    def test_partial_templates_start_below_the_page_heading(self):
        # 反向约束：片段从 h2 起是对的，但如果哪天片段里冒出 h1，
        # 页面上就会出现两个 h1。这条守着那个方向。
        offenders = []
        for template in sorted(TEMPLATES_DIR.rglob('_*.html')):
            levels = self._levels(template)
            if levels and levels[0] < 2:
                offenders.append('%s：片段从 h%d 开始' % (
                    template.relative_to(TEMPLATES_DIR).as_posix(), levels[0]))

        self.assertEqual(offenders, [], '\n'.join('  ' + item for item in offenders))

class DemoContentMixin:
    """一套够跑通主要页面的演示数据。

    每个字段都对应一个真实页面：没有 demand 就打不开求购详情，没有失物招领
    记录就打不开线索表单，没有订单就打不开交易详情，没有免费赠送商品就打不开
    领取申请队列。缺一项，那一类页面的语义检查就会静默失效——测试照样绿，
    只是那一页从此没人管。

    运营类页面还要一道权限：把它们单独放进 StaffPageSemanticsTests，
    由 is_staff 的账号渲染。混在同一个用例里会让普通用户视角的那半边
    永远渲染不出来。
    """

    STAFF = False
    password = 'semantics-password-123'

    def setUp(self):
        super().setUp()
        self.seller = User.objects.create_user('sem_seller', password=self.password)
        self.buyer = User.objects.create_user('sem_buyer', password=self.password)
        self.category = Category.objects.create(name='语义测试分类', description='守卫用')
        self.location = CampusLocation.objects.create(
            name='语义测试东门', building='东校区', address='图书馆一层东侧',
        )
        self.item = Item.objects.create(
            title='语义测试台灯', description='宿舍可用的暖光台灯', price='45.00',
            category=self.category, location=self.location, condition='9成新',
            seller=self.seller,
        )
        self.gift_item = Item.objects.create(
            title='语义测试英语教材', description='用完的教材', price='0.00',
            trade_mode='free', category=self.category, location=self.location,
            condition='有笔记', seller=self.seller,
        )
        self.demand = DemandPost.objects.create(
            requester=self.buyer, title='求购一盏台灯', description='宿舍床头用',
            category=self.category, location=self.location,
        )
        self.lost_found = LostFoundPost.objects.create(
            reporter=self.seller, post_type='lost', title='寻找一串钥匙',
            description='图书馆三楼', category=self.category,
            location=self.location, occurred_at=self.item.created_at,
        )
        # 登录用户自己发布的记录，用来渲染「收到的线索」和互助结果表单。
        self.owned_lost_found = LostFoundPost.objects.create(
            reporter=self.buyer, post_type='found', title='捡到一串钥匙',
            description='三楼自习区', category=self.category,
            location=self.location, occurred_at=self.item.created_at,
        )
        # 只有「已确认匹配」的响应才会在详情页渲染互助结果表单，
        # 少了这一条，表单标签那一类缺陷就静默漏过去了。
        DemandResponse.objects.create(
            demand=self.demand, item=self.item, responder=self.seller,
            message='我这盏台灯应该合适', status='accepted',
        )
        LostFoundLead.objects.create(
            post=self.owned_lost_found, respondent=self.seller,
            message='好像在三楼', status='accepted',
        )
        self.campaign = CampusCampaign.objects.create(
            title='语义测试专题', slug='semantics-smoke', description='守卫用专题',
            is_active=True,
        )
        self.order = Order.objects.create(
            item=self.item, buyer=self.buyer, seller=self.seller,
            agreed_price='45.00',
        )
        SavedSearch.objects.create(user=self.buyer, name='台灯关注')
        GiftApplication.objects.create(
            item=self.gift_item, applicant=self.buyer, applicant_note='想领取',
        )
        PrivateMessage.objects.create(
            sender=self.buyer, receiver=self.seller, item=self.item, content='还在吗',
        )
        if self.STAFF:
            self.buyer.is_staff = True
            self.buyer.save(update_fields=['is_staff'])
        self.client.force_login(self.buyer)


class SemanticsChecksMixin:
    """六类语义断言，普通用户视角和运营视角共用同一套。"""

    # 默认不带参数；需要参数的路由在子类里覆盖。
    ARGS = {}

    def _url(self, label, name):
        args = self.ARGS.get(label, lambda self: [])(self)
        return reverse(name, args=args)

    def _rendered_pages(self, pages=None, as_user=None):
        for label, name in (self.PAGES if pages is None else pages):
            with self.subTest(page=label):
                if as_user is not None:
                    self.client.force_login(as_user)
                response = self.client.get(self._url(label, name))
                if response.status_code != 200:
                    # 需要特定权限或特定状态的页面交给对应的用例覆盖；
                    # 这里只对能渲染出来的页面负责。
                    continue
                yield label, name, response.content.decode('utf-8')

    def _assert_accessible_error_summary(self, label, response, field_id):
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        builder = _parse(html)
        summaries = [
            node for node in _iter_nodes(builder.root, {'div'})
            if 'data-form-error-summary' in node.attrs
        ]
        self.assertEqual(len(summaries), 1, f'{label} 缺少或重复渲染错误摘要')
        summary = summaries[0]
        self.assertEqual(summary.attrs.get('role'), 'alert')
        self.assertEqual(summary.attrs.get('tabindex'), '-1')
        summary_links = [
            link.attrs.get('href') for link in _iter_nodes(summary, {'a'})
        ]
        self.assertIn(f'#{field_id}', summary_links)
        self.assertIn(field_id, builder.by_id)
        duplicates, dangling = _id_reference_problems(html)
        self.assertEqual(duplicates, [])
        self.assertEqual(dangling, [])
        self.assertIn('aria-invalid="true"', html)
        field = builder.by_id.get(field_id)
        self.assertIsNotNone(field, f'{label} 缺少字段 #{field_id}')
        describedby = field.attrs.get('aria-describedby', '').split()
        self.assertIn(f'{field_id}_error', describedby)

    def test_pages_have_a_single_top_level_heading_without_skips(self):
        problems = []
        for label, name, html in self._rendered_pages():
            for problem in _heading_problems(html):
                problems.append('%s（%s）：%s' % (label, name, problem))
        self.assertEqual(problems, [], '\n'.join('  ' + item for item in problems))

    def test_form_controls_have_labels(self):
        problems = []
        for label, name, html in self._rendered_pages():
            unlabelled, _, _ = _semantics_problems(html)
            for item in unlabelled:
                problems.append('%s（%s）：%s 没有关联标签' % (label, name, item))
        self.assertEqual(problems, [], '\n'.join('  ' + item for item in problems))

    def test_interactive_elements_have_accessible_names(self):
        problems = []
        for label, name, html in self._rendered_pages():
            _, nameless, _ = _semantics_problems(html)
            for item in nameless:
                problems.append('%s（%s）：%s 没有可访问名称' % (label, name, item))
        self.assertEqual(problems, [], '\n'.join('  ' + item for item in problems))

    def test_images_declare_alt(self):
        problems = []
        for label, name, html in self._rendered_pages():
            _, _, altless = _semantics_problems(html)
            for item in altless:
                problems.append('%s（%s）：%s 缺少 alt' % (label, name, item))
        self.assertEqual(problems, [], '\n'.join('  ' + item for item in problems))

    def test_ids_are_unique_within_each_page(self):
        # 重复 id 不会让页面变形，但会让 label for / aria-labelledby /
        # document.getElementById 全部只命中第一个，用户点标签聚焦到别的控件上。
        problems = []
        for label, name, html in self._rendered_pages():
            duplicates, _ = _id_reference_problems(html)
            for item in duplicates:
                problems.append('%s（%s）：%s' % (label, name, item))
        self.assertEqual(
            problems, [], '以下页面存在重复 id：\n' + '\n'.join('  ' + item for item in problems))

    def test_id_references_resolve_to_real_elements(self):
        # for / aria-labelledby / aria-describedby 指向不存在的元素，等于把
        # 标签、分组名、帮助文字、错误提示全部对读屏软件藏了起来。Django 会按
        # auto_id 约定自动生成 aria-describedby，模板必须用同一套拼法给帮助
        # 文字和错误提示补上 id，否则整站表单的辅助信息都是悬空的。
        problems = []
        for label, name, html in self._rendered_pages():
            _, dangling = _id_reference_problems(html)
            for item in dangling:
                problems.append('%s（%s）：%s' % (label, name, item))
        self.assertEqual(
            problems, [], '以下页面的 id 引用悬空：\n' + '\n'.join('  ' + item for item in problems))

    def test_page_list_actually_renders_enough_pages(self):
        # 页面清单是会腐烂的：某个视图改了权限、改了参数，对应条目就会静默
        # 变成 302/403，上面的检查随之少覆盖一个页面，而没有人会注意到。
        # 这条给清单本身兜底——渲染不出来的条目必须一直维持在一个很低的比例。
        rendered = sum(1 for _ in self._rendered_pages())
        self.assertGreaterEqual(
            rendered, self.MIN_RENDERED,
            '只渲染出 %d 个页面（共 %d 个条目），页面清单可能已经腐烂，'
            '请核对 ARGS 与各视图的权限要求。' % (rendered, len(self.PAGES)),
        )


class RenderedPageSemanticsTests(SemanticsChecksMixin, DemoContentMixin, TestCase):
    """普通用户视角：登录后能走通的每一类页面。"""

    MIN_RENDERED = 36

    PAGES = (
        ('首页', 'home'),
        ('商品列表', 'item_list'),
        ('分类商品列表', 'item_list_by_category'),
        ('求购广场', 'demand_list'),
        ('我的求购', 'my_demands'),
        ('失物招领列表', 'lost_found_list'),
        ('我的失物招领', 'my_lost_found'),
        ('专题列表', 'campaign_list'),
        ('我的专题', 'my_campaigns'),
        ('商品详情', 'item_detail'),
        ('求购详情', 'demand_detail'),
        ('失物招领详情（发布者视角）', 'lost_found_detail'),
        ('失物招领详情（访客视角）', 'lost_found_detail'),
        ('发布商品', 'new_item'),
        ('发布失物招领', 'new_lost_found'),
        ('编辑失物招领', 'edit_lost_found'),
        ('发布求购', 'new_demand'),
        ('编辑求购', 'edit_demand'),
        ('批量创建商品', 'bulk_create_items'),
        ('举报商品', 'report_item'),
        ('发起交易', 'create_order'),
        ('专题详情', 'campaign_detail'),
        ('我的交易', 'my_orders'),
        ('交易详情', 'order_detail'),
        ('我的发布', 'my_items'),
        ('我的领取申请', 'my_gift_applications'),
        ('管理领取申请', 'manage_gift_applications'),
        ('收藏夹', 'favorite_list'),
        ('贡献中心', 'contribution_center'),
        ('循环影响力', 'circular_impact_report'),
        ('机会流', 'opportunity_feed'),
        ('浏览历史', 'browsing_history'),
        ('关注列表', 'saved_search_list'),
        ('消息中心', 'inbox'),
        ('发送消息', 'send_message_item'),
        ('会话', 'conversation'),
        ('我的申诉', 'moderation_appeals'),
        ('举报列表', 'my_reports'),
        ('通知列表', 'notification_list'),
        ('站内动态', 'activity_center'),
        ('通知偏好', 'notification_preferences'),
        ('账户中心', 'profile'),
        ('编辑资料', 'edit_profile'),
        ('公开档案', 'public_profile'),
        ('校园认证', 'campus_verification'),
    )

    ARGS = {
        '分类商品列表': lambda self: [self.category.id],
        '商品详情': lambda self: [self.item.id],
        '求购详情': lambda self: [self.demand.id],
        '失物招领详情（发布者视角）': lambda self: [self.owned_lost_found.id],
        '失物招领详情（访客视角）': lambda self: [self.lost_found.id],
        '编辑失物招领': lambda self: [self.owned_lost_found.id],
        '编辑求购': lambda self: [self.demand.id],
        '举报商品': lambda self: [self.item.id],
        '发起交易': lambda self: [self.item.id],
        '专题详情': lambda self: [self.campaign.slug],
        '交易详情': lambda self: [self.order.id],
        '管理领取申请': lambda self: [self.gift_item.id],
        '发送消息': lambda self: [self.seller.id, self.item.id],
        '会话': lambda self: [self.seller.id],
        '公开档案': lambda self: [self.seller.id],
        '求购详情（卖家视角）': lambda self: [self.demand.id],
    }

    # 求购详情页有两套表单：一套给求购发布者（互助结果反馈），一套给手里有
    # 匹配商品的卖家（「用这件商品响应」）。上面那张清单统一以发布者身份登录，
    # 卖家那套表单永远渲染不出来，所以单独列一份、单独登录取证。
    SELLER_PAGES = (
        ('求购详情（卖家视角）', 'demand_detail'),
    )

    def test_invalid_forms_focus_an_accessible_error_summary(self):
        # GET 页面没有错误态，BoundField 不会输出 aria-invalid / aria-describedby。
        # 这里专门提交无效数据，守住「页面重新渲染后，错误能被读屏和键盘用户找到」
        # 这条路径：摘要可聚焦、链接能落到具体字段、字段引用的 id 都真实存在。
        self.order.status = 'completed'
        self.order.save(update_fields=['status'])
        cases = (
            (
                '举报商品',
                self.client.post(reverse('report_item', args=[self.item.id]), {}),
                'id_reason',
            ),
            (
                '发起交易',
                self.client.post(
                    reverse('create_order', args=[self.item.id]),
                    {'meeting_location': 999999, 'buyer_note': ''},
                ),
                'id_meeting_location',
            ),
            (
                '交易争议',
                self.client.post(reverse('open_dispute', args=[self.order.id]), {}),
                'id_reason',
            ),
            (
                '交易评价',
                self.client.post(reverse('rate_order', args=[self.order.id]), {}),
                'id_score',
            ),
            (
                '通知偏好',
                self.client.post(
                    reverse('notification_preferences'),
                    {'quiet_hours_start': 'not-a-time', 'quiet_hours_end': '08:00'},
                ),
                'id_quiet_hours_start',
            ),
            (
                '编辑资料',
                self.client.post(
                    reverse('edit_profile'),
                    {'username': '', 'email': 'semantics@example.com'},
                ),
                'id_username',
            ),
            (
                '校园认证',
                self.client.post(
                    reverse('campus_verification'),
                    {'campus_email': 'not-an-email'},
                ),
                'id_campus_email',
            ),
            (
                '发布商品',
                self.client.post(reverse('new_item'), {}),
                'id_title',
            ),
            (
                '发布求购',
                self.client.post(reverse('new_demand'), {}),
                'id_title',
            ),
            (
                '发布失物招领',
                self.client.post(reverse('new_lost_found'), {}),
                'id_title',
            ),
            (
                '发送消息',
                self.client.post(
                    reverse('send_message_item', args=[self.seller.id, self.item.id]),
                    {},
                ),
                'id_content',
            ),
            (
                '会话内发送消息',
                self.client.post(reverse('conversation', args=[self.seller.id]), {}),
                'id_content',
            ),
            (
                '提交失物招领线索',
                self.client.post(
                    reverse('submit_lost_found_lead', args=[self.lost_found.id]),
                    {},
                ),
                'id_message',
            ),
        )

        for label, response, field_id in cases:
            with self.subTest(page=label):
                self._assert_accessible_error_summary(label, response, field_id)

    def test_public_account_forms_focus_an_accessible_error_summary(self):
        # 登录和注册没有现成的登录态，单独跑无效提交，避免被上面的页面清单漏掉。
        self.client.logout()
        cases = (
            ('登录', self.client.post(reverse('login'), {}), 'id_username'),
            ('注册', self.client.post(reverse('register'), {}), 'id_username'),
        )
        for label, response, field_id in cases:
            with self.subTest(page=label):
                self._assert_accessible_error_summary(label, response, field_id)

    def test_decorative_placeholder_images_are_not_read_as_content(self):
        # 无图商品的占位图必须是装饰性的（alt=""），同时给包着它的链接补上
        # aria-label。否则读屏软件会把「暂无图片」当成链接名称读出来，
        # 用户听到的是四个字，而不是商品名。
        response = self.client.get(reverse('item_list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertNotIn('alt="暂无图片"', html)
        self.assertIn('aria-label="查看 语义测试台灯 的详情"', html)

    def test_seller_side_demand_forms_are_labelled_and_named(self):
        # 见 SELLER_PAGES 上的说明：这个表单只对「拥有匹配商品、且还没响应过这条
        # 求购」的卖家出现。少了这条用例，它的标签关联坏了也没人知道。
        problems = []
        for label, name, html in self._rendered_pages(self.SELLER_PAGES, as_user=self.seller):
            unlabelled, nameless, altless = _semantics_problems(html)
            for item in unlabelled:
                problems.append('%s（%s）：%s 没有关联标签' % (label, name, item))
            for item in nameless:
                problems.append('%s（%s）：%s 没有可访问名称' % (label, name, item))
            for item in altless:
                problems.append('%s（%s）：%s 缺少 alt' % (label, name, item))
        self.assertEqual(problems, [], '\n'.join('  ' + item for item in problems))

        response = self.client.get(
            reverse('demand_detail', args=[self.demand.id]))
        self.assertIn('<label for="id_message"', response.content.decode('utf-8'))


class NavigationSemanticsTests(DemoContentMixin, TestCase):
    """Global navigation must identify the current section and remain skippable."""

    CASES = (
        ('首页', 'home', (), '首页'),
        ('商品列表', 'item_list', (), '发现好物'),
        ('商品搜索结果', 'search_items', (), '发现好物'),
        ('商品详情', 'item_detail', ('item',), '发现好物'),
        ('求购详情', 'demand_detail', ('demand',), '求购广场'),
        ('失物招领详情', 'lost_found_detail', ('lost_found',), '失物招领'),
        ('机会流', 'opportunity_feed', (), '互助机会'),
        ('专题详情', 'campaign_detail', ('campaign',), '校园专题'),
        ('账户中心', 'profile', (), None),
    )

    def _url(self, name, args):
        values = [getattr(self, attr).id if attr != 'campaign' else self.campaign.slug for attr in args]
        return reverse(name, args=values)

    def test_skip_link_targets_a_focusable_main_region(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        builder = _parse(html)

        first_link = next(_iter_nodes(builder.root, {'a'}))
        self.assertIn('skip-link', first_link.attrs.get('class', '').split())
        self.assertEqual(first_link.attrs.get('href'), '#main-content')

        main = builder.by_id.get('main-content')
        self.assertIsNotNone(main, '<main> 必须提供 #main-content 跳转目标')
        self.assertEqual(main.tag, 'main')
        self.assertEqual(main.attrs.get('tabindex'), '-1')

    def test_only_the_matching_top_level_section_is_marked_current(self):
        problems = []
        for label, name, args, expected in self.CASES:
            with self.subTest(page=label):
                url = self._url(name, args)
                response = self.client.get(
                    url, {'q': '台灯'} if name == 'search_items' else None, follow=True,
                )
                self.assertEqual(response.status_code, 200, '%s 未能渲染' % label)
                actual = _nav_current_labels(response.content.decode('utf-8'))
                wanted = [] if expected is None else [expected]
                if actual != wanted:
                    problems.append('%s：期望 %r，实际 %r' % (label, wanted, actual))
        self.assertEqual(problems, [], '\n'.join('  ' + item for item in problems))


class StaffPageSemanticsTests(SemanticsChecksMixin, DemoContentMixin, TestCase):
    """运营视角：内容审核队列、治理工作台、运营看板、搜索洞察。

    这几个页面承载了全站最多的表格和筛选器，也是 Bootstrap 默认样式残留最久的
    地方。它们的 DOM 最复杂，恰恰最需要语义守卫。
    """

    STAFF = True
    MIN_RENDERED = 5

    PAGES = (
        ('内容审核队列', 'moderation_queue'),
        ('我的申诉', 'moderation_appeals'),
        ('举报记录', 'report_list'),
        ('争议列表', 'dispute_list'),
        ('治理工作台', 'governance_workbench'),
        ('运营看板', 'operations_dashboard'),
        ('搜索洞察', 'search_insights'),
    )

    def test_governance_review_forms_focus_an_accessible_error_summary(self):
        # 三类治理队列共用同一套处置形态：先看上下文，再提交结论和说明。
        # 这些页面过去只有字段旁的小字错误，键盘和读屏用户重新回到页面时
        # 找不到错误入口；这里用真实无效 POST 守住共享摘要与字段关联。
        report = Report.objects.create(
            item=self.item,
            reporter=self.buyer,
            reason='spam',
            detail='语义测试举报',
        )
        dispute = OrderDispute.objects.create(
            order=self.order,
            opened_by=self.buyer,
            reason='mismatch',
            detail='语义测试争议',
        )
        start_at = timezone.now() + timedelta(days=1)
        appointment = MeetingAppointment.objects.create(
            order=self.order,
            proposed_by=self.seller,
            location=self.location,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            status='confirmed',
        )
        incident = MeetingIncident.objects.create(
            appointment=appointment,
            reported_by=self.buyer,
            accused=self.seller,
            reason='no_show',
            detail='语义测试预约异常',
        )
        cases = (
            (
                '举报审核',
                self.client.post(reverse('review_report', args=[report.id]), {}),
                'id_status',
            ),
            (
                '争议处理',
                self.client.post(reverse('resolve_dispute', args=[dispute.id]), {}),
                'id_status',
            ),
            (
                '预约异常处理',
                self.client.post(
                    reverse('review_meeting_incident', args=[incident.id]), {},
                ),
                'id_status',
            ),
        )
        for label, response, field_id in cases:
            with self.subTest(page=label):
                self._assert_accessible_error_summary(label, response, field_id)


class TouchTargetTests(SimpleTestCase):
    """导航链接要够到 24x24 的最小触控目标。

    这是 WCAG 2.2 的 2.5.8（AA）。导航是唯一出现在每一页上的交互元素，
    它点不中比内容页里某个按钮点不中更难绕开——用户每一页都要先过这一关。

    直接量元素尺寸要起浏览器，代价和收益不成比例；这里改成约束样式表：
    基准规则（不在 @media 里的那条）必须显式给出不小于 24px 的最小触控尺寸。
    媒体查询里只覆盖间距和字号，min-height / min-width 会从基准规则继承下来，
    所以只查基准规则就够。显式写出来的好处是，以后谁想调密一点，
    必须先看见这条底线，而不是把它悄悄抹掉。
    """

    NAV_LINK_RULE = re.compile(r'\.site-nav \.navbar-nav \.nav-link\s*\{([^}]*)\}')
    MIN_TOUCH_PX = 24
    AT_RULE = re.compile(r'@[^{]*\{')
    STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'

    @classmethod
    def _base_css(cls):
        """剥掉所有 @media / @supports / @keyframes 块，只留基准规则。"""
        css = cls.STYLESHEET.read_text(encoding='utf-8')
        kept = []
        cursor = 0
        while True:
            match = cls.AT_RULE.search(css, cursor)
            if match is None:
                kept.append(css[cursor:])
                return ''.join(kept)
            kept.append(css[cursor:match.start()])
            depth = 1
            index = match.end()
            while index < len(css) and depth:
                if css[index] == '{':
                    depth += 1
                elif css[index] == '}':
                    depth -= 1
                index += 1
            cursor = index

    def _declarations(self, body):
        return dict(
            (name.strip(), value.strip())
            for name, _, value in (
                part.partition(':') for part in body.split(';') if ':' in part
            )
        )

    def _pixels(self, value):
        match = re.fullmatch(r'([0-9]*\.?[0-9]+)px', value.strip())
        return float(match.group(1)) if match else None

    def test_base_nav_rule_declares_a_minimum_touch_target(self):
        rules = self.NAV_LINK_RULE.findall(self._base_css())
        self.assertEqual(
            len(rules), 1,
            '基准规则里应该恰好有一条 .site-nav .navbar-nav .nav-link，实际 %d 条' % len(rules))

        declarations = self._declarations(rules[0])
        offenders = []
        for side in ('min-height', 'min-width'):
            value = declarations.get(side)
            if value is None:
                offenders.append('缺少 %s' % side)
                continue
            pixels = self._pixels(value)
            if pixels is None or pixels < self.MIN_TOUCH_PX:
                offenders.append('%s: %s 小于 %dpx' % (side, value, self.MIN_TOUCH_PX))

        self.assertEqual(
            offenders, [],
            '导航链接够不到最小触控目标（WCAG 2.5.8）：\n'
            + '\n'.join('  ' + item for item in offenders),
        )

class TableAccessibleNameTests(SimpleTestCase):
    """每张表都要有可访问名称：读屏软件进表之前，先得知道这是哪张表。

    为什么是静态扫而不是渲染：全站三百多张表里运营看板占了大半，而那些表
    大量挂在 {% if has_data %} / {% for row in ... %} 上。渲染态检查只能点亮
    演示数据恰好命中的一百多张，剩下的全在空状态、他人视角和没数据的分支里
    ——偏偏这些分支上线配上数据就是残废页面。只有扫模板源才全覆盖。

    三种命名手段都算过，各有分工：

      * <caption class="visually-hidden">  首选。语义原生，不依赖 ARIA，
        自包含不需要协调 id，也没有重复 id 的风险。Bootstrap 专门给 caption
        开了口子（只有 .visually-hidden:not(caption) 才绝对定位），所以视觉
        隐藏不会把表格布局带崩。
      * aria-label                         可行，但和 caption 是二选一，
        两处都要维护，没有必要。
      * aria-labelledby                    只留给标题本身是模板表达式的循环
        表格：每组的标题由 {{ group.kind_label }} 之类拼出来，写死的 caption
        要么和可见标题对不上，要么退化成「第一组 / 第二组」。直接引用可见
        标题，文案只有一份，不会漂。

    另外守两条容易坏的边界：caption 必须是 table 的第一个子元素（前面混进
    文本，浏览器会把文本连同 caption 一起丢掉），以及 caption 不能是空的。
    """

    TABLE = re.compile(r'<table\b[^>]*>', re.I)
    CAPTION = re.compile(r'^(\s*)<caption\b[^>]*>(.*?)</caption>', re.S | re.I)
    ARIA_NAME = re.compile(r'''\b(aria-label|aria-labelledby)=["']([^"']*)["']''', re.I)
    HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
    TAGSUB = re.compile(r'<[^>]+>')

    def _sources(self):
        for template in sorted(TEMPLATES_DIR.rglob('*.html')):
            relative = template.relative_to(TEMPLATES_DIR).as_posix()
            text = self.HTML_COMMENT.sub(' ', template.read_text(encoding='utf-8'))
            yield relative, text

    def test_every_table_has_an_accessible_name(self):
        offenders = []
        total = 0
        for relative, text in self._sources():
            for match in self.TABLE.finditer(text):
                total += 1
                caption = self.CAPTION.match(text[match.end():])
                if caption:
                    leading = caption.group(1)
                    if leading.strip():
                        # 非空白文本会被浏览器连同 caption 一起从表里丢掉，
                        # 只有空白（换行缩进）是安全的。
                        offenders.append('%s：caption 前面有文本 %r'
                                         % (relative, leading[:40]))
                        continue
                    inner = caption.group(2)
                    visible = re.sub(r'\s+', ' ', self.TAGSUB.sub('', inner)).strip()
                    if not visible and '{{' not in inner and '{%' not in inner:
                        offenders.append('%s：caption 是空的' % relative)
                    continue
                names = dict(
                    (name.lower(), value)
                    for name, value in self.ARIA_NAME.findall(match.group(0))
                )
                if not (names.get('aria-label') or '').strip() \
                        and not (names.get('aria-labelledby') or '').strip():
                    offenders.append('%s：表格没有可访问名称 %s'
                                     % (relative, match.group(0)[:80]))

        self.assertGreater(total, 0, '一张表都没扫到，正则大概失效了')
        self.assertEqual(
            offenders, [],
            '以下表格读屏软件读不出是什么表（WCAG 1.3.1 / 4.1.2）：\n'
            + '\n'.join('  ' + item for item in offenders),
        )

    def test_captions_are_hidden_without_being_removed(self):
        """caption 必须挂 visually-hidden，不能把表格顶出可见文字。

        运营看板一屏三十个面板，caption 一旦可见就会在每张表上方多出一行，
        页面节奏全乱。反过来，用 style="display:none" 之类的土办法隐藏，
        读屏软件也会一起忽略掉，等于没写。
        """
        offenders = []
        for relative, text in self._sources():
            for match in re.finditer(r'<caption\b([^>]*)>', text, re.I):
                if 'visually-hidden' not in match.group(1):
                    offenders.append('%s：<caption%s>' % (relative, match.group(1)[:60]))

        self.assertEqual(
            offenders, [],
            'caption 要加 visually-hidden；display:none 不行，读屏软件会一起忽略：\n'
            + '\n'.join('  ' + item for item in offenders),
        )

class PaginationAccessibleNameTests(SimpleTestCase):
    """分页链接要读得出名字，当前页还要标明 aria-current。

    分页器是「一个图标或一串数字」的组合，天生缺可访问名称：翻页按钮里只有
    一个 <i class="bi">，读屏软件念出来就是一句干巴巴的「链接」，听不出是上一页
    还是下一页。同页出现多个分页器时更糟——收件箱页同时有「收到的消息」和
    「发出的消息」两个分页器，四个图标链接在辅助技术里长得完全一样。

    这不是假想。templates/chat_messages/inbox.html 的四个翻页链接就是这么裸奔的，
    而站内其他分页器早就按 aria-label 的约定写好了；本类检查就是让这个约定不再
    靠人眼维持，新增分页器时漏了名字会直接红灯。

    另外要求当前页指示器带 aria-current="page"。只渲染「2 / 5」这段文本时，读屏
    软件知道有数字，不知道这就是当前所在页；aria-current 是唯一能把「当前位置」
    讲出来的属性。

    纯装饰的省略号（<span class="page-link" aria-hidden="true">…</span>）不进可访问
    名称计算，按 _is_hidden 跳过，不算缺陷。
    """

    HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)

    def _sources(self):
        for template in sorted(TEMPLATES_DIR.rglob('*.html')):
            relative = template.relative_to(TEMPLATES_DIR).as_posix()
            text = self.HTML_COMMENT.sub(' ', template.read_text(encoding='utf-8'))
            yield relative, text

    def _page_links(self, text):
        """产出 (节点, 解析结果)；解析结果要留着，_accessible_name 要用它的 by_id。"""
        builder = _parse(text)
        for node in _iter_nodes(builder.root):
            if 'page-link' in (node.attrs.get('class') or '').split():
                yield node, builder

    def _in_active_item(self, node):
        parent = node.parent
        while parent is not None and parent.tag != '#root':
            classes = (parent.attrs.get('class') or '').split()
            if 'page-item' in classes:
                return 'active' in classes
            parent = parent.parent
        return False

    def test_every_page_link_has_an_accessible_name(self):
        offenders = []
        total = 0
        for relative, text in self._sources():
            for node, builder in self._page_links(text):
                if _is_hidden(node):
                    continue
                total += 1
                if not _accessible_name(node, builder.by_id):
                    offenders.append(
                        '%s: <%s class="page-link"> 没有可访问名称'
                        % (relative, node.tag))

        self.assertGreater(total, 0, '一个 page-link 都没扫到，检查逻辑失效了')
        self.assertEqual(
            offenders, [],
            '分页链接要读得出名字（WCAG 2.4.4 / 4.1.2）。纯图标链接补 '
            'aria-label，图标本身加 aria-hidden="true"：\n'
            + '\n'.join('  ' + item for item in offenders),
        )

    def test_current_page_is_marked_as_current(self):
        offenders = []
        total = 0
        for relative, text in self._sources():
            for node, _builder in self._page_links(text):
                if not self._in_active_item(node):
                    continue
                total += 1
                if (node.attrs.get('aria-current') or '').strip() != 'page':
                    offenders.append(
                        '%s: 当前页指示器缺 aria-current="page"' % relative)

        self.assertGreater(total, 0, '一个当前页指示器都没扫到，检查逻辑失效了')
        self.assertEqual(
            offenders, [],
            '当前页要标明 aria-current="page"，读屏软件才念得出「当前页」'
            '而不是一串数字：\n'
            + '\n'.join('  ' + item for item in offenders),
        )


class GridFirstCellTests(SimpleTestCase):
    """窄首列网格的首格内容契约：固定宽度的第一轨只放得了很短的内容。

    这类缺陷长得很有规律，而且页面上一眼看不出病因：一条 grid 规则把第一轨写成
    42px / 72px / 89.6px 这样的固定长度，首格自然是图标、时间戳、序号这类一两个
    字的东西。可模板是会长大的——有人往同一行塞一个九字标签，轨道放不下就折行，
    行高从 20px 涨到 59px，整张卡片跟着错位；或者反过来，把只有三格内容的行套进
    四轨模板，最后一轨永远空着，数字和单位脱节。运营看板那张「仍未真正收到的卖家」
    两个毛病一起犯过，修完之后才决定把它钉成契约。

    浏览器审计当然抓得到，但代价是逐页逐宽度量，跑一轮以小时计，而且只有数据恰好
    渲染出那一行时才照得到。这里改成约束样式表和模板源，秒级跑完：

    1. 注册表守卫。把样式表里所有「首轨是固定长度且不超过 170px」的
       grid-template-columns 规则都列出来，要求每条都在契约表里登记过。新增一条
       窄首列规则而没人想过首格放什么，这一步就红。
    2. 内容契约。对登记过的选择器扫全部模板源，断言匹配元素的首个子元素落在允许
       集合里。允许条目同时约束行上的修饰类——.activity-row 的首格可以是 <span>，
       但只在带 .activity-row--wide-label（首轨改宽）的那行上成立。
    3. 伪元素契约。卖家工作台那张表窄屏时首格是
       ::before{content:attr(data-label)}，DOM 里根本没有子元素可查，只能反过来
       约束属性文本的长度。

    模板源要先内联 {% include %}：.seller-desk-table--stacked 挂在 _my_items_table.html
    的 <table> 上，.seller-desk-row td[data-label] 却在被 include 的 _my_item_row.html
    里。跨文件的选择器单文件扫，一条都匹配不到——契约会悄悄变成空转。

    还有一条教训写在最后一项检查里：运营看板和搜索洞察的分区默认是懒加载的，
    html.js 下 .ops-section[data-ops-deferred] > * { display: none }。整轮浏览器
    溢出扫描曾经一次都没量到过它们，34 + 13 个分区全是盲区。谁把标记去掉，
    谁就把下一个审审美的人送回盲区。
    """

    STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'
    MAIN_JS = PROJECT_ROOT / 'static' / 'js' / 'main.js'
    CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)
    GRID_COLUMNS = re.compile(r'grid-template-columns\s*:\s*([^;]+);')
    FIXED_TRACK = re.compile(r'^(\d*\.?\d+)(px|rem|em|pt)$')
    UNIT_TO_PX = {'px': 1.0, 'rem': 16.0, 'em': 16.0, 'pt': 96.0 / 72.0}
    MAX_FIRST_TRACK_PX = 170
    INCLUDE = re.compile(r"{%\s*include\s+['\"]([^'\"]+)['\"]")
    MAX_INCLUDE_DEPTH = 6
    SIMPLE_SELECTOR = re.compile(
        r'\.([\w-]+)'                       # .class
        r'|^([a-zA-Z][\w-]*)'               # 标签名，只在片段开头算
        r'|\[([\w-]+)(?:=(?:"([^"]*)"|\'([^\']*)\'|([^\]]*)))?\]'   # [attr] / [attr=val]
        r'|:not\(([^)]*)\)'                 # :not(简单选择器)
    )
    LAZY_HIDE_RULE = re.compile(
        r'html\.js\s+\.ops-section\[data-ops-deferred\]:not\(\.is-revealed\)\s*>\s*\*'
        r'[^{}]*\{\s*display:\s*none')

    # 首格是真实 DOM 子元素的选择器。
    # 每条 = ((首格标签, 首格必须带的 class), (行上必须同时出现的 class))
    GRID_FIRST_CELL_CONTRACTS = {
        '.auth-benefit-list li': (
            (('i', ()), ()),
        ),
        '.search-weekday-row': (
            (('span', ()), ()),
        ),
        '.ops-alert': (
            (('div', ('ops-alert-icon',)), ()),
        ),
        '.activity-row': (
            # 默认 42px 首轨只放得下 <time>；宽标签行必须自己把首轨改宽，
            # 否则「处于免打扰时段」这类长标签会折行把行高顶起来。
            (('time', ()), ()),
            (('span', ('text-muted',)), ('activity-row--wide-label',)),
        ),
        '.contribution-trend-chart .activity-row': (
            (('time', ()), ()),
        ),
        '.bulk-image-task': (
            (('span', ('bulk-image-task-index',)), ()),
        ),
        '.bulk-image-complete': (
            (('span', ('bulk-image-complete-icon',)), ()),
        ),
        '.chat-response-buckets li': (
            (('span', ('chat-response-bucket-label',)), ()),
        ),
        '.notification-response-buckets li': (
            (('span', ('notification-response-bucket-label',)), ()),
        ),
        '.supply-lifecycle-level': (
            (('span', ('supply-lifecycle-level-label',)), ()),
        ),
        '.trade-order-card': (
            (('a', ('trade-order-thumb',)), ()),
        ),
        '.order-info-list': (
            (('dt', ()), ()),
        ),
    }

    # 首格是 ::before{content:attr(...)} 的选择器：DOM 里没有子元素可查，
    # 改成约束属性文本的字符数。值 = (属性名, 最多几个字)
    GRID_FIRST_CELL_PSEUDO_CONTRACTS = {
        '.seller-desk-table--stacked .seller-desk-row '
        'td[data-label]:not([data-label="商品"]):not(.seller-desk-check)':
            ('data-label', 4),
    }

    # (模板, 懒加载属性, 分区样式类, JS 里对应的选择器)
    LAZY_SECTIONS = (
        ('listings/operations_dashboard.html', 'data-ops-deferred', 'ops-section',
         '.ops-section[data-ops-deferred]'),
        ('listings/search_insights.html', 'data-si-deferred', 'si-section',
         '.si-section[data-si-deferred]'),
    )

    # ---------------------------------------------------------------- 样式表

    @classmethod
    def _rules(cls):
        """产出全部 (选择器, 声明体)，@media / @supports 里的规则一起收进来。

        注释先按等长空白替换掉：注释里的字符会打乱 { } 配平，偏移量跟着全漂。
        @keyframes / @font-face 整块跳过，里面的 0% / from 不是选择器。
        """
        css = cls.CSS_COMMENT.sub(
            lambda match: ''.join('\n' if ch == '\n' else ' ' for ch in match.group(0)),
            cls.STYLESHEET.read_text(encoding='utf-8'))

        def walk(text):
            found = []
            cursor = 0
            while True:
                brace = text.find('{', cursor)
                if brace < 0:
                    return found
                prelude = text[cursor:brace].strip()
                depth, index = 1, brace + 1
                while index < len(text) and depth:
                    if text[index] == '{':
                        depth += 1
                    elif text[index] == '}':
                        depth -= 1
                    index += 1
                body = text[brace + 1:index - 1]
                if prelude.startswith('@'):
                    if prelude.startswith('@media') or prelude.startswith('@supports'):
                        found.extend(walk(body))
                else:
                    found.append((prelude, body))
                cursor = index

        return walk(css)

    @classmethod
    def _narrow_first_tracks(cls):
        """产出 {归一化选择器: [首轨像素值, ...]}，只留固定长度的窄首轨。"""
        found = {}
        for selector, body in cls._rules():
            match = cls.GRID_COLUMNS.search(body)
            if not match:
                continue
            tracks = match.group(1).split()
            fixed = cls.FIXED_TRACK.match(tracks[0]) if tracks else None
            if not fixed:
                continue
            pixels = float(fixed.group(1)) * cls.UNIT_TO_PX[fixed.group(2)]
            if pixels > cls.MAX_FIRST_TRACK_PX:
                continue
            found.setdefault(' '.join(selector.split()), []).append(pixels)
        return found

    # ---------------------------------------------------------------- 模板源

    @classmethod
    def _template_sources(cls):
        """产出 (相对路径, 已内联 include 的模板源)。"""
        for template in sorted(TEMPLATES_DIR.rglob('*.html')):
            relative = template.relative_to(TEMPLATES_DIR).as_posix()
            yield relative, cls._with_includes(
                template.read_text(encoding='utf-8'), frozenset(), 0)

    @classmethod
    def _with_includes(cls, text, seen, depth):
        if depth >= cls.MAX_INCLUDE_DEPTH:
            return text

        def inline(match):
            name = match.group(1)
            if name in seen:
                return ''
            target = TEMPLATES_DIR / name
            if not target.is_file():
                return ''
            return cls._with_includes(
                target.read_text(encoding='utf-8'), seen | {name}, depth + 1)

        return cls.INCLUDE.sub(inline, text)

    # ------------------------------------------------------------ 选择器匹配

    def _compound_matches(self, node, compound):
        tests = []
        for match in self.SIMPLE_SELECTOR.finditer(compound):
            name, tag, attr, double, single, bare, negated = match.groups()
            if name:
                tests.append(lambda n, v=name: v in (n.attrs.get('class') or '').split())
            elif tag:
                tests.append(lambda n, v=tag: n.tag == v)
            elif attr:
                expected = double if double is not None else (
                    single if single is not None else bare)
                if expected is None:
                    tests.append(lambda n, v=attr: v in n.attrs)
                else:
                    tests.append(
                        lambda n, v=attr, e=expected.strip():
                            (n.attrs.get(v) or '').strip() == e)
            elif negated:
                tests.append(lambda n, v=negated: not self._compound_matches(n, v))
        if not tests:
            return False
        return all(test(node) for test in tests)

    def _selector_matches(self, node, selector):
        compounds = selector.split()
        if not self._compound_matches(node, compounds[-1]):
            return False
        ancestor, index = node.parent, len(compounds) - 2
        while index >= 0:
            if ancestor is None or ancestor.tag == '#root':
                return False
            if self._compound_matches(ancestor, compounds[index]):
                index -= 1
            ancestor = ancestor.parent
        return True

    @staticmethod
    def _first_element(node):
        for child in node.children:
            if not isinstance(child, str):
                return child
        return None

    def _describe(self, node):
        first = self._first_element(node)
        if first is None:
            return '没有子元素'
        return '<%s class="%s">' % (first.tag, (first.attrs.get('class') or '').strip())

    # ------------------------------------------------------------------ 检查

    def test_every_narrow_first_track_rule_is_registered(self):
        registered = (set(self.GRID_FIRST_CELL_CONTRACTS)
                      | set(self.GRID_FIRST_CELL_PSEUDO_CONTRACTS))
        found = self._narrow_first_tracks()
        self.assertGreater(
            len(found), 0, '一条窄首列规则都没扫到，解析大概失效了')
        unregistered = sorted(
            '%s（首轨 %gpx）' % (selector, min(tracks))
            for selector, tracks in found.items() if selector not in registered)
        self.assertEqual(
            unregistered, [],
            '这些 grid 规则的第一轨是固定窄宽度，首格放什么必须先登记进契约表'
            '（GRID_FIRST_CELL_CONTRACTS / GRID_FIRST_CELL_PSEUDO_CONTRACTS）：\n'
            + '\n'.join('  ' + item for item in unregistered))

    def test_narrow_first_track_cells_only_hold_expected_content(self):
        problems = []
        matched = {}
        parsed = [(relative, _parse(text).root) for relative, text in self._template_sources()]
        for selector, allowed in self.GRID_FIRST_CELL_CONTRACTS.items():
            for relative, root in parsed:
                for node in _iter_nodes(root):
                    if not self._selector_matches(node, selector):
                        continue
                    matched[selector] = matched.get(selector, 0) + 1
                    if self._cell_allowed(node, allowed):
                        continue
                    problems.append('%s：%s 的首格是 %s' % (relative, selector, self._describe(node)))

        for selector in sorted(self.GRID_FIRST_CELL_CONTRACTS):
            self.assertIn(
                selector, matched,
                '契约表里的 %s 在模板里一次都没匹配到，检查已经空转了' % selector)
        self.assertEqual(
            problems, [],
            '窄首列里放了放不下的内容，要么换内容，要么给行加修饰类把首轨改宽：\n'
            + '\n'.join('  ' + item for item in sorted(problems)))

    def _cell_allowed(self, node, allowed):
        first = self._first_element(node)
        if first is None:
            return False
        row_classes = (node.attrs.get('class') or '').split()
        first_classes = (first.attrs.get('class') or '').split()
        for (tag, required), row_required in allowed:
            if first.tag != tag:
                continue
            if not set(required) <= set(first_classes):
                continue
            if not set(row_required) <= set(row_classes):
                continue
            return True
        return False

    def test_pseudo_element_first_cells_stay_short_enough(self):
        problems = []
        matched = set()
        for selector, (attribute, limit) in self.GRID_FIRST_CELL_PSEUDO_CONTRACTS.items():
            # ::before 那条规则的选择器是同一个前缀加 ::before，用前缀匹配一起收进来。
            bodies = [body for sel, body in self._rules()
                      if ' '.join(sel.split()).startswith(selector)]
            self.assertTrue(
                bodies, '契约表里的 %s 在样式表里已经不存在了' % selector)
            self.assertTrue(
                any('attr(%s)' % attribute in body for body in bodies),
                '%s 的首格不再是 ::before 伪元素，契约该换个登记方式了' % selector)
            for relative, text in self._template_sources():
                for node in _iter_nodes(_parse(text).root):
                    if not self._selector_matches(node, selector):
                        continue
                    matched.add(selector)
                    value = (node.attrs.get(attribute) or '').strip()
                    if len(value) > limit:
                        problems.append(
                            '%s：%s="%s" 有 %d 个字，%dpx 首轨放不下'
                            % (relative, attribute, value, len(value), limit))
        self.assertTrue(
            matched, '伪元素契约一条都没匹配到，检查已经空转了')
        self.assertEqual(
            problems, [],
            '窄屏时这些标签要挤进 ::before 伪元素，超长就会把内容顶出轨道：\n'
            + '\n'.join('  ' + item for item in sorted(problems)))

    def test_lazy_loaded_sections_stay_marked_for_forced_reveal(self):
        """懒加载分区必须留着标记，否则任何静态/渲染审计都会整片漏掉它们。

        运营看板 34 个分区、搜索洞察 13 个分区，在 html.js 下默认
        display:none，靠 IntersectionObserver 滚到才展开。浏览器审计不显式
        去掉 data-ops-deferred / data-si-deferred 再补 is-revealed，量到的
        就是一个空壳页面——上一轮整整两轮扫描都是这么白跑的。
        """
        problems = []
        css = self.STYLESHEET.read_text(encoding='utf-8')
        js = self.MAIN_JS.read_text(encoding='utf-8')
        for relative, attribute, section_class, selector in self.LAZY_SECTIONS:
            text = (TEMPLATES_DIR / relative).read_text(encoding='utf-8')
            deferred = len(re.findall(re.escape(attribute), text))
            sections = len(re.findall(r'\b%s\b' % re.escape(section_class), text))
            if deferred == 0:
                problems.append('%s：一个 %s 都没有' % (relative, attribute))
            if deferred > sections:
                problems.append(
                    '%s：%s 有 %d 个，比 %s 分区总数 %d 还多'
                    % (relative, attribute, deferred, section_class, sections))
            if selector not in js:
                problems.append('%s：JS 里找不到 %s' % (relative, selector))
        if not self.LAZY_HIDE_RULE.search(css):
            problems.append('样式表里没有 html.js 下懒加载分区的 display:none 规则')

        self.assertEqual(
            problems, [],
            '懒加载分区改坏了。这些分区无 JS 时不可见，任何溢出/视觉审计都必须'
            '先强制展开，标记一掉就会整片漏测：\n'
            + '\n'.join('  ' + item for item in problems))


class FilterPanelFlexTests(SimpleTestCase):
    """横向筛选行里的表单控件必须允许收缩，否则行会在中间视口溢出面板。

    通知中心的筛选行是四个控件一排：搜索框、两个下拉、一个提交按钮。搜索框写的是
    flex: 1 1 auto，两个下拉写的是 flex: 0 0 190px——刚性地钉在 190px。窄于 768px
    时有 flex-wrap: wrap 兜底，一行放不下就折行；可 768px 到 1023px 这一段既没折行、
    控件又都不肯让宽度，四个控件加三个间距需要 708px，而面板内容盒只有 672px，
    「应用筛选」按钮就整颗探到圆角边框外面 24px。页面上看不出病因：容器
    overflow-x 是 visible，页面本身不出现横向滚动条，只有按钮超出了自己的面板。

    私信页用的是同一个 .inbox-filter-panel，但只有一个下拉，放得下，所以同一个
    样式表在两个页面上一个出事一个不出事——这类缺陷只有把页面真的拉到那个宽度
    才照得到，而那一宽度落在常用笔记本和桌面之间，很容易整轮漏掉。

    这里改成约束样式表，秒级跑完：

    1. 收缩系数。筛选行里的表单控件不能是 flex: 0 0 <长度>，必须带 shrink，
       窄到放不下时自己让宽度，而不是把同一行的兄弟控件顶出容器。
    2. min-width: 0。<select> 的 min-content 宽度由最长的那条 option 决定，
       只给 flex-shrink 也会被 min-width:auto 挡死，必须显式置 0。
    3. 折行兜底。窄屏那条 flex-wrap: wrap 的媒体查询不能删，它是收缩也不够时的
       最后一道防线；断点也必须留在 768px 附近，不能被人随手抬上去。
    """

    STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'
    CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)
    FLEX_SHORTHAND = re.compile(r'flex\s*:\s*(-?[\d.]+)\s+(-?[\d.]+)\s+([^;]+);')
    FLEX_SHRINK_ONLY = re.compile(r'flex-shrink\s*:\s*(-?[\d.]+)')
    MIN_WIDTH_ZERO = re.compile(r'min-width\s*:\s*0(?:\s*;|\s*$)')
    MEDIA_QUERY = re.compile(
        r'@media\s+\(max-width:\s*([\d.]+)px\)\s*\{([\s\S]*?)\n\}')
    # 竖着叠放的兜底：控件占满一整行，flex 自然换行。
    STACK_BASIS = re.compile(r'flex-basis\s*:\s*100%')

    # 筛选行里的表单控件：必须允许收缩。
    SHRINKABLE_FILTER_CONTROLS = ('.inbox-status-field',)
    # 折行兜底所在的媒体查询上界。
    WRAP_BREAKPOINT_PX = 768
    WRAP_SELECTOR = '.inbox-filter-panel'

    @classmethod
    def _base_rules(cls, selector):
        """只取基础层（不在任何 @media 里）的规则。"""
        css = cls.CSS_COMMENT.sub(
            lambda match: ''.join('\n' if ch == '\n' else ' ' for ch in match.group(0)),
            cls.STYLESHEET.read_text(encoding='utf-8'))
        found, cursor = [], 0
        while True:
            brace = css.find('{', cursor)
            if brace < 0:
                return found
            prelude = css[cursor:brace].strip()
            depth, index = 1, brace + 1
            while index < len(css) and depth:
                if css[index] == '{':
                    depth += 1
                elif css[index] == '}':
                    depth -= 1
                index += 1
            if not prelude.startswith('@') and ' '.join(prelude.split()) == selector:
                found.append(css[brace + 1:index - 1])
            cursor = index

    def test_filter_controls_can_shrink(self):
        problems = []
        for selector in self.SHRINKABLE_FILTER_CONTROLS:
            bodies = self._base_rules(selector)
            self.assertTrue(
                bodies, '样式表里找不到 %s 的基础规则' % selector)
            for body in bodies:
                shorthand = self.FLEX_SHORTHAND.search(body)
                if shorthand:
                    shrink = float(shorthand.group(2))
                else:
                    only = self.FLEX_SHRINK_ONLY.search(body)
                    shrink = float(only.group(1)) if only else 1.0
                if shrink < 1:
                    problems.append(
                        '%s 的 flex 收缩系数是 %g：放不下时不肯让宽度，'
                        '会把同一行的兄弟控件顶出容器' % (selector, shrink))
                if not self.MIN_WIDTH_ZERO.search(body):
                    problems.append(
                        '%s 没有 min-width: 0：<select> 的 min-content 宽度由最长 '
                        'option 决定，flex-shrink 会被 min-width:auto 挡死' % selector)
        self.assertEqual(
            problems, [],
            '筛选行的表单控件改坏了。中间视口（768–1023px）既没折行兜底、'
            '控件又都不肯收缩，整行会溢出面板边框：\n'
            + '\n'.join('  ' + item for item in problems))

    def test_filter_panel_keeps_a_wrapping_fallback(self):
        """窄屏必须还能整行折起来，收缩到底也放不下时靠它兜底。"""
        problems = []
        saw_panel = False
        for match in self.MEDIA_QUERY.finditer(
                self.STYLESHEET.read_text(encoding='utf-8')):
            limit, body = float(match.group(1)), match.group(2)
            if self.WRAP_SELECTOR not in body:
                continue
            saw_panel = True
            # 断行有两条正当路子：整行开 flex-wrap，或者把每个控件按 100%
            #  basis 竖着叠起来。两条都没有，就等于这一档屏幕没有任何兜底。
            if 'flex-wrap: wrap' not in body and not self.STACK_BASIS.search(body):
                problems.append('%gpx 的断点里筛选行既不折行也不叠放' % limit)
            if limit > self.WRAP_BREAKPOINT_PX:
                problems.append(
                    '折行断点被抬到 %gpx：768–1023px 这段会连折行兜底一起失去'
                    % limit)
        self.assertTrue(
            saw_panel,
            '一条 @media 都没碰到 %s，这项检查空转了' % self.WRAP_SELECTOR)
        self.assertEqual(
            problems, [],
            '筛选行的折行兜底被改坏了：\n'
            + '\n'.join('  ' + item for item in problems))

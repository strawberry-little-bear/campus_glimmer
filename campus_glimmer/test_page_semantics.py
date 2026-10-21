"""页面语义守卫：标题层级、表单标签、可访问名称、图片替代文本。

这一层以前靠浏览器审计兜。浏览器审计有两个绕不开的代价：一是要起真实服务、
装驱动、逐页访问，跑一轮以小时计；二是它只看得到当前数据恰好渲染出来的分支，
没数据的空状态、别人的视角、只有发布者看得见的区块，全都测不到。

于是真实缺陷会以很低的概率漏过去：商品详情页用 h6 写「商品描述」，在首页到
h1 之后直接跳了五级；卡片标题用 h3，让整个列表页从 h1 跳到 h3；八处空状态
提示用 h3，读屏软件里跟同级正文标题混在一起。这些问题在页面上看不出来，
在 DOM 里一目了然。

这里用 Django test client 渲染同一批页面，再配一个 stdlib 的轻量 DOM 做同样的
检查。没有新依赖，跑起来是秒级，可以挂在每次提交上。

四类检查各有分工：

1. TemplateHeadingOrderTests 只读模板文件，不渲染。它能在没有数据、没有请求的
   情况下发现标题跳级，覆盖浏览器审计够不到的分支（空状态、他人视角）。
2. RenderedPageSemanticsTests 用一套演示数据把主要页面都渲染一遍，检查表单
   控件有没有标签、可交互元素有没有可访问名称、图片有没有 alt，以及 id 引用
   是否完整。前两项只有在真实 DOM 里才查得出来，静态扫描给不出答案。
3. TouchTargetTests 盯导航链接的触控尺寸。鼠标点得中的东西手指不一定点得中，
   而导航是唯一出现在每一页上的交互元素。

第 2 类里的 id 引用完整性是后来补的，起因是一次真实的线上缺陷：Django 的
BoundField.aria_describedby 会自动拼出 `id_<字段名>_helptext` /
`id_<字段名>_error`，而模板把 help_text 和错误渲染成了不带 id 的元素，于是
全站几十个表单的帮助文字和错误提示对读屏软件完全不可见——页面上看不出来，
DOM 里一抓一个准。重复 id 更隐蔽：HTMLParser 用「不存在才记」的方式收集
by_id，第二次出现会被静默折叠，上面几类检查对它完全免疫。
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

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
    Order,
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
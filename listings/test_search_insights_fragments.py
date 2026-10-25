# -*- coding: utf-8 -*-
"""搜索洞察分区按需取回的契约测试。

``search_insights_fragments.py`` 里那张表是「渲染这个分区需要哪些上下文」的唯一
权威。它是手维护的，而搜索洞察有 15 个分区、9 个统计块和 8 个独立面板——漏登记
一个键，症状是某个分区在懒加载路径下静默变成空态，而不带 ``si_lazy`` cookie 的
请求（本地默认走的就是这条）看起来完全正常。

所以这个文件做三件事：

* **反查**。拿一次真实渲染插桩，看每个 partial 到底读了哪些顶层键，再和表对。
  表里少写一个键、或者声明了却一个键都没用上，这里就红。
* **端点权限**。取回端点和整页同一套门槛：staff。匿名和非 staff 一样直接
  403（不跳登录页：这个端点的调用方是已经打开页面的 JS，跳过去没有意义）、
  锚点不存在 404——悄悄返回空片段比报错更难发现。
* **等价**。取回回来的 HTML，必须和不做懒加载时那一块一模一样。不一致就说明
  两条渲染路径的上下文不同，用户会在同一页里看到两套不一样的洞察。

页面那一半（哪些分区先渲染、CSS 有没有挂在 ``html.js`` 下面、JS 是不是只换类）
在 ``test_operations_deferred_sections`` 里，两个页面共用那套兜底。
"""
import csv
import io
import os
import re
from urllib.parse import quote

from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.client import RequestFactory
from django.urls import reverse

from . import operations_fragments, search_insights_fragments, views
from .analytics import SEARCH_INSIGHTS_PARTS, _close_search_insight_parts
from .search_insights_fragments import (
    SEARCH_INSIGHTS_FIRST_SCREEN,
    SEARCH_INSIGHTS_SECTION_FRAGMENTS,
    section_anchors,
)
from .views import SEARCH_INSIGHTS_PANEL_BUILDERS, _build_shared_search_insights

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_JS_PATH = os.path.join(PROJECT_ROOT, 'static', 'js', 'main.js')

# 视图和共享构建器自己补的键，和分区无关：整页和取回端点都会给，反查时单独归
# 一类。rejection_reasons 不查库、无条件给全，整页和分区取回必须看到同一份理由
# 列表，否则同一个下拉框在两种入口下选项会不一样，而那种不一致在页面上看不出来。
VIEW_LEVEL_KEYS = frozenset({
    'period_days', 'period_choices', 'period_start', 'period_end',
    'query_filter', 'rejection_reasons', 'si_lazy',
})

SECTION_OPEN_RE = re.compile(
    r'<section[^>]*\bclass="[^"]*\bsi-section\b[^"]*"[^>]*\bid="([^"]+)"'
)
DEFERRED_ATTR_RE = re.compile(r'\bdata-si-deferred\b')
# 分区里的真实内容标记：设计体系里的面板、空态和表格都算。搜索洞察的 partial
# 全都有 content-card 或 ops-metric-*，少了这一条懒加载测试就分不出「裁干净了」
# 和「裁出了白板」。
CONTENT_MARK_RE = re.compile(
    r'class="[^"]*\b(?:content-card|ops-panel|ops-table|ops-metric-card|ops-metric-grid'
    r'|empty-inline|search-quality-panel)\b'
)


class Recording(dict):
    """记录模板真正读到过哪些顶层键。

    必须用不带 request 的 ``render_to_string``：走了 RequestContext 的话，
    ``push()`` 内部会把整个 context dict 拷贝成普通 dict，插桩全部失效——
    输出会是空的，而这种「测试通过但其实什么都没测」最难发现。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accessed = set()

    def __contains__(self, key):
        hit = super().__contains__(key)
        if hit:
            self.accessed.add(key)
        return hit

    def __getitem__(self, key):
        if super().__contains__(key):
            self.accessed.add(key)
        return super().__getitem__(key)


def normalize(html):
    """抹掉两次请求之间注定不一样的字节。

    两样东西：CSRF 令牌（每次请求重新生成）和所有数字（计数、百分比、日期）。
    留着它们，等价性断言就永远不可能通过，而它们又都不是「分区渲染路径不同」
    的证据。
    """
    html = re.sub(r'(name="csrfmiddlewaretoken" value=")[^"]*(")', r'\1CSRF\2', html)
    return re.sub(r'\d+(?:\.\d+)?', 'N', html)


TAG = re.compile(r"<[^>]+>")


def panel_titles_in(html):
    """一段 HTML 里的面板标题（h2）：剥标签、去空白、按出现顺序去重。"""
    titles = []
    for raw_title in re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S):
        title = TAG.sub("", raw_title).strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def csv_sections(text):
    """CSV 里每一行第一个非空格子的集合，节标题和子标题都是这么落的。"""
    return {row[0].strip() for row in csv.reader(io.StringIO(text)) if row and row[0].strip()}


def section_inner(html, anchor):
    """返回页面上某个分区 <section> 内部的 HTML（不含 section 标签本身）。

    不能用非贪婪匹配：partial 里有嵌套 <section>，那样会在第一个嵌套结束处
    就剥断。这里按标签深度数，数到配对的 </section> 为止。
    """
    opened = re.compile(
        r'<section\b[^>]*\bclass="[^"]*\bsi-section\b[^"]*"[^>]*\bid="%s"[^>]*>'
        % re.escape(anchor)
    )
    match = opened.search(html)
    assert match, '页面上找不到分区 %s' % anchor
    start = match.end()
    depth = 1
    for token in re.finditer(r'<section\b|</section\s*>', html[start:]):
        depth += -1 if token.group(0).startswith('</') else 1
        if depth == 0:
            return html[start:start + token.start()]
    raise AssertionError('分区 %s 没有闭合的 </section>' % anchor)


def parse_sections(html):
    """按页面顺序返回 [(anchor, 是否延后)]。"""
    found = []
    for match in SECTION_OPEN_RE.finditer(html):
        tail = html[match.end():match.end() + 200]
        found.append((match.group(1), bool(DEFERRED_ATTR_RE.search(tail))))
    return found


def section_blocks(html):
    """把页面按分区切成块，返回 {anchor: 原始 HTML}。

    不能用非贪婪的 ``(.*?)</section>``：partial 里有嵌套 <section>，那样会在
    第一个嵌套结束处就剥断。改为按下一个分区开始位置割行，和嵌套深度无关。
    """
    marks = [(m.start(), m.group(1)) for m in SECTION_OPEN_RE.finditer(html)]
    blocks = {}
    for index, (start, anchor) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(html)
        blocks[anchor] = html[start:end]
    return blocks


class SearchInsightsFragmentTableTests(TestCase):
    """那张表和真实模板之间的对账。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='si-frag-table-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        self.html = self.client.get(
            reverse('search_insights'), {'days': 30},
        ).content.decode('utf-8')

    def test_the_table_order_matches_the_page_order(self):
        # 顺序错乱的后果很具体：取回端点按表顺序无所谓，但页面上分区顺序和表对不上
        # 时，首屏两个分区就不再是「折叠线以上」那两个，滚动加载会从页面中部开始。
        page_order = [match.group(1) for match in SECTION_OPEN_RE.finditer(self.html)]

        self.assertEqual(
            list(section_anchors()), page_order,
            '表的顺序和页面上的分区顺序不一致',
        )

    def test_every_partial_named_by_the_table_exists(self):
        missing = [
            partial for _anchor, partial, _parts, _panels in SEARCH_INSIGHTS_SECTION_FRAGMENTS
            if not os.path.exists(os.path.join(
                PROJECT_ROOT, 'templates', 'listings', 'search_insights', partial,
            ))
        ]
        self.assertEqual(missing, [], '表里点名的 partial 不存在')

    def test_the_first_screen_is_exactly_the_two_scorecard_sections(self):
        # 首屏只有指标卡和搜索质量分：八张卡加一个总分就是这页的记分卡。多一个
        # 分区进首屏，懒加载省下的字节就少一块；少一个，用户第一眼看到的是半页。
        self.assertEqual(
            set(SEARCH_INSIGHTS_FIRST_SCREEN), {'si-metrics', 'si-quality'},
            '首屏集合变了，先确认它仍然是折叠线以上的那一组',
        )
        self.assertEqual(len(SEARCH_INSIGHTS_FIRST_SCREEN), 2, '首屏分区数量变了')

    def test_every_section_declares_something_it_needs(self):
        empty = [
            anchor for anchor, _partial, parts, panels in SEARCH_INSIGHTS_SECTION_FRAGMENTS
            if not parts and not panels
        ]
        self.assertEqual(empty, [], '这些分区什么都没声明，取回回来只会是空壳')

    def test_every_statistical_part_is_claimed_by_some_section(self):
        # 统计块是 build_search_insights 内部的分支，没有任何分区声明它就永远
        # 不跑——那是最难发现的一种死代码：没人报错，只是那部分查询白写了。
        declared = set()
        for _anchor, _partial, parts, _panels in SEARCH_INSIGHTS_SECTION_FRAGMENTS:
            declared |= _close_search_insight_parts(parts)

        self.assertEqual(
            sorted(set(SEARCH_INSIGHTS_PARTS) - declared), [],
            '这些统计块没有任何分区声明，跑了也没人用',
        )

    def test_every_panel_builder_is_claimed_by_some_section(self):
        declared = {
            key
            for _anchor, _partial, _parts, panels in SEARCH_INSIGHTS_SECTION_FRAGMENTS
            for key in panels
        }
        registered = {key for key, _label, _builder in SEARCH_INSIGHTS_PANEL_BUILDERS}

        self.assertEqual(
            sorted(registered - declared), [],
            '这些面板构建器没有任何分区声明，跑了也没人用',
        )

    def test_the_table_matches_what_the_partials_actually_read(self):
        """反查：partial 真读了哪些键，就必须落在它声明的能力范围内。"""
        user = User.objects.create_user(username='si-frag-probe', password='x', is_staff=True)
        request = RequestFactory().get('/listings/operations/search-insights/')
        request.user = user
        get_token(request)
        # 整页渲染时服务端手里的全部键——比任何一个分区需要的都多。
        full = dict(_build_shared_search_insights(days=30))
        full['si_lazy'] = False

        mismatches = []
        for anchor, partial, parts, panels in SEARCH_INSIGHTS_SECTION_FRAGMENTS:
            # 这个分区单独取回时，服务端手里真会有的键。
            sliced = _build_shared_search_insights(days=30, anchors=(anchor,))
            available = VIEW_LEVEL_KEYS | set(sliced)

            context = Recording(full)
            context['request'] = request
            context['csrf_token'] = 'probe'
            render_to_string('listings/search_insights/' + partial, context)
            used = context.accessed - {'request', 'csrf_token'}

            missing = used - available
            if missing:
                mismatches.append(
                    '%s 读了 %s，但这些键不在它声明的能力范围内，取回时会静默变成空态'
                    % (anchor, sorted(missing))
                )
                continue

            # 反向：声明了统计块或面板，就得真的用上，否则每次取回都在白跑最贵的
            # 那一部分查询。整页那条路径看不出浪费，因为反正全都要跑。
            if not (used & (available - VIEW_LEVEL_KEYS)):
                mismatches.append(
                    '%s 声明了 %s / %s，但渲染它一个键都没读到' % (anchor, list(parts), list(panels))
                )

        self.assertEqual(mismatches, [], '\n'.join(mismatches))


class SearchInsightsFragmentEndpointTests(TestCase):
    """取回端点的权限与内容。"""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='si-frag-staff', password='safe-password-123', is_staff=True,
        )
        self.member = User.objects.create_user(
            username='si-frag-member', password='safe-password-123',
        )
        self.url = reverse('search_insights_fragment', kwargs={'anchor': 'si-terms'})

    def test_staff_gets_the_section(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url, {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertIn('content-card', response.content.decode('utf-8'))

    def test_anonymous_visitors_get_the_same_403_as_non_staff(self):
        # 取回端点故意不挂 @login_required：它的调用方是已经登录打开洞察页的 JS，
        # 匿名请求只会来自手敲地址或爬虫，跳登录页反而把「这个地址存在」暴露出去。
        # 和整页的 is_staff 门槛保持一致：403。
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)

    def test_non_staff_members_get_403(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)

    def test_unknown_anchors_are_404(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse('search_insights_fragment', kwargs={'anchor': 'si-nope'}),
        )

        self.assertEqual(response.status_code, 404)

    def test_the_cookie_does_not_change_what_a_fragment_renders(self):
        # cookie 只决定首屏，不决定单个分区怎么渲染。少了这条，带着 cookie 打开
        # 取回端点会拿到一个空壳，页面就再也填不上了。
        self.client.force_login(self.staff)
        plain = self.client.get(self.url, {'days': 30}).content.decode('utf-8')
        self.client.cookies['si_lazy'] = '1'
        lazy = self.client.get(self.url, {'days': 30}).content.decode('utf-8')

        self.assertEqual(normalize(plain), normalize(lazy))


class SearchInsightsFragmentEquivalenceTests(TestCase):
    """取回回来的内容，必须和不做懒加载时那一块一模一样。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='si-frag-equiv-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        self.full = self.client.get(
            reverse('search_insights'), {'days': 30},
        ).content.decode('utf-8')

    def test_every_fragment_matches_the_section_inside_the_full_page(self):
        mismatches = []
        for anchor in section_anchors():
            expected = normalize(section_inner(self.full, anchor))
            actual = normalize(self.client.get(
                reverse('search_insights_fragment', kwargs={'anchor': anchor}),
                {'days': 30},
            ).content.decode('utf-8'))

            if expected != actual:
                mismatches.append(anchor)

        self.assertEqual(
            mismatches, [],
            '这些分区取回回来的内容和不做懒加载时不一样：%s' % mismatches,
        )

    def test_the_first_screen_sections_are_identical_too(self):
        # 首屏两个分区不取回，但它们的 partial 也走同一张表。改表时最容易顺手
        # 把首屏某个分区标错，症状是首屏少一块内容——这里一并兜住。
        for anchor in SEARCH_INSIGHTS_FIRST_SCREEN:
            expected = normalize(section_inner(self.full, anchor))
            actual = normalize(self.client.get(
                reverse('search_insights_fragment', kwargs={'anchor': anchor}),
                {'days': 30},
            ).content.decode('utf-8'))
            self.assertEqual(expected, actual, '首屏分区 %s 取回结果和页面不一致' % anchor)

    def test_the_export_path_still_renders_the_whole_page(self):
        # 导出永远走整份：CSV 是一次性、离线的，没有人「滚到这里再导」。
        # 这条盯着 anchors=None 那条分支——它一旦被改成按首屏裁，导出的 CSV 会
        # 少掉十三个分区，而页面看不出任何异常。
        # 比对单位是「面板标题」，不是分区 HTML：CSV 是另一种载体，HTML 里那套
        # 标签和链接本来就不该、也不可能出现在表格里。
        full = self.client.get(reverse('search_insights'), {'days': 30}).content.decode('utf-8')
        export = self.client.get(reverse('search_insights_export'), {'days': 30}).content.decode('utf-8-sig')
        sections = csv_sections(export)

        for anchor in section_anchors():
            if anchor in SEARCH_INSIGHTS_FIRST_SCREEN:
                continue
            titles = panel_titles_in(section_inner(full, anchor))
            self.assertTrue(titles, '分区 %s 里一个小标题都没有' % anchor)
            for title in titles:
                self.assertIn(
                    title, sections,
                    '分区 %s 的标题「%s」没有出现在导出里' % (anchor, title),
                )


class SearchInsightsLazyLoadingTests(TestCase):
    """带着 si_lazy cookie 打开洞察页时，服务端只渲染首屏两个分区。

    这是把延后从「CSS 藏起来」升级成「服务端也不渲染」的那一半。风险全在
    「内容消失」上：cookie 种下了、地址没给，或者地址给了但内容没裁干净，
    用户在浏览器里看到的就是 13 个永远转圈的骨架，而 Django 测试客户端
    （不带 cookie）一切正常——所以这里必须用带 cookie 的客户端跑一遍。
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='si-lazy-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        # cookie 由 base.html 的内联脚本种，这里直接模拟「第二次打开」。
        self.client.cookies['si_lazy'] = '1'
        self.html = self.client.get(
            reverse('search_insights'), {'days': 30},
        ).content.decode('utf-8')
        self.sections = parse_sections(self.html)

    def test_only_the_first_two_sections_carry_content(self):
        blocks = section_blocks(self.html)
        rendered = [
            anchor for anchor, _deferred in self.sections
            if CONTENT_MARK_RE.search(blocks[anchor])
        ]

        self.assertEqual(
            rendered, [anchor for anchor, _ in self.sections[:2]],
            '懒加载时应该只有首屏两个分区有内容，现在多渲染了：%s' % (
                sorted(set(rendered) - SEARCH_INSIGHTS_FIRST_SCREEN),
            ),
        )

    def test_every_deferred_section_points_at_its_fragment_endpoint(self):
        # 空壳必须带取回地址，否则就是一块永远转圈的骨架。属性与内容同进同退：
        # 有 data-si-src 就必须有 {% if si_lazy %} 包着的那段，反之亦然。
        blocks = section_blocks(self.html)
        missing = []
        wrong = []
        for anchor, _deferred in self.sections[2:]:
            block = blocks[anchor]
            match = re.search(r'data-si-src="([^"]+)"', block)
            if not match:
                missing.append(anchor)
                continue
            expected = reverse(
                'search_insights_fragment', kwargs={'anchor': anchor},
            ) + '?days=30&amp;q='
            if match.group(1) != expected:
                wrong.append((anchor, match.group(1), expected))

        self.assertEqual(missing, [], '这些分区是空的却没有取回地址')
        self.assertEqual(wrong, [], '这些分区的取回地址不对')

    def test_the_fragment_url_carries_the_active_filters(self):
        # 取回地址必须带上当前筛选：用户在洞察页筛了「台灯」，滚到底部取回回来的
        # 分区还按全量统计，就是同一页里两套数据。&amp; 是 HTML 转义后的 &，
        # 少了它属性值会在第一个 & 处被截断。
        html = self.client.get(
            reverse('search_insights'), {'days': 7, 'q': '台灯'},
        ).content.decode('utf-8')
        block = section_blocks(html)['si-terms']
        match = re.search(r'data-si-src="([^"]+)"', block)

        self.assertIsNotNone(match, '带筛选的懒加载分区没有取回地址')
        expected = (
            reverse('search_insights_fragment', kwargs={'anchor': 'si-terms'})
            + '?days=7&amp;q=' + quote('台灯', safe='')
        )
        self.assertEqual(match.group(1), expected)

    def test_lazy_page_is_much_smaller_than_the_full_one(self):
        # 懒加载的意义就在字节数上。这条不是性能基准，只拦住「裁了但没裁多少」
        # 和「干脆没裁」两种回归。
        # cookie 挂在客户端上，不在响应对象上：先摘掉再发一次请求，才是「第一次
        # 打开洞察页」那条全量渲染的路径。
        del self.client.cookies['si_lazy']
        full_html = self.client.get(
            reverse('search_insights'), {'days': 30},
        ).content.decode('utf-8')

        # 45% 是运营看板的阈值，照搬过来会假红：这一页的固定外壳（页头、工具栏、
        # 页脚、基座模板）占比比看板高得多，十三个分区能砍掉的字节天然更少。
        self.assertLess(
            len(self.html) * 100, len(full_html) * 55,
            '懒加载页面只比全量页面小了 %.1f%%，分区内容大概没真裁掉' % (
                100 - len(self.html) * 100.0 / len(full_html),
            ),
        )

    def test_the_fragment_endpoint_ignores_the_cookie(self):
        # 取回端点永远渲染自己那一个分区，和 cookie 无关：cookie 只决定首屏，
        # 不决定单个分区怎么渲染。
        response = self.client.get(
            reverse('search_insights_fragment', kwargs={'anchor': 'si-insights'}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            CONTENT_MARK_RE.search(response.content.decode('utf-8')),
            '取回 si-insights 拿到了一段没有面板、空态或表格的内容',
        )


class SearchInsightsRevealContractTests(TestCase):
    """洞察页那一半的懒加载：JS 必须真的把分区取回来。

    上面几条都在服务端，盯的是「裁没裁、地址对不对」。裁掉的内容要靠 main.js
    填回去，而填回去这件事在 Django 测试客户端里完全看不出来。这里读源码把那
    几条约定钉住：洞察页必须复用同一个工厂，必须观察滚动，也必须处理 hash 直达
    ——从看板点进某个分区时，浏览器是按占位高度算偏移的，不先把前面的分区取回来，
    真实内容到位后整页会位移，用户看到的是另一个分区。
    """

    def setUp(self):
        self.js = io.open(MAIN_JS_PATH, encoding='utf-8').read()
        self.start = self.js.index(
            "const siPanels = document.querySelector('.search-insights-panels');"
        )
        self.end = self.js.index("const chat = document.querySelector('.chat-messages');")
        self.si_block = self.js[self.start:self.end]

    def test_the_insights_page_reuses_the_shared_factory(self):
        self.assertIn('createSectionRevealer(siPanels, {', self.si_block)
        self.assertIn(
            "deferredSelector: '.si-section[data-si-deferred]'", self.si_block,
            '洞察页没有用自己的延后选择器，会去看运营看板的分区',
        )
        self.assertIn("srcAttr: 'data-si-src'", self.si_block)
        self.assertIn("failedAttr: 'data-si-failed'", self.si_block)

    def test_the_insights_page_watches_scrolling_and_hash_jumps(self):
        self.assertIn('IntersectionObserver', self.si_block, '洞察页没有观察滚动')
        self.assertIn('siRevealThrough(record.target)', self.si_block)
        self.assertIn(
            'siRevealThrough(null)', self.si_block,
            '没有 IntersectionObserver 的旧浏览器要一次揭完，否则整页停在骨架态',
        )
        self.assertIn('hashchange', self.si_block, 'hash 直达没有重新定位')
        self.assertIn('siRevealThrough(section)', self.si_block)

    def test_the_insights_page_never_reveals_a_section_it_did_not_ask_for(self):
        # 洞察页没有跳转条，所以不存在「点 chip 跳到很远」；但工厂的揭示方向
        # 是共用的，这里顺手确认洞察页没有另起一套揭开逻辑。
        self.assertNotIn("classList.add('is-revealed')", self.si_block)


class HelperNameCollisionTests(TestCase):
    """两个分区模块有同名助手，views 里必须都起别名。

    ``search_insights_fragments`` 的 ``section_panel_keys`` / ``section_partial``
    和 ``operations_fragments`` 里的那两个同名。若在 views 里不起别名直接导入，后
    一个会把前一个盖掉：看板取回端点于是拿洞察的表去查看板的锚点，一个键都匹配
    不上，每个延后分区静默变成空态——而页面其它部分完全正常，因为不带 cookie 的
    全量渲染走的是另一条路。只有把取回结果和全量渲染放在一起比才发现，那正是
    ``test_operations_fragments`` 的等价性测试抓到的。

    这里把「views 里每个名字指向哪个模块」钉死。它红的时候症状还没出现，但原因
    已经写在断言消息里，不必再从 38 个空分区倒查。
    """

    def test_the_dashboard_helpers_still_point_at_the_dashboard_module(self):
        self.assertIs(
            views.section_panel_keys, operations_fragments.section_panel_keys,
            'views.section_panel_keys 指向了别的模块，运营看板的取回端点会拿错表',
        )
        self.assertIs(
            views.section_partial, operations_fragments.section_partial,
            'views.section_partial 指向了别的模块，运营看板的取回端点会找错 partial',
        )

    def test_the_search_insight_helpers_are_aliased_in_views(self):
        self.assertIs(
            views.search_insights_panel_keys,
            search_insights_fragments.section_panel_keys,
            '洞察的面板筛选助手没有按别名导入，_build_shared_search_insights 会拿错表',
        )
        self.assertIs(
            views.search_insights_partial,
            search_insights_fragments.section_partial,
            '洞察的 partial 查找助手没有按别名导入，取回端点会找错文件',
        )

    def test_the_two_modules_do_not_share_a_helper(self):
        # 两个模块的同名助手本来就不是一个函数：各自查各自那张表。哪天有人为了
        # 「去重」把其中一个指到另一个，上面两条会先红，这里补一句直白的理由。
        self.assertIsNot(
            operations_fragments.section_panel_keys,
            search_insights_fragments.section_panel_keys,
        )
        self.assertIsNot(
            operations_fragments.section_partial,
            search_insights_fragments.section_partial,
        )

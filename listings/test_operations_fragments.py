# -*- coding: utf-8 -*-
"""运营看板分区按需取回的契约测试。

``operations_fragments.py`` 里那张表是「渲染这个分区需要哪些上下文」的唯一权威。
它是手维护的，而看板有 38 个分区、119 个上下文键——漏登记一个键，症状是某个
面板在生产环境里静默变成空态，页面其它部分看起来完全正常，本地测试也全是绿的
（因为不带 cookie 的请求走的还是全量渲染）。

所以这个文件做三件事：

* **反查**。拿一次真实渲染插桩，看每个 partial 到底读了哪些顶层键，再和表对。
  表里少写一个键、或者多写一个根本用不到的键，这里就红。
* **端点权限**。取回端点和整页同一套门槛：登录 + staff。匿名 302、非 staff 403、
  锚点不存在 404——悄悄返回空片段比报错更难发现。
* **等价**。取回回来的 HTML，必须和不做懒加载时那一块逐字节一样。不一致就说明
  两条渲染路径的上下文不同，用户会在同一页里看到两套不一样的看板。
"""
import os
import re

from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.client import RequestFactory
from django.urls import reverse

from .operations_fragments import (
    ALL_SHARED_PANELS,
    OPERATIONS_SECTION_FRAGMENTS,
    section_anchors,
)
from .operations_nav import OPERATIONS_DASHBOARD_SECTIONS
from .views import (
    OPERATIONS_DASHBOARD_SHARED_BUILDERS,
    _build_shared_operations_dashboard,
    _operations_dashboard_extras,
    build_operations_dashboard,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 视图和共享构建器自己补的键，和分区无关：页面和取回端点都会给，所以反查时
# 单独归一类，不算「核心统计」也不算「共享面板」。
# dashboard_build_performance 由 _build_shared_operations_dashboard 自己塞，量的是
# 本次请求跑了哪些构建器，因此任何一次调用都有它，与裁不裁分区无关。
VIEW_LEVEL_KEYS = frozenset({
    'period_days', 'period_start', 'period_end', 'period_choices',
    'pending_dispute_count', 'pending_moderation_count',
    'pending_high_moderation_count', 'operations_sections', 'ops_lazy',
    'demand_radar_tasks', 'demand_radar_outcomes', 'demand_outcome_status_filter',
    'dashboard_build_performance',
})

# 唯一一个 needs_core 为真却一个核心键都不读的分区。它量的是整座看板的构建
# 耗时，核心统计那一行必须在报告里，所以裁掉它报告就是假的。
CORE_EXEMPT_ANCHORS = frozenset({'ops-build-performance'})


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

    两样东西：CSRF 令牌（每次请求重新生成）和构建耗时（跑一次变一次）。留着它们，
    等价性断言就永远不可能通过，而它们又都不是「分区渲染路径不同」的证据。
    """
    html = re.sub(r'(name="csrfmiddlewaretoken" value=")[^"]*(")', r'\1CSRF\2', html)
    return re.sub(r'\d+(?:\.\d+)?', 'N', html)


def section_inner(html, anchor):
    """返回页面上某个分区 <section> 内部的 HTML（不含 section 标签本身）。

    不能用非贪婪匹配：partial 里有嵌套 <section>，那样会在第一个嵌套结束处
    就剥断。这里按标签深度数，数到配对的 </section> 为止。
    """
    opened = re.compile(
        r'<section\b[^>]*\bclass="[^"]*\bops-section\b[^"]*"[^>]*\bid="%s"[^>]*>'
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


# 这几个分区的行按「本次请求量到的指标」倒序取前 N 行。构建耗时分区就是如此：
# 两次请求的耗时不可能一样，前 12 名也就不一样，连哪一行带「最慢」徽章都会变。
# 拿逐字节比对去要求它们，等于要求两次请求跑出一模一样的耗时——那是永远不可能
# 绿的断言，而它红的原因和「两条渲染路径不一致」毫无关系。往这个集合里加锚点
# 前，先确认那个分区真的是按运行时指标排序，而不是顺手把一条真实的渲染差异关掉。
UNORDERED_SECTIONS = frozenset({'ops-build-performance'})


def without_sorted_rows(html):
    """把按运行时指标排序的行整行抹掉，只留骨架。"""
    return re.sub(r'<tr>\s*<td><strong>[^<]*</strong></td>.*?</tr>', 'ROW', html, flags=re.S)


def sorted_row_labels(html):
    """各行第一格的标签，按出现顺序。"""
    return re.findall(r'<td><strong>([^<]*)</strong></td>', html)


class OperationsFragmentTableTests(TestCase):
    """那张表和真实模板之间的对账。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.days = 30
        cls.core = build_operations_dashboard(days=cls.days)
        cls.panel_keys = frozenset(
            key for key, _label, _builder in OPERATIONS_DASHBOARD_SHARED_BUILDERS
        )
        cls.request = RequestFactory().get('/listings/operations/')

    def setUp(self):
        # 不带 staff 会话去 GET 看板只会拿到登录跳转页，那时候「页面上的分区
        # 顺序」是个空列表——测试会红，但红得莫名其妙。所以这里必须真的登录。
        self.user = User.objects.create_user(
            username='frag-table-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)

    def _context(self):
        """整座看板的完整上下文——比任何一个分区需要的都多。"""
        dashboard = _build_shared_operations_dashboard(days=self.days)
        dashboard.update(self.core)
        dashboard = _operations_dashboard_extras(self.request, self.days, dashboard)
        dashboard['period_days'] = self.days
        dashboard['period_start'] = None
        dashboard['period_end'] = None
        dashboard['period_choices'] = ()
        dashboard['pending_dispute_count'] = 0
        dashboard['pending_moderation_count'] = 0
        dashboard['pending_high_moderation_count'] = 0
        dashboard['operations_sections'] = OPERATIONS_DASHBOARD_SECTIONS
        dashboard['ops_lazy'] = False
        return dashboard

    def test_the_table_covers_every_section_exactly_once(self):
        # 只比集合，不比顺序：表的顺序是页面顺序，而分区导航按主题重新排过，
        # 两者本来就不一致（「交易与交付」在页面上横跨第 16 到第 31 个分区）。
        nav_anchors = {
            anchor
            for _label, items in OPERATIONS_DASHBOARD_SECTIONS
            for anchor, _text in items
        }
        self.assertEqual(
            set(section_anchors()), nav_anchors,
            '表的锚点和分区导航不是同一批：多了、少了或者名字对不上',
        )
        self.assertEqual(len(section_anchors()), len(set(section_anchors())), '锚点重复')

    def test_the_table_order_matches_the_page_order(self):
        # 顺序错乱的后果很具体：跳转条按导航顺序排，分区按页面顺序排，两边对不上
        # 时用户点第 3 个 chip 会跳到第 17 个分区。所以页面顺序必须单独钉一次。
        html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        page_order = [
            match.group(1) for match in re.finditer(
                r'<section[^>]*\bclass="[^"]*\bops-section\b[^"]*"[^>]*\bid="([^"]+)"', html,
            )
        ]

        self.assertEqual(
            list(section_anchors()), page_order,
            '表的顺序和页面上的分区顺序不一致',
        )

    def test_every_partial_named_by_the_table_exists(self):
        missing = [
            partial for _anchor, partial, _core, _keys in OPERATIONS_SECTION_FRAGMENTS
            if not os.path.exists(os.path.join(
                PROJECT_ROOT, 'templates', 'listings', 'operations', partial,
            ))
        ]
        self.assertEqual(missing, [], '表里点名的 partial 不存在')

    def test_the_table_matches_what_the_templates_actually_read(self):
        """反查：模板真读了哪些键，就必须和表登记的一致。"""
        user = User.objects.create_user(username='frag-probe', password='x', is_staff=True)
        request = RequestFactory().get('/listings/operations/')
        request.user = user
        get_token(request)
        full = self._context()

        mismatches = []
        for anchor, partial, needs_core, panels in OPERATIONS_SECTION_FRAGMENTS:
            context = Recording(full)
            context['request'] = request
            context['csrf_token'] = 'probe'
            render_to_string('listings/operations/' + partial, context)
            used = context.accessed - {'request', 'csrf_token'}

            # 取回这个分区时，服务端手里真会有的键。
            available = VIEW_LEVEL_KEYS | (
                set(self.core) if needs_core else set()
            ) | (
                self.panel_keys if panels == ALL_SHARED_PANELS else set(panels)
            )
            missing = used - available
            if missing:
                mismatches.append(
                    '%s 读了 %s，但这些键不在它声明的能力范围内，取回时会静默变成空态'
                    % (anchor, sorted(missing))
                )
                continue

            # 反向：标了 needs_core 就得真的用上核心统计，否则每次取回都在白跑
            # 整座看板最贵的那一部分。唯一的例外是构建耗时分区。
            if needs_core and anchor not in CORE_EXEMPT_ANCHORS:
                covered = self.panel_keys if panels == ALL_SHARED_PANELS else set(panels)
                if not (used & set(self.core) - covered):
                    mismatches.append(
                        '%s 标了 needs_core，但没有任何一个核心键是它独占需要的' % anchor
                    )

        self.assertEqual(mismatches, [], '\n'.join(mismatches))


class OperationsFragmentEndpointTests(TestCase):
    """取回端点的权限与内容。"""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='frag-staff', password='safe-password-123', is_staff=True,
        )
        self.member = User.objects.create_user(
            username='frag-member', password='safe-password-123',
        )
        self.url = reverse('operations_dashboard_fragment', kwargs={'anchor': 'ops-demand'})

    def test_staff_gets_the_section(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url, {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertIn('ops-panel', response.content.decode('utf-8'))

    def test_anonymous_visitors_are_sent_to_the_login_page(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_non_staff_members_get_403(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)

    def test_unknown_anchors_are_404(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse('operations_dashboard_fragment', kwargs={'anchor': 'ops-nope'}),
        )

        self.assertEqual(response.status_code, 404)

    def test_the_cookie_does_not_change_what_a_fragment_renders(self):
        # cookie 只决定首屏，不决定单个分区怎么渲染。少了这条，带着 cookie 打开
        # 取回端点会拿到一个空壳，页面就再也填不上了。
        self.client.force_login(self.staff)
        plain = self.client.get(self.url, {'days': 30}).content.decode('utf-8')
        self.client.cookies['ops_lazy'] = '1'
        lazy = self.client.get(self.url, {'days': 30}).content.decode('utf-8')

        self.assertEqual(normalize(plain), normalize(lazy))


class OperationsFragmentEquivalenceTests(TestCase):
    """取回回来的内容，必须和不做懒加载时那一块一模一样。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='frag-equiv-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        self.full = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')

    def test_every_fragment_matches_the_section_inside_the_full_page(self):
        mismatches = []
        for anchor in section_anchors():
            expected = normalize(section_inner(self.full, anchor))
            actual = normalize(self.client.get(
                reverse('operations_dashboard_fragment', kwargs={'anchor': anchor}),
                {'days': 30},
            ).content.decode('utf-8'))

            if anchor in UNORDERED_SECTIONS:
                # 行序由本次请求的耗时决定，见 UNORDERED_SECTIONS 上方的说明。
                if without_sorted_rows(expected) != without_sorted_rows(actual):
                    mismatches.append(anchor)
            elif expected != actual:
                mismatches.append(anchor)

        self.assertEqual(
            mismatches, [],
            '这些分区取回回来的内容和不做懒加载时不一样：%s' % mismatches,
        )

    def test_the_build_performance_rows_are_the_same_builders(self):
        """构建耗时分区的行序不可比，但行的来源必须一致。

        两次请求各跑一遍 65 个构建器，前 12 名按耗时排，两份 HTML 的行序必然
        不同，所以上面那条测试只比骨架。这里补上被骨架抹掉的部分：两边行数必须
        一样多，而且每一行的标签都得是注册表里真有的构建器（含核心统计那一行）。
        少了这条，懒加载漏跑面板就只剩骨架能兜住，行数对不上没人管。
        """
        registry = {
            label for _key, label, _builder in OPERATIONS_DASHBOARD_SHARED_BUILDERS
        }
        registry.add('运营看板核心统计')
        page = normalize(section_inner(self.full, 'ops-build-performance'))
        fragment = normalize(self.client.get(
            reverse('operations_dashboard_fragment',
                    kwargs={'anchor': 'ops-build-performance'}),
            {'days': 30},
        ).content.decode('utf-8'))

        page_rows = sorted_row_labels(page)
        fragment_rows = sorted_row_labels(fragment)

        self.assertTrue(page_rows, '页面上一个构建器耗时行都没有')
        self.assertEqual(
            len(fragment_rows), len(page_rows),
            '取回回来的耗时行数和页面上不一样：%d 对 %d'
            % (len(fragment_rows), len(page_rows)),
        )
        unknown = sorted((set(page_rows) | set(fragment_rows)) - registry)
        self.assertEqual(unknown, [], '这些耗时行的标签不在注册表里：%s' % unknown)

    def test_the_first_screen_sections_are_identical_too(self):
        # 首屏四个分区不取回，但它们的 partial 也走同一张表。改表时最容易顺手
        # 把首屏某个分区标错，症状是首屏少一块内容——这里一并兜住。
        for anchor, _partial, _core, _keys in OPERATIONS_SECTION_FRAGMENTS[:4]:
            expected = normalize(section_inner(self.full, anchor))
            actual = normalize(self.client.get(
                reverse('operations_dashboard_fragment', kwargs={'anchor': anchor}),
                {'days': 30},
            ).content.decode('utf-8'))
            self.assertEqual(expected, actual, '首屏分区 %s 取回结果和页面不一致' % anchor)
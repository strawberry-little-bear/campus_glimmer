# -*- coding: utf-8 -*-
"""运营看板分区跳转条的契约测试。

看板由 38 个 partial 顺序拼成，一屏滚不到头。``operations_nav.py`` 描述「有哪
些分区、怎么分组」，模板负责把每个 include 包进带 id 的 ``<section>``。两边分开
书写，只改一边就会静默失效——chip 还在，点下去却不动；或者分区还在，却完全
没有入口。这个文件就是拦住这种漂移的。
"""
import io
import os
import re

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .operations_nav import OPERATIONS_DASHBOARD_SECTIONS

# 看板的 partial 里另有 14 个 <section id="...">（图表、折叠块之类），所以两条
# 正则都必须带上 class，否则会把无关元素算进分区。
SECTION_ID_RE = re.compile(r'<section[^>]*\bclass="[^"]*\bops-section\b[^"]*"[^>]*\bid="([^"]+)"')
CHIP_RE = re.compile(r'<a[^>]*\bclass="[^"]*\bops-jump-chip\b[^"]*"[^>]*\bhref="#([^"]+)"')
TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates', 'listings', 'operations_dashboard.html',
)

GROUP_LABEL_RE = re.compile(r'ops-jump-group-label[^>]*>([^<]+)<')


def nav_anchors():
    return [anchor for _label, items in OPERATIONS_DASHBOARD_SECTIONS for anchor, _text in items]


class OperationsDashboardUmbrellaTests(SimpleTestCase):
    """umbrella 只能当多主题分区的中转，不许只剩一个孩子。

    按主题拆一个分区有两种结局：真的是多主题，umbrella 留着转发；拆完发现只有
    一个主题，umbrella 就该退场——孩子提回主模板直接 include，自己删掉，再去
    ``operations_nav.py`` 补一个锚点。留着一层单转发的代价不只是多一次 include：
    分区数会和运营看到的跳转条对不上，而两边分开书写，没有任何渲染错误会提醒
    这件事。``_ops_queues.html`` 就是这么退场的（免费领取申请 + 商品图片覆盖，
    后者不是队列）。
    """

    PARTIALS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'templates', 'listings', 'operations',
    )
    INCLUDE_RE = re.compile(r"{%\s*include\s+'listings/operations/([^']+)'\s*%\}")
    COMMENT_RE = re.compile(r'{%\s*comment\s*%}.*?{%\s*endcomment\s*%\}', re.DOTALL)

    def _partial_names(self):
        return sorted(
            name for name in os.listdir(self.PARTIALS_DIR) if name.endswith('.html')
        )

    def test_no_umbrella_partial_is_left_with_a_single_child(self):
        names = self._partial_names()
        # 目录都找错了还跑什么断言；顺手把「一个 partial 都没有」也拦在外面。
        self.assertGreaterEqual(len(names), 38, '没读到分区片段目录，先确认路径')

        for name in names:
            source = io.open(os.path.join(self.PARTIALS_DIR, name), encoding='utf-8').read()
            children = self.INCLUDE_RE.findall(source)
            if not children:
                continue
            leftover = self.COMMENT_RE.sub('', source)
            leftover = self.INCLUDE_RE.sub('', leftover).strip()
            if leftover:
                continue

            self.assertGreaterEqual(
                len(children), 2,
                '%s 只剩 %d 个孩子：把孩子提回主模板直接 include、删掉这一层，'
                '再去 operations_nav.py 补锚点，两个守卫测试的常量跟着 +1' % (name, len(children)),
            )


class OperationsDashboardSectionNavTests(TestCase):
    """分区导航：锚点必须真的落到页面上，数量和顺序都不能漂。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='ops-nav-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        self.html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        self.section_ids = SECTION_ID_RE.findall(self.html)

    def test_every_declared_anchor_lands_on_a_real_section(self):
        missing = [anchor for anchor in nav_anchors() if anchor not in set(self.section_ids)]

        self.assertEqual(missing, [], '导航里有锚点在页面上找不到对应分区')

    def test_every_section_is_reachable_from_the_nav(self):
        # 反方向：新增了分区却忘了登记，用户就只能靠一路滚动找到它。
        orphans = sorted(set(self.section_ids) - set(nav_anchors()))

        self.assertEqual(orphans, [], '页面上有分区没有出现在跳转条里')

    def test_the_nav_covers_every_section_exactly_once(self):
        anchors = nav_anchors()

        self.assertEqual(len(anchors), len(set(anchors)), '导航里有重复锚点')
        self.assertEqual(len(anchors), len(self.section_ids))

    def test_each_group_keeps_the_page_order_of_its_own_sections(self):
        # 组与组之间按主题归类，不跟页面顺序走；但组内必须跟页面顺序一致，
        # 否则用户在组内从左往右扫，方向和他往下滚是打架的。
        position = {anchor: index for index, anchor in enumerate(self.section_ids)}
        for label, items in OPERATIONS_DASHBOARD_SECTIONS:
            indexes = [position[anchor] for anchor, _text in items]
            self.assertEqual(indexes, sorted(indexes), '「%s」组内的顺序和页面不一致' % label)

    def test_all_sections_are_grouped_and_labelled(self):
        groups = OPERATIONS_DASHBOARD_SECTIONS

        self.assertEqual(len(groups), 6)
        self.assertEqual(len(nav_anchors()), 38)
        for label, items in groups:
            self.assertTrue(label, '分组名不能为空')
            self.assertTrue(items, '空分组会渲染出一个孤立的分组标题')
            for anchor, text in items:
                self.assertTrue(text, '%s 缺少显示文案' % anchor)

    def test_every_group_label_and_chip_is_rendered(self):
        self.assertEqual(
            GROUP_LABEL_RE.findall(self.html),
            [label for label, _items in OPERATIONS_DASHBOARD_SECTIONS],
        )
        self.assertEqual(len(CHIP_RE.findall(self.html)), 38)

    def test_the_page_has_no_duplicated_ids(self):
        # 加分区导航时踩过的坑：32 个 include 被包进 <section> 之后，原来那批
        # 裸 include 还留在模板里，于是 31 个面板渲染了两遍，页面里出现重复
        # id，锚点开始乱跳。这类问题在页面上不仔细翻很难看出来，所以在这一点名。
        seen, repeated = set(), []
        for node_id in re.findall(r'\sid="([^"]+)"', self.html):
            if node_id in seen:
                repeated.append(node_id)
            seen.add(node_id)

        self.assertEqual(repeated, [], '页面里有重复的 DOM id')

    def test_each_partial_is_included_exactly_once(self):
        # 渲染结果里看不出「同一个 partial 被包了两次」，因为两遍内容是一样的；
        # 只有回到模片源码数 include 才能发现。
        source = io.open(TEMPLATE_PATH, encoding='utf-8').read()
        includes = re.findall(r"{% include 'listings/operations/([^']+)' %}", source)

        self.assertEqual(len(includes), len(set(includes)), '同一个 partial 被包含了多次')
        self.assertEqual(len(includes), 38)

    def test_anonymous_visitors_never_see_the_nav(self):
        self.client.logout()
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 302)


class OperationsDashboardThemePurityTests(TestCase):
    """「校园专题」分区里不能再混进学期节律的面板。

    umbrella 规则只管「一个文件里多个主题」，还有一种更隐蔽的情况：分区本身是
    单主题，却有一块面板讲的是别的事。``_campaigns.html`` 三块面板里两块讲专题，
    第三块「学期节律与交易高峰」整块来自 ``academic_calendar``——没有 umbrella
    可删，两个守卫测试也看不出来，只能靠读。运营点「校园专题」chip 跳下来看到
    一段学期历，而真正想找的「学期阶段」又没有直达入口。

    现在它独立成 ``ops-academic-calendar``，和 ``ops-semester-phase`` 的两个视角
    并列。这条测试把决定钉住：面板的归属看它讲什么，不看它原来写在哪个文件里。
    """

    def setUp(self):
        staff = User.objects.create_user(
            username='ops-theme-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)
        self.html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')

    def _section_html(self, anchor):
        match = re.search(
            r'<section class="ops-section" id="%s"[^>]*>.*?(?=<section class="ops-section"|\Z)'
            % anchor, self.html, re.DOTALL,
        )
        self.assertIsNotNone(match, '页面上找不到 %s 分区' % anchor)
        return match.group(0)

    def test_the_campaigns_section_holds_only_campaign_panels(self):
        campaigns = self._section_html('ops-campaigns')

        self.assertIn('campaign-analytics-panel', campaigns)
        self.assertIn('campaign-phase-review-panel', campaigns)
        self.assertNotIn(
            'academic-calendar-panel', campaigns,
            '「校园专题」分区里出现了学期节律面板，chip 文案和内容又对不上了',
        )

    def test_the_academic_calendar_section_holds_the_rhythm_panel(self):
        calendar = self._section_html('ops-academic-calendar')

        self.assertIn('academic-calendar-panel', calendar)
        self.assertNotIn(
            'campaign-analytics-panel', calendar,
            '学期节律分区里出现了专题面板，两个分区互相串了',
        )

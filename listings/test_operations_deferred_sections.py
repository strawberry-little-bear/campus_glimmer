# -*- coding: utf-8 -*-
"""运营看板分区延后揭开的契约测试。

看板 38 个分区里只有前 4 个是首屏，其余 34 个先占位、滚到附近再揭开。
这是纯前端优化：响应体和不做优化时逐字节一样，服务端一个字节都没少渲染。
风险也全在前端——一旦选择器少了 ``html.js`` 前缀，关掉 JavaScript 的浏览器
（以及 Django 测试客户端）就会看到一片白板，而页面里恰好还压着真实内容，
翻源码很难发现。

这个文件把三条约定钉死：
* 只有前 4 个分区不加 ``data-ops-deferred``，一个不多一个不少；
* 分区内容照常渲染，没有为了省字节把东西裁掉；
* 所有隐藏规则都挂在 ``html.js`` 下面，无脚本环境必须全量可见。
"""
import io
import json
import os
import re
import shutil
import subprocess

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .operations_nav import (
    OPERATIONS_DASHBOARD_FIRST_SCREEN,
    OPERATIONS_DASHBOARD_SECTIONS,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只认带 ops-section 类的 <section>。看板的 partial 里另有 14 个
# <section id=...>（图表、折叠块之类），少了 class 条件就会把它们误当成分区。
SECTION_OPEN_RE = re.compile(
    r'<section[^>]*\bclass="[^"]*\bops-section\b[^"]*"[^>]*\bid="([^"]+)"'
)
DEFERRED_ATTR_RE = re.compile(r'\bdata-ops-deferred\b')
# 分区里的真实内容标记：设计体系里的面板、空态和表格都算。
CONTENT_MARK_RE = re.compile(
    r'class="[^"]*\b(?:content-card|ops-panel|ops-table|ops-metric-card|ops-metric-grid'
    r'|empty-inline|notification-insight-grid)\b'
)
BASE_TEMPLATE_PATH = os.path.join(PROJECT_ROOT, 'templates', 'base.html')
CSS_PATH = os.path.join(PROJECT_ROOT, 'static', 'css', 'style.css')
MAIN_JS_PATH = os.path.join(PROJECT_ROOT, 'static', 'js', 'main.js')

# 提到 data-ops-deferred 的整条选择器，用来检查它挂在哪里。
CSS_DEFERRED_SELECTOR_RE = re.compile(r'^([^\n{}]*data-ops-deferred[^\n{}]*)\{', re.MULTILINE)


def read_text(path):
    return io.open(path, encoding='utf-8').read()


def nav_anchors():
    return [anchor for _label, items in OPERATIONS_DASHBOARD_SECTIONS for anchor, _text in items]


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


class OperationsDashboardDeferredSectionTests(TestCase):
    """分区延后：哪些先渲染、哪些等滚动，不能随便漂。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='ops-deferred-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        self.html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        self.sections = parse_sections(self.html)

    def test_only_the_first_four_sections_render_eagerly(self):
        eager = [anchor for anchor, deferred in self.sections if not deferred]

        self.assertEqual(len(self.sections), 38, '分区数量变了，先确认跳转条和分区仍然对应')
        self.assertEqual(
            eager,
            [anchor for anchor, _ in self.sections[:4]],
            '首屏分区应该正好是页面最前面的四个，现在不是',
        )
        self.assertEqual(
            set(eager),
            set(OPERATIONS_DASHBOARD_FIRST_SCREEN),
            '首屏集合和页面顺序对上了，即使顺序对上了、成员也对上了',
        )

    def test_every_other_section_is_deferred(self):
        deferred = {anchor for anchor, is_deferred in self.sections if is_deferred}
        expected = set(nav_anchors()) - set(OPERATIONS_DASHBOARD_FIRST_SCREEN)

        self.assertEqual(deferred, expected, '延后集合应该正好是“除首屏外的全部”')

    def test_deferred_sections_still_render_their_full_content(self):
        # 拖后加载只能隐藏，不能不渲染。否则无脚本用户和搜索引擎担心的那部分
        # 就真的消失了，而本地有 JS 的浏览器里照样正常。
        blocks = section_blocks(self.html)
        empty = [
            anchor for anchor, deferred in self.sections
            if deferred and not CONTENT_MARK_RE.search(blocks[anchor])
        ]

        self.assertEqual(empty, [], '这些分区占了位却没有渲染出任何面板、空态或表格')

    def test_the_js_switch_is_installed_in_the_base_template_head(self):
        base = read_text(BASE_TEMPLATE_PATH)

        self.assertIn("classList.add('js')", base, 'base.html 里找不到增强开关')
        # 必须在 <body> 之前：晚一步加类，用户会先看到一帧全量内容再跳回骨架。
        self.assertLess(
            base.index("classList.add('js')"),
            base.index('<body'),
            '增强开关必须在 <head> 里，不能等 body 开始后才加',
        )

    def test_every_deferred_css_rule_is_gated_behind_the_js_class(self):
        # 这条是整个方案的安全带：看起来无限接近的写法
        #   .ops-section[data-ops-deferred] > * { display: none; }
        # 会让关脚本的浏览器和测试客户端直接白屏，而页面还能正常返回 200。
        selectors = CSS_DEFERRED_SELECTOR_RE.findall(read_text(CSS_PATH))

        self.assertTrue(selectors, 'CSS 里已经没有延后规则了，这个文件也就失去了意义')
        ungated = [s for s in selectors if not s.strip().startswith('html.js')]
        self.assertEqual(ungated, [], '这些规则没有挂在 html.js 下面，无脚本用户会看到空分区')

    def test_the_hiding_rule_targets_children_not_the_section_itself(self):
        # 隐掉整个 <section> 的话，骨架屏和“正在等你滚到这里”也会一起消失，
        # 用户看到的就是一片真空白，还以为页面坏了。
        css = read_text(CSS_PATH)
        # 只看真把内容塚掉的那条规则（display: none）。
        # 占位文字的 ::after 也用 display，但那是布局不是隐藏。
        hiding = []
        for selector in CSS_DEFERRED_SELECTOR_RE.findall(css):
            body = css.split(selector + '{', 1)[1].split('}', 1)[0]
            if 'display:none' in body.replace(' ', ''):
                hiding.append(selector.strip())

        self.assertTrue(hiding, '找不到负责隐藏内容的规则')
        for selector in hiding:
            self.assertTrue(
                selector.endswith('> *'),
                '隐藏规则应该只遮子元素，否则骨架屏也会被一起隐掉：' + selector,
            )

    def test_the_reveal_logic_only_toggles_a_class(self):
        # 前端只负责换类。真把节点删了，无脚本和搜索引擎就什么都看不到，
        # 而日常浏览器里照样正常——这种问题只能在这里提前拦住。
        js = read_text(MAIN_JS_PATH)
        start = js.index("const opsPanels = document.querySelector('.operations-panels');")
        end = js.index("const chat = document.querySelector('.chat-messages');")
        block = js[start:end]

        self.assertIn('data-ops-deferred', block, 'main.js 里找不到延后分区的逻辑')
        self.assertIn('is-revealed', block)
        self.assertIn('IntersectionObserver', block, '应该用 IntersectionObserver 判断滚动位置')
        for forbidden in ('.remove(', '.innerHTML', '.outerHTML', '.removeChild(', '.replaceChildren('):
            self.assertNotIn(forbidden, block, '揭开分区只能换类，不能动 DOM：' + forbidden)

    def test_pages_outside_the_dashboard_never_carry_the_attribute(self):
        # 这个属性只属于看板。别的页面上出现它，说明有人把规则复制到了不该用的地方。
        for path in (reverse('home'), reverse('item_list'), reverse('my_items')):
            body = self.client.get(path).content.decode('utf-8')

            self.assertNotIn('data-ops-deferred', body, '%s 上出现了只属于看板的属性' % path)

# 揭开帮手的行为探针。上面的测试都在读源码文本，读不出「揭开方向写反了」这种
# bug：把 target 之后的分区一起揭开，和只揭开 target 之前的分区，源码看起来同样
# 合理，只有真跑一遍才知道谁对。所以这里把 main.js 里的帮手原样抽出来，用一堆假
# 分区在 node 里执行，断言它到底揭开了哪些。
NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function balancedFrom(text, from) {
    let depth = 0;
    for (let i = text.indexOf('{', from); i < text.length; i += 1) {
        if (text[i] === '{') depth += 1;
        else if (text[i] === '}') {
            depth -= 1;
            if (depth === 0) return i;
        }
    }
    return -1;
}

function constFunctions(text) {
    const out = [];
    const re = /const\s+([A-Za-z_$][\w$]*)\s*=\s*function\b/g;
    let m;
    while ((m = re.exec(text)) !== null) {
        const end = balancedFrom(text, m.index);
        if (end < 0) break;
        out.push({ name: m[1], body: text.slice(m.index, end + 1) });
        re.lastIndex = end;
    }
    return out;
}

const opsStart = src.indexOf("const opsPanels");
const opsEnd = src.indexOf("const chat");
if (opsStart < 0 || opsEnd < opsStart) {
    process.stdout.write(JSON.stringify({ error: '找不到运营看板的 JS 代码块' }));
    process.exit(2);
}
const region = src.slice(opsStart, opsEnd);
const helpers = constFunctions(region).filter(function (item) {
    return item.body.indexOf('is-revealed') >= 0 && item.body.indexOf('deferred') >= 0;
});
if (helpers.length !== 1) {
    process.stdout.write(JSON.stringify({
        error: '揭开分区的帮手不唯一，先人工确认再改这个测试',
        names: helpers.map(function (item) { return item.name; }),
    }));
    process.exit(3);
}
const helper = helpers[0];

// 帮手必须被两处共用：观察器负责「滚到附近就揭开」，跳转条负责「一步跳到很远」。
// 任何一处自己加类，另一处就会漏，而且日常滚动看不出异常。
const ioStart = region.indexOf('new IntersectionObserver');
const ioEnd = ioStart < 0 ? -1 : balancedFrom(region, ioStart);
const jump = constFunctions(region).filter(function (item) { return item.name === 'jumpTo'; })[0];
const callers = {
    observer: ioStart >= 0 && ioEnd > ioStart &&
        region.slice(ioStart, ioEnd).indexOf(helper.name + '(') >= 0,
    jumpTo: !!jump && jump.body.indexOf(helper.name + '(') >= 0,
};

// 假分区只记录自己被加了哪个类，不掺任何真实逻辑。
const revealed = [];
const deferred = [];
for (let i = 0; i < 6; i += 1) {
    const index = i;
    deferred.push({
        id: 's' + index,
        classList: { add: function (cls) { if (cls === 'is-revealed') revealed.push(index); } },
    });
}
const revealThrough = new Function('deferred', helper.body + '\nreturn ' + helper.name + ';')(deferred);

const scenarios = [];
[['first', 0], ['middle', 2], ['last', 5], ['none', null]].forEach(function (pair) {
    revealed.length = 0;
    revealThrough(pair[1] === null ? null : deferred[pair[1]]);
    scenarios.push({ label: pair[0], revealed: revealed.slice() });
});
process.stdout.write(JSON.stringify({ name: helper.name, callers: callers, scenarios: scenarios }));
"""


class OperationsDeferredRevealDirectionTests(TestCase):
    """揭开的方向只能朝前：目标及其之前的分区，不能把后面的分区一起抖出来。

    这条契约曾经真的被写反过：帮手揭开的是 target **之后**的分区，于是
    * 滚到第一个延后分区的一瞬间，28 个分区被一次揭开，文档高度涨 4.8 倍，
      「延后渲染」名存实亡；
    * 点跳转 chip 时，目标上面的分区还是 300px 骨架，scrollIntoView 按骨架
      高度算距离，落点差了整整一万多像素。

    两种症状在 Django 测试客户端里都看不出来——那里干脆没有 JS。
    """

    def setUp(self):
        self.js = read_text(MAIN_JS_PATH)
        start = self.js.index("const opsPanels = document.querySelector('.operations-panels');")
        end = self.js.index("const chat = document.querySelector('.chat-messages');")
        self.ops_block = self.js[start:end]

    def _probe(self):
        if shutil.which('node') is None:
            self.skipTest('环境里没有 node，跑不了 main.js 的行为探针')
        completed = subprocess.run(
            ['node', '-e', NODE_HARNESS, MAIN_JS_PATH],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(
            completed.returncode, 0,
            '探针脚本本身失败了：%s' % (completed.stderr or completed.stdout),
        )
        return json.loads(completed.stdout)

    def test_the_reveal_helper_uncovers_only_sections_up_to_its_target(self):
        result = self._probe()
        by_label = {item['label']: item['revealed'] for item in result['scenarios']}
        everything = list(range(6))

        self.assertEqual(
            by_label['first'], [0],
            '揭开第一个分区时不该动到第二个，否则一滚动就把整页都抖开了',
        )
        self.assertEqual(
            by_label['middle'], [0, 1, 2],
            '揭开中段分区时，必须连它上面的分区一起补上——那些是被快速滚动跳过的',
        )
        self.assertEqual(by_label['last'], everything, '最后一个分区应该把整页揭完')
        self.assertEqual(by_label['none'], everything, '没有观察器的旧浏览器要一次揭完')

    def test_both_the_observer_and_the_jump_bar_go_through_the_same_helper(self):
        # 只有一处调用帮手的话，另一处就会留下永远不揭开的骨架分区。
        result = self._probe()

        self.assertTrue(
            result['callers']['observer'],
            'IntersectionObserver 的回调没有调用揭开帮手，滚到的分区会一直停在骨架态',
        )
        self.assertTrue(
            result['callers']['jumpTo'],
            '跳转条没有调用揭开帮手，点 chip 会落在按骨架高度算错的偏移上',
        )

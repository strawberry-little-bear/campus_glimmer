# -*- coding: utf-8 -*-
"""运营看板分区延后揭开的契约测试。

看板 38 个分区里只有前 4 个是首屏，其余 34 个延后。延后分两层，两层都要有人盯：

* CSS 那层是兜底。``html.js`` 下面把分区内容藏起来，滚近时由 main.js 加
  ``is-revealed`` 揭开。关掉脚本的浏览器没有 ``html.js``，整页照常全量可见。
* JS 那层是懒加载。带着 ``ops_lazy`` cookie 来的请求，服务端只渲染首屏四个
  分区，其余 34 个只留一个空 ``<section>`` 加一条 ``data-ops-src`` 取回地址。
  cookie 由 base.html 里一段内联脚本种下，所以关掉脚本的浏览器永远种不下它，
  也就永远拿到全量页面——「没有 JS 就没有数据」这个分支不存在。

风险也全在前端：一旦选择器少了 ``html.js`` 前缀，关掉 JavaScript 的浏览器
（以及 Django 测试客户端）就会看到一片白板，而页面里恰好还压着真实内容，
翻源码很难发现。

这个文件把四条约定钉死：
* 只有前 4 个分区不加 ``data-ops-deferred``，一个不多一个不少；
* 不带 cookie 时 34 个延后分区照常全量渲染，没有为了省字节把东西裁掉；
* 带 cookie 时那 34 个分区是空的，且每个都带一条指向取回端点的地址；
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
        # 内容本来就在响应体里的分区（不带 cookie 的全量渲染），前端只负责换类。
        # 取回分区当然要写 innerHTML——那是它唯一的职责，禁不掉。真正要拦住的
        # 是「无脚本用户也能看到的页面被 JS 拆掉」：那种改动会让没有 JS 的浏览器
        # 和搜索引擎什么都看不到，而日常浏览器里照样正常。
        js = read_text(MAIN_JS_PATH)
        start = js.index("const opsPanels = document.querySelector('.operations-panels');")
        end = js.index("const chat = document.querySelector('.chat-messages');")
        block = js[start:end]

        self.assertIn('data-ops-deferred', block, 'main.js 里找不到延后分区的逻辑')
        self.assertIn('data-ops-src', block, 'main.js 里没有按需取回的入口')
        self.assertIn('is-revealed', block)
        self.assertIn('IntersectionObserver', block, '应该用 IntersectionObserver 判断滚动位置')
        # 拆节点才能造成的破坏，逐个禁掉。innerHTML 不在此列：懒加载分区的内容
        # 只能整段取回、整段塞入，禁掉它这个功能就不存在了。它只允许出现在唯一
        # 一处——取回成功那一跳，多写一处就说明有人顺手改了别的地方的 DOM。
        for forbidden in ('.removeChild(', '.replaceChildren(', '.replaceWith(', '.outerHTML', '.insertAdjacentHTML('):
            self.assertNotIn(forbidden, block, '分区内容不能拆节点：' + forbidden)
        self.assertEqual(
            block.count('.innerHTML ='), 1,
            'innerHTML 只该出现在取回成功那一处，现在有 %d 处' % block.count('.innerHTML ='),
        )

    def test_pages_outside_the_dashboard_never_carry_the_attribute(self):
        # 这个属性只属于看板。别的页面上出现它，说明有人把规则复制到了不该用的地方。
        for path in (reverse('home'), reverse('item_list'), reverse('my_items')):
            body = self.client.get(path).content.decode('utf-8')

            self.assertNotIn('data-ops-deferred', body, '%s 上出现了只属于看板的属性' % path)

class OperationsDashboardLazyLoadingTests(TestCase):
    """带着 ops_lazy cookie 打开看板时，服务端只渲染首屏四个分区。

    这是把延后从「CSS 藏起来」升级成「服务端也不渲染」的那一半。风险全在
    「内容消失」上：cookie 种下了、地址没给，或者地址给了但内容没裁干净，
    用户在浏览器里看到的就是 34 个永远转圈的骨架，而 Django 测试客户端
    （不带 cookie）一切正常——所以这里必须用带 cookie 的客户端跑一遍。
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='ops-lazy-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        # cookie 由 base.html 的内联脚本种，这里直接模拟「第二次打开」。
        self.client.cookies['ops_lazy'] = '1'
        self.html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        self.sections = parse_sections(self.html)

    def test_only_the_first_four_sections_carry_content(self):
        blocks = section_blocks(self.html)
        rendered = [
            anchor for anchor, _deferred in self.sections
            if CONTENT_MARK_RE.search(blocks[anchor])
        ]

        self.assertEqual(
            rendered, [anchor for anchor, _ in self.sections[:4]],
            '懒加载时应该只有首屏四个分区有内容，现在多渲染了：%s' % (
                sorted(set(rendered) - set(OPERATIONS_DASHBOARD_FIRST_SCREEN)),
            ),
        )

    def test_every_deferred_section_points_at_its_fragment_endpoint(self):
        # 空壳必须带取回地址，否则就是一块永远转圈的骨架。属性与内容同进同退：
        # 有 data-ops-src 就必须有 {% if ops_lazy %} 包着的那段，反之亦然。
        blocks = section_blocks(self.html)
        missing = []
        wrong = []
        for anchor, _deferred in self.sections[4:]:
            block = blocks[anchor]
            match = re.search(r'data-ops-src="([^"]+)"', block)
            if not match:
                missing.append(anchor)
                continue
            expected = reverse(
                'operations_dashboard_fragment', kwargs={'anchor': anchor},
            ) + '?days=30'
            if match.group(1) != expected:
                wrong.append((anchor, match.group(1), expected))

        self.assertEqual(missing, [], '这些分区是空的却没有取回地址')
        self.assertEqual(wrong, [], '这些分区的取回地址不对')

    def test_lazy_page_is_much_smaller_than_the_full_one(self):
        # 懒加载的意义就在字节数上。这条不是性能基准，只拦住「裁了但没裁多少」
        # 和「干脆没裁」两种回归。
        # cookie 挂在客户端上，不在响应对象上：先摘掉再发一次请求，才是「第一次
        # 打开看板」那条全量渲染的路径。
        del self.client.cookies['ops_lazy']
        full_html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')

        self.assertLess(
            len(self.html) * 100, len(full_html) * 45,
            '懒加载页面只比全量页面小了 %.1f%%，分区内容大概没真裁掉' % (
                100 - len(self.html) * 100.0 / len(full_html),
            ),
        )

    def test_the_fragment_endpoint_ignores_the_cookie(self):
        # 取回端点永远渲染自己那一个分区，和 cookie 无关：cookie 只决定首屏，
        # 不决定单个分区怎么渲染。少了这条，带着 cookie 打开 fragment 会拿到
        # 一个空壳，页面就再也填不上了。
        response = self.client.get(
            reverse('operations_dashboard_fragment', kwargs={'anchor': 'ops-demand'}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            CONTENT_MARK_RE.search(response.content.decode('utf-8')),
            '取回 ops-demand 拿到了一段没有面板、空态或表格的内容',
        )


# 揭开帮手的行为探针。上面的测试都在读源码文本，读不出「揭开方向写反了」这种
# bug：把 target 之后的分区一起揭开，和只揭开 target 之前的分区，源码看起来同样
# 合理，只有真跑一遍才知道谁对。所以这里把 main.js 里的帮手原样抽出来，用一堆假
# 分区在 node 里执行，断言它到底揭开了哪些。
NODE_HARNESS = r'''
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

// 懒加载之后是三个函数：isDeferredSection 判断分区在不在延后名单里，
// loadSection 负责单个分区（取回或只换类），revealThrough 负责「连它上面的
// 一起」。探针把三个都原样抽出来，拼进同一个 Function 再跑——只抽一个的话，
// 另外两个会变成未定义引用，测到的是 ReferenceError 而不是真实行为。
const wanted = ['isDeferredSection', 'loadSection', 'revealThrough'];
const picked = {};
for (const name of wanted) {
    const found = constFunctions(region).filter(function (item) { return item.name === name; });
    if (found.length !== 1) {
        process.stdout.write(JSON.stringify({
            error: '运营看板 JS 里 ' + name + ' 不是恰好一个，先人工确认再改这个测试',
            names: found.map(function (item) { return item.name; }),
        }));
        process.exit(3);
    }
    picked[name] = found[0].body;
}

// 揭开必须被两处共用：观察器负责「滚到附近就揭开」，跳转条负责「一步跳到很远」。
// 任何一处自己加类，另一处就会漏，而且日常滚动看不出异常。
const ioStart = region.indexOf('new IntersectionObserver');
const ioEnd = ioStart < 0 ? -1 : balancedFrom(region, ioStart);
const jump = constFunctions(region).filter(function (item) { return item.name === 'jumpTo'; })[0];
const callers = {
    observer: ioStart >= 0 && ioEnd > ioStart &&
        region.slice(ioStart, ioEnd).indexOf('revealThrough(') >= 0,
    jumpTo: !!jump && jump.body.indexOf('revealThrough(') >= 0,
};

// 假分区只记录自己身上发生的每一次调用，不掺任何真实逻辑。
// getAttribute 一律返回 null：这正是不带 cookie 的全量渲染，内容已在响应体里，
// 于是 loadSection 应该只换类、别的什么都不做。调用流水就是这条契约的证据。
const revealed = [];
const logs = [];
const deferred = [];
for (let i = 0; i < 6; i += 1) {
    const index = i;
    const calls = [];
    logs.push(calls);
    deferred.push({
        id: 's' + index,
        getAttribute: function (name) { calls.push('getAttribute:' + name); return null; },
        hasAttribute: function (name) { calls.push('hasAttribute:' + name); return false; },
        removeAttribute: function (name) { calls.push('removeAttribute:' + name); },
        setAttribute: function (name, value) { calls.push('setAttribute:' + name); },
        addEventListener: function (type) { calls.push('addEventListener:' + type); },
        classList: {
            add: function (cls) {
                calls.push('classList.add:' + cls);
                if (cls === 'is-revealed') revealed.push(index);
            },
            remove: function (cls) { calls.push('classList.remove:' + cls); },
            contains: function (cls) { calls.push('classList.contains:' + cls); return false; },
        },
    });
}
const revealThrough = new Function(
    'deferred',
    picked.isDeferredSection + '\n' + picked.loadSection + '\n' + picked.revealThrough + '\nreturn revealThrough;',
)(deferred);

const scenarios = [];
[['first', 0], ['middle', 2], ['last', 5], ['none', null]].forEach(function (pair) {
    revealed.length = 0;
    logs.forEach(function (calls) { calls.length = 0; });
    revealThrough(pair[1] === null ? null : deferred[pair[1]]);
    scenarios.push({
        label: pair[0],
        revealed: revealed.slice(),
        calls: logs.map(function (calls) { return calls.slice(); }),
    });
});
process.stdout.write(JSON.stringify({ callers: callers, scenarios: scenarios }));
'''


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
            capture_output=True, text=True, encoding='utf-8', timeout=120,
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

    def test_sections_without_a_source_are_left_completely_alone(self):
        # 不带 cookie 的全量渲染里，34 个延后分区的内容就在响应体里，JS 唯一的
        # 职责是换类。这条用探针盯行为而不是读源码：假分区上没有 data-ops-src，
        # 于是除了 getAttribute 和 classList.add，不该再出现任何一次调用。
        result = self._probe()
        untouched = ['getAttribute:data-ops-src', 'classList.add:is-revealed']

        for scenario in result['scenarios']:
            for index, calls in enumerate(scenario['calls']):
                expected = untouched if index in scenario['revealed'] else []
                self.assertEqual(
                    calls, expected,
                    '场景 %s 的第 %d 个分区被多动了一次：%s' % (scenario['label'], index, calls),
                )

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

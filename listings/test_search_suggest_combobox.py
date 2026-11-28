# -*- coding: utf-8 -*-
"""全局搜索建议的 ARIA combobox 契约测试。

base.html 里的搜索框以前只在 JS 里翻转 aria-expanded，但输入框没有
role="combobox"。按 ARIA 1.2，纯 textbox 不支持 aria-expanded，
读屏会把这个状态整个忽略；激活的建议项也没有 aria-selected 和
aria-activedescendant，键盘用户走到第几项只有眼睛知道。

这里的两层验证和运营看板的探针同一套路：

* 渲染态：真实请求首页，确认输入框带着 combobox 角色、
  aria-autocomplete="list"、aria-controls 指向真正的 listbox。
* 行为态：用 Node 抽出 main.js 里的 createSearchSuggestController
  工厂，配上假 DOM 跑真实逻辑，断言方向键、Enter、Escape、点击
  写回和防抖/取消都按同一份 ARIA 状态机走。

没有 node 的环境会跳过行为探针；渲染态契约在任何环境都生效。
"""
import io
import json
import os
import re
import shutil
import subprocess

from django.test import TestCase
from django.urls import reverse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_TEMPLATE_PATH = os.path.join(PROJECT_ROOT, 'templates', 'base.html')
MAIN_JS_PATH = os.path.join(PROJECT_ROOT, 'static', 'js', 'main.js')


def read_text(path):
    return io.open(path, encoding='utf-8').read()


NODE_HARNESS = r''''use strict';
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

function blockFrom(text, from) {
    const open = text.indexOf('{', from);
    if (open < 0) return '';
    const end = balancedFrom(text, open);
    return end < 0 ? '' : text.slice(from, end + 1);
}

const factoryStart = src.indexOf('const createSearchSuggestController = function');
if (factoryStart < 0) {
    process.stdout.write(JSON.stringify({ error: 'main.js 里找不到 createSearchSuggestController 工厂' }));
    process.exit(2);
}
const factoryEnd = balancedFrom(src, factoryStart);
if (factoryEnd < 0) {
    process.stdout.write(JSON.stringify({ error: 'createSearchSuggestController 没有闭合的代码块' }));
    process.exit(2);
}
const factory = src.slice(factoryStart, factoryEnd + 1);

// 源码层断言：ARIA 状态只允许从 setActive 一条路出。键盘分支里如果
// 还有人手写 classList.toggle('is-active') 或者直接改 aria-selected，
// 就说明状态机又被拆散了——这正是这次重构要消灭的写法。
const keydownBody = blockFrom(factory, factory.indexOf("searchInput.addEventListener('keydown'"));
const hideBody = blockFrom(factory, factory.indexOf('const hideSuggestions'));
const renderBody = blockFrom(factory, factory.indexOf('const renderSuggestions'));
const source = {
    factoryWired: src.indexOf('createSearchSuggestController({') >= 0,
    keydownUsesSetActive: (keydownBody.match(/setActive\(/g) || []).length >= 1,
    keydownHasNoManualState: keydownBody.indexOf("classList.toggle") < 0
        && keydownBody.indexOf("aria-selected") < 0
        && keydownBody.indexOf("aria-activedescendant") < 0,
    hideClearsActive: hideBody.indexOf('setActive(-1)') >= 0
        && hideBody.indexOf("aria-activedescendant") < 0,
    renderMarksOptions: renderBody.indexOf("aria-selected", renderBody.indexOf("role', 'option'")) >= 0
        && renderBody.indexOf('option.id =') >= 0,
};

// ---- 假 DOM：够工厂跑起来就行，不模拟浏览器 ----
function makeInput() {
    const attrs = {};
    const handlers = {};
    return {
        value: '',
        attrs: attrs,
        handlers: handlers,
        addEventListener: function (type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        fire: function (type, event) { (handlers[type] || []).forEach(function (fn) { fn(event); }); },
        setAttribute: function (name, value) { attrs[name] = value; },
        getAttribute: function (name) { return attrs[name] === undefined ? null : attrs[name]; },
        removeAttribute: function (name) { delete attrs[name]; },
    };
}

function makeMenu() {
    const children = [];
    return {
        id: 'search-suggestions',
        hidden: true,
        children: children,
        replaceChildren: function () { children.length = 0; },
        appendChild: function (node) { children.push(node); },
        querySelectorAll: function () { return children; },
    };
}

function makeForm() {
    return {
        dataset: { searchSuggestEndpoint: '/search/suggest/' },
        submits: 0,
        submit: function () { this.submits += 1; },
        contains: function () { return true; },
    };
}

const createdOptions = [];
const documentShim = {
    outsideListeners: [],
    createElement: function (tag) {
        const node = {
            tag: tag,
            type: '',
            className: '',
            id: '',
            dataset: {},
            attrs: {},
            listeners: {},
            parts: {},
            addEventListener: function (type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
            click: function () { (this.listeners.click || []).forEach(function (fn) { fn(); }); },
            setAttribute: function (name, value) { this.attrs[name] = value; },
            getAttribute: function (name) { return this.attrs[name] === undefined ? null : this.attrs[name]; },
            querySelector: function (selector) {
                if (!this.parts[selector]) this.parts[selector] = { textContent: '' };
                return this.parts[selector];
            },
            classList: {
                toggle: function (cls, force) {
                    const classes = node.className ? node.className.split(/\s+/) : [];
                    const has = classes.indexOf(cls) >= 0;
                    const want = force === undefined ? !has : force;
                    if (want && !has) classes.push(cls);
                    if (!want && has) classes.splice(classes.indexOf(cls), 1);
                    node.className = classes.join(' ');
                    return want;
                },
            },
        };
        createdOptions.push(node);
        return node;
    },
    addEventListener: function (type, fn) { documentShim.outsideListeners.push([type, fn]); },
};

const pendingTimers = [];
let timerSeq = 0;
const windowShim = {
    setTimeout: function (fn, ms) {
        timerSeq += 1;
        pendingTimers.push({ id: timerSeq, fn: fn, ms: ms });
        return timerSeq;
    },
    clearTimeout: function (id) {
        for (let i = pendingTimers.length - 1; i >= 0; i -= 1) {
            if (pendingTimers[i].id === id) pendingTimers.splice(i, 1);
        }
    },
};

const fetchCalls = [];
// 默认给一个永不 resolve 的安静 thenable：防抖场景只关心请求发出去了，
// 不需要真的响应；abort 场景再换成自己的 responder。
let fetchResponder = function () {
    return {
        then: function () { return this; },
        catch: function () { return this; },
    };
};
function fakeFetch(url, init) {
    fetchCalls.push({ url: url, init: init });
    return fetchResponder(url, init);
}

const createFn = new Function(
    'document', 'window', 'fetch',
    factory + '\nreturn createSearchSuggestController;'
);
const input = makeInput();
const menu = makeMenu();
const form = makeForm();
const controller = createFn(documentShim, windowShim, fakeFetch)({
    form: form,
    input: input,
    menu: menu,
});

function optionSnapshot() {
    return menu.children.map(function (option, index) {
        return {
            index: index,
            id: option.id,
            role: option.attrs.role,
            selected: option.attrs['aria-selected'],
            className: option.className,
        };
    });
}

function inputSnapshot() {
    return {
        expanded: input.attrs['aria-expanded'],
        descendant: input.attrs['aria-activedescendant'] || null,
    };
}

// 场景：渲染建议后的初始 ARIA 状态
controller.renderSuggestions([
    { text: '高等数学教材', label: '商品', meta: '八成新', kind: 'item' },
    { text: '线性代数', label: '分类', kind: 'category' },
    { text: '东区图书馆', label: '地点', kind: 'location' },
]);
const render = { options: optionSnapshot(), input: inputSnapshot(), hidden: menu.hidden };

// 场景：方向键循环 + Escape 收起
const nav = [];
input.fire('keydown', { key: 'ArrowDown', preventDefault: function () {} });
nav.push({ options: optionSnapshot(), input: inputSnapshot() });
input.fire('keydown', { key: 'ArrowDown', preventDefault: function () {} });
nav.push({ options: optionSnapshot(), input: inputSnapshot() });
input.fire('keydown', { key: 'ArrowUp', preventDefault: function () {} });
nav.push({ options: optionSnapshot(), input: inputSnapshot() });
input.fire('keydown', { key: 'ArrowUp', preventDefault: function () {} });
nav.push({ options: optionSnapshot(), input: inputSnapshot() });
input.fire('keydown', { key: 'Escape', preventDefault: function () {} });
nav.push({ options: optionSnapshot(), input: inputSnapshot(), hidden: menu.hidden });

// 场景：Enter 激活当前项（前一轮 Escape 收起了菜单，先重新渲染）
controller.renderSuggestions([
    { text: '高等数学教材', label: '商品', meta: '八成新', kind: 'item' },
    { text: '线性代数', label: '分类', kind: 'category' },
    { text: '东区图书馆', label: '地点', kind: 'location' },
]);
input.fire('keydown', { key: 'ArrowDown', preventDefault: function () {} });
const valueBeforeEnter = input.value;
const submitsBeforeEnter = form.submits;
input.fire('keydown', { key: 'Enter', preventDefault: function () {} });
const enter = {
    valueBefore: valueBeforeEnter,
    valueAfter: input.value,
    submits: form.submits - submitsBeforeEnter,
    hidden: menu.hidden,
};

// 场景：点击选项写回输入框并提交
controller.renderSuggestions([{ text: '二手自行车', label: '商品', kind: 'item' }]);
const submitsBeforeClick = form.submits;
menu.children[0].click();
const clickPath = {
    value: input.value,
    submits: form.submits - submitsBeforeClick,
    hidden: menu.hidden,
};

// 场景：防抖还在，连打两个字只留最后一个计时器
input.value = '高数';
input.fire('input', {});
input.fire('input', {});
const debounce = {
    pendingAfterTwoInputs: pendingTimers.length,
    delay: pendingTimers.length ? pendingTimers[0].ms : null,
};
if (pendingTimers.length) {
    const only = pendingTimers.pop();
    only.fn();
}
debounce.fetchedAfterFire = fetchCalls.length;

// 场景：旧请求会被 AbortController 取消
fetchResponder = function () {
    return {
        then: function (onFulfilled) { this._then = onFulfilled; return this; },
        catch: function () { return this; },
    };
};
input.value = '英语';
input.fire('input', {});
pendingTimers.pop().fn();
const firstSignal = fetchCalls.length === 2 ? fetchCalls[1].init.signal : null;
input.value = '英语听力';
input.fire('input', {});
pendingTimers.pop().fn();
const abort = {
    calls: fetchCalls.length,
    firstAborted: firstSignal ? firstSignal.aborted : null,
    firstUrl: fetchCalls.length > 0 ? fetchCalls[0].url : null,
    lastUrl: fetchCalls.length > 2 ? fetchCalls[2].url : null,
};

process.stdout.write(JSON.stringify({ source: source, render: render, nav: nav, enter: enter, clickPath: clickPath, debounce: debounce, abort: abort }));
'''


class SearchSuggestComboboxTemplateTests(TestCase):
    """渲染态：首页的搜索框必须自带完整的 combobox 声明。"""

    def test_home_search_input_declares_combobox_contract(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')

        input_match = re.search(r'<input name="q"[^>]*>', html)
        self.assertIsNotNone(input_match, '首页找不到全局搜索输入框')
        tag = input_match.group(0)
        self.assertIn('role="combobox"', tag, '搜索输入框缺少 combobox 角色，读屏会忽略 aria-expanded')
        self.assertIn('aria-autocomplete="list"', tag, 'combobox 必须声明建议来自候选列表')
        self.assertIn('aria-expanded="false"', tag)
        self.assertIn('autocomplete="off"', tag)

        controls_match = re.search(r'aria-controls="([^"]+)"', tag)
        self.assertIsNotNone(controls_match, '搜索输入框没有声明 aria-controls')
        menu_id = controls_match.group(1)
        self.assertIn(
            'id="%s" role="listbox"' % menu_id,
            html,
            'aria-controls 指向的元素必须真的是 listbox',
        )

    def test_template_keeps_listbox_options_ready_for_dynamic_ids(self):
        html = read_text(BASE_TEMPLATE_PATH)
        self.assertIn('data-search-suggest-endpoint', html)
        self.assertIn('role="listbox"', html)


class SearchSuggestComboboxBehaviorTests(TestCase):
    """行为态：main.js 里的 combobox 工厂跑真实逻辑。"""

    def setUp(self):
        self.js = read_text(MAIN_JS_PATH)
        self.assertIn(
            'const createSearchSuggestController = function',
            self.js,
            '搜索建议逻辑必须留在可抽取的顶层工厂里',
        )
        self.assertIn(
            'createSearchSuggestController({',
            self.js,
            'DOMContentLoaded 里必须通过工厂接线，不允许旁路实现',
        )

    def _probe(self):
        if shutil.which('node') is None:
            self.skipTest('环境里没有 node，无法把 main.js 逻辑当探针跑')
        completed = subprocess.run(
            ['node', '-e', NODE_HARNESS, MAIN_JS_PATH],
            capture_output=True, text=True, encoding='utf-8', timeout=120,
        )
        self.assertEqual(
            completed.returncode, 0,
            '探针脚本运行失败了：%s' % (completed.stderr or completed.stdout),
        )
        return json.loads(completed.stdout)

    def test_factory_follows_one_aria_state_path(self):
        result = self._probe()
        source = result['source']
        self.assertTrue(source['factoryWired'], '工厂没有被 DOMContentLoaded 接线')
        self.assertTrue(source['keydownUsesSetActive'], '键盘导航没有统一走 setActive')
        self.assertTrue(source['keydownHasNoManualState'], '键盘分支里还在手写 ARIA 状态')
        self.assertTrue(source['hideClearsActive'], '收起建议时没有清掉激活态')
        self.assertTrue(source['renderMarksOptions'], '渲染建议时没有给选项打上 ARIA 标记')

    def test_render_suggestions_declares_option_ids_and_selection(self):
        result = self._probe()
        render = result['render']
        self.assertFalse(render['hidden'])
        self.assertEqual(render['input']['expanded'], 'true')
        for option in render['options']:
            self.assertEqual(option['id'], 'search-suggestions-option-%d' % option['index'])
            self.assertEqual(option['role'], 'option')
            self.assertEqual(option['selected'], 'false')
            self.assertNotIn('is-active', option['className'])
        self.assertIsNone(render['input']['descendant'])

    def test_arrow_keys_cycle_and_escape_resets_aria_state(self):
        result = self._probe()
        nav = result['nav']
        # 第一次 ArrowDown 停在第一项，再次 ArrowDown 到第二项
        self.assertEqual(nav[0]['input']['descendant'], 'search-suggestions-option-0')
        self.assertEqual(nav[1]['input']['descendant'], 'search-suggestions-option-1')
        self.assertEqual(nav[1]['options'][1]['selected'], 'true')
        self.assertEqual(nav[1]['options'][1]['className'], 'search-suggest-option is-active')
        # ArrowUp 回到第一项，再 ArrowUp 从第一项绕到最后一项
        self.assertEqual(nav[2]['input']['descendant'], 'search-suggestions-option-0')
        self.assertEqual(nav[3]['input']['descendant'], 'search-suggestions-option-2')
        # Escape 收起后，expanded 归位、activedescendant 摘除、选项全部取消选中
        final = nav[4]
        self.assertTrue(final['hidden'])
        self.assertEqual(final['input']['expanded'], 'false')
        self.assertIsNone(final['input']['descendant'])
        for option in final['options']:
            self.assertEqual(option['selected'], 'false')
            self.assertNotIn('is-active', option['className'])

    def test_enter_activates_current_suggestion(self):
        result = self._probe()
        enter = result['enter']
        self.assertEqual(enter['valueBefore'], '')
        self.assertEqual(enter['valueAfter'], '高等数学教材')
        self.assertEqual(enter['submits'], 1)
        self.assertTrue(enter['hidden'])

    def test_clicking_an_option_writes_back_and_submits(self):
        result = self._probe()
        click_path = result['clickPath']
        self.assertEqual(click_path['value'], '二手自行车')
        self.assertEqual(click_path['submits'], 1)
        self.assertTrue(click_path['hidden'])

    def test_input_debounce_is_preserved(self):
        result = self._probe()
        debounce = result['debounce']
        self.assertEqual(debounce['pendingAfterTwoInputs'], 1, '连续输入应该复用同一个防抖计时器')
        self.assertEqual(debounce['delay'], 180)
        self.assertEqual(debounce['fetchedAfterFire'], 1)
        self.assertIn('q=%E9%AB%98%E6%95%B0', result['abort']['firstUrl'] or '', '请求参数没有带编码后的查询词')

    def test_previous_request_is_aborted_before_the_next_one(self):
        result = self._probe()
        abort = result['abort']
        self.assertEqual(abort['calls'], 3)
        self.assertTrue(abort['firstAborted'], '发起新请求前没有 abort 上一个请求')
"""动效契约：prefers-reduced-motion 必须是总闸，不是点名表。

样式表里早有两条 @media (prefers-reduced-motion: reduce)，但都是按选择器点名
处理的——一条收进度步骤的脉冲与过渡，一条收懒加载骨架的微光。点名法有个结构性
漏洞：往后任何人新增一处 transition 或 keyframes，都会悄悄落在覆盖之外。这类缺口
在页面上完全看不出来，开发机不打开「减少动态效果」就永远发现不了，只有真的在系统
里关了动效的用户会撞上。写这份契约时漏网的就有 .unread-toast 的一次性入场动画和
几十处 hover 过渡。

所以兜底必须是 * 通配 + !important 的总闸，让新增动效默认就被收口，而不是指望
每个人记得往点名表里再添一行。

实现上刻意把时长压到 0.01ms、循环次数钉成 1，而不是写 transition: none：属性该
变的照样变，只是不再有中间帧，infinite 的脉冲也会随之停下，不会把元素留在半空
状态。.unread-toast 的基础态没有 opacity: 0，压秒后只是瞬间出现，不会变成看不见。

这里同时守住两件事：总闸本身要写对，以及它确实有东西可收——如果把全场动画都删
了，总闸会变成空转，那种「通过」是假的。
"""

import re
from pathlib import Path

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'

CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)
REDUCED_MOTION_MEDIA = re.compile(
    r'@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{')
UNIVERSAL_SELECTOR = re.compile(r'(?<![\w-])\*(?![\w-])')


def iter_blocks(text, open_index):
    """从 open_index 处的 '{' 出发，产出 (起始, 结束, 块体) 直至配对花括号。"""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return index + 1
    raise AssertionError('样式表里有没闭合的花括号')


def media_blocks(css):
    """产出 (起始位置, 结束位置, 块体文本)。"""
    for match in REDUCED_MOTION_MEDIA.finditer(css):
        open_index = css.index('{', match.end() - 1)
        end = iter_blocks(css, open_index)
        yield match.start(), end, css[open_index + 1:end - 1]


def motion_declarations(css):
    """产出 (位置, 属性, 声明文本)，跳过 reduced-motion 块内的声明。"""
    spans = [(start, end) for start, end, _body in media_blocks(css)]
    for match in re.finditer(r'(?<![-\w])(animation|transition)\s*:', css):
        if any(start <= match.start() < end for start, end in spans):
            continue
        yield match.start(), match.group(1), css[match.start():css.find(';', match.start())]


class ReducedMotionFallbackTests(SimpleTestCase):
    """prefers-reduced-motion 下全场动效必须被压到瞬时。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = CSS_COMMENT.sub(' ', STYLESHEET.read_text(encoding='utf-8'))
        cls.blocks = list(media_blocks(cls.css))

    def _catch_all(self):
        """找出通配收口的那个 reduced-motion 块。"""
        for _start, _end, body in self.blocks:
            if UNIVERSAL_SELECTOR.search(body):
                return body
        return None

    def test_reduced_motion_catch_all_exists(self):
        body = self._catch_all()
        self.assertIsNotNone(
            body,
            '缺一条 * 通配的 @media (prefers-reduced-motion: reduce) 总闸。'
            '点名式的规则照顾不到后来新增的 transition / keyframes，'
            '只有真的在系统里关掉动效的用户会撞上，页面看不出异常。')

    def test_catch_all_pins_durations_and_stops_loops(self):
        body = self._catch_all()
        if body is None:
            self.skipTest('总闸不存在，由 test_reduced_motion_catch_all_exists 报')
        expected = {
            'animation-duration': '0.01ms',
            'animation-iteration-count': '1',
            'transition-duration': '0.01ms',
            'scroll-behavior': 'auto',
        }
        for prop, value in expected.items():
            pattern = re.compile(
                r'(?<![-\w])' + re.escape(prop) + r'\s*:\s*'
                + re.escape(value) + r'\s*!important')
            self.assertRegex(
                body, pattern,
                '总闸里 %s 要是 %s !important。时长压到 0.01ms 而不是写 '
                'transition: none——属性该变的照样变，只是不再有中间帧，'
                '不会把元素留在半空状态。' % (prop, value))

    def test_catch_all_really_has_motion_to_cover(self):
        """总闸不能空转：全场得有动画/过渡声明在它管辖区之外。"""
        outside = list(motion_declarations(self.css))
        self.assertGreater(
            len(outside), 0,
            'reduced-motion 块之外一条 animation / transition 都没有，'
            '总闸是在空转——要么是真没动效了（那把总闸和这条断言一起删），'
            '要么是解析逻辑失效了。')

    def test_targeted_progress_step_rule_survives(self):
        """点名规则要还在：进度步骤除了停动画，还要有一个静态 box-shadow 兜着。"""
        matched = [
            body for _start, _end, body in self.blocks
            if 'progress-step' in body and 'animation' in body
        ]
        self.assertTrue(
            matched,
            '进度步骤的 reduced-motion 点名规则不见了。总闸只负责压秒，'
            '不负责补静态 box-shadow，删了它当前步骤会失去视觉指示。')
        self.assertRegex(
            matched[-1], r'box-shadow\s*:',
            '进度步骤的 reduced-motion 规则要同时给一个静态 box-shadow，'
            '只写 animation: none 的话当前步骤的高亮就整块没了。')

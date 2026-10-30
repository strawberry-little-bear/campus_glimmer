"""对比度契约：把 WCAG AA 的文字可读性钉在静态断言里。

站点里唯一大面积铺浅色文字的地方是三块深绿面板：首页的 .hero-panel、登录
注册页的 .auth-story、账户中心的 .account-hero。它们的观感来自「一段线性渐变
叠加若干径向高光」，而浏览器真正取到的背景色是逐层合成之后的结果——写样式的
时候盯着任何一个色标看都觉得够深，合成之后的最亮处却可能刚好跌到 4.5:1 以下。
DevTools 的点选取色抓不到这类问题：点中的那个像素恰好不是最亮处。

同类问题还有两种更隐蔽的形态。一种是半透明前景：rgba(255,255,255,.76) 不是
「白字」，合成到深绿上之后大约只剩 #d2e0d8，比值直接掉掉一整个档位，只看 color
声明会以为没问题。另一种是令牌联动：--muted 被全站四百多处引用，改一次就同时
改掉所有次要文字，而它在暖色渐变面板上恰好压在 4.5 的临界线上，差 0.01 就是不合格。

这里按与浏览器一致的分层合成规则，把每个浅色文字位点的最坏背景算出来，再断言
达到 AA。改渐变色标、高光透明度、文字透明度、令牌值中的任何一项，这里都会立刻
变红，不必再为此起一轮浏览器审计。

判据与 DOM 真值审计（CDP 逐页实测 53 个页面，工具链在仓库外的工作目录）一致：

* 前景先按 alpha 合成到背景上，再算比值；
* 一段多停止点渐变取「与前景比值最小的那个停止点」，即最坏情况；
* 这些位点的字号都小于 18.66px，一律按 4.5:1 要求，不吃大字豁免。
"""

import re
from pathlib import Path

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'

CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)
RULE = re.compile(r'([^{}]+)\{([^{}]*)\}')
VARIABLE = re.compile(r'(--[\w-]+)\s*:\s*([^;]+)')
VAR_USE = re.compile(r'var\((--[\w-]+)\s*(?:,\s*([^)]+))?\)')
COLOR_TOKEN = re.compile(r'rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}\b|\btransparent\b')
CODE_TARGET = re.compile(r'(?<![-\w])code(?![\w-])')
WHITE = (255.0, 255.0, 255.0)
HEX6 = re.compile(r'^#[0-9a-fA-F]{6}$')


def parse_color(token):
    """把 CSS 颜色字面值解析成 (rgb, alpha)。"""
    token = token.strip()
    if token == 'transparent':
        return (0.0, 0.0, 0.0), 0.0
    match = re.fullmatch(r'rgba?\(([^)]+)\)', token)
    if match:
        parts = [float(part) for part in match.group(1).split(',')]
        if len(parts) < 3:
            raise AssertionError('无法解析颜色 %r' % token)
        return (parts[0], parts[1], parts[2]), (parts[3] if len(parts) > 3 else 1.0)
    digits = token.lstrip('#')
    if len(digits) == 3:
        digits = ''.join(char * 2 for char in digits)
    elif len(digits) == 8:
        digits = digits[:6]
    if not HEX6.match('#' + digits):
        raise AssertionError('无法解析颜色 %r' % token)
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)), 1.0


def luminance(rgb):
    def channel(value):
        value = value / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])


def contrast(first, second):
    high, low = luminance(first), luminance(second)
    if high < low:
        high, low = low, high
    return (high + 0.05) / (low + 0.05)


def composite(top, alpha, bottom):
    return tuple(top[index] * alpha + bottom[index] * (1 - alpha) for index in range(3))


def split_layers(value):
    """按顶层逗号切开 background 简写的多层声明，括号里的逗号不算分隔符。"""
    layers, depth, current = [], 0, ''
    for char in value:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        if char == ',' and depth == 0:
            layers.append(current)
            current = ''
        else:
            current += char
    if current.strip():
        layers.append(current)
    return layers


def iter_rules(css):
    """按文档顺序产出 (选择器集合, 规则体)。

    选择器按逗号切开后逐个精确比对，所以 `.contribution-page .account-hero-actions
    .btn-light` 不会被误当成 `.account-hero-actions .btn-light`——这两条规则在同
    一个页面上是两种完全不同的按钮：深底透明白字，和白底深字。
    """
    for match in RULE.finditer(css):
        selectors = {part.strip() for part in match.group(1).split(',')}
        selectors.discard('')
        if selectors:
            yield selectors, match.group(2)


def resolve(value, variables):
    """把 var(--x) / var(--x, fallback) 替换成字面值。"""
    def replace(match):
        name, fallback = match.group(1), match.group(2)
        if name in variables:
            return variables[name].strip()
        if fallback and fallback.strip():
            return fallback.strip()
        raise AssertionError('style.css 里没有定义变量 %s' % name)
    return VAR_USE.sub(replace, value)


class StylesheetMixin:
    """读一次样式表，提供规则查询与层叠取值。"""

    # 正文的 AA 阈值。契约覆盖的位点字号都小于 18.66px，不吃大字豁免；
    # 图标等非文本元素的 3:1 不在这份契约里，仍按浏览器审计复核。
    AA_NORMAL = 4.5

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = CSS_COMMENT.sub(' ', STYLESHEET.read_text(encoding='utf-8'))
        cls.variables = dict(VARIABLE.findall(cls.css))

    def _bodies(self, selector):
        bodies = [body for selectors, body in iter_rules(self.css) if selector in selectors]
        self.assertTrue(
            bodies, 'style.css 里找不到选择器 %r，契约表可能过期了' % selector)
        return bodies

    def _last_declaration(self, selector, prop):
        """取某个选择器上某个属性的层叠胜出值，同特异性后者胜。"""
        pattern = re.compile(r'(?<![-\w])' + re.escape(prop) + r'\s*:\s*([^;]+)')
        found = None
        for body in self._bodies(selector):
            match = pattern.search(body)
            if match:
                found = match.group(1).strip()
        return found

    def _declaration(self, selector, prop):
        found = self._last_declaration(selector, prop)
        self.assertIsNotNone(found, '%s 上没有 %s 声明' % (selector, prop))
        return resolve(found, self.variables)

    def _background_layers(self, selector):
        found = self._last_declaration(selector, 'background')
        return split_layers(resolve(found, self.variables)) if found else []


class StylesheetContrastTests(StylesheetMixin, SimpleTestCase):
    """按浏览器的合成顺序，复算每个浅色文字位点的最坏背景。"""

    def _worst_background(self, chain, foreground, alpha):
        """从最外层祖先往里逐层合成，返回与前景比值最小的那个背景色。"""
        painted = (255.0, 255.0, 255.0)
        for selector in reversed(chain):
            for layer in reversed(self._background_layers(selector)):
                stops = [parse_color(token) for token in COLOR_TOKEN.findall(layer)]
                if not stops:
                    continue
                worst = None
                for rgb, stop_alpha in stops:
                    candidate = composite(rgb, stop_alpha, painted) if stop_alpha < 1 else rgb
                    effective = composite(foreground, alpha, candidate) if alpha < 1 else foreground
                    value = contrast(effective, candidate)
                    if worst is None or value < worst[0]:
                        worst = (value, candidate)
                if worst is not None:
                    painted = worst[1]
        return painted

    def _assert_contrast(self, label, chain):
        foreground, alpha = parse_color(self._declaration(chain[-1], 'color'))
        background = self._worst_background(chain, foreground, alpha)
        effective = composite(foreground, alpha, background) if alpha < 1 else foreground
        ratio = contrast(effective, background)
        shade = [int(round(channel)) for channel in background]
        self.assertGreaterEqual(
            ratio, self.AA_NORMAL,
            '%s：前景 alpha=%.2f 落在最坏背景 #%02x%02x%02x 上只有 %.2f:1，'
            '低于 AA 要求的 %.1f:1。深绿面板的对比度由渐变色标和高光透明度共同决定，'
            '改其中任何一项都要在这里重新达标。'
            % (label, alpha, shade[0], shade[1], shade[2], ratio, self.AA_NORMAL))

    # (说明, 从最外层祖先到元素自身的选择器链)
    CONTRAST_CASES = [
        ('首页 hero 面板的眼眉行', ['.hero-panel', '.hero-panel .eyebrow']),
        ('首页 hero 面板的正文', ['.hero-panel', '.hero-copy']),
        ('首页 hero 面板的信任条目', ['.hero-panel', '.hero-trust']),
        ('登录页故事面板的眼眉行', ['.auth-story', '.auth-story-eyebrow']),
        ('登录页故事面板的正文',
         ['.auth-story', '.auth-story > .auth-story-content > p']),
        ('登录页故事面板的受益条目', ['.auth-story', '.auth-benefit-list li']),
        ('登录页故事面板的提示胶囊', ['.auth-story', '.auth-story-note']),
        ('账户中心 hero 的眼眉行', ['.account-hero', '.account-identity .eyebrow']),
        ('账户中心 hero 的简介', ['.account-hero', '.account-bio']),
        ('账户中心 hero 的次级按钮', ['.account-hero', '.account-hero-actions .btn-light']),
        ('贡献页 hero 的数据球',
         ['.account-hero', '.contribution-page .contribution-hero-orb',
          '.contribution-page .contribution-hero-orb span']),
        ('消息工作台 hero 的说明', ['.inbox-hero', '.inbox-hero p']),
        ('订单操作卡的眼眉行', ['.order-action-card', '.order-action-card .eyebrow']),
    ]

    def test_light_text_positions_meet_aa(self):
        for label, chain in self.CONTRAST_CASES:
            with self.subTest(case=label):
                self._assert_contrast(label, chain)


class ContrastTokenTests(StylesheetMixin, SimpleTestCase):
    """令牌级下限。

    --muted 被全站四百多处引用，<code> 的颜色只在一条全局规则里定义，都属于
    「改一处、影响一片」的形状，值得和上面的位点测试分开钉住。
    """

    # 深色文字的最坏情况是最亮的底色，所以白底必须在内。
    LIGHT_SURFACES = ('--surface', '--cream', '--surface-soft', '--mint')

    def _token(self, name):
        self.assertIn(name, self.variables, 'style.css 里没有定义 %s' % name)
        value = self.variables[name].strip()
        self.assertRegex(
            value, HEX6, '%s 必须是六位十六进制色值，现在是 %r' % (name, value))
        return parse_color(value)[0]

    def test_muted_token_clears_aa_on_every_light_surface(self):
        muted = self._token('--muted')
        for name in self.LIGHT_SURFACES:
            with self.subTest(surface=name):
                self.assertGreaterEqual(
                    contrast(muted, self._token(name)), self.AA_NORMAL,
                    '--muted 落在 %s 上不达标' % name)

    def test_primary_tokens_clear_aa_on_light_surfaces(self):
        for name in ('--primary', '--primary-dark'):
            colour = self._token(name)
            for surface in self.LIGHT_SURFACES:
                with self.subTest(token=name, surface=surface):
                    self.assertGreaterEqual(
                        contrast(colour, self._token(surface)), self.AA_NORMAL,
                        '%s 落在 %s 上不达标' % (name, surface))

    def test_global_code_colour_clears_aa_on_light_surfaces(self):
        """全局 code 字色要扛住它能出现的每一种浅色版面。

        style.css 里只有一条 `code { color: ... }`，没有给它画底板：行内代码是
        嵌在正文、卡片、表格里用的，底板由所在的版面给。所以这里不能只对着一个
        假想的芯片底色算，要把所有浅色底面都过一遍——最深的那一档就是真实下限。
        """
        colour, _ = parse_color(self._declaration('code', 'color'))
        for name in self.LIGHT_SURFACES:
            with self.subTest(surface=name):
                self.assertGreaterEqual(
                    contrast(colour, self._token(name)), self.AA_NORMAL,
                    '全局 code 字色落在 %s 上只有 %.2f:1' % (name, contrast(colour, self._token(name))))

    def test_local_code_rules_stay_legible(self):
        """局部改了 code 的规则，要在它真正会落到的底面上达标。

        局部规则分两种形状：一种自己画了底板（芯片），字色还是继承来的全局值；
        另一种只改字色（比如把某列的行内代码换成暖色），底板继承表格。第二种
        没有本地背景可算，就按最坏情况拿最亮的浅色底面试它——版面哪天从白卡挪
        到米白底，这个断言会先红，而不是等用户来报。
        """
        base, _ = parse_color(self._declaration('code', 'color'))
        checked = 0
        for selectors, body in iter_rules(self.css):
            targets = [s for s in selectors if CODE_TARGET.search(s)]
            if not targets:
                continue
            local = re.search(r'(?<![-\w])color\s*:\s*([^;]+)', body)
            background = re.search(r'(?<![-\w])background\s*:\s*([^;]+)', body)
            if not local and not background:
                continue
            label = sorted(targets)[0]
            colour, alpha = parse_color(
                resolve(local.group(1), self.variables)) if local else (base, 1.0)
            checked += 1
            if background:
                stops = [parse_color(token)
                         for token in COLOR_TOKEN.findall(resolve(background.group(1), self.variables))]
                for rgb, stop_alpha in stops:
                    surface = composite(rgb, stop_alpha, WHITE) if stop_alpha < 1 else rgb
                    self.assertGreaterEqual(
                        contrast(composite(colour, alpha, surface), surface), self.AA_NORMAL,
                        '%s 在自己的底板 #%02x%02x%02x 上只有 %.2f:1'
                        % (label, surface[0], surface[1], surface[2],
                           contrast(composite(colour, alpha, surface), surface)))
            else:
                for name in self.LIGHT_SURFACES:
                    surface = self._token(name)
                    with self.subTest(rule=label, surface=name):
                        self.assertGreaterEqual(
                            contrast(composite(colour, alpha, surface), surface), self.AA_NORMAL,
                            '%s 的字色落在 %s 上只有 %.2f:1'
                            % (label, name, contrast(composite(colour, alpha, surface), surface)))
        self.assertGreater(checked, 0, '一条 code 局部规则都没扫到，正则大概失效了')

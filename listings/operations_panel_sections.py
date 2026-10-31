# -*- coding: utf-8 -*-
"""怎么从运营看板里，干净地切出一个面板。

运营看板是一页一百多个面板，页面和 CSV 导出是同一份数据的两种形状。测试要断言
「某个面板里有什么、没有什么」，第一步都是把那个面板从整页里切出来。这件事看着
只是字符串处理，实则藏着上一轮一整类失败，所以把它收在这里一处，别再各写一份。

最要紧的一条：不要按裸面板名做子串切片。CSV 的第一段是「看板自身构建耗时」，
六十九个共享构建器的标签都会以「构建：<标签>」的形状在那里出现一次，而那张表
按耗时降序排，顺序随数据波动。也就是说，一个面板标签在 CSV 里的第一次出现，往往
落在耗时表里，不在它自己的面板里。按 `text.split('标签', 1)` 切，切出来的是耗时表
到下一个面板之间的一大块，边界跟着耗时排序漂移：这一轮全绿，下一轮换台机器就跑红。

正确的切法只有一种：按行切，而且只认整格相等的行。节标题在导出里独占一格，
`row[0] == 标题` 就是那一行；表头行至少两格，天然不会和节标题混。终点也不能用
「下一条数据长什么样」去认，要用下一个节标题——节标题的清单只能从页面拿，因为节
标题（<h2>）和小标题（<h3>）在导出里形状一模一样，都是独占一格的单行，光看 CSV
分不出谁是边界。

页面那一侧的切法同理：从某个 <h2> 到下一个 <h2> 之前。面板在页面上包在
<section id="..."> 里，有 id 可用时按 id 切更稳；没有 id 的地方，按 <h2> 切和按 id
切等价，因为标题在页面上唯一。

用法：

    sections = panel_sections(html)
    body = section_body(export_rows(text), '出售价格轨迹质量', sections)
    panel = panel_html(html, '出售价格轨迹质量')

两个切片函数找不到标题时都直接报错，不返回空串。空串会让 assertNotIn 这类反向
断言空转——看着是通过，其实什么都没验。
"""

import csv
import io
import re

PANEL_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
SUBHEAD = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
TAG = re.compile(r"<[^>]+>")


def strip_tags(raw):
    """去掉一段 HTML 的标签，只留文字。"""
    return TAG.sub("", raw).strip()


def panel_sections(html):
    """面板的节标题集合，按 <h2> 抓。

    节标题和子标题在导出里形状一模一样——都是独占一格的单行。要区分它们，只能
    回到页面：<h2> 是节，<h3> 是节底下的小标题。少了这一步，导出里每个小标题
    都会被当成新的一节，于是「这个小标题归哪一节」就永远问不出来。
    """
    return {strip_tags(raw) for raw in PANEL_TITLE.findall(html) if strip_tags(raw)}


def panel_subheads(html):
    """面板的小标题集合，按 <h3> 抓。

    存在的意义是反面那条：小标题不能和任何一个节标题同名。section_body 拿节标题
    当边界，哪个小标题要是撞了名，它所在的那一节就会被提前截断，断点之后的断言
    全部失真，而且从失败信息上看不出是哪里截的。
    """
    return {strip_tags(raw) for raw in SUBHEAD.findall(html) if strip_tags(raw)}


def panel_html(html, title):
    """页面里某个面板的那一段，从它的 <h2> 到下一个 <h2> 之前。

    标题找不到就报错，不返回空串：空的切片会让 assertNotIn 空转成通过。
    """
    marks = [(m.start(), m.end(), strip_tags(m.group(1))) for m in PANEL_TITLE.finditer(html)]
    own = [index for index, (_, _, name) in enumerate(marks) if name == title]
    if not own:
        raise AssertionError('运营看板页面上没有标题为 %r 的面板' % title)
    start = marks[own[0]][1]
    following = own[0] + 1
    end = marks[following][0] if following < len(marks) else len(html)
    return html[start:end]


def export_rows(text):
    """CSV 的全部行，每行是去空白后的格子列表。"""
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]


def section_body(rows, title, sections):
    """某个节标题之后、下一个节标题之前的所有行。

    节标题独占一格时，它下面紧跟的就是这个节的内容；标题即表头时，它自己那行也要算。
    节与节之间用空行分隔，但空行也出现在同一节的多个小标题之间，所以真正的边界
    是下一个节标题，不是下一个空行。

    节标题的判据只看第一格：导出里「标题即表头」的节把节标题和列名写在同一行，
    这种行的第一格仍然是节标题。判据用面板的 <h2> 清单兜底——只有面板上真的
    存在这个标题，它才算下一节的开头；否则一段说明文字恰好以某个节标题起头，
    就会把一节劈成两半。

    起点也必须是整格相等。按子串找起点，第一刀就会落进顶部的耗时表——那里的标签
    长成「构建：<标题>」，子串是命中的，整格不是。
    """
    body = []
    started = False
    for row in rows:
        if not started:
            if row and row[0] == title:
                started = True
                body.append(row)
            continue
        # 下一个节标题出现，本节到此为止。小标题不算边界。
        if row and row[0] in sections and row[0] != title:
            break
        body.append(row)
    return body


def export_section(text, title, sections):
    """导出文本里 title 这一节的行，是 export_rows 加 section_body 的合写。

    找不到标题就报错，不返回空行列表：空列表会让 assertNotIn 空转成通过，而按
    子串切片的老写法在锚点丢失时会直接抛 IndexError，同样是响亮的失败。
    """
    rows = section_body(export_rows(text), title, sections)
    if not rows:
        raise AssertionError('运营看板导出里没有标题为 %r 的节' % title)
    return rows


def export_section_text(text, title, sections):
    """导出文本里 title 这一节的 CSV 原文。

    「某个词在不在这一节里」这种问法要的是原文，不是行，所以另给一版。断言子串的
    测试用这一版；断言整行（比如某个表头）的测试用 export_section。
    """
    return '\n'.join(','.join(row) for row in export_section(text, title, sections))

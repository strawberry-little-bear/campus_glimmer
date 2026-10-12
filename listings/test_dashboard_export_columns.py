# -*- coding: utf-8 -*-
"""守住「导出的每一张表，都认得出自己的表头」这条线：CSV 要能直接当表格用。

上一轮的覆盖断言只比节标题那一格。节标题对上了，节下面是什么并没人管——而这正是
偏差能藏住的地方。实测过：三十九张面板表格里，三十八张在导出里找不到列名一致的表头
行；还有十九个节的键值摘要行紧跟在节标题后面，形状和表头一模一样，运营把它当表头
导入 Excel，得到的列名是「周期内运行 / 0 / 失败 / 0」。

面板和导出的列名本来就该不一样：面板的列名要塞进窄卡片，写的是短名；下载回去的表
要能单独读，写的是全名。所以这个模块不要求两边列名相同，它要求的是另一件事——
导出的表头行必须认得出自己是表头，而且每个面板表格都得在导出里有一张对应的表。

认得出的办法是行形状。节标题独占一格；表头行至少两格，且紧跟节标题或子标题；键值
摘要是成对的「名字, 数值, 名字, 数值」，它只能挂在显式的口径子标题底下。三种行各有
各的落点，就不必再靠人在脑子里数格子判断这一行到底是什么。

三条边界比其余更要紧。面板上不是表格的面板没有表头可比：汇总卡片、漏斗、排行这些
本来就是给眼睛看的，硬要给它们编列名只会得到一份更假的表；所以这里只比模板里真的
用 <table> 画出来的表。空态文案不是数据行：当前还没有搜索记录这种句子整行只有一格，
它不该被算成表头。以及这里只做比对，不改写看板、不改写导出，也不为了让某个列名
对上而调整任何一边的文案。
"""

import csv
import io
import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AcademicPhase, AcademicTerm, CampusCampaign, CampusLocation, Category,
    CommunityContribution, DemandPost, DemandResponse, Item, MutualAidFeedback,
    Order,
)
from chat_messages.models import ModerationEvent
from .task_runs import normalise_metrics, record_task_run

from datetime import timedelta


PANEL_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
TABLE = re.compile(r"<table.*?</table>", re.S)
TH = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
SUBHEAD = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
TAG = re.compile(r"<[^>]+>")


def panel_tables(html):
    """按文档顺序抓出每张面板表格，连同它所属的节标题和子标题。

    返回 [(节标题, 子标题或 None, [列名, ...]), ...]：一个面板里有多张表就返回多条，
    顺序与模板一致。只收模板里真的用 <table> 画出来的表；汇总卡片和漏斗不是表格，
    不收。子标题取表格之前最近的那个 <h3>，没有就是 None。
    """
    marks = [(m.start(), m.end(), _text(m.group(1))) for m in PANEL_TITLE.finditer(html)]
    tables = []
    for index, (_, body_end, title) in enumerate(marks):
        segment_end = marks[index + 1][0] if index + 1 < len(marks) else len(html)
        segment = html[body_end:segment_end]
        subheads = [
            (body_end + m.start(), _text(m.group(1)))
            for m in SUBHEAD.finditer(segment)
        ]
        for table in TABLE.finditer(segment):
            columns = [_text(cell) for cell in TH.findall(table.group(0))]
            columns = [column for column in columns if column]
            if not columns:
                continue
            table_at = body_end + table.start()
            subhead = None
            for position, name in subheads:
                if position < table_at:
                    subhead = name
            tables.append((title, subhead, columns))
    return tables


def export_rows(text):
    """CSV 的全部行，每行是去空白后的格子列表。"""
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]


def is_key_value_summary(row):
    """成对的「名字, 数值, 名字, 数值」摘要行。

    这种行紧跟节标题时和表头行形状相同，运营会把它当表头导进 Excel，得到的
    列名就成了「周期内运行 / 0 / 失败 / 0」。它必须落在一个显式的口径子标题
    底下，不能紧跟着节标题。判据只看成对的偶数格：奇数格的行是数据行，
    不是摘要。
    """
    filled = [cell for cell in row if cell]
    if len(filled) < 4 or len(filled) % 2 != 0:
        return False
    values = filled[1::2]
    return all(_looks_like_value(cell) for cell in values)


def is_header_row(row):
    """表头行：至少两格有内容，不是键值摘要，也不是数据行。

    数据行最容易混进来：一张十一列的表，它的每一行数据也是十一格，前两格还是
    「命令名, 数字」这种形状。少了这一条，表头被砍掉一列也查不出来——那条被
    误判成表头的数据行会替真表头把宽度凑够，尺子就瞎了。

    判据是「整行没有一格是纯数字」：列名里不会出现纯数字，而数据行里几乎总有
    一格是。不按占比走，宽表的尾部常常是一串空字符串，占比会被稀释过去，
    十一列的数据行只占三格数字也过不了线。
    """
    filled = [cell for cell in row if cell]
    if len(filled) < 2:
        return False
    if is_key_value_summary(row):
        return False
    # 第一格是数字的行一定是数据：列名不以数字起头。
    if _looks_like_value(filled[0]):
        return False
    # 数据行里至少有一格是纯数字，表头行一整行都是列名，不会有纯数字。
    # 判据不按占比：宽表的尾部常常是一串空字符串，占比会被稀释过去，
    # 十一列的数据行只占三格数字也过不了线。
    if any(_is_number(cell) for cell in row):
        return False
    return True


def _is_number(cell):
    if not cell:
        return False
    try:
        float(cell.replace('%', '').replace(',', ''))
        return True
    except ValueError:
        return False


def _looks_like_value(cell):
    if cell in ('', '—', '-'):
        return True
    try:
        float(cell.replace('%', '').replace(',', ''))
        return True
    except ValueError:
        return False


def _text(raw):
    return TAG.sub("", raw).strip()


def _under_subhead(body, subhead):
    """从子标题那一行开始，到本段结束为止的行。"""
    out = []
    started = False
    for row in body:
        if row and row[0] == subhead and not started:
            started = True
        if started:
            out.append(row)
    return out


def panel_sections(html):
    """面板的节标题集合，按 <h2> 抓。

    节标题和子标题在导出里形状一模一样——都是独占一格的单行。要区分它们，只能
    回到面板：<h2> 是节，<h3> 是节底下的小标题。少了这一步，导出里每个小标题
    都会被当成新的一节，于是「这个小标题归哪一节」就永远问不出来。
    """
    return {
        TAG.sub('', raw).strip()
        for raw in PANEL_TITLE.findall(html) if TAG.sub('', raw).strip()
    }


def _header_gaps(title, subhead, columns, body):
    """一张面板表格在导出里的表头缺口，没有缺口就返回空列表。

    导出的表不能比面板还窄。面板是窄卡片，常常把第二个数字塞进 <small> 里
    省一列，导出则一个字段一列，所以导出比面板宽是正常的；反过来导出比面板
    窄，就是下载回去的表丢了面板上看得见的信息。列名不要求相同——面板的列名
    要塞进窄卡片，导出的列名要能单独读，本来就该不一样。
    """
    heads = [row for row in body if is_header_row(row)]
    if not heads:
        return [(title, subhead, columns, 'no header row at all')]
    widths = {len(row) for row in heads}
    if not any(width >= len(columns) for width in widths):
        return [(title, subhead, columns, 'widths %s' % sorted(widths))]
    return []


def section_body(rows, title, sections):
    """某个节标题之后、下一个节标题之前的所有行。

    节标题独占一格时，它下面紧跟的就是这个节的内容；标题即表头时，它自己那行也要算。
    节与节之间用空行分隔，但空行也出现在同一节的多个小标题之间，所以真正的边界
    是下一个节标题，不是下一个空行。

    节标题的判据只看第一格：导出里「标题即表头」的节把节标题和列名写在同一行，
    这种行的第一格仍然是节标题。判据用面板的 <h2> 清单兜底——只有面板上真的
    存在这个标题，它才算下一节的开头；否则一段说明文字恰好以某个节标题起头，
    就会把一节劈成两半。
    """
    body = []
    started = False
    for row in rows:
        if not started:
            if row and row[0] == title:
                started = True
                body.append(row)
            continue
        # 下一个节标题出现，本节到此为止。子标题不算边界。
        if row and row[0] in sections and row[0] != title:
            break
        body.append(row)
    return body


class DashboardExportColumnTests(TestCase):
    """每个面板表格，都要在导出里有一张认得出表头的表。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='column-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)

    def fetch(self):
        html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        text = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        ).content.decode('utf-8-sig')
        return html, export_rows(text)

    def test_every_panel_table_has_a_recognisable_header_in_the_export(self):
        html, rows = self.fetch()

        tables = panel_tables(html)
        self.assertTrue(tables)

        sections = panel_sections(html)
        missing = []
        for title, subhead, columns in tables:
            body = section_body(rows, title, sections)
            if subhead:
                body = _under_subhead(body, subhead)
            missing.extend(_header_gaps(title, subhead, columns, body))
        self.assertEqual(missing, [])

    def test_every_panel_subheading_stays_inside_its_section(self):
        # 面板里的 <h3> 在导出里必须还是它那个 <h2> 节底下的子标题，不能被提到顶层：
        # 提到顶层之后，运营对着屏幕找这一节，下载回去只看到几个陌生的顶层节。
        html, rows = self.fetch()

        tables = panel_tables(html)
        subheads = {
            (title, subhead) for title, subhead, _ in tables if subhead
        }
        self.assertTrue(subheads)

        sections = panel_sections(html)
        leaked = []
        for title, subhead in sorted(subheads):
            # 子标题必须在它所属的节里出现过。
            body = section_body(rows, title, sections)
            if not _under_subhead(body, subhead):
                leaked.append((title, subhead))
        self.assertEqual(leaked, [])

    def _seed_a_populated_campus(self):
        """造一批跨模块的数据，让面板上大多数的表都真的有行可渲染。

        空态下面板只渲染得出九张表，其余的都折叠成一句提示；只在空态下比对，等于
        放过了三十张表的结构。这里把商品、求购、响应、反馈、贡献、专题、学期和
        定时任务台账都铺上，看板才会把它的表格真正画出来。
        """
        seller = User.objects.create_user(username='column-seller')
        buyer = User.objects.create_user(username='column-buyer')
        category = Category.objects.create(name='列名测试分类')
        location = CampusLocation.objects.create(
            name='列名测试地点', building='列名测试楼', is_public=True,
        )
        item = Item.objects.create(
            title='列名测试商品', description='用于看板列名比对', price='18.00',
            category=category, condition='良好', seller=seller,
            location=location, status='available',
        )
        demand = DemandPost.objects.create(
            requester=buyer, title='列名测试求购', description='用于看板列名比对',
            category=category, location=location, status='active',
        )
        Order.objects.create(
            item=item, buyer=buyer, seller=seller, meeting_location=location,
            agreed_price='18.00', status='completed',
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=seller, message='我有',
            match_score=90, status='accepted',
        )
        MutualAidFeedback.objects.create(
            demand_response=response, submitted_by=buyer,
            outcome='completed', tags=['on_time'], note='很快',
        )
        CommunityContribution.objects.create(
            user=buyer, kind='mutual_aid_completed', points=10,
            title='完成一次校园互助', source_key='column-mutual-aid-1',
        )
        campaign = CampusCampaign.objects.create(title='列名测试专题', description='看板')
        campaign.items.add(item)
        term = AcademicTerm.objects.create(
            name='列名测试学期', slug='column-test-term', kind='spring',
            starts_on=timezone.localdate() - timedelta(days=20),
            ends_on=timezone.localdate() + timedelta(days=60),
        )
        AcademicPhase.objects.create(
            term=term, phase='regular', start_offset=0, end_offset=14,
        )
        for name in ('expire_items', 'refresh_demand_matches'):
            with record_task_run(name) as run:
                run.metrics = normalise_metrics({'sent': 3})
            with record_task_run(name) as run:
                run.metrics = normalise_metrics({'sent': 1})
        # 内容审核面板铺够样本，否则它整块折成一句空态提示，四张表一张都不渲染。
        reviewer = User.objects.create_user(username='column-reviewer')
        now = timezone.now()
        for index in range(3):
            ModerationEvent.objects.create(
                channel='comment', author=buyer, item=item,
                content='被拦下的内容', matched_terms='外部链接',
                risk_score=20, risk_level='low', status='dismissed',
                reviewed_by=reviewer, reviewed_at=now - timedelta(days=1),
            )
            ModerationEvent.objects.create(
                channel='private_message', author=buyer, item=item,
                content='被拦下的内容', matched_terms='高风险交易、手机号',
                risk_score=85, risk_level='high', status='confirmed',
                reviewed_by=reviewer, reviewed_at=now - timedelta(days=2),
            )

    def test_the_same_holds_once_the_panels_have_rows(self):
        # 空态只渲染九张表，这一条把数据铺满再比一次：结构要对上的不是空态文案，
        # 而是运营真正会盯着看的那三十来张表。
        self._seed_a_populated_campus()
        html, rows = self.fetch()

        tables = panel_tables(html)
        self.assertGreater(len(tables), 12)

        sections = panel_sections(html)
        missing = []
        for title, subhead, columns in tables:
            body = section_body(rows, title, sections)
            if subhead:
                body = _under_subhead(body, subhead)
            missing.extend(_header_gaps(title, subhead, columns, body))
        self.assertEqual(missing, [])

    def test_no_key_value_summary_masquerades_as_a_header(self):
        # 键值摘要紧跟在节标题后面时，它和表头行形状相同，运营会把它当表头。
        # 这里反过来查：节标题的下一行如果是成对的键值，就必须先落一个口径子标题。
        _, rows = self.fetch()

        leaks = []
        for index, row in enumerate(rows):
            if index == 0:
                continue
            previous = rows[index - 1]
            # 节标题独占一个空行分隔出来的区域，且那一行只有一个非空格子。
            if not previous or not previous[0]:
                continue
            if any(cell for cell in previous[1:]):
                continue
            # 上一行自己是键值行的话，它不可能是节标题。
            if not is_header_row(previous) and len(previous) > 1:
                continue
            if not is_key_value_summary(row):
                continue
            if previous[0] in ('口径与门槛', '说明', '导出口径'):
                continue
            leaks.append((previous[0], row))
        self.assertEqual(leaks, [])
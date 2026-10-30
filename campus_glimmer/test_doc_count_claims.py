"""文档里的计数声明必须是真的（docs 目录下全部 markdown）。

docs/ 下有大量自我声明的条目数：展开这 17 条口径、（9 条，含缩进子项）、
模型分组（52 行，按业务领域归组）、共 30 项，分九组。读者会把这些数字当成
承诺——折叠块写着「16 条」而里面躺着 17 条，比不写数字更糟，因为读者从此
不再自己数。

三类折叠块有三种数法，混用正是上一轮漂移的成因：

* 「（N 条，含缩进子项）」数块内所有层级的 bullet，缩进子项在那里是正文；
* 「展开这 N 条口径」与「展开这 N 个方向」只数顶层 bullet，缩进项是某条口径
  的推理过程，不是额外口径；
* 「按业务领域归组」「按业务链路归组」的表格型清单数 markdown 表格的数据行，
  表头和分隔行都不算。

两份设计文档还有第四种写法：「共 N 项，分 M 组」。每组自己声明小计，小计之和
必须等于 N，而 N 又必须等于那段话点名的测试文件里真实的 def test_ 个数。后者
对着代码验，不对着散文验。
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / 'docs'

FENCE = chr(96) * 3
SUMMARY_RE = re.compile(r'^\s*<summary>(.*?)</summary>\s*$')
PAREN_RE = re.compile(r'（(\d+)\s*([^）]*)）')
EXPAND_RE = re.compile(r'展开这\s*(\d+)\s*(?:条口径|个方向)')
BULLET_RE = re.compile(r'^(\s*)(?:[-*+]|\d+[.)])\s+')
GROUP_TOTAL_RE = re.compile(r'共\s*(\d+)\s*项，分([一二三四五六七八九十]+)组')
GROUP_ITEM_RE = re.compile(r'^\s*-\s+\*\*[^*]*（(\d+)\s*项[^）]*）')
TEST_DEF_RE = re.compile(r'^\s*def (test_\w+)', re.MULTILINE)

CN_NUMERALS = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}

# 这两篇设计文档把测试个数写成了散文，所以每组小计之和要同时对上声明总数与
# 真实测试文件里的 def test_ 个数。对着代码验，才不会自己被自己骗过去。
GROUP_CLAIM_DOCS = {
    'backup-script-boundary-design.md': 'campus_glimmer/test_backup_script.py',
    'object-storage-ci-integration-design.md': 'campus_glimmer/test_object_storage_integration.py',
}


def _is_table_separator(line):
    stripped = line.strip()
    if not stripped.startswith('|'):
        return False
    cells = stripped.strip('|').split('|')
    if not any(cell.strip() for cell in cells):
        return False
    for cell in cells:
        if not re.match(r'^[\s:-]*$', cell):
            return False
    return any('-' in cell for cell in cells)


def _count_entries(body):
    inside_fence = False
    bullets = 0
    top_level = 0
    pipe_rows = 0
    for line in body:
        if line.lstrip().startswith(FENCE):
            inside_fence = not inside_fence
            continue
        if inside_fence or not line.strip():
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            bullets += 1
            if len(bullet.group(1)) == 0:
                top_level += 1
        elif line.strip().startswith('|') and not _is_table_separator(line):
            pipe_rows += 1
    return {'bullets': bullets, 'top_level': top_level, 'pipe_rows': pipe_rows}


def _details_blocks(lines):
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped == '<details>':
                start = index
            continue
        if stripped == '</details>':
            yield start, lines[start + 1:index]
            start = None


def _pop_summary(body, start):
    for offset, line in enumerate(body):
        match = SUMMARY_RE.match(line)
        if match is not None:
            del body[:offset + 1]
            return match.group(1), start + offset + 1
    return None, None


def _iter_docs():
    for path in sorted(DOCS_DIR.rglob('*.md')):
        yield path, path.read_text(encoding='utf-8-sig').split(chr(10))


class SummaryCountClaimTests(SimpleTestCase):
    def test_summary_counts_match_block_contents(self):
        mismatches = []
        checked = 0
        for path, lines in _iter_docs():
            rel = path.relative_to(ROOT).as_posix()
            for start, body in _details_blocks(lines):
                summary, summary_line = _pop_summary(body, start)
                if summary is None:
                    continue
                expanded = EXPAND_RE.search(summary)
                paren = PAREN_RE.search(summary)
                if '含缩进子项' in summary:
                    rule, stated, key = '含缩进子项', int(paren.group(1)), 'bullets'
                elif expanded is not None:
                    rule, stated, key = '顶层条目', int(expanded.group(1)), 'top_level'
                elif paren is not None and '归组' in paren.group(2):
                    rule, stated, key = '表格数据行', int(paren.group(1)), 'pipe_rows_minus_header'
                elif paren is not None:
                    rule, stated, key = '条目数', int(paren.group(1)), 'bullets'
                else:
                    continue
                counts = _count_entries(body)
                actual = counts['pipe_rows'] - 1 if key == 'pipe_rows_minus_header' else counts[key]
                checked += 1
                if actual != stated:
                    mismatches.append(
                        rel + ':' + str(summary_line) + ' 「' + summary[:40] + '」声明 ' + str(stated)
                        + '（' + rule + '），实际 ' + str(actual)
                    )
        self.assertGreater(checked, 150, '扫到的计数声明太少，说明解析规则失效了')
        self.assertEqual(mismatches, [], '折叠块计数声明与真实内容不一致')

    def test_module_map_section_headings_match_their_lists(self):
        # 这两个二级标题和紧跟其后的折叠块说的是同一张清单，数字被写了两遍。
        # 只修折叠块等于把另一半留着漂，所以两边都钉住。
        headings = {
            'listings/ 的业务模块': 'table',
            '业务链路的测试模块': 'bullets',
        }
        lines = (DOCS_DIR / 'module-map.md').read_text(encoding='utf-8-sig').split(chr(10))
        mismatches = []
        for index, line in enumerate(lines):
            match = re.match(r'^##\s+(.+?)（(\d+)\s*个）\s*$', line)
            if match is None or match.group(1) not in headings:
                continue
            rule, stated = headings[match.group(1)], int(match.group(2))
            for start, body in _details_blocks(lines[index:]):
                summary, _line = _pop_summary(body, start)
                if summary is None:
                    continue
                counts = _count_entries(body)
                actual = counts['pipe_rows'] - 1 if rule == 'table' else counts['bullets']
                if actual != stated:
                    mismatches.append(
                        'module-map.md:' + str(index + 1) + ' 「' + match.group(1) + '」声明 '
                        + str(stated) + '（' + rule + '），实际 ' + str(actual)
                    )
                break
        self.assertEqual(mismatches, [], 'module-map 的章节标题计数与清单内容不一致')

    def test_table_claims_do_not_hide_bullets(self):
        wrong = []
        for path, lines in _iter_docs():
            rel = path.relative_to(ROOT).as_posix()
            for start, body in _details_blocks(lines):
                summary, _line = _pop_summary(body, start)
                if summary is None:
                    continue
                paren = PAREN_RE.search(summary)
                if paren is None or '归组' not in paren.group(2):
                    continue
                counts = _count_entries(body)
                if counts['bullets'] or counts['pipe_rows'] < 2:
                    wrong.append(
                        rel + ':' + str(_line) + ' 声明是表格清单，块内却有 '
                        + str(counts['bullets']) + ' 条 bullet 和 ' + str(counts['pipe_rows']) + ' 行表格'
                    )
        self.assertEqual(wrong, [], '表格型清单里混进了 bullet')


class DesignDocGroupTotalTests(SimpleTestCase):
    def test_group_subtotals_match_declared_total_and_real_test_count(self):
        for doc_name, test_rel in sorted(GROUP_CLAIM_DOCS.items()):
            doc = DOCS_DIR / doc_name
            lines = doc.read_text(encoding='utf-8-sig').split(chr(10))
            claim_index = None
            claim = None
            for index, line in enumerate(lines):
                claim = GROUP_TOTAL_RE.search(line)
                if claim is not None:
                    claim_index = index
                    break
            self.assertIsNotNone(claim, doc_name + ' 里找不到「共 N 项，分 M 组」的声明')
            stated_total = int(claim.group(1))
            stated_groups = CN_NUMERALS[claim.group(2)]

            subtotals = []
            for line in lines[claim_index + 1:]:
                item = GROUP_ITEM_RE.match(line)
                if item is not None:
                    subtotals.append(int(item.group(1)))
                    continue
                if line.strip():
                    break

            self.assertEqual(
                len(subtotals), stated_groups,
                doc_name + ' 声明分 ' + str(stated_groups) + ' 组，实际列出 ' + str(len(subtotals)) + ' 组',
            )
            self.assertEqual(
                sum(subtotals), stated_total,
                doc_name + ' 各组小计之和 ' + str(sum(subtotals)) + ' 与声明总数 ' + str(stated_total) + ' 不一致',
            )
            real_tests = len(TEST_DEF_RE.findall((ROOT / test_rel).read_text(encoding='utf-8')))
            self.assertEqual(
                stated_total, real_tests,
                doc_name + ' 声明 ' + str(stated_total) + ' 项，' + test_rel + ' 里实际有 '
                + str(real_tests) + ' 个 def test_',
            )

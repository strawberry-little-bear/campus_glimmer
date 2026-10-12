# -*- coding: utf-8 -*-
"""学期阶段统计快照，第一次被运营读到。

`AcademicTermSnapshot` 是站内最安静的一张表：`refresh_academic_term_snapshots` 按学期阶
段一行一行往里写，`sync_academic_calendar` 每周刷一次，admin 里注册了、`refreshed_at` 还是
只读字段。**但它从头到尾没有任何一条读路径。** `academic_calendar.build_academic_calendar`
是学期日历唯一的消费方，而它一次都没读过这张表——它拿 `AcademicPhase` 的日期区间在渲染的
那一刻实时聚合 `Item`、`DemandPost`、`Order`、`SearchQuery`。全仓库搜 `AcademicTermSnapshot`
只有模型定义、admin 注册、`update_or_create` 和测试四处，模板里连 snapshot 这个词都没有。

也就是说这个函数每周都在往一张没人看的表里写数。数据是真的，口径是真的，存储是真的，只
是从来没有人读过它一眼。本模块把这个表的真实状态第一次读出来。

站内已有二十多个「量我们自己的功能」的模块，量的是「平台做了一个判断准不准」或「平台给了
一个工具好不好用」。本模块量的是第三种东西：**一个已经存在很久的持久化设施，它到底还在
不在工作。** `sync_academic_calendar` 自己接了 `record_task_run`，所以「命令有没有跑」台账
有答案；但台账答不了快照的内容对不对、有多少早就过期、阶段被删之后快照会怎样。

**本模块所有判据都建立在一条从 Django 源码查证的事实上：这张表的写入是无条件的。**
`refresh_academic_term_snapshots` 走 `update_or_create`，乍看 `refreshed_at` 能不能反映「数
据变了」取决于值没变时会不会跳过保存。它不会。Django 的 `update_or_create`
（`django/db/models/query.py` 第 1002-1014 行）拿到已存在的对象后，除了 `defaults` 的字段
之外，**显式地把 `pre_save` 不是默认实现的所有字段补进 `update_fields`**，源码注释写得很直
白：「Add fields which are set on pre_save(), e.g. auto_now fields」。`refreshed_at` 正是
`auto_now=True`。所以每次调用，不管十个计数有没有一个变化，`refreshed_at` 都会被推进到当
下。

这条事实的后果：`refreshed_at` 只能回答**「命令上次写到这一行是什么时候」**，回答不了「这
一行的内容上次变化是什么时候」。一条阶段两个月前就结束了、商品再也不会新增，它的十个计数
从此再也不变，但每周一次的刷新照样把 `refreshed_at` 推着走，于是它看起来永远「新鲜」。因
此**本模块不报「内容陈旧度」，只报「刷新陈旧度」**，并把这句话写在面板上。把刷新时间当成
内容时间报出去，会让一条早就不会再变的阶段一直显示为刚刚更新——比没有数字更糟。

**同理不报快照数和实时数的偏差。** 理论上可以把两者都算一遍再相比，但那是拿一个已知没人
读的缓存和实时值比大小，得出的是「这个缓存确实不准」，而它不准的原因恰恰是没人读。报一个
没有消费方受害者的偏差，只是在给一个已经没人用的缓存做体检。要做这个对比，正确做法是先给
它一个读路径（让 `build_academic_calendar` 改读快照），那是一个功能改动，不是一次度量。

**孤儿行的判据是匹配键失效，不是学期停用。** `AcademicPhase.start_date` 不是数据库列，是
模型上的 `@property`（`term.starts_on + start_offset`），而快照的唯一约束是
`UniqueConstraint(fields=['term', 'phase_key', 'start_date'])`。运营把 `starts_on` 往后改一
周，四个阶段的 `start_date` 全部平移，匹配键全部失配，`update_or_create` 找不到旧行、走
create 插入四条新行，旧的四条没有任何东西会去删——`AcademicTermSnapshot` 的外键只到
`AcademicTerm`，删阶段删不掉它们，改开始日期也删不掉它们。台账里这次运行显示「刷新阶段快
照 4 条」和上学期一模一样，而表里从这一刻起是 8 条。

但**已停用学期下面的快照行不是孤儿**：`AcademicTerm.is_active` 的 help_text 写着「停用后
该学期不再参与阶段计算，但历史数据保留」，那是设计如此的残留。把它算进孤儿会把一条被写死
的行为报成异常。所以孤儿只指「学期还启用着，但这一行的 `(phase_key, start_date)` 在当前阶
段集合里找不到对应」。

**过期判据取「阶段结束日期距今年数」，不取 `refreshed_at`。** 一条阶段上周才结束和一条上
学期就结束的，`refreshed_at` 可能都是昨天（刷新无条件推进），只有 `end_date` 能分开。所以
过期看 `end_date`，门槛 `STALE_PHASE_DAYS`；而「刷新中断」才该看 `refreshed_at`——台账里有
很多次成功运行，但某一行远早于最近一次成功运行，说明这一行没被刷到，它对应的阶段在最近一
次刷新时已经不在启用中的学期里了。这两个信号角度不同，都报。

**四类分母各自独立，不能混。** 启用中学期的应有阶段、快照表实际行数、同一学期下的快照行、
台账里该命令的运行。因为有多余行的存在，「实际行数 >= 应有行数」是常态，两者相减得到的是
孤儿和停用残留的合计，不区分两者——要分开必须再按匹配键分组，本模块两个都报。

本模块只报告：不刷新任何快照、不删任何孤儿行、不改任何 `AcademicTerm.starts_on`、不删任何
`AcademicPhase`、不动 `refresh_academic_term_snapshots` 的任何逻辑、不改唯一约束、不建新
表、不加迁移、不碰 `build_academic_calendar` 的实时计算路径、不因为孤儿行多就建议删表。
"""

from datetime import timedelta

from django.utils import timezone

from .models import AcademicPhase, AcademicTerm, AcademicTermSnapshot, TaskRun


# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一条阶段结束超过这么多天，算「早就过期」。取 30 天：一个学期阶段动辄十来周，结束一个月
# 之后这条阶段的十个计数基本不会再变，此后每周的刷新只是把 `refreshed_at` 推着走。注意这
# 个判据看的是 `end_date` 而不是 `refreshed_at`，见模块 docstring 里那条 Django 源码事实。
STALE_PHASE_DAYS = 30

# 一行的 `refreshed_at` 比最近一次成功运行早这么多天以上，算「这一行没被刷到」。取 7 天：
# 刷新是按周的（README 写「按周刷新快照即可」），一周没刷到一行不是巧合。门槛取 7 天而不是
# 1 天，是为了容忍一次漏跑或一次手动只刷部分学期的运行。
REFRESH_GAP_DAYS = 7

# 命令名称，和 `task_run_health.KNOWN_COMMANDS` 里那一条对齐。这里不 import 那个常量，是因
# 为本模块只关心这一条命令，import 会把整个健康度模块的适用范围拖进来。
COMMAND_NAME = 'sync_academic_calendar'

# 覆盖率要落到这么多百分比以下才单独提示。取 90：一行没覆盖到对应的是阶段被删或学期被停
# 用，百分之几的缺口可以是手工维护的正常痕迹，超过一成说明维护动作和快照已经脱节。
COVERAGE_WARN_PERCENT = 90


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _share_or_none(part, whole):
    """占比；分母为零时返回 None，不返回 0。

    空平台报 0% 会被读成「覆盖率是零，一个都没有」，而真相是根本没有分母。和其余模块同一
    条规矩：没有分母时这个数不存在，不是 0。
    """
    return _percent(part, whole) if whole else None


def _expected_phase_keys():
    """启用中的学期当前应有的 (term_id, phase_key, start_date) 集合。

    `start_date` 是 `AcademicPhase` 的 `@property`，不是数据库列，所以这一列只能在 Python
     算出来再比。这也正是孤儿行的来源：改一次 `AcademicTerm.starts_on`，这一列整体平移，
    唯一约束的匹配键全部失配。
    """
    expected = set()
    phases = AcademicPhase.objects.select_related('term').filter(
        term__is_active=True,
    )
    for phase in phases:
        expected.add((phase.term_id, phase.phase, phase.start_date))
    return expected


def _load_snapshot_rows():
    """快照表全量行，只取本模块用得到的列。

    这个表的规模由「学期数 x 阶段数 + 历史残留」决定，通常是个位数到几十行，全量读进内存
    比按学期分组查若干次更清楚。不做时间过滤：孤儿和停用残留都是历史行，恰恰是要读的。
    """
    return list(
        AcademicTermSnapshot.objects.values(
            'id', 'term_id', 'term__name', 'term__is_active', 'phase_key',
            'phase_label', 'start_date', 'end_date', 'day_count',
            'refreshed_at',
        )
    )


def _build_table_scale(rows):
    """这张表本身的规模：行数、涉及学期数、阶段数、十个计数是不是都空着。

    行数是这张表最直观的体量，但单看行数读不出形状，所以同时给出涉及多少个学期、多少个不
    同阶段。十个计数全为空的行一并数出来——那说明写入路径跑过了但一个数都没算出来，比行数
    为零更值得看。
    """
    term_ids = {row['term_id'] for row in rows}
    phase_keys = {row['phase_key'] for row in rows}
    empty_counter_rows = sum(
        1 for row in rows
        if row['day_count'] == 0 and row['start_date'] >= row['end_date']
    )
    return {
        'row_total': len(rows),
        'term_total': len(term_ids),
        'phase_key_total': len(phase_keys),
        'empty_counter_rows': empty_counter_rows,
    }


def _build_coverage(expected, rows):
    """应有阶段里有多少在快照表里找到了对应行。

    分子分母都是应有阶段，不是快照行：问的是「该有快照的阶段是不是都有」，用快照行当分母
    会把孤儿行算成覆盖率的一部分，而孤儿行恰恰是多余的。应有的那一侧由
    `_expected_phase_keys` 从 `is_active=True` 的学期算出来。
    """
    actual = {(row['term_id'], row['phase_key'], row['start_date']) for row in rows}
    covered = expected & actual
    return {
        'expected_phase_total': len(expected),
        'covered_phase_total': len(covered),
        'covered_share': _share_or_none(len(covered), len(expected)),
    }


def _build_orphan_rows(rows, expected):
    """启用中的学期下、匹配键已经失效的快照行。

    判据是「学期还启用着，但 `(phase_key, start_date)` 不在当前阶段集合里」。已停用学期的
    行不算孤儿——`is_active` 的 help_text 明写「历史数据保留」，那是设计如此，见模块
    docstring。所以这里先按 `term__is_active` 切一刀，剩下的才可能是孤儿。
    """
    orphans = [
        row for row in rows
        if row['term__is_active']
        and (row['term_id'], row['phase_key'], row['start_date']) not in expected
    ]
    inactive_rows = [row for row in rows if not row['term__is_active']]
    rows.sort(key=lambda row: (row['term__name'], row['start_date']))
    return {
        'orphan_total': len(orphans),
        'orphan_rows': [
            {
                'term_name': row['term__name'],
                'phase_label': row['phase_label'] or row['phase_key'],
                'start_date': row['start_date'],
                'end_date': row['end_date'],
                'refreshed_at': row['refreshed_at'],
            }
            for row in orphans
        ],
        'inactive_term_row_total': len(inactive_rows),
        'inactive_terms': sorted({row['term__name'] for row in inactive_rows}),
    }


def _build_stale_rows(rows, now):
    """结束日期已经过去很久的阶段，按 `end_date` 判，不看 `refreshed_at`。

    判据为什么不能用 `refreshed_at`：刷新是无条件推进它的，一条上学期就结束的阶段，只要命
    令还在按周跑，它的 `refreshed_at` 就一直是昨天。只有 `end_date` 能把「上周才结束」和「上
    学期就结束」分开。见模块 docstring 里那条 Django 源码事实。
    """
    threshold = timezone.localdate(now) - timedelta(days=STALE_PHASE_DAYS)
    stale = [row for row in rows if row['end_date'] < threshold]
    rows.sort(key=lambda row: (row['end_date'], row['term__name']))
    return {
        'stale_threshold_days': STALE_PHASE_DAYS,
        'stale_total': len(stale),
        'stale_share': _share_or_none(len(stale), len(rows)),
        'stale_rows': [
            {
                'term_name': row['term__name'],
                'phase_label': row['phase_label'] or row['phase_key'],
                'start_date': row['start_date'],
                'end_date': row['end_date'],
                'days_since_end': (timezone.localdate(now) - row['end_date']).days,
            }
            for row in stale
        ],
    }


def _build_command_state(start, now):
    """命令本身还在不在跑：周期内的运行、成功、失败和最近一次成功的时间。

    只读台账里 `sync_academic_calendar` 的行。这里不重复 `task_run_health` 已经做过的事
    （成功率、连续失败、静默天数），本模块只取「最近一次成功是什么时候」这一个数，因为
    「刷新中断」的判据要拿它当基准。失败次数一并给出，但它不是判据——一次失败之后重跑成功
    是常态。
    """
    runs = TaskRun.objects.filter(
        name=COMMAND_NAME, started_at__gte=start, started_at__lte=now,
    )
    succeeded = runs.filter(status='succeeded')
    last_success = succeeded.order_by('-finished_at').first()
    return {
        'command_name': COMMAND_NAME,
        'run_total': runs.count(),
        'failed_total': runs.filter(status='failed').count(),
        'last_success_at': last_success.finished_at if last_success else None,
        'ran_in_period': runs.exists(),
    }


def _build_refresh_gap_rows(rows, command_state, now):
    """台账里有成功运行、但这一行没被刷到的快照行。

    判据是「最近一次成功运行」和「这一行的 `refreshed_at`」相差超过 `REFRESH_GAP_DAYS`。只有
    在周期内确实有成功运行时才判——没有成功运行就没有基准，此时该报的是命令没跑，不是某些
    行没刷到。这和孤儿行是两个角度：孤儿是「表里有当前不存在的阶段」，这里是「当前存在的
    阶段没被刷到」。
    """
    gap_threshold = now - timedelta(days=REFRESH_GAP_DAYS)
    last_success = command_state['last_success_at']
    if last_success is None:
        return {
            'gap_threshold_days': REFRESH_GAP_DAYS,
            'gap_total': 0,
            'gap_rows': [],
            'has_baseline': False,
        }
    # 基准本身要经得起「命令这周没跑」：最近一次成功运行如果自己就比门槛还老，那每一行都会
    # 被判成没刷到，那是命令停了，不是行的问题。所以基准再和门槛取一次较晚者。
    baseline = max(last_success, gap_threshold)
    gap_rows = [
        row for row in rows
        if row['refreshed_at'] is not None and row['refreshed_at'] < baseline
    ]
    return {
        'gap_threshold_days': REFRESH_GAP_DAYS,
        'gap_total': len(gap_rows),
        'gap_rows': [
            {
                'term_name': row['term__name'],
                'phase_label': row['phase_label'] or row['phase_key'],
                'refreshed_at': row['refreshed_at'],
                'days_since_refresh': (now - row['refreshed_at']).days,
            }
            for row in gap_rows
        ],
        'has_baseline': True,
    }


def _recommendations(scale, coverage, orphan_rows, stale_rows, command_state, gap_rows):
    """只报告之后留下的几句话：哪一件事现在就该有人去看。

    每条都对着一个能被证实的信号，不写「建议持续关注」这类没有指向的话。所有条目都以「本模
    块只报告」收尾，因为这个模块不改任何东西。
    """
    notes = []
    # 空表这件事永远排第一：一个从来没人读的表，如果连行都没有，那「表是空的」比「命令这周
    # 没跑」更要紧——后者只是原因之一，前者是运营第一眼该看到的事实。放在这里而不是放在
    # 末尾的兜底分支，是为了不让它被任何一条别的提示挤掉。
    if not scale['row_total']:
        notes.append(
            '快照表还是空的。第一次运行 `sync_academic_calendar` 之后，这里会开始统计；'
            '也要留意它至今没有任何读路径，`build_academic_calendar` 一直是实时计算的。'
        )
    if orphan_rows['orphan_total']:
        notes.append(
            f'有 {orphan_rows["orphan_total"]} 行快照的匹配键已经失效——学期还启用着，'
            f'但它的阶段标识和开始日期在当前阶段里找不到对应。最常见的原因是改过'
            f'`AcademicTerm.starts_on`：`AcademicPhase.start_date` 是算出来的属性，'
            f'一改整体平移，唯一约束全部失配，旧行不会被更新、只会被新键旁置。'
            f'台账里那次运行显示「刷新阶段快照」的条数不会有任何变化，所以这件事只能'
            f'从表里读出来。本模块只报告，不删这些行、不改任何 starts_on。'
        )
    if coverage['covered_share'] is not None and coverage['covered_share'] < COVERAGE_WARN_PERCENT:
        notes.append(
            f'启用中的学期里只有 {coverage["covered_share"]}% 的阶段有对应快照，'
            f'共 {coverage["expected_phase_total"]} 个阶段、覆盖到 '
            f'{coverage["covered_phase_total"]} 个。缺的那部分是阶段被删过或学期被停用过，'
            f'也可能是刷新没跑到。本模块只报告，不补刷任何快照。'
        )
    if stale_rows['stale_total']:
        notes.append(
            f'有 {stale_rows["stale_total"]} 行快照对应的阶段早在 '
            f'{stale_rows["stale_threshold_days"]} 天之前就结束了，占全部快照的 '
            f'{stale_rows["stale_share"]}%。这些行的数字停在阶段结束那一刻，之后不会再变；'
            f'注意它们的 `refreshed_at` 仍然可能很新——刷新是无条件推进这一列的，见面板下方'
            f'的说明。本模块只报告，不刷新也不删。'
        )
    if gap_rows['gap_total']:
        notes.append(
            f'台账里有成功的运行，但 {gap_rows["gap_total"]} 行快照的 `refreshed_at` 比'
            f'最近一次成功运行早了 {gap_rows["gap_threshold_days"]} 天以上——这些行没被'
            f'刷到，它们对应的阶段在最近一次刷新时已经不在启用中的学期里。本模块只报告，'
            f'不补刷。'
        )
    if not command_state['ran_in_period']:
        notes.append(
            f'最近这些天里 `{COMMAND_NAME}` 一次都没跑过。这张表本来就没人读，命令再停掉'
            f'的话，它连「每周被写一次」这个最后的动作都不会再有。本模块只报告，不替你跑'
            f'这个命令。'
        )
    if command_state['failed_total']:
        notes.append(
            f'周期内 `{COMMAND_NAME}` 失败了 {command_state["failed_total"]} 次。失败原因'
            f'属于运行台账和失败分类模块的事，本模块不重复发布。'
        )
    if not notes:
        notes.append(
            '这张表的规模和覆盖没有明显异常。但要注意：它仍然没有任何读路径，'
            '`build_academic_calendar` 至今是实时计算的。本模块只报告。'
        )
    return notes


def build_academic_term_snapshot_quality(days=30, now=None):
    """量学期阶段统计快照：这张没人读的表，现在是什么状态。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    start = timezone.make_aware(
        timezone.datetime.combine(today - timedelta(days=days - 1), timezone.datetime.min.time()),
    )

    rows = _load_snapshot_rows()
    expected = _expected_phase_keys()
    scale = _build_table_scale(rows)
    coverage = _build_coverage(expected, rows)
    orphan_rows = _build_orphan_rows(rows, expected)
    stale_rows = _build_stale_rows(rows, now)
    command_state = _build_command_state(start, now)
    gap_rows = _build_refresh_gap_rows(rows, command_state, now)
    recommendations = _recommendations(
        scale, coverage, orphan_rows, stale_rows, command_state, gap_rows,
    )

    extra_total = scale['row_total'] - coverage['expected_phase_total']
    if coverage['expected_phase_total'] and orphan_rows['orphan_total']:
        headline = (
            f'快照表现有 {scale["row_total"]} 行，比启用中学期的应有阶段多 '
            f'{extra_total} 行，其中 {orphan_rows["orphan_total"]} 行的匹配键已经失效；'
            f'应有阶段覆盖 {coverage["covered_share"]}%。'
        )
    elif scale['row_total']:
        headline = (
            f'快照表现有 {scale["row_total"]} 行，覆盖启用中学期的 '
            f'{coverage["covered_share"]}%，没有发现匹配键失效的行。'
        )
    else:
        headline = '快照表还是空的，`sync_academic_calendar` 可能还没有成功跑过一次。'

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(rows),
        'summary': headline,
        'metrics': {
            'row_total': scale['row_total'],
            'term_total': scale['term_total'],
            'phase_key_total': scale['phase_key_total'],
            'empty_counter_rows': scale['empty_counter_rows'],
            'expected_phase_total': coverage['expected_phase_total'],
            'covered_phase_total': coverage['covered_phase_total'],
            'covered_share': coverage['covered_share'],
            'orphan_total': orphan_rows['orphan_total'],
            'inactive_term_row_total': orphan_rows['inactive_term_row_total'],
            'stale_total': stale_rows['stale_total'],
            'stale_threshold_days': stale_rows['stale_threshold_days'],
            'gap_total': gap_rows['gap_total'],
            'gap_threshold_days': gap_rows['gap_threshold_days'],
            'command_run_total': command_state['run_total'],
            'command_failed_total': command_state['failed_total'],
            'command_last_success_at': command_state['last_success_at'],
            'command_ran_in_period': command_state['ran_in_period'],
            'coverage_warn_percent': COVERAGE_WARN_PERCENT,
        },
        'scale': scale,
        'coverage': coverage,
        'orphan_rows': orphan_rows,
        'stale_rows': stale_rows,
        'command_state': command_state,
        'gap_rows': gap_rows,
        'recommendations': recommendations,
    }


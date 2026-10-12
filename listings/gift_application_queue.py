# -*- coding: utf-8 -*-
"""免费领取的申请队列，第一次被运营读到。

`listings.GiftApplication` 是站内唯一一条「多人竞争、平台只记录不排序」的队列：
一件免费商品可以被任意多的人同时申请，卖家在后台看到一列 `pending`，选定一个，
其余的人由 `accept_gift_application` 在同一笔事务里置成 `rejected`。

它有三个字段一直在被写，读路径却只有后台那一列：

- `applicant_note`（申请说明）——唯一的写路径是 `views.py` 的
  `applicant_note=form.cleaned_data.get('buyer_note', '')`，唯一的读路径是
  `admin.py` 的 `search_fields`；
- `decided_at`（处理时间）——三处写路径：卖家选定申请人、其余的人置 `rejected`、
  申请人自己撤回。读路径只有 `admin.py` 的 list_display / list_filter /
  readonly_fields；
- `meeting_location`（领取地点）——创建时写入，同样只有 admin 在读。

于是数据库里一直躺着一个没有任何代码算过的差：`decided_at - created_at`，一件
免费商品的申请从提交到被处理隔了多久。而运营侧的处境比这个差更难看——**现在分不出
一件免费商品是「没人要」还是「有 8 个人要、卖家还没决定」**。前者该去改免费商品
的曝光位置，后者该去催卖家决定，两件事的处置方式完全相反，而后台把两者显示成同一个
样子：一列 `pending`。

这个模块把这些字段读出来，只报告，不改写任何一条申请。几处口径上的取舍比数字本身
更要紧，它们都在下面的注释里。

分母只收已经处理完的申请。`selected` / `rejected` / `withdrawn` 三种状态都带
`decided_at`，有起止两点，算得出隔了多久；`pending` 没有处理时刻，是右删失样本，
单独数出来，不丢也不混。

撤回单独一档，不和「未选中」混在一起。`withdrawn` 是申请人自己点的撤回，可能是
改主意了，也可能是等太久等不住了——平台没有能力区分这两者。而 `rejected` 是卖家做
的决定。把两者合成一个「没成」的档，会让撤回率变成一个说不清是什么的数。

`applicant_note` 只报填写率和填/没填两组的选中率对照，一个字符的正文都不读。它是
自由文本，可能写着电话、微信、楼栋号。而对照本身只做对照、不给因果：愿意写一段说明
的申请人可能本来就是更认真、更想要的那批人，平台没有能力把「说明起了作用」和
「这个人本来就更容易被选中」分开。

队列长度按商品聚合，选中率按申请聚合。一件商品收到多少份申请是商品维度的事实，
一份申请有没有被选中是申请维度的事实。一份申请的「选中率」必然是 100%，把它和
八份申请的选中率混在一起平均，会得到一件压根不存在的商品，所以只收到一份申请的
商品单独一档。

本模块不给卖家排序、不替卖家推荐申请人、不改任何申请状态、不催卖家、不给申请人
发提醒。队列现在是按先来后到排的，任何「更该被选中」的排序都是平台在替一个具体的
人做决定，而这个决定会直接决定另一个人能不能拿到东西。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import GiftApplication

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一组样本至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条
# 样本上几个百分点的差距是噪声。
MIN_MEASURED_APPLICATIONS = 12

# 两组对照（填了说明 / 没填说明）各自的下限。选中率是稀有事件——一件商品只选中
# 一个人，其余全是 rejected——所以两组各要这么多条，差距才落得到噪声带外面。
MIN_GROUP_APPLICATIONS = 12

# 两组选中率的差值不满这么多百分点，视为噪声，不给方向。阈值和其余模块同一条线。
NOISE_POINTS = 8

# 商品清单最多列这么多条。清单是给「现在去看」用的，不是给归档用的。
ITEM_LIST_LIMIT = 20

# 分类清单最多列这么多条。分类是给「哪一类免费商品问的人多」用的，长尾分类一两
# 条样本说明不了任何事，列出来只会让人以为它有问题。
CATEGORY_LIST_LIMIT = 10

# 队列长度分档。互斥，按一件商品收到的申请份数切。第二档单独列出来：一份申请的
# 「选中率」必然是 100%，把它和八份申请的选中率混在一起平均，会得到一件压根不
# 存在的商品。
QUEUE_BUCKETS = (
    (0, '没有申请', '没人要，该去看曝光位置'),
    (1, '一份申请', '独一份，谈不上竞争，选中与否都不说明吸引力'),
    (2, '两到四份', '有竞争，但卖家很快就能看完'),
    (5, '五份及以上', '竞争激烈，卖家要一个个挑，最容易拖'),
)

# 处理时长分档。互斥，左闭右开，按小时和天切。最后一档是尾巴：隔了七天以上才
# 处理的，多半是卖家压根没打开过后台，那正是要动作的一批。
DECISION_BUCKETS = (
    (86400, '当天内', '当天就处理了，卖家在看这个队列'),
    (259200, '一到三天', '隔了一两天，是正常的回访节奏'),
    (604800, '三到七天', '隔了快一周，可能是在忙，也可能是忘了这回事'),
    (None, '超过七天', '隔了一周以上，多半是压根没打开过后台'),
)

# 队列长度维度上「该去看」的判定门槛：五份及以上的商品占比高过这个百分点，才
# 值得运营去数一数有多少免费商品在排队等决定。
CROWDED_ITEM_POINTS = 20

# 一条申请的完整轨迹，一次查完固定下来。等待分布、填写率对照、队列长度分布各读
# 一遍，按记录现算会让每条记录打好几遍库，而看板是一次打开全算的。
Application = namedtuple('Application', [
    'row', 'item', 'applicant', 'note_length', 'decided_seconds',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime``
    类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_elapsed(seconds):
    """处理时长按小时和天说话，不足一天的说「当天内」。

    这个模块报的是小时到天尺度的量。报「平均等了 4 小时 23 分」会让运营以为这是
    小时级的流程，而真正要动作的对象是「卖家隔了多久才打开后台」。
    """
    if seconds is None:
        return '—'
    if seconds < 86400:
        return '当天内'
    return f'{int(seconds // 86400)} 天'


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _bucket_index(value, buckets):
    """一份互斥分档的下标。上限是 None 的是最后一档，吃掉所有剩下的。"""
    for index, (limit, _label, _hint) in enumerate(buckets):
        if limit is None or value < limit:
            return index
    return len(buckets) - 1


def _bucket_rows(values, buckets):
    """一份互斥分档，每一档带上它自己的口径说明。"""
    rows = [
        {'label': label, 'hint': hint, 'count': 0}
        for _limit, label, hint in buckets
    ]
    for value in values:
        rows[_bucket_index(value, buckets)]['count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(values))
    return rows


def _queue_bucket_index(queue_length):
    """队列长度分档。第一档的上限是 0，收的是「一份申请都没有」的商品。"""
    if queue_length <= 0:
        return 0
    if queue_length == 1:
        return 1
    if queue_length <= 4:
        return 2
    return 3


def _fetch_applications(*, decided_after=None, decided_before=None):
    """按显式排序取出窗口内已经处理完的申请，带上商品、申请人和分类。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。

    窗口按 ``decided_at`` 取，不按 ``created_at``。要回答的是「这段时间里被处理掉的
    申请，从提交到处理隔了多久」，所以终点是处理；一件上周提交、这周才被选中的商品，
    等的是上周提交的那一批人。

    只收三种已经处理完的状态。`pending` 没有处理时刻，谈不上隔了多久；它是右删失
    样本，由调用方单独数出来。
    """
    queryset = GiftApplication.objects.filter(
        status__in=('selected', 'rejected', 'withdrawn'),
        decided_at__isnull=False,
    ).select_related('item', 'item__category', 'applicant')
    if decided_after is not None:
        queryset = queryset.filter(decided_at__gte=decided_after)
    if decided_before is not None:
        queryset = queryset.filter(decided_at__lte=decided_before)

    applications = []
    for row in queryset.order_by('decided_at', 'pk'):
        decided_seconds = max(
            (row.decided_at - row.created_at).total_seconds(), 0.0,
        )
        applications.append(Application(
            row=row,
            item=row.item,
            applicant=row.applicant,
            note_length=len(row.applicant_note or ''),
            decided_seconds=decided_seconds,
        ))
    return applications


def _note_rows(applications):
    """申请说明的填写率，以及填 / 没填两组的选中率对照。

    正文一个字符都不读。`applicant_note` 是自由文本，可能写着电话、微信、楼栋号，
    本模块只报长度是不是零——那是这个字段唯一能安全回答的问题。

    两组各自不足 ``MIN_GROUP_APPLICATIONS`` 条时，对照整组不给方向，只报各自的
    样本量。选中率是稀有事件：一件商品只选中一个人，其余全是 rejected，所以两组的
    下限比其余维度高，差值也要够大才落得到噪声带外面。

    差值只做对照、不给因果。愿意写一段说明的申请人可能本来就是更认真、更想要的那
    批人，平台没有能力把「说明起了作用」和「这个人本来就更容易被选中」分开。
    """
    filled = [a for a in applications if a.note_length > 0]
    empty = [a for a in applications if a.note_length == 0]

    def selected_count(group):
        return sum(1 for a in group if a.row.status == 'selected')

    rows = []
    for label, group in (('填了申请说明', filled), ('没有填申请说明', empty)):
        selected = selected_count(group)
        rows.append({
            'label': label,
            'count': len(group),
            'share': _percent(len(group), len(applications)),
            'selected_count': selected,
            'selected_rate': _percent(selected, len(group)),
            'is_small_sample': len(group) < MIN_GROUP_APPLICATIONS,
        })

    small = any(row['is_small_sample'] for row in rows)
    difference = None
    direction = ''
    if not small:
        difference = round(rows[0]['selected_rate'] - rows[1]['selected_rate'], 1)
        if abs(difference) >= NOISE_POINTS:
            direction = '填了说明的更容易被选中' if difference > 0 else (
                '没填说明的更容易被选中'
            )
        else:
            direction = '两组差距落在噪声带内'
    return {
        'rows': rows,
        'filled_count': len(filled),
        'empty_count': len(empty),
        'fill_rate': _percent(len(filled), len(applications)),
        'difference': difference,
        'direction': direction,
        'is_comparable': not small,
        'noise_points': NOISE_POINTS,
    }


def _item_rows(applications):
    """按商品聚合的队列长度分布，队长的排在前面。

    一条申请就是一个（商品，申请人）对，所以队列长度是商品维度的事实，选中率是
    申请维度的事实，两者不能混在一个数里。一份申请的「选中率」必然是 100%，把它和
    八份申请的选中率混在一起平均，会得到一件压根不存在的商品。

    排序先按队列长度，再按申请量，都相同时按商品标题稳定。样本量进排序键是必须的：
    不按它压下去，榜单前几行会被同一批只有一份申请的商品占满，而那说明不了任何事。
    """
    grouped = {}
    for application in applications:
        grouped.setdefault(application.item.pk, {
            'item': application.item,
            'total': 0,
            'selected': 0,
            'withdrawn': 0,
        })
        entry = grouped[application.item.pk]
        entry['total'] += 1
        if application.row.status == 'selected':
            entry['selected'] += 1
        elif application.row.status == 'withdrawn':
            entry['withdrawn'] += 1

    rows = []
    for entry in grouped.values():
        item = entry['item']
        rows.append({
            'item_id': item.pk,
            'title': item.title,
            'category_name': item.category.name if item.category_id else '未分类',
            'queue_length': entry['total'],
            'selected_count': entry['selected'],
            'withdrawn_count': entry['withdrawn'],
            'queue_label': QUEUE_BUCKETS[_queue_bucket_index(entry['total'])][1],
            'is_crowded': entry['total'] >= 5,
        })
    rows.sort(key=lambda row: (-row['queue_length'], -row['selected_count'], row['title']))
    return rows


def _queue_bucket_rows(applications, item_rows):
    """队列长度分档。分母是商品数，不是申请数。

    分母必须是商品：要回答的是「有多少件免费商品有五份以上的申请」，不是「有多少份
    申请落在五份以上的队列里」。按申请数走，一件八人竞争的商品会在分布里出现八次，
    「竞争激烈」那一档被同一件商品撑起来，而它其实只是一件商品。
    """
    rows = [
        {'label': label, 'hint': hint, 'count': 0}
        for _limit, label, hint in QUEUE_BUCKETS
    ]
    for row in item_rows:
        rows[_queue_bucket_index(row['queue_length'])]['count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(item_rows))
    return rows


def _category_rows(applications, item_rows):
    """按商品分类分组的申请量与选中率，申请量大的排在前面。"""
    category_names = {
        row['item_id']: row['category_name'] for row in item_rows
    }
    grouped = {}
    for application in applications:
        name = category_names.get(application.item.pk, '未分类')
        entry = grouped.setdefault(name, {
            'category_name': name, 'applications': 0, 'selected': 0,
        })
        entry['applications'] += 1
        if application.row.status == 'selected':
            entry['selected'] += 1

    rows = []
    for entry in grouped.values():
        rows.append({
            'category_name': entry['category_name'],
            'applications': entry['applications'],
            'selected_count': entry['selected'],
            'selected_rate': _percent(entry['selected'], entry['applications']),
        })
    rows.sort(key=lambda row: (-row['applications'], row['category_name']))
    return rows


def _withdrawn_rows(applications, limit=ITEM_LIST_LIMIT):
    """撤回的申请，按提交时间倒序。

    单独一档，不和「未选中」混在一起：撤回是申请人自己点的，可能是改主意了，也可能
    是等太久等不住了——平台没有能力区分这两者。所以这里只报规模，不给方向。
    """
    withdrawn = [
        application for application in applications
        if application.row.status == 'withdrawn'
    ]
    withdrawn.sort(key=lambda a: (-a.decided_seconds, a.row.pk))
    return [
        {
            'item_title': a.item.title,
            'applicant_username': a.applicant.username,
            'note_length': a.note_length,
            'decided_label': _format_elapsed(a.decided_seconds),
            'decided_seconds': a.decided_seconds,
            'created_at': a.row.created_at,
            'decided_at': a.row.decided_at,
        }
        for a in withdrawn[:limit]
    ]


def _summary(stats, note, item_rows, queue_rows, category_rows, withdrawn_rows, pending_count):
    parts = [
        f'周期内共有 {stats["measured_count"]} 份免费领取申请被处理完'
    ]
    parts.append(f'，中位 {stats["median_label"]}、P90 {stats["p90_label"]}')
    if queue_rows:
        empty = queue_rows[0]
        parts.append(
            f'；{empty["count"]} 件免费商品一份申请都没收到'
        )
    if note['filled_count'] or note['empty_count']:
        parts.append(
            f'；{note["fill_rate"]}% 的申请填了申请说明'
            f'（{note["filled_count"]} / {note["filled_count"] + note["empty_count"]}）'
        )
    if note['is_comparable']:
        parts.append(f'，{note["direction"]}')
    crowded = sum(1 for row in item_rows if row['is_crowded'])
    if crowded:
        parts.append(f'；{crowded} 件商品有五份以上的申请在排队')
    if withdrawn_rows:
        parts.append(
            f'。{stats["withdrawn_count"]} 份申请被申请人自己撤回，已逐条列在下面'
        )
    if pending_count:
        parts.append(
            f'；另有 {pending_count} 份申请还挂在「等待选择」，未计入任何等待分布'
        )
    if stats['is_small_sample']:
        parts.append(
            f'。样本不足 {MIN_MEASURED_APPLICATIONS} 条，占比只作参考'
        )
    return ''.join(parts)


def build_gift_application_queue(days=30, now=None):
    """统计免费领取申请队列的长度、处理时长和申请说明填写情况，只报告不改写。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    applications = _fetch_applications(decided_after=start, decided_before=now)
    decided_list = [a.decided_seconds for a in applications]
    decision_rows = _bucket_rows(decided_list, DECISION_BUCKETS)
    note = _note_rows(applications)
    item_rows = _item_rows(applications)
    queue_rows = _queue_bucket_rows(applications, item_rows)
    category_rows = _category_rows(applications, item_rows)
    withdrawn_rows = _withdrawn_rows(applications)

    selected_count = sum(
        1 for a in applications if a.row.status == 'selected'
    )
    withdrawn_count = sum(
        1 for a in applications if a.row.status == 'withdrawn'
    )
    stats = {
        'measured_count': len(applications),
        'item_count': len(item_rows),
        'selected_count': selected_count,
        'withdrawn_count': withdrawn_count,
        'selected_rate': _percent(selected_count, len(applications)),
        'withdrawn_rate': _percent(withdrawn_count, len(applications)),
        'mean_label': _format_elapsed(_mean(decided_list)),
        'median_label': _format_elapsed(_percentile(decided_list, 0.5)),
        'p90_label': _format_elapsed(_percentile(decided_list, 0.9)),
        'rows': decision_rows,
        'is_small_sample': len(applications) < MIN_MEASURED_APPLICATIONS,
    }

    # 还挂在 pending 上的申请单独数，不计入任何等待分布：它们没有处理时刻，是右
    # 删失样本。按 created_at 取窗口，因为要数的是「这段时间里提交的申请，有多少还没
    # 有回音」。
    pending_count = GiftApplication.objects.filter(
        status='pending', created_at__gte=start,
    ).count()

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(applications),
        'summary': _summary(
            stats, note, item_rows, queue_rows, category_rows,
            withdrawn_rows, pending_count,
        ),
        'stats': stats,
        'note': note,
        'queue_rows': queue_rows,
        'item_rows': item_rows[:ITEM_LIST_LIMIT],
        'item_row_total': len(item_rows),
        'item_row_limit': ITEM_LIST_LIMIT,
        'category_rows': category_rows[:CATEGORY_LIST_LIMIT],
        'category_row_total': len(category_rows),
        'category_row_limit': CATEGORY_LIST_LIMIT,
        'withdrawn_rows': withdrawn_rows,
        'withdrawn_row_total': withdrawn_count,
        'withdrawn_row_limit': ITEM_LIST_LIMIT,
        'pending_count': pending_count,
        'crowded_item_points': CROWDED_ITEM_POINTS,
        'min_measured_applications': MIN_MEASURED_APPLICATIONS,
        'min_group_applications': MIN_GROUP_APPLICATIONS,
    }

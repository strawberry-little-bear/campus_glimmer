# -*- coding: utf-8 -*-
"""证据文件的形状：争议双方实际传了什么、有多大。

``OrderDisputeEvidence.attachment`` 上挂着 ``validate_dispute_evidence``——5 MB
上限、四种扩展名，这是站内唯一一处把「治理侧要能审阅」写成代码的产品规则。而
争议域唯一的读者 ``dispute_resolution_quality`` 只数附件条数，
``attachment.name`` 与 ``attachment.size`` 一次都没有被任何一个数字读到。一套
上线运行的格式规则，运营看不到它的形状：不知道双方实际传的是什么格式、有多
大、有没有人在反复撞 5 MB 上限。

本模块补上这一刀。它读同一批附件（按上传时刻切窗口），报四件事：

1. 扩展名分布：四种允许格式各自多少份、占比，含「其它」。
2. 大小分档：按 100 KB / 1 MB / 3 MB / 贴上限分档，报每档多少份、占比。
3. 贴上限的比例：单独数接近 5 MB 的（≥ 4.5 MB）与已经超过上限的。
4. 每笔争议的附件数与总大小：一笔争议带几份、总大小多少。

## 边界

**扩展名的「其它」档理论上恒为 0。** 入口上挂着校验器，不合规的扩展名根本存
不进来。但它仍然单列、不合并、不省略：直接入库、历史数据、命令行造数、或者
校验器将来被绕过，都会让这一档变成非 0。一个恒为 0 的格子是有信息的——它说明
校验在生效。把它省掉，这个信息就没了。

**大小档位取规则里的原值，不取分位数。** 5 MB 是写在校验器里的数，4.5 MB 是它
的九成。运营看表时要能认出自己在表单上见过的那几个数；一个每月都会移动的分位
边界什么也说明不了，而这个数要能回答「上限是不是该调」——只有固定档能回答这个
问题。档位互斥、左闭右开、全列，包括 0 样本档：「3 MB 以上没有人」本身是结论，
不是表格的缺口。

**「贴上限」单独数一档。** 这是本模块唯一一个运营能直接动作的数：一批文件反复
撞上限，说明上限在挡人，该调；一个都没有，说明上限宽松，甚至可以收紧。它和
「4.5 MB 到 5 MB 之间有多少份」的区别在于，前者是一个阈值观察，后者只是一格
分布。运营问的是前者。**已经超过上限的（≥ 5 MB）在这一档里再单独数一次**：
校验器拦着，它理论上也是 0，非 0 就说明有路径绕过了校验——那是一个 bug，不是
一条产品建议。

**窗口按证据上传时刻切，不按争议发起时刻切。** ``dispute_resolution_quality``
按争议发起时刻切，问的是「本期的争议处理得怎么样」；本模块按附件上传时刻切，
问的是「本期交上来的材料长什么样」。两个窗口的分母不是同一批记录，这一点必须
写明，否则读者会拿两边的「附件数」对账，然后以为有一个是错的。

**「一份附件都没有的争议」不在本模块的分母里。** 本模块按证据切窗口，看不见
没有证据的争议。那个数在 ``dispute_resolution_quality`` 里
（``without_evidence_count``），但它按争议发起切窗口，也不是同一批记录。两边的
分工是：它报有没有附件，本模块报附件长什么样。

**不判断「证据够不够」。** 平台没有能力判断一段聊天记录能不能证明「商品与描述
不符」，这个判断只有处理人能做。本模块报「一笔争议平均带几份、总大小中位多
少」，不报「几份才算充分」。

**不读文件内容。** 不 OCR、不解码、不看图片尺寸、不判断这份截图是不是真的和这
笔订单有关。本模块只读 ``attachment.name``（路径字符串）和
``attachment.size``（存储返回的字节数），这两个都是存储层的元数据，不是内容。

**不报「上传失败过几次」。** ``validate_dispute_evidence`` 在表单层抛
``ValidationError``，失败不留任何记录。所以「有多少次上传因为超过 5 MB 被拒」
这个数，库里没有，本模块不知道，不编。这是全站一贯的底线：数据不可知不等于
样本不足，必须明确说「这个数不知道」，不能拿一个近似值顶上去。

**还在流程中的争议是右删失样本。** ``add_dispute_evidence`` 要求争议在
``open`` / ``reviewing``，所以已完结争议的附件数在结案那一刻就定了；而还挂在
流程里的争议以后还会补附件，它现在的份数只是下界。所以第四节的交叉表里，已
完结组报平均份数与总大小中位，流程中组只报规模、不给方向——把两组的均值放在
一起比，会把「还在谈」读成「材料少」。

**0 字节的文件和读不到大小的文件不是一回事。** 前者是「传了一个空文件」，进大
小分档的「0 KB」一格；后者是「存储层出了问题」，单独数出规模，不摊派到任何档
位，也不进任何分母。一个 0 字节的截图可能是用户选错了文件，一个读不到大小的
附件可能是文件被外部挪走了——前者是用户行为，后者是基础设施问题。

**不因为「PDF 没人用」就建议下掉 PDF。** 格式规则是治理侧的可审阅性要求，不是
使用频率的函数。PDF 占比低可能只是用户手上没有扫描件，不代表它不该被允许。本
模块只把现状读出来。

本模块是只读聚合。不加迁移、不加字段、不改模型、不改
``validate_dispute_evidence`` 的 5 MB 上限、不改它的四种扩展名集合、不按用户排
名、不输出用户名、不读 ``note``、不列证据文件名（那是用户上传时的原始名，可能
带着真名、身份证号、订单号）。
"""

from collections import namedtuple
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from .borrow_risk import _rate
from .dispute_resolution_quality import (
    CLOSED_STATUSES, OPEN_STATUSES, PERIOD_CHOICES, _period_start,
)
from .models import OrderDisputeEvidence


# 校验器里的两个原值。本模块只读它们，不改它们。
MAX_EVIDENCE_SIZE = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.pdf')

# 「贴上限」取上限的九成。4.5 MB 是一个阈值观察，不是一格分布——运营问的是
# 「有没有人在撞上限」，不是「4.5 MB 到 5 MB 之间有几份」。
NEAR_LIMIT_SIZE = int(MAX_EVIDENCE_SIZE * 0.9)

# 大小分档。档位取规则里的原值（5 MB、它的九成，以及 100 KB / 1 MB / 3 MB 这几个
# 运营能认出来的整数），不取分位数：一个每月都会移动的分位边界什么也说明不了，
# 而这个数要能回答「上限是不是该调」——只有固定档能回答这个问题。互斥、左闭右
# 开、全列，包括 0 样本档。
SIZE_BANDS = (
    (0, 1, '0 KB'),
    (1, 102400, '100 KB 以内'),
    (102400, 1048576, '100 KB - 1 MB'),
    (1048576, 3145728, '1 MB - 3 MB'),
    (3145728, NEAR_LIMIT_SIZE, '3 MB - 4.5 MB'),
    (NEAR_LIMIT_SIZE, None, '贴上限（≥ 4.5 MB）'),
)

# 扩展名档。四种允许格式各一档，另设「其它」兜住校验范围之外的扩展名——包括
# 没有扩展名的。它理论上恒为 0，但一个恒为 0 的格子是有信息的：它说明校验在
# 生效。
EXTENSION_BANDS = (
    ('.jpg', 'JPG'),
    ('.jpeg', 'JPEG'),
    ('.png', 'PNG'),
    ('.webp', 'WebP'),
    ('.pdf', 'PDF'),
)
OTHER_EXTENSION_LABEL = '其它'

# 一笔争议带几份。分母是窗口内至少有一份附件的争议，所以从 1 份起。
EVIDENCE_COUNT_BANDS = (
    (1, 2, '1 份'),
    (2, 3, '2 份'),
    (3, 5, '3-4 份'),
    (5, None, '5 份及以上'),
)

# 默认窗口与 dispute_resolution_quality 一致：两个模块的看板入口在同一个下拉
# 里，读同一段时间才有可比性。
DEFAULT_LOOKBACK_DAYS = 30

# 要给某一格式或某一大小档一个占比方向，至少这么多样。附件比争议还少，所以比
# dispute_resolution_quality 的 8 条松一档；但不能再低——低于这个数，一个百分点
# 的波动就是一份附件。
MIN_EVIDENCE_SAMPLE = 5

# 百分点差值不满此值视为噪声，不给方向。全站同一套。
NOISE_POINTS = 8


# 一份附件，连它所属争议的状态一起取回来。一次查完固定下来：扩展名分布、大小
# 分档、每笔争议的份数各读一遍，按记录现算会让每份附件多打好几次存储。
EvidenceRecord = namedtuple('EvidenceRecord', [
    'extension', 'size', 'dispute_id', 'dispute_status',
])


def _band_label(value, bands):
    """分档标签。桶是 (下界, 上界, 标签) 的元组，互斥、左闭右开。

    上界为 None 的那一档是最后一档，兜住所有超过前一档的样本。
    """
    for lower, upper, label in bands:
        if value >= lower and (upper is None or value < upper):
            return label
    return bands[-1][2]


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _format_size(size):
    """把一个字节数渲染成最粗但仍可读的单位。"""
    if size is None:
        return '—'
    size = int(size)
    if size < 1024:
        return f'{size} B'
    if size < 1024 * 1024:
        return f'{size / 1024:.0f} KB'
    return f'{size / 1024 / 1024:.1f} MB'


def _extension_label(name):
    """Which extension band a stored path falls into.

    ``Path(...).suffix`` 对没有扩展名的路径返回空串，空串落进「其它」——校验
    器不允许没有扩展名的文件，所以它和不合规的扩展名是同一类事。
    """
    suffix = Path(name or '').suffix.lower()
    for key, label in EXTENSION_BANDS:
        if suffix == key:
            return label
    return OTHER_EXTENSION_LABEL


def _fetch_evidence(*, start, end):
    """窗口内的附件，连所属争议的状态一起取回来。

    大小从字段自己的存储读，不从 ``default_storage`` 读：换后端时这个字段可能
    挂在另一个存储上，而本模块要读的正是这一个字段的文件。

    读不到大小不静默吞成 0。存储后端的「这个文件不在」在本地文件系统上是
    ``FileNotFoundError``（``OSError`` 的子类）；换后端时如果它抛的不是
    ``OSError``，这里会变成一条异常而不是一行统计——那正是想要的：一个读不到
    大小的附件必须被看见，不能被摊派进任何一档。
    """
    storage = OrderDisputeEvidence._meta.get_field('attachment').storage
    rows = list(
        OrderDisputeEvidence.objects
        .filter(created_at__gte=start, created_at__lte=end)
        .order_by('created_at', 'id')
        .values('id', 'attachment', 'created_at', 'dispute_id', 'dispute__status')
    )
    records = []
    for row in rows:
        name = row['attachment'] or ''
        try:
            size = storage.size(name)
        except OSError:
            size = None
        records.append(EvidenceRecord(
            extension=_extension_label(name),
            size=size,
            dispute_id=row['dispute_id'],
            dispute_status=row['dispute__status'],
        ))
    return records


def _extension_rows(records):
    """四种允许格式各自多少份，外加「其它」。

    分母是窗口内全部附件，包括读不到大小的那些：扩展名只取决于
    ``attachment.name`` 这个字符串，和文件在不在存储里无关。这是本模块唯一
    一张分母含「读不到大小」的表，面板上必须写明，否则读者会拿它和第二节的
    分母对账。
    """
    total = len(records)
    counts = {label: 0 for _key, label in EXTENSION_BANDS}
    counts[OTHER_EXTENSION_LABEL] = 0
    for record in records:
        counts[record.extension] += 1

    rows = []
    for key, label in EXTENSION_BANDS:
        count = counts[label]
        rows.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _rate(count, total),
            'is_allowed': True,
            'is_small_sample': count < MIN_EVIDENCE_SAMPLE,
            'note': '校验器允许的格式' if count else '没有人用过这种格式',
        })
    other = counts[OTHER_EXTENSION_LABEL]
    rows.append({
        'key': OTHER_EXTENSION_LABEL,
        'label': OTHER_EXTENSION_LABEL,
        'count': other,
        'share': _rate(other, total),
        'is_allowed': False,
        'is_small_sample': other < MIN_EVIDENCE_SAMPLE,
        'note': (
            '校验器允许的范围之外，非 0 说明有路径绕过了 validate_dispute_evidence'
            if other else '恒为 0，校验在生效'
        ),
    })
    return rows


def _size_rows(records):
    """按固定档报文件多大。

    分母是能读到大小的附件。读不到大小的单独数出规模，不摊派到任何一档——
    它是基础设施问题，不是用户传了一个奇怪的文件。

    0 样本档全列：「3 MB 以上没有人」本身是结论，不是表格的缺口。
    """
    readable = [record for record in records if record.size is not None]
    unreadable_count = len(records) - len(readable)
    total = len(readable)
    counts = {label: 0 for _lower, _upper, label in SIZE_BANDS}
    for record in readable:
        counts[_band_label(record.size, SIZE_BANDS)] += 1

    rows = []
    for _lower, _upper, label in SIZE_BANDS:
        count = counts[label]
        rows.append({
            'label': label,
            'count': count,
            'share': _rate(count, total),
            'is_small_sample': count < MIN_EVIDENCE_SAMPLE,
        })
    return rows, total, unreadable_count


def _near_limit_stats(records):
    """贴上限与超过上限的份数。

    「贴上限」是阈值观察，不是一格分布：一批文件反复撞上限，说明上限在挡人；
    一个都没有，说明上限宽松。**已经超过上限的单列**——校验器拦着，它理论上
    也是 0，非 0 就说明有路径绕过了校验，那是一个 bug。
    """
    readable = [record for record in records if record.size is not None]
    near = [record for record in readable if record.size >= NEAR_LIMIT_SIZE]
    over = [record for record in near if record.size >= MAX_EVIDENCE_SIZE]
    return {
        'near_limit_count': len(near),
        'near_limit_rate': _rate(len(near), len(readable)),
        'over_limit_count': len(over),
        'over_limit_rate': _rate(len(over), len(readable)),
        'near_limit_threshold': NEAR_LIMIT_SIZE,
        'near_limit_label': _format_size(NEAR_LIMIT_SIZE),
        'max_size': MAX_EVIDENCE_SIZE,
        'max_size_label': _format_size(MAX_EVIDENCE_SIZE),
        'has_sample': len(readable) >= MIN_EVIDENCE_SAMPLE,
        'min_sample': MIN_EVIDENCE_SAMPLE,
    }


def _group_by_dispute(records):
    """按争议分组。组内顺序即上传顺序，第一条就是最早进来的那条。"""
    grouped = {}
    for record in records:
        grouped.setdefault(record.dispute_id, []).append(record)

    disputes = []
    for dispute_id in sorted(grouped):
        rows = grouped[dispute_id]
        sizes = [record.size for record in rows if record.size is not None]
        status = rows[0].dispute_status
        disputes.append({
            'dispute_id': dispute_id,
            'evidence_count': len(rows),
            'readable_count': len(sizes),
            'total_size': sum(sizes),
            'status': status,
            'is_closed': status in CLOSED_STATUSES,
        })
    return disputes


def _per_dispute_stats(disputes):
    """一笔争议带几份、总大小多少。

    分母是窗口内至少有一份附件的争议。**「一份附件都没有的争议」不在这里**：
    本模块按证据上传时刻切窗口，看不见没有证据的争议；那个数在
    ``dispute_resolution_quality`` 里，但它按争议发起切窗口，也不是同一批记录。
    """
    total = len(disputes)
    counts = {label: 0 for _lower, _upper, label in EVIDENCE_COUNT_BANDS}
    for dispute in disputes:
        counts[_band_label(dispute['evidence_count'], EVIDENCE_COUNT_BANDS)] += 1

    rows = []
    for _lower, _upper, label in EVIDENCE_COUNT_BANDS:
        count = counts[label]
        rows.append({
            'label': label,
            'count': count,
            'share': _rate(count, total),
            'is_small_sample': count < MIN_EVIDENCE_SAMPLE,
        })

    totals = [dispute['total_size'] for dispute in disputes]
    return {
        'dispute_count': total,
        'count_rows': rows,
        'max_count': max((d['evidence_count'] for d in disputes), default=0),
        'median_total_size': _percentile(totals, 0.5),
        'median_total_label': _format_size(_percentile(totals, 0.5)),
        'p90_total_label': _format_size(_percentile(totals, 0.9)),
        'average_total_label': _format_size(
            round(sum(totals) / len(totals)) if totals else None
        ),
        'max_total_label': _format_size(max(totals)) if totals else '—',
    }


def _status_cross_rows(disputes):
    """每笔争议的材料规模 × 争议现在处于什么状态。

    **已完结组给方向，流程中组只报规模。** ``add_dispute_evidence`` 只在
    ``open`` / ``reviewing`` 开放，所以已完结争议的附件数在结案那一刻就定了；
    而还挂在流程里的争议以后还会补附件，它现在的份数只是下界。把两组的均值
    放在一起比，会把「还在谈」读成「材料少」——那是右删失样本被当成观测值
    的老问题。
    """
    groups = (
        ('closed', '已完结', [d for d in disputes if d['is_closed']]),
        ('open', '还在流程中', [d for d in disputes if d['status'] in OPEN_STATUSES]),
    )
    rows = []
    for key, label, group in groups:
        if not group:
            continue
        counts = [dispute['evidence_count'] for dispute in group]
        totals = [dispute['total_size'] for dispute in group]
        rows.append({
            'key': key,
            'label': label,
            'dispute_count': len(group),
            'evidence_count': sum(counts),
            'average_count': round(sum(counts) / len(group), 1),
            'median_total_label': _format_size(_percentile(totals, 0.5)),
            'max_total_label': _format_size(max(totals)),
            'gives_direction': key == 'closed' and len(group) >= MIN_EVIDENCE_SAMPLE,
            'is_small_sample': len(group) < MIN_EVIDENCE_SAMPLE,
            'min_sample': MIN_EVIDENCE_SAMPLE,
            'note': (
                '结案时附件数已定，可以给方向'
                if key == 'closed' else
                'add_dispute_evidence 只在 open / reviewing 开放，这一组以后还会补，'
                '当前份数只是下界，只报规模、不给方向'
            ),
        })
    return rows


def _summary(extension_rows, size_total, near, per_dispute, cross_rows, total_count):
    """一段话把这个模块的读数讲完，样本不足的地方明说。"""
    if not total_count:
        return '当前周期还没有交易争议证据，证据文件的形状暂时无从读起。'

    parts = [f'周期内上传了 {total_count} 份争议证据']
    top = max(extension_rows, key=lambda row: row['count'])
    if top['count']:
        parts.append(
            f"，其中 {top['count']} 份是 {top['label']}"
            f"（{top['share']}%）"
        )
    other = [row for row in extension_rows if not row['is_allowed']][0]
    if other['count']:
        parts.append(f"，{other['count']} 份扩展名在校验允许的范围之外")
    if size_total:
        parts.append(f"，能读到大小的 {size_total} 份")
    if near['near_limit_count']:
        parts.append(f"，{near['near_limit_count']} 份贴着 {near['near_limit_label']} 的上限")
        if near['over_limit_count']:
            parts.append(f"，其中 {near['over_limit_count']} 份已经超过上限")
    if per_dispute['dispute_count']:
        parts.append(
            f"。{per_dispute['dispute_count']} 笔争议带了材料，"
            f"一笔最多 {per_dispute['max_count']} 份，"
            f"总大小中位 {per_dispute['median_total_label']}"
        )
    if not near['has_sample'] and size_total:
        parts.append(
            f"，不足 {near['min_sample']} 份，占比只作参考"
        )
    return ''.join(parts)


def build_dispute_evidence_shape(days=DEFAULT_LOOKBACK_DAYS, now=None):
    """统计争议证据的格式与大小分布，只报告，不改任何字段、不动任何文件。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = DEFAULT_LOOKBACK_DAYS
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    records = _fetch_evidence(start=start, end=now)

    extension_rows = _extension_rows(records)
    size_rows, size_total, unreadable_count = _size_rows(records)
    near = _near_limit_stats(records)
    disputes = _group_by_dispute(records)
    per_dispute = _per_dispute_stats(disputes)
    cross_rows = _status_cross_rows(disputes)

    return {
        'days': days,
        'period_start': start,
        'period_end': now,
        'evidence_count': len(records),
        'readable_count': size_total,
        'unreadable_count': unreadable_count,
        'dispute_count': len(disputes),
        'has_data': bool(records),
        'summary': _summary(
            extension_rows, size_total, near, per_dispute, cross_rows, len(records),
        ),
        'extension_rows': extension_rows,
        'size_rows': size_rows,
        'near_limit': near,
        'per_dispute': per_dispute,
        'status_cross_rows': cross_rows,
        'allowed_extensions': list(ALLOWED_EXTENSIONS),
        'min_sample': MIN_EVIDENCE_SAMPLE,
        'noise_points': NOISE_POINTS,
    }

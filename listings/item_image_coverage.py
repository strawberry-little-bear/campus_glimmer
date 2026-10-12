# -*- coding: utf-8 -*-
"""商品图片配了多少张，第一次被运营读到。

`listings.ItemImage` 是站内唯一一条「商品的配图」记录，结构极简：一个指向 `Item`
的外键，一个 `upload_to='items/'` 的 `ImageField`。没有 `is_primary`，没有 `order`，
没有 `created_at`——**排第一的那张图就是 `item.images.first`**，而模板里五处
（商品详情主图、缩略图列、我的商品列表、搜索结果卡片、后台 inline）全都靠这一句取图。

写入路径一条，在 `views.py` 的 `edit_item`：`ItemImageFormSet` 是
`inlineformset_factory(Item, ItemImage, extra=3, max_num=5, can_delete=True)`——
一件商品最多五张图，删了旧图不算历史，`ItemImage` 行直接消失。

于是读路径分居两处，性质完全不同：

- **产品侧**：`transaction_safety.build_transaction_safety` 在订单安全卡上判
  `if not item.images.exists()`，挂一条 warning「商品暂未上传图片」，提示买家
  「交付前再次核对成色」。平台已经承认没图的商品会让交易前的确认变难。
- **运营侧**：零。`analytics.py`、`campus_pulse.py`、`supply_lifecycle.py`、
  `price_insights.py` 在给运营的建议文案里反复出现「图片」两个字——「重点完善
  图片、描述和交付安排」「引导卖家补充图片与描述」——**但没有任何一个数字告诉
  运营：全站现在有多少件商品一张图都没有，以及这件事到底有没有影响成交。**

三个模块都在建议补图，没有一个模块数过图。**「引导卖家补充图片」这句建议已经挂了
三处，而运营看不到建议的对象是谁、有多少、补了之后有没有用。**

同一句正确的话，在不同站点状态下的含义完全相反：如果全站九成商品都有图，这句话是
在优化体验；如果三成商品一张图都没有，这句话是在说供给质量本身有问题。运营现在
分不清自己在哪一种处境里。

本模块只报告，不改写任何一件商品、任何一张图、任何一个订单。几处口径上的取舍比
数字本身更要紧。

分母只收**当前在架的商品**（`status='available'`）。已售出、已预订、已过期的商品
不进分母——它们已经走完了自己的流程，图片补不补都改变不了结局，把它们算进「有多少
商品没图」会让这个数字永远虚高，而虚高的部分运营做不了任何事。

成交的口径和 `analytics.py` 既有的一致：走到了 `completed` 或 `returned` 的订单。
`cancelled` 单独一类——它是一次真的失败，而且失败在下单之后，比「没有订单」更值得
被看见。

**「有图 vs 无图」的成交率对照不做因果。** 一件没图的商品往往同时也是描述更短、
定价更随意、卖家更不活跃的那一件。平台没有能力把「图起了作用」和「认真的人本来就
既传图也写好描述」分开。而且图片数和成交之间还夹着一个选择效应：**卖家给一件商品
传几张图，本身就是他对这件商品信心的表达**，一张没传的商品可能确实是难出手的那
一件，而不是因为没传图才难出手。这两件事在观测数据里分不开，所以这一栏只做对照、
不给方向性的结论。

「上传过又删光了」和「从来没传过」**在图片数上完全一样，都是零**，而运营该做的
动作不一样：前者该去问是不是图有问题，后者该去引导第一次上传。`ItemImage` 删了就
没了，没有软删、没有 `created_at`，所以「曾经有过几张图」这个问题结构上就答不了。
本模块只从 `Report` 里找一个间接线索——无图商品里有多少曾被举报过——**不把删图
动机分成主动被动两类**，平台没有能力从现有字段判断一个人为什么删图。

配几张图最好，只报形状不报最优值。说「4-5 张图的成交率最高，所以应该引导大家传满
五张」是一个相关不等于因果的典型陷阱：传满五张图的卖家可能本来就更认真，也可能他的
商品本来就更好出手。而且 `max_num=5` 是产品约束，不是运营结论。

不读图片内容：不做清晰度检测、不做违规识别、不判断图片和描述是否一致。也不打开
文件判断大小、格式、尺寸——`ImageField` 背后是本地 media 目录，逐个 open 会在一个
成熟站点上变成一次磁盘风暴，而看板是一次打开全算的。**图片是二进制文件，运营看板
只数行数。**

不按卖家聚合、不排名、不报「谁最不爱传图」——那是一份指责清单，不是一份看板。
不改 `max_num=5`、不改主图选取规则：主图是 `images.first`，依赖 `ItemImage` 的默认
排序，改成显式 `is_primary` 是一次 schema 变更，不在本模块范围内。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import Item, Order, Report

# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 在架商品少于这么多件时，成交率对照只报数字不给方向。
MIN_MEASURED_ITEMS = 12

# 两组（有图 / 无图）的成交率相差不满这么多个百分点，视为噪声。和其余模块同一个量级。
NOISE_POINTS = 8

# 只数当前在架的商品。已售出、已预订、已过期的商品不进分母——它们已经走完了自己的
# 流程，图片补不补都改变不了结局，算进「有多少商品没图」会让这个数字永远虚高，
# 而虚高的部分运营做不了任何事。
ON_SALE_STATUS = 'available'

# 订单结局里算成交的状态。`completed` 是交易完成，`returned` 是借用归还——借用订单
# 走到 `returned` 是一次完整的闭环，和 `completed` 等价。口径和 analytics.py 一致。
DEALT_STATUSES = ('completed', 'returned')

# 取消单独一类，不算成交也不算未成交：它是一次真的失败，而且失败在下单之后，
# 比「没有订单」更值得被看见。
CANCELLED_STATUS = 'cancelled'

# 图片数的分档。上界 None 的那一档兜底。`max_num=5` 是产品约束不是运营结论，
# 所以最后一档只写「4 张及以上」，不写「传满」。
IMAGE_COUNT_BUCKETS = (
    (1, '一张图都没有'),
    (2, '1 张图'),
    (4, '2-3 张图'),
    (None, '4 张及以上'),
)

# 无图商品清单的上限。清单是给「现在去处理」用的，不是给归档用的。
NO_IMAGE_LIST_LIMIT = 20

# 一件商品在图片数上的一行快照。一次查完固定下来：成交率对照、分档、分类与交易方式
# 拆分、无图清单各读一遍，按商品现算会让每件商品打好几遍库，而看板是一次打开全算的。
ItemCoverage = namedtuple('ItemCoverage', [
    'item', 'image_count', 'report_count',
    'dealt_count', 'cancelled_count', 'open_count', 'order_count',
])

# 无图商品清单的一行。`category_label` 和 `trade_mode_label` 在模块里就对成名字，
# 模板不需要知道 CHOICES 的存在。
NoImageRow = namedtuple('NoImageRow', [
    'item', 'category_label', 'trade_mode_label', 'report_count',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _bucket_index(count, buckets):
    """分档下标。桶是 (上界, 标签) 的元组，左闭右开。"""
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or count < limit:
            return index
    return len(buckets) - 1


def _fetch_item_coverage(days, now):
    """按显式排序取出窗口内发布的在架商品，连图片数和订单结局一起算好。

    统计窗口按 `Item.created_at` 切。一件商品的成交发生在发布之后的任何时候，
    按订单时刻切会把「发布很久、刚刚才成交」的商品整条丢出去，而它正是最该被看见的
    那一批——老商品才更可能一张图都没有。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    """
    start = _period_start(days, timezone.localtime(now).date())
    items = list(
        Item.objects.select_related('category', 'location', 'seller').annotate(
            image_count=Count('images'),
        ).filter(
            status=ON_SALE_STATUS,
            created_at__gte=start,
            created_at__lte=now,
        ).order_by('-created_at', 'id')
    )
    if not items:
        return []

    item_ids = [item.id for item in items]
    # 订单按商品聚合好再逐件贴上去。全表扫一遍会在一个成熟站点上变成一次没有索引可用
    # 的全表关联，而这里要的只是每件商品各自有过几笔什么结局的订单。
    order_rollup = {
        row['item_id']: row
        for row in Order.objects.filter(item_id__in=item_ids).values('item_id').annotate(
            dealt=Count('id', filter=Q(status__in=DEALT_STATUSES)),
            cancelled=Count('id', filter=Q(status=CANCELLED_STATUS)),
            total=Count('id'),
        )
    }
    # 举报只用来给无图商品找一个间接线索：曾被举报过的商品，删图可能是被动的。
    # 三种在办与已办状态都算存在过，只数条数，不读举报理由——那可能是「描述与图片不一致」这样的自由文本。
    report_rollup = {
        row['item_id']: row['count']
        for row in Report.objects.filter(
            item_id__in=item_ids, status__in=['pending', 'reviewing', 'resolved'],
        ).values('item_id').annotate(count=Count('id'))
    }

    coverage = []
    for item in items:
        rollup = order_rollup.get(item.id, {})
        dealt = rollup.get('dealt', 0)
        cancelled = rollup.get('cancelled', 0)
        total = rollup.get('total', 0)
        coverage.append(ItemCoverage(
            item=item,
            image_count=item.image_count,
            report_count=report_rollup.get(item.id, 0),
            dealt_count=dealt,
            cancelled_count=cancelled,
            # 还在进行中的订单：既不成交也没取消，周期结束时还没走完所属流程。
            open_count=max(total - dealt - cancelled, 0),
            order_count=total,
        ))
    return coverage


def _build_coverage_stats(coverage):
    """全站有多少件商品一张图都没有。分母是在架商品数。"""
    total = len(coverage)
    no_image = [row for row in coverage if row.image_count == 0]
    with_image = [row for row in coverage if row.image_count > 0]
    return {
        'total': total,
        'no_image_count': len(no_image),
        'no_image_rate': _percent(len(no_image), total),
        'with_image_count': len(with_image),
        'is_small_sample': len(no_image) < MIN_MEASURED_ITEMS
        or len(with_image) < MIN_MEASURED_ITEMS,
        'min_measured_items': MIN_MEASURED_ITEMS,
        # 曾被举报过的无图商品。只是一个间接线索：删图可能是被动的，也可能这条举报
        # 和图毫无关系。平台没有能力从现有字段判断一个人为什么删图。
        'no_image_reported_count': sum(1 for row in no_image if row.report_count),
    }


def _build_conversion_contrast(coverage):
    """没图的商品，成交率是不是真的更低。

    **这一栏只做对照，不给因果。** 一件没图的商品往往同时也是描述更短、定价更随意、
    卖家更不活跃的那一件。平台没有能力把「图起了作用」和「认真的人本来就既传图也写
    好描述」分开。而图片数和成交之间还夹着一个选择效应：卖家给一件商品传几张图，本身
    就是他对这件商品信力的表达——一张没传的商品可能确实是难出手的那一件，而不是因为
    没传图才难出手。
    """
    def group_rate(rows):
        dealt = sum(row.dealt_count for row in rows)
        orders = sum(row.order_count for row in rows)
        return {
            'item_count': len(rows),
            'order_count': orders,
            'dealt_count': dealt,
            'cancelled_count': sum(row.cancelled_count for row in rows),
            'open_count': sum(row.open_count for row in rows),
            # 分母是订单数，不是商品数。一件下过五笔单的商品在这个率里出现五次，
            # 而它确实产生了五笔交易——成交率回答的是「一笔交易走成了没有」。
            'dealt_rate': _percent(dealt, orders),
            'cancelled_rate': _percent(sum(row.cancelled_count for row in rows), orders),
        }

    no_image = [row for row in coverage if row.image_count == 0]
    with_image = [row for row in coverage if row.image_count > 0]
    without = group_rate(no_image)
    within = group_rate(with_image)
    gap = round(within['dealt_rate'] - without['dealt_rate'], 1)
    both_big_enough = (
        len(no_image) >= MIN_MEASURED_ITEMS
        and len(with_image) >= MIN_MEASURED_ITEMS
    )
    return {
        'no_image': without,
        'with_image': within,
        'gap_points': gap,
        'gap_is_meaningful': both_big_enough and abs(gap) >= NOISE_POINTS,
        'is_measurable': both_big_enough,
        'noise_points': NOISE_POINTS,
    }


def _build_image_count_stats(coverage):
    """配几张图最好。只报形状，不报最优值。

    说「4 张及以上那一档成交率最高，所以应该引导大家传满五张」是一个相关不等于因果的
    典型陷阱：传满五张图的卖家可能本来就更认真，也可能他的商品本来就更好出手。而且
    `max_num=5` 是产品约束，不是运营结论。
    """
    rows = []
    for index, bucket in enumerate(IMAGE_COUNT_BUCKETS):
        group = [
            row for row in coverage
            if _bucket_index(row.image_count, IMAGE_COUNT_BUCKETS) == index
        ]
        orders = sum(row.order_count for row in group)
        dealt = sum(row.dealt_count for row in group)
        rows.append({
            'key': index,
            'label': bucket[1],
            'item_count': len(group),
            'item_share': _percent(len(group), len(coverage)),
            'order_count': orders,
            'dealt_count': dealt,
            'dealt_rate': _percent(dealt, orders),
            # 某一档样本不足时，成交率只报数字不给方向。
            'is_small_sample': len(group) < MIN_MEASURED_ITEMS,
        })
    return {'rows': rows}


def _build_breakdown_stats(coverage, field, labels):
    """成交率按某个维度拆开，不按总数报。

    免费商品走的是 `GiftApplication` 申请队列而不是直接预约，一份申请能不能被选中和
    图片的关系，与出售商品下单的关系不是一回事。合并会把两种体验平均成一个没有
    解释力的数。分类同理：不同品类的出镜难度完全不同。
    """
    rows = []
    for key, label in labels:
        group = [row for row in coverage if getattr(row.item, field) == key]
        orders = sum(row.order_count for row in group)
        dealt = sum(row.dealt_count for row in group)
        no_image = sum(1 for row in group if row.image_count == 0)
        rows.append({
            'key': key,
            'label': label,
            'item_count': len(group),
            'no_image_count': no_image,
            'no_image_rate': _percent(no_image, len(group)),
            'order_count': orders,
            'dealt_count': dealt,
            'dealt_rate': _percent(dealt, orders),
            'is_small_sample': len(group) < MIN_MEASURED_ITEMS,
        })
    # 样本多的排在前面：运营要看的是规模，不是字母序。
    rows.sort(key=lambda row: (-row['item_count'], str(row['key'])))
    return {'rows': rows}


def _build_no_image_rows(coverage):
    """无图商品清单。只带商品标题、分类、交易方式和举报数，不带任何用户名。"""
    trade_labels = dict(Item.TRADE_MODE_CHOICES)
    category_labels = {
        category.id: category.name
        for category in {row.item.category for row in coverage if row.item.category_id}
    }
    stuck = [row for row in coverage if row.image_count == 0]
    # 越新的排在越前面——越新的越可能是还没被任何曝光碰过的新品，现在去补还来得及。
    stuck.sort(key=lambda row: row.item.created_at, reverse=True)
    rows = []
    for row in stuck[:NO_IMAGE_LIST_LIMIT]:
        rows.append(NoImageRow(
            item=row.item,
            category_label=category_labels.get(row.item.category_id, '未分类'),
            trade_mode_label=trade_labels.get(row.item.trade_mode, row.item.trade_mode),
            report_count=row.report_count,
        ))
    return {
        'rows': rows,
        'row_total': len(stuck),
        'row_limit': NO_IMAGE_LIST_LIMIT,
    }


def _summary(coverage_stats, contrast, image_counts):
    if not coverage_stats['total']:
        return '当前周期没有在架的新商品，图片覆盖也就无从谈起。'
    parts = [
        f"周期内新上了 {coverage_stats['total']} 件在售商品，"
        f"其中 {coverage_stats['no_image_count']} 件一张图都没有"
        f"（{coverage_stats['no_image_rate']}%）。"
    ]
    if coverage_stats['is_small_sample']:
        parts.append(
            f"两组各需至少 {coverage_stats['min_measured_items']} 件才给方向，"
            f"目前只报数字。"
        )
    elif contrast['gap_is_meaningful']:
        parts.append(
            f"有图商品的成交率 {contrast['with_image']['dealt_rate']}%，"
            f"无图商品 {contrast['no_image']['dealt_rate']}%，"
            f"相差 {contrast['gap_points']} 个百分点。"
        )
    elif contrast['is_measurable']:
        parts.append(
            f"两组成交率相差 {contrast['gap_points']} 个百分点，"
            f"不足噪声阈值，不作解读。"
        )
    if coverage_stats['no_image_reported_count']:
        parts.append(
            f"无图商品里有 {coverage_stats['no_image_reported_count']} 件"
            f"曾被举报过，删图可能是被动的。"
        )
    return ''.join(parts)


def build_item_image_coverage(days=30, now=None):
    """统计在架商品的图片覆盖与成交对照，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    coverage = _fetch_item_coverage(days, now)
    coverage_stats = _build_coverage_stats(coverage)
    contrast = _build_conversion_contrast(coverage)
    image_counts = _build_image_count_stats(coverage)
    by_trade_mode = _build_breakdown_stats(
        coverage, 'trade_mode', list(Item.TRADE_MODE_CHOICES),
    )
    by_category = _build_breakdown_stats(
        coverage, 'category_id',
        sorted(
            {
                (row.item.category_id, row.item.category.name)
                for row in coverage if row.item.category_id
            },
            key=lambda pair: pair[1],
        ),
    )
    no_image_rows = _build_no_image_rows(coverage)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(coverage),
        'summary': _summary(coverage_stats, contrast, image_counts),
        'coverage': coverage_stats,
        'contrast': contrast,
        'image_counts': image_counts,
        'by_trade_mode': by_trade_mode,
        'by_category': by_category,
        'no_image_rows': no_image_rows,
        'image_count_labels': [label for _, label in IMAGE_COUNT_BUCKETS],
        'dealt_statuses': DEALT_STATUSES,
        'noise_points': NOISE_POINTS,
        'min_measured_items': MIN_MEASURED_ITEMS,
    }
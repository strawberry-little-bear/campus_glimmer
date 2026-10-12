# -*- coding: utf-8 -*-
"""交易评价打了多少分，第一次被运营读到。

`Rating` 是站内唯一一条「一笔交易走完之后，双方各自给对方打一个分」的记录。模型只有
六个字段：`order`、`rater`、`ratee`、`score`（1 到 5 星）、`comment`（自由文本，可空）、
`created_at`。写入路径只有 `views.py` 的 `rate_order`，它要求订单已经是 `completed` 或
`returned`、要求当前用户是买家或卖家、要求这一方还没评过
（`UniqueConstraint(fields=['order', 'rater'])`）。

读取路径有三处：商品详情页拿卖家的最近五条评价和一个 `Avg` / `Count` 摘要；订单详情页
把这一笔的全部评价列给双方本人看；后台把它们列给管理员。**没有一处拿 `score` 和另一个
数比过大小。** `reputation.py` 读了 `Avg` 和 `Count` 用来算「评价表现良好」徽章，那是整个
项目里唯一碰过 `score` 的地方，而它只求均值。

于是一套五星制评价系统上线运行，从来没有人知道它分布成什么形状。四个具体的问题一个
都答不上来：这笔交易有多少人被评价了；分打出来之后多久才提交；交易结果和分数有没有
关系；写没写文字和打多少分有没有关系。更麻烦的是第二层——没有任何一处代码在盯着这套
评价的完整性，`rate_order` 的入口对双方永久开放，却没有任何东西告诉运营「有多少交易
双方都没评」。

这个模块把它们读出来，只报告，不改写任何一条评价。几处口径上的取舍比数字本身更要紧，
它们都在下面的注释里。

覆盖率的分母是「已经走完的交易」，不是「全部交易」。一笔交易只有走到 `completed` 或
`returned` 才算有了被评价的资格——`rate_order` 开头就挡掉了其余状态。所以分母只收这两
种状态的订单。还挂在 `pending` / `confirmed` / `meeting` / `borrowed` 的订单是右删失
样本：交易还没结束，谈不上有没有被评价。把它算进分母会让覆盖率凭空变小，和订单阶段流、
归还握手、交付预约到场是同一个道理，这部分单独数出来，不丢也不混。

「多久才评」按天分档，不按小时。评价的时间尺度是天：一笔交易完成之后，一个人可能当天
就评，也可能下周想起来才评。报「平均 26.4 小时」对一个按月发生的动作没有意义。已评价
的样本按 `rating.created_at` 减订单完成时刻算，**订单完成时刻取 `OrderEvent` 里
`to_status` 为 `completed` 或 `returned` 的那一条的 `created_at`**——它是系统里唯一一个
「这一刻交易真的走完了」的记录，比 `Order.updated_at` 可靠，后者任何一次保存都会动。
没有完成事件的评价不计入时延分布：那说明 `OrderEvent` 缺了这一笔，属于数据本身不完整，
单独数出来，不拿 `order.created_at` 近似——那是下单时刻，用它会系统性地把等待时间拉长。
还没评的那些订单不报「已经等了多久」：那是右删失样本，而且它跨周期，按周期切开会让一个
等了三个月的等待被算成三段短的。只报规模。

评价人数分布按订单聚合，分母是订单数。一笔交易可以是零条、一条、两条评价，分布按
「这笔交易收到了几条」报，分母是交易数——一笔双方都评了的交易在分布里只出现一次。这和
免费领取队列、有货关注集中度是同一个道理：按评价数走会让「两条」那一档被同一笔交易
撑两次。

双向评价必须按方向拆成「这一方评没评」，不能只看总数。一笔交易两条评价，可能是买家评了
卖家、卖家也评了买家；只报总数的覆盖率会骗人——只要有一个人评了就算「有评价」，而另
一个人可能永远不评。运营看到 100% 会以为评价环节很健康，而那一半沉默的人恰恰是最该被
看见的。

和交付异常的关系只做「有没有」对照，不判断因果。带已确认交付异常
（`MeetingIncident.status = 'resolved'`）的交易和不带的交易各自打分，**只报对照，不给
因果**：一个经常被提交异常的用户可能本来就更严格、更容易不满，一个从不提交异常的用户
可能只是好说话，平台没有能力把「这次交易真的出了问题」和「这个人本来就爱挑刺」分开。
把它做成因果会直接变成一个对具体用户的负面标签，那是这套评价最不该变成的东西。两组
各自至少 12 笔才给方向，差值不满 8 个百分点视为噪声。

分数分布不报「好评率」。五星制报「五星占多少」有诱惑力，但它会诱导人去优化那个数，而
它是这个模块唯一不能碰的东西。只报六个桶各自的占比和数量，不合成任何一个「好评率」
「满意度」。`Avg` 已经在 `reputation.py` 里了，本模块不重复它。

自由文本只报「写了没写」和长度，一个字符的内容都不读。`comment` 是自由文本，可能写着
电话、微信、楼栋号。本模块只报填写率、长度分档，以及「写了 vs 没写」两组的分数对照。
第三个是这个字段唯一有信息量的一问——一星是不是更容易配一段话。不切词、不聚类、不读
内容；`mutual_aid_feedback_notes.py` 已经为互助反馈的 `note` 做了切词，那是另一个字段、
另一套约束，本模块不把它复制过来。

本模块不读任何用户的邮箱、电话、微信：清单第一格只带订单号和商品标题，那是运营要认出
来的东西。不催评、不改 `rate_order` 的开放期限、不把评价写回任何人的信誉分、不做「疑似
刷分」检测、不隐藏低分评价、不补 `OrderEvent` 缺失的完成事件。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import Item, MeetingIncident, Order, OrderEvent, Rating

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一组订单至少要这么多笔，占比和对照才给方向。和其余模块同一个量级：几十条样本上
# 几个百分点的差距是噪声。
MIN_MEASURED_ORDERS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 「多久才评」的分档。互斥，左闭右开，按天切。评价的时间尺度是天：一个人交易完成
# 之后可能当天就评，也可能下周想起来才评，报「平均 26.4 小时」没有意义。
LATENCY_BUCKETS = (
    (86400, '1 天内', '当天就评，还记得清细节'),
    (3 * 86400, '1-3 天', '隔一两天，记忆还新鲜'),
    (7 * 86400, '3-7 天', '一周内，属于正常补评'),
    (30 * 86400, '7-30 天', '隔了挺久，多半是翻记录翻出来的'),
    (None, '超过 30 天', '一个月以后，记忆已经很不可靠'),
)

# 一笔交易收到了几条评价。分母是订单数，不是评价数——理由见模块开头。
RATING_COUNT_BUCKETS = (
    (0, '没人评价', '双方都没评，这笔交易在评价体系里是隐形的'),
    (1, '只有一方评价', '另一个人再也不会回来评这笔交易'),
    (2, '双方都评价', '唯一健康的形状'),
)

# 已经走完的交易状态。只有它们才谈得上「有没有被评价」。
CLOSED_STATUSES = ('completed', 'returned')

# 评价时长的清单上限。清单是给「现在去核实」用的，不是给归档用的。
LATENCY_LIST_LIMIT = 20

# 零评价订单的清单上限，同上。
UNRATED_LIST_LIMIT = 20

# 自由文本长度分档。固定四档，和互助反馈补充说明的长度分档一个思路。
COMMENT_LENGTH_BUCKETS = (
    (20, '20 字以内'),
    (60, '21-60 字'),
    (150, '61-150 字'),
    (None, '150 字以上'),
)

# 分数桶。1 到 5 星全列出来，一星单独一档——它是这套评价里唯一真正重要的信号，
# 混进「低分」里会被摊薄。
SCORE_BUCKETS = tuple((score, f'{score} 星') for score in range(1, 6))

TRADE_MODE_LABELS = dict(Item.TRADE_MODE_CHOICES)

# 一笔已经走完的交易，加上它收到的评价。一次查完固定下来：覆盖率、双向拆分、
# 时延分布、异常对照各读一遍，按记录现算会让每笔交易打好几遍库，而看板是一次
# 打开全算的。
ClosedOrder = namedtuple('ClosedOrder', [
    'order', 'ratings', 'completed_at', 'has_incident',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_days(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 86400:
        return f'{max(seconds // 3600, 0)} 小时'
    return f'{seconds // 86400} 天'


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _bucket_index(seconds, buckets):
    """分档下标。桶是 (上限, 标签[, 说明]) 的元组，互斥、左闭右开。

    上限为 None 的那一档是最后一档，兜住所有超过前一档的样本。
    """
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _fetch_closed_orders(*, created_after=None, created_before=None):
    """按显式排序取出已经走完的交易，连它们的评价和完成时刻一起取回来。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。

    统计窗口按 `Order.created_at` 切，不按完成时刻切：交易是窗口内发生的，评价可以
    在窗口外才落库——那笔交易的评价数算进来，但它的时延不进时延分布（它不在窗口内
    被评价）。反过来不成立：窗口内评价的交易一定在窗口内或更早创建。
    """
    orders = Order.objects.select_related(
        'item', 'item__category', 'item__location', 'buyer', 'seller',
    ).filter(status__in=CLOSED_STATUSES).order_by('created_at', 'id')
    if created_after is not None:
        orders = orders.filter(created_at__gte=created_after)
    if created_before is not None:
        orders = orders.filter(created_at__lte=created_before)
    orders = list(orders)

    if not orders:
        return []

    # 完成时刻取 OrderEvent 里 to_status 为 completed / returned 的那一条。
    # 它是系统里唯一一个「这一刻交易真的走完了」的记录，比 Order.updated_at 可靠
    # ——后者任何一次保存都会动，包括卖家改一次商品标题。
    completed_events = {}
    for event in OrderEvent.objects.filter(
        order__in=orders, to_status__in=CLOSED_STATUSES,
    ).order_by('created_at', 'id'):
        # 同一笔订单可能有多条 closed 事件（例如 completed 之后又被推进），
        # 取最早的那一条：评价是从那一刻起才有资格的。
        completed_events.setdefault(event.order_id, event.created_at)

    ratings = {}
    for rating in Rating.objects.filter(order__in=orders).select_related(
        'rater', 'ratee',
    ).order_by('created_at', 'id'):
        ratings.setdefault(rating.order_id, []).append(rating)

    incidents = set(
        MeetingIncident.objects.filter(
            appointment__order__in=orders, status='resolved',
        ).values_list('appointment__order_id', flat=True)
    )

    result = []
    for order in orders:
        result.append(ClosedOrder(
            order=order,
            ratings=ratings.get(order.id, []),
            completed_at=completed_events.get(order.id),
            has_incident=order.id in incidents,
        ))
    return result


def _direction_counts(closed_order):
    """买家评了卖家几条、卖家评了买家几条。"""
    buyer_to_seller = 0
    seller_to_buyer = 0
    for rating in closed_order.ratings:
        if rating.rater_id == closed_order.order.buyer_id:
            buyer_to_seller += 1
        elif rating.rater_id == closed_order.order.seller_id:
            seller_to_buyer += 1
    return buyer_to_seller, seller_to_buyer


def _build_coverage_stats(closed_orders):
    """这笔交易有几个人被评价了，双向拆开报。"""
    total = len(closed_orders)
    by_count = [0] * (len(RATING_COUNT_BUCKETS) + 1)
    for closed_order in closed_orders:
        count = len(closed_order.ratings)
        by_count[min(count, len(RATING_COUNT_BUCKETS))] += 1

    count_rows = []
    for index, (limit, label, note) in enumerate(RATING_COUNT_BUCKETS):
        count_rows.append({
            'label': label,
            'note': note,
            'count': by_count[index],
            'share': _percent(by_count[index], total),
        })

    buyer_rated = 0
    seller_rated = 0
    both_rated = 0
    neither_rated = 0
    for closed_order in closed_orders:
        buyer_to_seller, seller_to_buyer = _direction_counts(closed_order)
        buyer_side = seller_to_buyer > 0
        seller_side = buyer_to_seller > 0
        if buyer_side:
            buyer_rated += 1
        if seller_side:
            seller_rated += 1
        if buyer_side and seller_side:
            both_rated += 1
        elif not buyer_side and not seller_side:
            neither_rated += 1

    return {
        'total': total,
        'rating_count_rows': count_rows,
        'rated_count': total - by_count[0],
        'rating_rate': _percent(total - by_count[0], total),
        'buyer_rated_count': buyer_rated,
        'buyer_rated_rate': _percent(buyer_rated, total),
        'seller_rated_count': seller_rated,
        'seller_rated_rate': _percent(seller_rated, total),
        'both_rated_count': both_rated,
        'both_rated_rate': _percent(both_rated, total),
        'one_sided_count': total - both_rated - neither_rated,
        'one_sided_rate': _percent(total - both_rated - neither_rated, total),
        'neither_rated_count': neither_rated,
        'neither_rated_rate': _percent(neither_rated, total),
        'is_small_sample': total < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_latency_stats(closed_orders):
    """分打出来之后多久才提交。

    只收有完成事件的已评价样本。没有完成事件的说明 `OrderEvent` 缺了这一笔，属于
    数据本身不完整，单独数出来——不拿 `order.created_at` 近似，那是下单时刻，用它
    会系统性地把等待时间拉长。还没评的订单不报「已经等了多久」：那是右删失样本，
    而且它跨周期，按周期切开会让一个等了三个月的等待被算成三段短的。只报规模。
    """
    measured = []
    missing_event = 0
    for closed_order in closed_orders:
        if not closed_order.ratings or closed_order.completed_at is None:
            if closed_order.ratings and closed_order.completed_at is None:
                missing_event += 1
            continue
        # 一笔交易可能有多条评价，取最早的那一条：它是这一方第一次被提醒到的时点。
        first = min(closed_order.ratings, key=lambda r: r.created_at)
        seconds = (first.created_at - closed_order.completed_at).total_seconds()
        if seconds < 0:
            # 评价早于完成事件：要么事件补记晚了，要么数据被改过。这种样本
            # 算不出真实的等待时长，单独数出来，不塞进任何一档。
            missing_event += 1
            continue
        measured.append((closed_order, first, seconds))

    seconds_list = [seconds for _order, _rating, seconds in measured]
    buckets = [{'label': label, 'note': note, 'count': 0} for _limit, label, note in LATENCY_BUCKETS]
    for seconds in seconds_list:
        buckets[_bucket_index(seconds, LATENCY_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(seconds_list))

    unrated = [closed_order for closed_order in closed_orders if not closed_order.ratings]

    slowest = sorted(measured, key=lambda item: (-item[2], item[0].order.id))[:LATENCY_LIST_LIMIT]

    return {
        'measured_count': len(measured),
        'missing_event_count': missing_event,
        'unrated_count': len(unrated),
        'average_seconds': _mean(seconds_list),
        'average_label': _format_days(_mean(seconds_list)),
        'median_label': _format_days(_percentile(seconds_list, 0.5)),
        'p90_label': _format_days(_percentile(seconds_list, 0.9)),
        'fastest_label': _format_days(min(seconds_list)) if seconds_list else '—',
        'slowest_label': _format_days(max(seconds_list)) if seconds_list else '—',
        'bucket_rows': buckets,
        'late_count': sum(1 for seconds in seconds_list if seconds >= 30 * 86400),
        'late_rate': _percent(
            sum(1 for seconds in seconds_list if seconds >= 30 * 86400), len(seconds_list),
        ),
        'slowest_rows': [
            {
                'order': closed_order.order,
                'rating': rating,
                'latency_label': _format_days(seconds),
            }
            for closed_order, rating, seconds in slowest
        ],
        'unrated_rows': [
            {
                'order': closed_order.order,
                'days_label': _format_days(
                    (timezone.now() - closed_order.order.created_at).total_seconds()
                ),
            }
            for closed_order in sorted(
                unrated, key=lambda item: item.order.created_at, reverse=False,
            )[:UNRATED_LIST_LIMIT]
        ],
        'unrated_row_total': len(unrated),
        'unrated_row_limit': UNRATED_LIST_LIMIT,
    }


def _build_score_stats(closed_orders):
    """分数分布。只报六个桶，不合成任何「好评率」。"""
    scores = []
    for closed_order in closed_orders:
        for rating in closed_order.ratings:
            scores.append(rating.score)

    buckets = [
        {'label': label, 'count': 0, 'score': score}
        for score, label in SCORE_BUCKETS
    ]
    for score in scores:
        buckets[min(max(score, 1), 5) - 1]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(scores))

    low_count = buckets[0]['count'] + buckets[1]['count']
    return {
        'total': len(scores),
        'bucket_rows': buckets,
        'average': round(sum(scores) / len(scores), 2) if scores else None,
        'low_count': low_count,
        'low_rate': _percent(low_count, len(scores)),
        'top_score_count': buckets[4]['count'],
        'top_score_rate': _percent(buckets[4]['count'], len(scores)),
        'is_small_sample': len(scores) < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_incident_contrast(closed_orders):
    """带已确认交付异常的交易和不带的交易，各自打分。只报对照，不给因果。"""
    groups = []
    for key, label in (('incident', '有已确认交付异常'), ('clean', '无交付异常')):
        group = [
            closed_order for closed_order in closed_orders
            if (closed_order.has_incident if key == 'incident' else not closed_order.has_incident)
        ]
        scores = [rating.score for closed_order in group for rating in closed_order.ratings]
        rows = [{'label': label, 'count': 0, 'score': score} for score, label in SCORE_BUCKETS]
        for score in scores:
            rows[min(max(score, 1), 5) - 1]['count'] += 1
        for row in rows:
            row['share'] = _percent(row['count'], len(scores))
        groups.append({
            'key': key,
            'label': label,
            'order_count': len(group),
            'rating_count': len(scores),
            'average': round(sum(scores) / len(scores), 2) if scores else None,
            'low_rate': _percent(
                sum(1 for score in scores if score <= 2), len(scores),
            ),
            'top_rate': _percent(sum(1 for score in scores if score == 5), len(scores)),
            'bucket_rows': rows,
        })

    incident = groups[0]
    clean = groups[1]
    gap = None
    is_noise = True
    if incident['order_count'] and clean['order_count']:
        gap = round(incident['low_rate'] - clean['low_rate'], 1)
        is_noise = abs(gap) < NOISE_POINTS
    return {
        'rows': groups,
        'low_rate_gap': gap,
        'is_noise': is_noise,
        'noise_points': NOISE_POINTS,
        'has_both_sides': bool(incident['order_count'] and clean['order_count']),
    }


def _build_comment_stats(closed_orders):
    """自由文本只报「写了没写」和长度，一个字符的内容都不读。"""
    ratings = [rating for closed_order in closed_orders for rating in closed_order.ratings]
    filled = [rating for rating in ratings if (rating.comment or '').strip()]
    lengths = [len(rating.comment.strip()) for rating in filled]

    buckets = [{'label': label, 'count': 0} for _limit, label in COMMENT_LENGTH_BUCKETS]
    for length in lengths:
        index = 0
        for position, (limit, _label) in enumerate(COMMENT_LENGTH_BUCKETS):
            if limit is None or length <= limit:
                index = position
                break
        buckets[index]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(lengths))

    # 「写了 vs 没写」两组的分数对照。这是 comment 唯一有信息量的一问：
    # 一星是不是更容易配一段话。
    wrote = [rating.score for rating in filled]
    silent = [rating.score for rating in ratings if rating not in filled]
    gap = None
    is_noise = True
    if wrote and silent:
        wrote_average = sum(wrote) / len(wrote)
        silent_average = sum(silent) / len(silent)
        gap = round(wrote_average - silent_average, 2)
        # 分数是 1 到 5 的量表，差值换算成百分点再和噪声门槛比。
        is_noise = abs(gap) * 100 / 4 < NOISE_POINTS
    return {
        'rating_count': len(ratings),
        'filled_count': len(filled),
        'fill_rate': _percent(len(filled), len(ratings)),
        'average_length': round(sum(lengths) / len(lengths), 1) if lengths else 0,
        'max_length': max(lengths) if lengths else 0,
        'length_rows': buckets,
        'wrote_count': len(wrote),
        'wrote_average': round(sum(wrote) / len(wrote), 2) if wrote else None,
        'silent_count': len(silent),
        'silent_average': round(sum(silent) / len(silent), 2) if silent else None,
        'average_gap': gap,
        'is_noise': is_noise,
        'noise_points': NOISE_POINTS,
    }


def _build_trade_mode_stats(closed_orders):
    """按交易方式拆开，不合并。

    出售、免费领取、限期借用是三件不同的事：出售有价格，免费领取有「没人要还是有人在
    等」的问题，借用有归还。用户在三种交易里写评价的心态完全不同，混在一起报会让
    三边都被稀释。
    """
    rows = []
    for key, label in TRADE_MODE_LABELS.items():
        group = [
            closed_order for closed_order in closed_orders
            if closed_order.order.item.trade_mode == key
        ]
        if not group:
            continue
        scores = [rating.score for closed_order in group for rating in closed_order.ratings]
        rated = sum(1 for closed_order in group if closed_order.ratings)
        rows.append({
            'key': key,
            'label': label,
            'order_count': len(group),
            'rating_count': len(scores),
            'rated_count': rated,
            'rating_rate': _percent(rated, len(group)),
            'average': round(sum(scores) / len(scores), 2) if scores else None,
        })
    rows.sort(key=lambda row: -row['order_count'])
    return {'rows': rows}


def _summary(coverage, latency, score, contrast, comment, trade_mode):
    """一段话把这个模块的读数讲完，样本不足的地方明说。"""
    parts = []
    if coverage['total']:
        parts.append(
            f"周期内 {coverage['total']} 笔交易走完，"
            f"{coverage['rated_count']} 笔收到了评价（{coverage['rating_rate']}%）"
        )
        parts.append(
            f"，双方都评的 {coverage['both_rated_count']} 笔"
            f"（{coverage['both_rated_rate']}%）、只有一方评的 "
            f"{coverage['one_sided_count']} 笔"
        )
        if coverage['neither_rated_count']:
            parts.append(f"、双方都没评的 {coverage['neither_rated_count']} 笔")
    else:
        parts.append('当前周期还没有走完的交易。')

    if latency['measured_count']:
        parts.append(
            f"。{latency['measured_count']} 条评价的中位提交间隔 "
            f"{latency['median_label']}、P90 {latency['p90_label']}"
        )
        if latency['late_count']:
            parts.append(
                f"，其中 {latency['late_count']} 条是交易完成一个月以后才补的"
                f"（{latency['late_rate']}%）"
            )
    if latency['missing_event_count']:
        parts.append(
            f"；另有 {latency['missing_event_count']} 条评价找不到对应的订单完成事件，"
            f"不计入时延分布"
        )

    if score['total']:
        parts.append(
            f"。{score['total']} 条评价均分 {score['average']}，"
            f"两星及以下 {score['low_count']} 条（{score['low_rate']}%）"
        )
        if score['is_small_sample']:
            parts.append(f"，不足 {score['min_measured_orders']} 条，占比只作参考")

    if contrast['has_both_sides'] and contrast['low_rate_gap'] is not None:
        if contrast['is_noise']:
            parts.append(
                f"。有交付异常的交易和无异常的交易，两星及以下占比相差 "
                f"{abs(contrast['low_rate_gap'])} 个百分点，在噪声范围内"
            )
        else:
            parts.append(
                f"。有已确认交付异常的交易，两星及以下占比比无异常的高 "
                f"{contrast['low_rate_gap']} 个百分点——这只是对照，不构成因果"
            )

    if comment['rating_count']:
        parts.append(
            f"。{comment['fill_rate']}% 的评价写了文字，平均 "
            f"{comment['average_length']} 字"
        )
        if comment['average_gap'] is not None and not comment['is_noise']:
            parts.append(
                f"，写了文字的平均分比没写的高 {comment['average_gap']} 分"
            )
    return ''.join(parts)


def build_rating_quality(days=30, now=None):
    """统计交易评价的覆盖率、提交间隔、分数分布与交付异常对照，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    closed_orders = _fetch_closed_orders(created_after=start, created_before=now)

    coverage = _build_coverage_stats(closed_orders)
    latency = _build_latency_stats(closed_orders)
    score = _build_score_stats(closed_orders)
    contrast = _build_incident_contrast(closed_orders)
    comment = _build_comment_stats(closed_orders)
    trade_mode = _build_trade_mode_stats(closed_orders)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(closed_orders),
        'summary': _summary(coverage, latency, score, contrast, comment, trade_mode),
        'coverage': coverage,
        'latency': latency,
        'score': score,
        'incident_contrast': contrast,
        'comment': comment,
        'trade_mode': trade_mode,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'noise_points': NOISE_POINTS,
    }
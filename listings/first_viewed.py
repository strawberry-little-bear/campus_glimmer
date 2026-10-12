# -*- coding: utf-8 -*-
"""从第一次心动到下一步，中间隔了多久。

`BrowsingHistory` 有三列一直在写：`view_count`、`first_viewed_at`、
`last_viewed_at`。前两列被反复读取——`analytics` 拿它算活跃用户和详情浏览数，
`campaign_analytics` 按专题看浏览与收藏的转化，`lifecycle_diagnostics` 和
`price_insights` 拿 `view_count` 判断一件商品到底有没有人看。唯独
`first_viewed_at` 只出现在 `models.py` 的定义、`admin.py` 的 `list_display` 和
`readonly_fields` 里：它由 `auto_now_add` 写一次，此后永远不变，而除了后台
那一列，没有任何一行代码读过它。

写入路径在商品详情页：登录用户点进一件不是自己发布的商品，`get_or_create`
一条浏览记录，重复访问只把 `view_count` 加一、刷新 `last_viewed_at`。于是
数据库里一直躺着一个没人看过的差：`last_viewed_at - first_viewed_at`，一个用户
对一件商品的考虑窗口。

这个模块把它读出来，只报告，不改写任何一笔浏览记录、任何一件商品、任何一笔
订单。几处口径上的取舍比数字本身更要紧，它们都在下面的注释里。

`view_count` 回答「看了几次」，两个时间戳的差回答「从第一次心动到最近一次回访
拖了多久」。这两件事不一样，而不一样的地方正是运营能下手的地方。一件被看了
12 次、横跨 9 天、最后既没收藏也没下单的商品，和一件被看了 1 次的商品，在
`view_count` 的榜上前者显眼得多，但真正该被问的恰恰是前者：为什么有人反复
回访，最后却不行动。

已经有模块在读供给侧的新鲜度（`supply_lifecycle`）、价格参考
（`price_insights`）、生命周期诊断（`lifecycle_diagnostics`）。它们都回答
「这件商品现在是什么状态」。没有一个模块回答「看它的人想了多久，想完之后
做了什么」。这是用户决策时长的缺口，不是商品状态的缺口，所以本模块不去重数
商品、也不去重数人，只按一条浏览记录本身统计——一条 `BrowsingHistory` 就是
一个（用户，商品）对，本身就是一次心动的完整轨迹。按人聚会把同一个人对十件
商品的考虑混成一个数，按商品聚会把十个人对一件商品的考虑混成一个数，而模块
要问的恰恰是「一次心动想了多久」。

考虑窗口一律按天分档，不按小时。一次考虑的时间尺度是天，报「平均考虑了 41
小时」会让人以为是小时级的问题，而去查一个按天运转的流程。等待时长没有上界，
所以报分布而不是平均值：一个想了 40 天的买家会被一个平均值藏起来，而它恰恰是
最该被看见的那一条。

模块不判定「为什么没行动」。反复看却不行动，可能是价格，可能是描述没说清，
可能是照片看不出成色，也可能只是买家自己拖——平台没有能力区分这四者，也就
不该替其中任何一种下结论。模块只报「看了多久、之后做了什么」，把「想了很久
却没有下一步」这批样本数出来交给运营，不降价、不下架、不给卖家发提醒，也不
把任何统计写回浏览记录。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import BrowsingHistory, Favorite, Order

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 至少回访过一次才算有考虑窗口。只看过一次的记录没有窗口可言——
# `first_viewed_at` 和 `last_viewed_at` 是同一次写入的产物，差是零，而零和
# 「当天就走了」不是一回事：当天就走了的样本窗口是真的短，只看一次的样本
# 压根没有窗口。
MIN_REVISITS_FOR_WINDOW = 2

# 考虑窗口分档。互斥，且按天切。最后一档是尾巴，长尾才是要动作的那一批，所以
# 它单独一档而不是并进「7-14 天」里被平均掉。
WINDOW_BUCKETS = (
    (86400, '当天', '第一次看到，当天就又回访了，几乎没有犹豫'),
    (3 * 86400, '1-3 天', '三天之内想清楚，是校园交易里最常见的节奏'),
    (7 * 86400, '3-7 天', '等满一周之内，通常是在比价或者等另一件'),
    (14 * 86400, '7-14 天', '已经超过一周，多半是价格或者描述让人下不了手'),
    (None, '超过两周', '长尾：想了很久仍然没有下一步，最该被看见的一批'),
)

# 下单决策窗口的分档。它比回访窗口长，因为从心动到掏钱中间还隔着一整套预约、
# 见面、议价，所以分档更粗：以周为单位。
DECISION_BUCKETS = (
    (86400, '当天', '第一次看到当天就下单了'),
    (7 * 86400, '1-7 天', '一周之内想清楚，是正常的决策节奏'),
    (30 * 86400, '7-30 天', '拖过了三周，通常是在比价或者等另一件'),
    (90 * 86400, '30-90 天', '拖了近三个月，商品可能已经过了保鲜期'),
    (None, '超过 90 天', '长尾：心动和下单之间隔了一整个学期'),
)

# 一组样本至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条
# 样本上几个百分点的差距是噪声。
MIN_MEASURED_HISTORIES = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 待核实清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
STALLED_LIST_LIMIT = 20

# 下一步动作。收藏和预约是两件不同的事：收藏是留待观察，预约是准备成交。
# 合并成一个「有动作」会让「只是想留着」和「已经在推进」变成一回事。
NEXT_STEP_LABELS = {
    'favorite': '收藏了',
    'ordered': '下单了',
    'both': '收藏且下单了',
    'none': '没有任何下一步',
}

NEXT_STEP_HINTS = {
    'favorite': '放进心愿单但没预约——多半是在比价，或者还没下定决心',
    'ordered': '已经预约，走到了见面那一环',
    'both': '先收藏后下单，是考虑清楚了才动手的路径',
    'none': '反复回访却既没有收藏也没有预约，本模块要指出的正是这一批',
}

# 一次心动的完整轨迹。一条浏览记录配上下一步动作，一次查完就固定下来，后面
# 三个统计各读各的，谁也不重复查库——这段考虑要被三个分母分别看一遍，按记录
# 现算会让每一条记录打三遍库，而看板是一次打开全算的。
Journey = namedtuple('Journey', [
    'history', 'window_seconds', 'step', 'order', 'favorite', 'ordered_before',
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
    """考虑时长一律按天说话，不足一天的说「当天」。

    这个模块报的全是天尺度的量，报「19 小时」会让运营以为是小时级的问题，
    而去查一个按天运转的流程。
    """
    if seconds is None:
        return '—'
    if seconds < 86400:
        return '当天'
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


def _bucket_index(seconds, buckets):
    for index, (limit, _label, _hint) in enumerate(buckets):
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _bucket_rows(seconds_list, buckets):
    """一份互斥分档，每一档带上它自己的口径说明。"""
    rows = [
        {'label': label, 'hint': hint, 'count': 0}
        for _limit, label, hint in buckets
    ]
    for seconds in seconds_list:
        rows[_bucket_index(seconds, buckets)]['count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(seconds_list))
    return rows


def _fetch_histories(*, first_viewed_after=None, first_viewed_before=None):
    """按显式排序取出窗口内第一次被看到的浏览记录，带上商品与买卖双方。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。

    这里按 ``first_viewed_at`` 取窗口，不按 ``last_viewed_at``。窗口要回答的是
    「这段时间里新产生的心动考虑了多久」，所以它的起点是第一次心动；一条
    40 天前第一次被看到、昨天还在回访的记录，属于 40 天前那一批心动，不属于
    昨天。按 ``last_viewed_at`` 取会把一条横跨多个周期的长考虑塞进最后一个
    周期，而那个周期的其他记录都是当天心动的，混在一起两个数都不能用。
    """
    histories = BrowsingHistory.objects.select_related(
        'item', 'item__seller', 'user',
    ).order_by('first_viewed_at', 'id')
    if first_viewed_after is not None:
        histories = histories.filter(first_viewed_at__gte=first_viewed_after)
    if first_viewed_before is not None:
        histories = histories.filter(first_viewed_at__lte=first_viewed_before)
    return list(histories)


def _window_seconds(history):
    """从第一次心动到最近一次回访隔了多久，只看过一次的是 None。

    `view_count` 是计数器，它和两个时间戳是两个独立的事实：一次 `view_count`
    被手工改过的记录（后台可以改，测试也会改）会让「只看一次却有窗口」或
    「看了多次却没有窗口」出现。所以这里同时看两个条件——回访过一次，且
    两个时间戳都写上了。只有两个都满足才谈得上窗口，否则算 None，并让调用方
    把它单独数出来，而不是当成一个零天的窗口混进分布里。

    零和「当天」要分开：零说明两个时间戳是同一次写入的产物，压根没有第二次
    访问；「当天」说明第二次访问真的发生了，只是发生在同一天。把前者算成
    后者，会让「当天就回访」的比例虚高，而那正是本模块要报的数。
    """
    if not (history.first_viewed_at and history.last_viewed_at):
        return None
    if history.view_count < MIN_REVISITS_FOR_WINDOW:
        return None
    return max((history.last_viewed_at - history.first_viewed_at).total_seconds(), 0.0)


def _favorite_after(history):
    """这条记录的用户在第一次心动之后收藏了这件商品没有。

    只认 `first_viewed_at` 之后的收藏：一件先被收藏、后来才第一次被浏览的
    商品，收藏不是浏览的结果，不能算成心动的下一步。反过来，浏览之后很久
    才收藏的仍然算——收藏发生在这段考虑之内，它就是这段考虑的一个可能终点。

    按创建时间升序取第一条，显式排序：同一用户同一商品的收藏理论上只有一条，
    但取「第一条」这件事本身必须稳定，否则两次打开看板会给出不同的终点。
    """
    return Favorite.objects.filter(
        user_id=history.user_id, item_id=history.item_id,
        created_at__gte=history.first_viewed_at,
    ).order_by('created_at', 'id').first()


def _order_after(history):
    """这条记录的买家在第一次心动之后为这件商品下过单没有。

    同样只认 `first_viewed_at` 之后的订单。窗口的终点取第一笔订单的创建
    时刻：一个人对一件商品只可能真正买一次，第一笔才是那次心动的结果，
    后面的订单属于另一件事。

    返回 `(order, side)`，side 是 `'after'`、`'before'` 或 `'none'`。
    `'before'` 表示「先下单、后浏览」——窗口是负的。这不等于「没有下一步」，
    所以单独一个状态，不能和 none 混在一起：一个负的考虑窗口说明这条记录的
    顺序和「心动—考虑—下单」的叙事不一致，它可能是线下说好了再来平台走一遍，
    也可能是收藏之后隔了很久才点进详情页。把它算成「没有任何下一步」，会让
    这批本已成交的样本看起来像失败的样本。
    """
    after = Order.objects.filter(
        buyer_id=history.user_id, item_id=history.item_id,
        created_at__gte=history.first_viewed_at,
    ).order_by('created_at', 'id').first()
    if after:
        return after, 'after'
    before = Order.objects.filter(
        buyer_id=history.user_id, item_id=history.item_id,
        created_at__lt=history.first_viewed_at,
    ).order_by('created_at', 'id').first()
    if before:
        return before, 'before'
    return None, 'none'


def _next_step(history):
    """这段考虑的终点走到了哪一步：收藏、下单、两者都有、还是都没有。

    返回 `(step, order, order_side)`。收藏和预约必须分开报：收藏是留待观察，
    预约是准备成交。合并成一个「有动作」会让「只是想留着」和「已经在推进」
    变成一回事，而这两批样本要做的运营动作完全不同。
    """
    favorite = _favorite_after(history)
    order, order_side = _order_after(history)
    has_favorite = favorite is not None
    has_order = order is not None and order_side == 'after'
    if has_favorite and has_order:
        step = 'both'
    elif has_order:
        step = 'ordered'
    elif has_favorite:
        step = 'favorite'
    else:
        step = 'none'
    return step, order, order_side, favorite


def _journeys(histories):
    """把每条回访过的浏览记录补上它的下一步，一次查完就固定下来。

    这段考虑要被三个分母分别看一遍（窗口分布、下一步构成、下单决策时长），
    按记录现算会让每一条记录打三遍库，而看板是一次打开全算的。所以在这里
    一次性物化成 `Journey`，后面三个统计各读各的。
    """
    journeys = []
    for history in histories:
        step, order, order_side, favorite = _next_step(history)
        journeys.append(Journey(
            history=history,
            window_seconds=_window_seconds(history),
            step=step,
            order=order if order_side == 'after' else None,
            favorite=favorite,
            ordered_before=order_side == 'before',
        ))
    return journeys


def _build_revisit_window_stats(journeys):
    """从第一次心动到最近一次回访，隔了多久。

    分母只收回访过的记录。只看过一次的记录没有窗口可言，它单独数出来，
    是「看了就走」的规模，也是前者的对照组：两者放在一起，才能回答「这个
    平台上的心动，通常是想多久的」。

    窗口算不出来（两个时间戳缺一个）的记录在这一节被剔除，不计入分档也不
    计入分母：它连「有没有第二次访问」都无法确认，把它当成零天的窗口混进
    分布，会让「当天就回访」的比例虚高。
    """
    measured = [j for j in journeys if j.window_seconds is not None]
    seconds_list = [j.window_seconds for j in measured]
    return {
        'measured_count': len(seconds_list),
        'bucket_rows': _bucket_rows(seconds_list, WINDOW_BUCKETS),
        'average_label': _format_days(_mean(seconds_list)),
        'median_label': _format_days(_percentile(seconds_list, 0.5)),
        'p90_label': _format_days(_percentile(seconds_list, 0.9)),
        'fastest_label': _format_days(min(seconds_list)) if seconds_list else '—',
        'slowest_label': _format_days(max(seconds_list)) if seconds_list else '—',
        'same_day_count': sum(1 for s in seconds_list if s < 86400),
        'over_week_count': sum(1 for s in seconds_list if s >= 7 * 86400),
        'over_two_weeks_count': sum(1 for s in seconds_list if s >= 14 * 86400),
        # 单看一次占比由调用方在拿到 single_view 的总数之后回填：它要拿两堆合起来
        # 做分母，而这一节只看得到回访过的那一堆。
        'single_view_share': 0,
        'is_small_sample': len(seconds_list) < MIN_MEASURED_HISTORIES,
        'min_measured_histories': MIN_MEASURED_HISTORIES,
    }


def _build_next_step_stats(journeys):
    """反复回访之后，有没有走到下一步。

    分母是回访过的记录。只看过一次的记录不进这一列：它连窗口都没有，问它
    「之后做了什么」没有意义——一次心动之后没有再回访，本身就是一种回答，
    而它已经由上一节的单看一次数出来了。

    收藏和下单分开报，先下单后浏览的订单单独一列：它的考虑窗口是负的，
    算进「没有任何下一步」会让这批本已成交的样本看起来像失败的样本。
    """
    counts = {key: 0 for key in NEXT_STEP_LABELS}
    total = 0
    ordered_before = 0
    for journey in journeys:
        if journey.ordered_before:
            ordered_before += 1
            counts['none'] += 1
            total += 1
            continue
        counts[journey.step] += 1
        total += 1
    rows = [
        {
            'step': step,
            'label': NEXT_STEP_LABELS[step],
            'count': counts[step],
            'share': _percent(counts[step], total),
            'hint': NEXT_STEP_HINTS[step],
        }
        for step in ('none', 'favorite', 'ordered', 'both')
    ]
    return {
        'measured_total': total,
        'ordered_before_count': ordered_before,
        'none_count': counts['none'],
        'none_share': _percent(counts['none'], total),
        'favorite_count': counts['favorite'],
        'ordered_count': counts['ordered'],
        'both_count': counts['both'],
        'acted_count': total - counts['none'],
        'acted_share': _percent(total - counts['none'], total),
        'rows': rows,
        'has_direction': total >= MIN_MEASURED_HISTORIES,
        'min_measured_histories': MIN_MEASURED_HISTORIES,
    }


def _build_decision_stats(journeys):
    """下单的人，从第一次心动到下单拖了多久。

    分母是「先浏览、后下单」的记录。窗口从 `first_viewed_at` 算到
    `Order.created_at`。先下单后浏览的记录不进分母——它的窗口是负的，而一个
    负的考虑窗口说明这条记录的顺序和「心动—考虑—下单」的叙事不一致，单独
    数出来但不混。

    分档比回访窗口粗，以周为单位：从心动到掏钱中间还隔着一整套预约、见面、
    议价，它天然比「回访」长。用同一套天分档会把绝大多数样本堆在最后一档，
    那张表就等于没分。
    """
    seconds_list = []
    for journey in journeys:
        if journey.step in ('ordered', 'both') and journey.order is not None:
            seconds = max(
                (journey.order.created_at - journey.history.first_viewed_at
                 ).total_seconds(),
                0.0,
            )
            seconds_list.append(seconds)
    return {
        'measured_count': len(seconds_list),
        'bucket_rows': _bucket_rows(seconds_list, DECISION_BUCKETS),
        'average_label': _format_days(_mean(seconds_list)),
        'median_label': _format_days(_percentile(seconds_list, 0.5)),
        'p90_label': _format_days(_percentile(seconds_list, 0.9)),
        'fastest_label': _format_days(min(seconds_list)) if seconds_list else '—',
        'slowest_label': _format_days(max(seconds_list)) if seconds_list else '—',
        'same_day_count': sum(1 for s in seconds_list if s < 86400),
        'over_month_count': sum(1 for s in seconds_list if s >= 30 * 86400),
        'is_small_sample': len(seconds_list) < MIN_MEASURED_HISTORIES,
        'min_measured_histories': MIN_MEASURED_HISTORIES,
    }


def _item_status_label(item):
    return {
        'available': '在售',
        'reserved': '已预约',
        'completed': '已完成',
        'cancelled': '已下架',
    }.get(item.status, item.status)


def _build_stalled_rows(journeys):
    """想了很久却没有下一步的记录，逐条列出来。

    这是本模块唯一一份「现在去核实」用得上的清单。挑出来的口径是：窗口已经
    拖过一周、且至今没有任何下一步动作。一周是本模块的天分档里「正常节奏」
    的上界——三天之内想清楚是常见的，等满一周之内还在比价也说得过去，超过
    一周仍然既没有收藏也没有预约，才是运营该看一眼的。

    每行带上商品当前状态，因为「这件商品还在不在架上」决定了这条记录还能不能
    被补救：一件已经售出或下架的商品，再提醒也没有意义；一件还在架上、还被
    反复回访的商品，才值得运营去想它是价格问题还是描述问题。

    不报「价格太高」也不报「描述不清」：模块只报事实——想了多久、有没有
    下一步、商品现在什么状态。为什么没行动，平台没有能力区分价格、描述、
    成色、和买家自己拖，也就不该替任何一种下结论。
    """
    rows = []
    for journey in journeys:
        seconds = journey.window_seconds
        if seconds is None or seconds < 7 * 86400:
            continue
        if journey.step != 'none':
            continue
        history = journey.history
        item = history.item
        rows.append({
            'item_title': item.title,
            'item_id': item.id,
            'buyer': history.user.username,
            'seller': item.seller.username if item.seller_id else '—',
            'view_count': history.view_count,
            'window_label': _format_days(seconds),
            'window_seconds': seconds,
            'first_viewed_label': timezone.localtime(history.first_viewed_at).strftime('%m-%d'),
            'last_viewed_label': timezone.localtime(history.last_viewed_at).strftime('%m-%d'),
            'item_status': item.status,
            'item_status_label': _item_status_label(item),
            'item_price': item.price,
            'is_still_available': item.status == 'available',
            'has_favorite': journey.favorite is not None,
        })
    # 想得最久的排在前面：清单是给「现在去核实」用的，越久的越该先被看见。
    rows.sort(key=lambda row: (-row['window_seconds'], row['item_title']))
    return rows


def _summary(window_stats, next_step_stats, decision_stats, stalled_rows,
             single_view_count, revisit_count, journey_total, ordered_before):
    """一段话把三个问题说完，样本不够的地方当场说明只作参考。"""
    if not journey_total:
        return '当前周期还没有新的商品浏览记录，考虑窗口统计暂无可核对的样本。'

    parts = []
    if window_stats['measured_count']:
        parts.append(
            f"周期内 {revisit_count} 条浏览记录至少回访过一次，考虑窗口中位 "
            f"{window_stats['median_label']}、P90 {window_stats['p90_label']}"
        )
        if window_stats['over_two_weeks_count']:
            parts.append(f"，{window_stats['over_two_weeks_count']} 条拖过了整整两周")
        if single_view_count:
            parts.append(
                f"；另有 {single_view_count} 条只看了一次"
                f"（占 {window_stats['single_view_share']}%），没有窗口可言，单独数"
            )
    else:
        parts.append(
            f"周期内 {journey_total} 条浏览记录都只看了一次，没有人回访过，"
            f"考虑窗口统计暂无可分档的样本"
        )

    if next_step_stats['measured_total']:
        parts.append(
            f"。回访过的记录里，{next_step_stats['none_count']} 条既没有收藏也没有下单"
            f"（{next_step_stats['none_share']}%）"
        )
        if next_step_stats['has_direction'] and next_step_stats['none_count']:
            parts.append('——反复看却不行动，可能是价格、描述或成色，也可能只是买家自己拖')
        elif not next_step_stats['has_direction']:
            parts.append(
                f"（不足 {next_step_stats['min_measured_histories']} 条，占比只作参考）"
            )
        if next_step_stats['favorite_count']:
            parts.append(f"，{next_step_stats['favorite_count']} 条只收藏了没预约")
        if next_step_stats['ordered_count'] + next_step_stats['both_count']:
            parts.append(
                f"，{next_step_stats['ordered_count'] + next_step_stats['both_count']} 条走到了下单"
            )

    if decision_stats['measured_count']:
        parts.append(
            f"。从第一次心动到下单，中位 {decision_stats['median_label']}、"
            f"P90 {decision_stats['p90_label']}"
        )
        if decision_stats['over_month_count']:
            parts.append(f"，{decision_stats['over_month_count']} 条拖过了一个月")

    if stalled_rows:
        parts.append(
            f"。{len(stalled_rows)} 条想了超过一周、至今没有任何下一步，已逐条列在下面"
        )
    if ordered_before:
        parts.append(
            f"；另有 {ordered_before} 条是先下单、后浏览，考虑窗口是负的，"
            f"不计入任何等待分布"
        )
    if window_stats['is_small_sample'] and window_stats['measured_count']:
        parts.append(
            f"。周期内可分档的回访记录不足 {window_stats['min_measured_histories']} 条，"
            f"占比只作参考"
        )
    return ''.join(parts)


def build_first_viewed(days=30, now=None):
    '''统计从第一次心动到下一步隔了多久，只报告不改写任何记录。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    histories = _fetch_histories(
        first_viewed_after=start, first_viewed_before=now,
    )

    # 回访过和只看一次分开。分拣只看 `view_count`，不看时间戳——时间戳的差在
    # `_window_seconds` 里再看一次，那里同时要求两个时间戳都写上了。
    revisited = [h for h in histories if h.view_count >= MIN_REVISITS_FOR_WINDOW]
    single_view = [h for h in histories if h.view_count < MIN_REVISITS_FOR_WINDOW]

    journeys = _journeys(revisited)
    window_stats = _build_revisit_window_stats(journeys)
    # 单看一次的占比要把两堆合起来做分母：它回答的是「这个平台上的心动，
    # 有多大比例看一眼就走」，只看回访过的那一堆会让这个比例永远是零。
    window_stats['single_view_share'] = _percent(len(single_view), len(histories))
    next_step_stats = _build_next_step_stats(journeys)
    decision_stats = _build_decision_stats(journeys)
    stalled_rows = _build_stalled_rows(journeys)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(histories),
        'summary': _summary(
            window_stats, next_step_stats, decision_stats, stalled_rows,
            len(single_view), len(revisited), len(histories),
            next_step_stats['ordered_before_count'],
        ),
        'window_stats': window_stats,
        'next_step_stats': next_step_stats,
        'decision_stats': decision_stats,
        'stalled_rows': stalled_rows,
        'stalled_row_limit': STALLED_LIST_LIMIT,
        'revisit_count': len(revisited),
        'single_view_count': len(single_view),
        'history_total': len(histories),
        'next_step_labels': NEXT_STEP_LABELS,
    }

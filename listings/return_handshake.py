# -*- coding: utf-8 -*-
"""归还登记的那两次握手，第一次被运营读到。

借用的归还是这套系统里唯一一个「两个人各自点一下、顺序完全由双方自己决定」的闭环。
买家点「登记我已归还」写 ``buyer_returned_at``，卖家点「确认已收到归还物品」写
``seller_returned_at``，两个字段都写上，订单才从 ``borrowed`` 变成 ``returned``，
商品才重新开放借用，催收阶梯才被关闭。而读取路径只有两处：订单详情页把它们显示给
双方本人看，后台把它们列给管理员。运营侧一条统计都没有。

已经有四个模块在读借用的逾期：``borrow_risk`` 按分类和押金分层，``borrow_rhythm``
把它和学期阶段交叉，``borrow_escalation`` 管三级催收阶梯，``governance_sla`` 管治理
队列时效。它们全都从「东西还没回来」这一侧切进去。而「还没回来」只是问题的一半，
另一半是东西已经回来了、或者借用方说已经回来了，而另一方没有点确认。这个状态在
数据里完全可见——一笔 ``borrowed`` 订单带着一个已经填了的 ``buyer_returned_at``——
但没有任何一个模块在看它。

这不是一个抽象的缺口，它对应两类方向完全相反的运营动作。第一类是东西回来了、卖家
忘了点确认：订单卡在 ``borrowed``，商品一直是 ``reserved``，催收阶梯可能已经升到
三级、可能已经进了治理队列，而东西已经躺在卖家手里了；``close_escalation`` 只在双方
都确认的那一刻才跑，所以这条记录永远不会自己关掉。第二类是东西真的没回来，而系统
已经不再说话：``process_borrow_due_notifications`` 只发一次提醒、一次逾期通知，
``borrow_escalation`` 的文档自己写着「a single notice is not a follow-up
process」，而那之后也没有第二个动作。

这个模块把那两次握手读出来，只报告，不改写任何一笔订单。几处口径上的取舍比数字本身
更要紧，它们都在下面的注释里。

分母只收「周期内创建、且已经离开 ``borrowed``」的借用订单。已经离开的意思是走到了
``returned``，或者带着一次真实的借用记录被取消；从来没进入过 ``borrowed`` 就被取消的
订单，握手压根没开始，不算进来。还挂在 ``borrowed`` 的订单是右删失样本——握手还没
结束，根本谈不上有没有完成——把它算进分母会让完成率凭空变小，和 ``handoff_code_usage``、
``meeting_attendance`` 是同一个道理，这部分单独数出来，不丢也不混。

等待时长一律按天分档，不按小时。一次归还等待的时间尺度是天，报「平均等了 26 小时」
对一个按周借用、按周归还的场景没有意义。已登记未确认的等待时长从先登记那一刻算到
现在，而「现在」不属于任何统计周期，所以它按订单的一生累计，不按周期切——把它按
周期切开，会让一个跨越周期边界的等待被算成两段短的，而那正是最不该被切开的样本。

「逾期未归还」和「已登记未确认」必须分开报。这两类都表现为「订单还没结束」，但运营
动作完全相反：前者是催收阶梯该管的事，后者是「去提醒另一方点一下」的事。把它们合成
一个「进行中的借用」数字，会让人以为问题只有一种。

模块报「登记之后订单再也没有动过」的订单，不报「卡住」的订单——前者是 ``OrderEvent``
里可以直接数出来的事实，后者是一个判断。报了前者又报了「此后再无事件」的清单，比
一个「疑似僵住」的徽标有用得多：前者可以被核实，后者只能被相信。
"""

from datetime import time as dt_time, timedelta

from django.db.models import Count
from django.utils import timezone

from .models import DeliveryConfirmation, Order, OrderEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 借用中的订单状态。两个归还时间戳都只在订单处于这个状态时被写，视图里 confirm_return
# 守着这一条：状态一变，按钮就消失，两个字段从此不再变化。
BORROWED_STATUS = 'borrowed'

# 走完借用的订单状态。只有走到这里，两次握手才真的都点过了。
RETURNED_STATUS = 'returned'

# 借用结束的另一种方式。它可能发生在借用开始之前（没约成），也可能发生在借用之后
# （东西拿回来了但走的是取消而不是归还确认），所以必须回去问事件表这一笔到底有没
# 有进入过借用中。
CANCELLED_STATUS = 'cancelled'

# 等待时长分档。互斥，且按天切：一次归还等待的时间尺度是天。最后一档是尾巴，长尾
# 才是要动作的那一批，所以它单独一档而不是并进「7 天以上」里被平均掉。
WAIT_BUCKETS = (
    (86400, '当天', '登记当天对方就确认了，握手只花了不到一天'),
    (3 * 86400, '1-3 天', '一方登记之后，另一方在三天内跟上'),
    (7 * 86400, '3-7 天', '等满一周之内，还在正常借用的节奏里'),
    (14 * 86400, '7-14 天', '已经超过一周，通常意味着其中一方忘了这回事'),
    (None, '超过两周', '长尾：等了两周以上，商品和押金都还卡在这一笔上'),
)

# 一组订单至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条样本上
# 几个百分点的差距是噪声。
MIN_MEASURED_ORDERS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 待核实清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
STALLED_LIST_LIMIT = 20

# 先登记的一方是谁。借用方先登记是正常路径；发布者先登记意味着他先收到了东西、而
# 借用方还没点，这个顺序在借用的语境里是反常的——它通常说明双方对「归还」的定义
# 不一致（东西放下了但没交接，或者放下了一个不一样的东西）。
FIRST_REGISTERED_LABELS = {
    'buyer': '借用方先登记',
    'seller': '发布者先登记',
    'both': '双方同时登记',
}


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


def _format_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} 分钟'
    if seconds < 3600:
        return f'{seconds // 60} 分钟'
    if seconds < 86400:
        return f'{seconds // 3600} 小时'
    return f'{seconds // 86400} 天'


def _format_days(seconds):
    """等待时长一律按天说话，不足一天的说「当天」。

    这个模块报的全是天尺度的量，报「19 小时」会让运营以为是小时级的问题，
    而去查一个按周运转的流程。
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


def _bucket_rows(seconds_list):
    """一份互斥分档，每一档带上它自己的口径说明。"""
    rows = [
        {'label': label, 'hint': hint, 'count': 0}
        for _limit, label, hint in WAIT_BUCKETS
    ]
    for seconds in seconds_list:
        rows[_bucket_index(seconds, WAIT_BUCKETS)]['count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(seconds_list))
    return rows


def _fetch_borrow_orders(*, created_after=None, created_before=None):
    """按显式排序取出窗口内创建的借用订单，带上交付确认记录。

    只按 ``item__trade_mode`` 筛借用：商品是不是借用、和这笔订单后来走到哪一步，
    是两个独立的事实。一笔借用商品被卖掉、或者发布者改了交易方式，都不影响它在
    借用期间真的发生过两次握手。反过来，一笔出售订单根本不经过 ``borrowed``，
    两个归还时间戳永远为空，把它算进分母只会稀释完成率。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。
    """
    orders = Order.objects.select_related(
        'item', 'buyer', 'seller', 'delivery_confirmation',
    ).filter(
        item__trade_mode='borrow',
    ).order_by('created_at', 'id')
    if created_after is not None:
        orders = orders.filter(created_at__gte=created_after)
    if created_before is not None:
        orders = orders.filter(created_at__lte=created_before)
    return list(orders)


def _ever_borrowed(order_ids):
    """这些订单里，哪些真的进入过 ``borrowed``，返回各自第一次进入的时刻。

    一笔取消的借用订单可能是两种完全不同的情况：下单之后没约成，取消在借用开始
    之前；或者东西已经拿走、已经逾期、走的是取消而不是归还确认。前者的两次握手
    压根没开始，后者发生过一次不完整的借用。不区分这两者，取消的订单会被当成
    同一类处理，而它们的运营含义正相反。

    事件表里混着一批 ``from_status == to_status`` 的记录——签到、约定交付时间、
    发起争议，都是真动作但不是阶段切换。拿它们当进入时刻，会把一笔一直在本阶段里
    打转的订单读成一笔刚进来的订单，这里和订单阶段流面板、``handoff_code_usage``
    用的是同一条规则。
    """
    events = OrderEvent.objects.filter(
        order_id__in=list(order_ids),
    ).values('order_id', 'from_status', 'to_status', 'created_at').order_by(
        'order_id', 'created_at', 'id',
    )
    entered_at = {}
    for event in events:
        if event['to_status'] != BORROWED_STATUS:
            continue
        if event['from_status'] and event['from_status'] == event['to_status']:
            continue
        entered_at.setdefault(event['order_id'], event['created_at'])
    return entered_at


def _last_event_at_by_order(order_ids):
    """每笔订单最后一个事件的发生时刻，没有事件就是 None。

    「登记之后订单再也没有动过」是一个可以直接数出来的事实，而「卡住」是一个判断。
    ``OrderEvent`` 记了每一次状态流转，所以前者查得到；后者要平台替运营猜这一笔
    到底是忘了、是争议中、还是双方已经在线下解决了——平台没有能力区分，也就不该
    替运营下这个结论。
    """
    if not order_ids:
        return {}
    latest = {}
    rows = OrderEvent.objects.filter(
        order_id__in=list(order_ids),
    ).values('order_id', 'created_at', 'id').order_by('order_id', 'created_at', 'id')
    for row in rows:
        latest[row['order_id']] = row['created_at']
    return latest


def _confirmation_of(order):
    """这笔订单的交付确认记录，没有就 None。

    ``buyer_returned_at`` 和 ``seller_returned_at`` 住在 ``DeliveryConfirmation`` 上，
    而这张表是 ``get_or_create`` 出来的——一笔从没生成过确认码、也从没点过归还的
    订单，压根没有这一行。所以这两个字段为空有两种完全不同的含义：没有这一行，
    和这一行存在但两个字段都空。本模块只关心字段本身，两者一视同仁，都不算登记过。
    """
    try:
        return order.delivery_confirmation
    except DeliveryConfirmation.DoesNotExist:
        return None


def _return_side(order, confirmation):
    """这笔订单的两次握手各自登记了没有，以及谁先登记。

    返回 ``(buyer_at, seller_at, first_side)``。``first_side`` 取先写上时间戳的
    一方；两边都空时是 None，两边同时写上时是 ``'both'``。同时登记是可能的——
    双方各自点一下，两个请求可以落在同一秒内。
    """
    buyer_at = confirmation.buyer_returned_at if confirmation else None
    seller_at = confirmation.seller_returned_at if confirmation else None
    if buyer_at and seller_at:
        if buyer_at == seller_at:
            first_side = 'both'
        elif buyer_at < seller_at:
            first_side = 'buyer'
        else:
            first_side = 'seller'
    elif buyer_at:
        first_side = 'buyer'
    elif seller_at:
        first_side = 'seller'
    else:
        first_side = None
    return buyer_at, seller_at, first_side


def _wait_seconds(order, buyer_at, seller_at, now):
    """先登记到后登记隔了多久，只有双方都登记了才算得出来。

    一方登记、另一方没登记的订单没有「等待多久」可言——它只有「等了多久还没
    等到」，那是另一问，由 ``_pending_wait_seconds`` 回答。把两者混在一个间隔表里，
    等于把已经结束的握手和还没结束的握手平均成一个数字。
    """
    if not (buyer_at and seller_at):
        return None
    return max((seller_at - buyer_at).total_seconds(), 0.0)


def _pending_wait_seconds(order, buyer_at, seller_at, now):
    """一方登记了、另一方没有，从先登记那一刻算到现在等了多久。

    这一列没有上界，所以它报分布而不是平均值：一个等了四十天的订单会被一个
    平均值藏起来，而它恰恰是最该被看见的那一条。它也不按统计周期切——「现在」
    不属于任何统计周期，按周期切会把一个跨越边界的等待算成两段短的。
    """
    registered_at = buyer_at or seller_at
    if not registered_at:
        return None
    return max((now - registered_at).total_seconds(), 0.0)


def _is_overdue(order, now):
    """这笔借用是不是已经过了预计归还时间，而双方都没登记归还。

    逾期未归还是催收阶梯该管的事，本模块只报数、不改阶梯、不提前催收。
    """
    return bool(
        order.return_due_at
        and order.return_due_at < now
        and not order.returned_at
    )


def _classify(orders, ever_borrowed, now):
    """把窗口内的借用订单分成四堆，每堆各自回答一个问题。

    四堆互斥且完备，这是本模块的地基：

    - ``concluded``：进入过借用、且已经离开（归还了，或者带着真实借用记录被取消）。
      握手完成率的分母。取消的借用订单留着——一次东西已经拿走、最后走取消的订单，
      仍然发生过握手，把它剔掉会让完成率凭空变好，而那正是最该被看见的那批。
    - ``still_borrowed``：还挂在 ``borrowed``。握手还没结束，是右删失样本，单独数。
    - ``cancelled_before``：从没进入过借用就被取消。握手压根没开始，不算进任何分母，
      只报个数，否则一笔没约成的取消会被读成「一次失败的握手」。
    - ``no_borrow``：走到 ``returned`` 却查不到进入 ``borrowed`` 的事件。这不该发生，
      但真发生了就得单独数出来——静默忽略它会让分母悄悄变小，而没人知道为什么。
    """
    concluded, still_borrowed, cancelled_before, no_borrow = [], [], [], []
    for order in orders:
        if order.status == RETURNED_STATUS:
            if order.id in ever_borrowed:
                concluded.append(order)
            else:
                no_borrow.append(order)
        elif order.status == BORROWED_STATUS:
            still_borrowed.append(order)
        elif order.status == CANCELLED_STATUS:
            if order.id in ever_borrowed:
                concluded.append(order)
            else:
                cancelled_before.append(order)
        else:
            # pending / confirmed / meeting / completed：这些都不是借用的归宿。
            # 一笔借用商品最后以「交易完成」收场是发布者改了交易方式，握手没发生过。
            cancelled_before.append(order)
    return concluded, still_borrowed, cancelled_before, no_borrow


def _build_completion_stats(concluded):
    """借用出去之后，有多少走完了归还握手。

    分母只收已经离开 ``borrowed`` 的订单。一笔还挂在 ``borrowed`` 的订单，握手还没
    结束，根本谈不上有没有完成——把它算进分母会让完成率凭空变小，这是右删失样本，
    和 ``handoff_code_usage`` 的当面交付生成率、``meeting_attendance`` 的到场率是
    同一个道理。还没离开的订单单独数出来，不丢也不混。

    取消的订单留在分母里，而且它和「归还」必须分开报：一次走取消收场的借用，两次
    握手可能一次都没点过。把取消并进「已完成握手」会让完成率虚高，把它整个剔掉又会让
    分母只剩顺利的那批。两个数都给，运营自己判断。
    """
    completed, incomplete, cancelled = [], [], []
    for order in concluded:
        confirmation = _confirmation_of(order)
        if order.status == RETURNED_STATUS:
            completed.append(order)
        else:
            incomplete.append(order)
            cancelled.append(order)
    # 双方都登记的订单里，有多少是「走到了 returned」收场的。一笔双方都登记了、却仍然
    # 卡在 borrowed 的订单不该存在（视图在两者都写上那一刻就改状态），真有就单独数。
    both_registered = sum(
        1 for order in concluded
        if _return_side(order, _confirmation_of(order))[2] is not None
    )
    return {
        'concluded_total': len(concluded),
        'returned_count': len(completed),
        'cancelled_count': len(cancelled),
        'incomplete_count': len(incomplete),
        'completion_rate': _percent(len(completed), len(concluded)),
        # 双方都登记过归还的订单数。它和 returned_count 应当相等；不相等说明有订单
        # 两次握手都点了却没有走到 returned，那是数据里的一处矛盾，值得单独看一眼。
        'both_registered_count': both_registered,
        'is_small_sample': len(concluded) < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_first_registered_stats(concluded, still_borrowed):
    """谁先登记：借用方先登记是正常路径，发布者先登记是反常的。

    这个维度不能从等待间隔里推出来，而且它决定了两件事：现在在等哪一方，以及先登记
    那一刻到第二个时间戳之间的间隔归谁。把两种顺序混在一张间隔表里，等于把「等对方
    的买家」和「等对方的卖家」平均成一个数字，而这两群人的行为完全不一样。

    发布者先登记通常说明双方对「归还」的定义不一致——东西放下了但没交接，或者放下的
    是一个不一样的东西。所以它单独一列，不并进「完成了」里被稀释掉。
    """
    counts = {'buyer': 0, 'seller': 0, 'both': 0, 'none': 0}
    total = 0
    for order in list(concluded) + list(still_borrowed):
        confirmation = _confirmation_of(order)
        first_side = _return_side(order, confirmation)[2]
        if first_side is None:
            counts['none'] += 1
            continue
        counts[first_side] += 1
        total += 1
    rows = [
        {
            'side': side,
            'label': FIRST_REGISTERED_LABELS[side],
            'count': counts[side],
            'share': _percent(counts[side], total),
        }
        for side in ('buyer', 'seller', 'both')
    ]
    seller_first = counts['seller'] + counts['both']
    return {
        'measured_total': total,
        'buyer_first_count': counts['buyer'],
        'seller_first_count': counts['seller'],
        'both_count': counts['both'],
        'unregistered_count': counts['none'],
        'seller_first_share': _percent(seller_first, total),
        'buyer_first_share': _percent(counts['buyer'], total),
        'rows': rows,
        # 发布者先登记是反常路径，样本够的时候单独点出来，样本不够只报数。
        'has_direction': total >= MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_wait_stats(concluded, now):
    """登记过归还的订单，多久等到对方确认。

    只收双方都登记了的订单：等待有终点才谈得上等了多久。一方登记、另一方一直没登记
    的订单不进来——它没有「等待多久」可言，它只有「等了多久还没等到」。
    """
    seconds_list = []
    for order in concluded:
        confirmation = _confirmation_of(order)
        buyer_at, seller_at, _ = _return_side(order, confirmation)
        seconds = _wait_seconds(order, buyer_at, seller_at, now)
        if seconds is not None:
            seconds_list.append(seconds)
    return {
        'measured_count': len(seconds_list),
        'bucket_rows': _bucket_rows(seconds_list),
        'average_label': _format_days(_mean(seconds_list)),
        'median_label': _format_days(_percentile(seconds_list, 0.5)),
        'p90_label': _format_days(_percentile(seconds_list, 0.9)),
        'fastest_label': _format_days(min(seconds_list)) if seconds_list else '—',
        'slowest_label': _format_days(max(seconds_list)) if seconds_list else '—',
        'same_day_count': sum(1 for s in seconds_list if s < 86400),
        'over_week_count': sum(1 for s in seconds_list if s >= 7 * 86400),
        'is_small_sample': len(seconds_list) < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_stalled_stats(still_borrowed, now):
    """卡在中间态的订单，现在等了多久——以及它们是不是就此没了动静。

    这一节把两类状态分开报，因为它们的运营动作完全相反：

    - 逾期未归还：``return_due_at`` 已过，双方都没登记。这是催收阶梯该管的事，
      看板只报数，不改阶梯、不提前催收。
    - 已登记未确认：一方已登记，另一方没有。这是「去提醒另一方点一下」的事，
      和催收无关——东西很可能已经回来了。

    把它们合成一个「进行中的借用」数字，会让人以为问题只有一种。而真实情况是，一批
    东西已经回来了只是没人点按钮，另一批东西真的不在，治理工作台要做的动作一个都不一样。
    """
    overdue, registered = [], []
    for order in still_borrowed:
        confirmation = _confirmation_of(order)
        buyer_at, seller_at, first_side = _return_side(order, confirmation)
        if first_side is None:
            if _is_overdue(order, now):
                overdue.append(order)
            continue
        registered.append(order)

    registered_seconds = [
        _pending_wait_seconds(order, *_return_side(order, _confirmation_of(order))[:2], now)
        for order in registered
    ]
    registered_seconds = [s for s in registered_seconds if s is not None]

    return {
        'overdue_count': len(overdue),
        'registered_count': len(registered),
        'bucket_rows': _bucket_rows(registered_seconds),
        'average_label': _format_days(_mean(registered_seconds)),
        'median_label': _format_days(_percentile(registered_seconds, 0.5)),
        'slowest_label': _format_days(max(registered_seconds)) if registered_seconds else '—',
        'over_week_count': sum(1 for s in registered_seconds if s >= 7 * 86400),
        'over_two_weeks_count': sum(1 for s in registered_seconds if s >= 14 * 86400),
        'is_small_sample': len(registered) < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_stalled_rows(still_borrowed, last_event_at, now):
    """已登记未确认的订单逐条列出来，每条都带上「登记之后还有没有动静」。

    这是本模块唯一一份「现在去核实」用得上的清单：一方已经登记、另一方没有，而运营
    要知道的不只是「等多久」，还有「等的时候有没有人说过话」。一条登记之后双方又提交
    了争议、又补了证据的订单，和一条登记之后订单再也没动过的订单，要做的动作完全
    不同——前者去治理工作台看争议，后者去提醒另一方点一下按钮。

    不报「卡住」，报「登记之后没有任何事件」：前者是一个判断，后者是一个可以从
    ``OrderEvent`` 里数出来的事实。
    """
    rows = []
    for order in still_borrowed:
        confirmation = _confirmation_of(order)
        buyer_at, seller_at, first_side = _return_side(order, confirmation)
        if first_side is None:
            continue
        registered_at = buyer_at or seller_at
        pending_seconds = _pending_wait_seconds(order, buyer_at, seller_at, now)
        last_at = last_event_at.get(order.id)
        rows.append({
            'item_title': order.item.title,
            'buyer': order.buyer.username,
            'seller': order.seller.username,
            'first_side': first_side,
            'first_label': FIRST_REGISTERED_LABELS.get(first_side, '双方同时登记'),
            'waiting_for': '卖家确认' if first_side == 'buyer' else '买家登记',
            'buyer_registered': bool(buyer_at),
            'seller_registered': bool(seller_at),
            'waiting_label': _format_days(pending_seconds),
            'waiting_seconds': pending_seconds,
            'is_overdue': _is_overdue(order, now),
            'due_label': (
                timezone.localtime(order.return_due_at).strftime('%m-%d %H:%M')
                if order.return_due_at else '—'
            ),
            # 登记之后订单还有没有动过。None 表示这一笔连事件都没有——那本身就值得
            # 被看见：一次从没留下任何事件却停在借用中的订单，比一条僵住的订单更怪。
            'last_event_at': last_at,
            'moved_after_register': bool(last_at and last_at > registered_at),
            'last_event_label': (
                timezone.localtime(last_at).strftime('%m-%d %H:%M') if last_at else '无事件'
            ),
        })
    # 等得最久的排在前面：清单是给「现在去核实」用的，越久的越该先被看见。
    rows.sort(key=lambda row: (-row['waiting_seconds'], row['item_title']))
    return rows


def _build_cancelled_rows(cancelled_before):
    """从没进入过借用就被取消的订单，单独列出来。

    这些订单的两次握手压根没开始，所以它们不进任何分母。但把它们整个丢掉会让
    「周期内借用订单总数」这个数变小，而运营会拿它去对别的表。列出来，并说清楚
    它们为什么不在分母里。
    """
    rows = []
    for order in cancelled_before:
        rows.append({
            'item_title': order.item.title,
            'buyer': order.buyer.username,
            'seller': order.seller.username,
            'status_label': order.get_status_display(),
        })
    return rows


def _summary(completion, first_registered, wait_stats, stalled_stats, stalled_rows,
             still_borrowed, cancelled_before, no_borrow):
    """一段话把这个模块的读数讲完，样本不足的地方明说。

    中间态那一段不放在「有完成样本才说」的分支里。一个周期完全可能一笔借用都没走完、
    却有一批卡在借用中的订单——而那正是本模块最该说话的时候：完成率没有样本，而
    「东西回来了没人点确认」是全模块唯一活着的信号。为了句式整齐把它和完成率绑在
    同一个条件里，等于在最需要它的那个周期里把它藏起来。
    """
    parts = []
    if completion['concluded_total']:
        parts.append(
            f"周期内 {completion['concluded_total']} 笔借用走完了流程，"
            f"{completion['returned_count']} 笔双方都登记了归还"
            f"（{completion['completion_rate']}%）"
        )
        if completion['cancelled_count']:
            parts.append(f"，{completion['cancelled_count']} 笔带着真实借用记录被取消")
        if completion['both_registered_count'] != completion['returned_count']:
            parts.append(
                f"；其中 {completion['both_registered_count']} 笔两次握手都点了，"
                f"和归还数不一致，这处矛盾值得单独看一眼"
            )
    else:
        parts.append('当前周期还没有走完借用的订单，归还握手统计暂无可核对的样本。')

    if still_borrowed:
        parts.append(
            f"另有 {len(still_borrowed)} 笔还挂在借用中——握手还没结束，"
            f"它们不计入完成率分母，单独数在下面。"
        )

    if first_registered['measured_total']:
        parts.append(
            f"登记过归还的订单里，{first_registered['buyer_first_count']} 笔是借用方先登记，"
            f"{first_registered['seller_first_count']} 笔是发布者先登记"
        )
        if first_registered['has_direction'] and first_registered['seller_first_count']:
            parts.append('——发布者先登记是反常路径，通常说明双方对「归还」的定义不一致')
        elif not first_registered['has_direction']:
            parts.append(
                f"（不足 {first_registered['min_measured_orders']} 笔，"
                f"先登记方向只作参考）"
            )

    if wait_stats['measured_count']:
        parts.append(
            f"，双方都登记的等待中位 {wait_stats['median_label']}、"
            f"P90 {wait_stats['p90_label']}"
        )
        if wait_stats['over_week_count']:
            parts.append(f"，{wait_stats['over_week_count']} 笔等满了整整一周")

    if stalled_stats['overdue_count'] or stalled_stats['registered_count']:
        parts.append('。当前仍挂在借用中的订单里：')
        if stalled_stats['overdue_count']:
            parts.append(
                f"{stalled_stats['overdue_count']} 笔已过预计归还时间而双方都没登记"
                f"（催收阶梯该管的那一类）"
            )
        if stalled_stats['registered_count']:
            parts.append(
                f"，{stalled_stats['registered_count']} 笔一方已登记、另一方没有"
                f"（东西可能已经回来了，该做的是提醒另一方点一下）"
            )
            if stalled_stats['over_two_weeks_count']:
                parts.append(
                    f"，其中 {stalled_stats['over_two_weeks_count']} 笔已经等了超过两周"
                )
    if cancelled_before:
        parts.append(
            f"；另有 {len(cancelled_before)} 笔从没进入过借用就被取消，"
            f"握手没开始，不计入任何分母"
        )
    if no_borrow:
        parts.append(
            f"；{len(no_borrow)} 笔状态是已归还却查不到进入借用中的事件，"
            f"这是数据里的一处矛盾，已单独列出"
        )
    if completion['is_small_sample'] and completion['concluded_total']:
        parts.append(
            f"。周期内走完借用的订单不足 {completion['min_measured_orders']} 笔，"
            f"占比只作参考"
        )
    return ''.join(parts)


def build_return_handshake(days=30, now=None):
    '''统计借用归还的两次握手，只报告不改写任何字段，也不自动推进任何一笔订单。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    orders = _fetch_borrow_orders(created_after=start, created_before=now)
    ever_borrowed = _ever_borrowed([order.id for order in orders])

    concluded, still_borrowed, cancelled_before, no_borrow = _classify(
        orders, ever_borrowed, now,
    )

    completion = _build_completion_stats(concluded)
    first_registered = _build_first_registered_stats(concluded, still_borrowed)
    wait_stats = _build_wait_stats(concluded, now)
    stalled_stats = _build_stalled_stats(still_borrowed, now)
    last_event_at = _last_event_at_by_order([order.id for order in still_borrowed])
    stalled_rows = _build_stalled_rows(still_borrowed, last_event_at, now)
    cancelled_rows = _build_cancelled_rows(cancelled_before)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(orders),
        'summary': _summary(
            completion, first_registered, wait_stats, stalled_stats, stalled_rows,
            still_borrowed, cancelled_before, no_borrow,
        ),
        'completion_stats': completion,
        'first_registered_stats': first_registered,
        'wait_stats': wait_stats,
        'stalled_stats': stalled_stats,
        'stalled_rows': stalled_rows,
        'stalled_row_limit': STALLED_LIST_LIMIT,
        'still_borrowed_count': len(still_borrowed),
        'cancelled_before_count': len(cancelled_before),
        'cancelled_rows': cancelled_rows,
        'no_borrow_count': len(no_borrow),
        'first_registered_labels': FIRST_REGISTERED_LABELS,
    }

# -*- coding: utf-8 -*-
"""在现场念出来的那六位数字，第一次被运营读到。

交付确认码是这套系统里少见的"平台自己推荐、自己记录、自己从不统计"的机制。
``DeliveryConfirmation`` 为它留了五个字段：哈希、末两位提示、生成时间、使用时间、
错误次数。买家在订单详情页点一下生成（``generate_delivery_code``），卖家在现场
核对（``confirm_delivery``）——写路径是完整的，有行锁，有五次错误锁定，有"重新
生成即作废旧码"的语义。而读取路径只有两处：后台把状态显示给管理员，订单详情页把
码显示给买家本人。运营侧一条统计都没有。

更能说明问题的是交易安全卡。``transaction_safety`` 在订单进入"待当面交付"时，如果
发现没有确认码，会明确建议"建议启用交付确认码"，并给一个"进入订单详情生成确认码"
的按钮。一个平台在向用户推荐一个自己从不统计使用情况的机制——推荐语写得很确定，
而推荐语背后没有任何一个数字。

这个模块把那五个字段读出来，只报告，不改写。它回答六个问题：进入当面交付的订单里
有多少真的生成了码；生成了有没有被用；错误尝试是怎么分布的；从生成到现场使用隔了
多久；有没有人重新生成过；以及撞到五次锁定的订单后来怎么样了。

几处口径上的取舍比数字本身更要紧。

分母只收"已经离开当面交付阶段"的订单。一个现在还挂在"待当面交付"的订单，根本还
谈不上有没有生成码——见面还没发生。把它算进分母会让生成率凭空变小，这是右删失样
本，和订单阶段流面板、"约好的那一次见面"面板是同一个道理；还没离开这个阶段的订
单单独数出来，不丢也不混。

``handoff_code_attempts`` 是最后一枚码的错误次数，不是这笔订单累计的错误次数。
买家重新生成会把 attempts 归零，于是一个现场出过问题、后来换过码的订单，在这里
看起来和一次就对的订单一模一样。所以"错误次数"这一列必须和"重新生成"那一列合起
来读，单看任何一列都会得出一个过于乐观的结论。

重新生成的次数从通知记录反推：每次生成都会给卖家发一条"买家已生成交付确认码"的
站内通知，而这条通知没有配聚合键，所以每生成一次就多一条。这个口径有一个真实的
漏洞——如果卖家关掉了"订单状态更新"这类通知，生成就不会留下这条记录。所以重新
生成的数字是下界，不是准数；本模块把它标成下界，而不是当成精确值报。也因此，
重新生成按订单的一生累计，不按统计周期切：``handoff_code_issued_at`` 每次生成都被
覆盖，早期的生成时间在数据里已经不存在了，想按周期切也切不出来。

用没用到确认码、和订单最终有没有完成，只做对照，不给因果。生成码的订单更可能完
成，很可能只是因为愿意生成码的双方本来就是更认真的那批人；平台没有能力把这两件
事分开，也就不该把对照讲成结论。两组样本各自要过门槛，差值落在噪声带内不给方向。
"""

from datetime import time as dt_time, timedelta

from django.db.models import Count
from django.utils import timezone

from .models import DeliveryConfirmation, Notification, Order, OrderEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 确认码只在这个阶段生成、也只在这个阶段被核对。视图里两个入口都守着这一条。
MEETING_STATUS = 'meeting'

# 已经走完当面交付的阶段。订单只有从"待当面交付"走到这些状态，才谈得上"生成了
# 没有、用了没有"；还挂在本阶段的订单是右删失样本，单独数，不进分母。
CONCLUDED_STATUSES = ('completed', 'borrowed', 'returned', 'cancelled')

# 其中真正做成了的那些。取消不是失败：它可能只是双方没约成，和确认码用没用无关。
SUCCESS_STATUSES = ('completed', 'borrowed', 'returned')

# 五次错误即锁定，这是视图里写死的阈值。撞到这一档的订单单独一列，因为它意味
# 着现场发生过一次真实的沟通问题，而不是手滑打错一位。
LOCK_THRESHOLD = 5

# 错误尝试分档。每一档都互斥，且必须和"重新生成"那一列合起来读——重新生成会把
# attempts 归零，所以这一列描述的是最后一枚码，不是这笔订单的全部现场情况。
ATTEMPT_BUCKETS = (
    ('一次就对', '第一次就输对了，没有多余动作'),
    ('错 1 次后对', '手滑一位，第二次对上'),
    ('错 2 次后对', '沟通了两轮才对上'),
    ('错 3-4 次后对', '现场反复核对，最后一次机会用上了'),
    ('还没核对过', '码生成了，但没有一次核对记录'),
    ('撞到 5 次锁定', '错误次数已达上限，这枚码作废'),
)

# 生成到现场使用的间隔分档。按小时和天切，不按分钟：一枚码的意义就在"现场那一
# 下"，隔半小时和隔两小时说的是同一件事。
ISSUE_TO_USE_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '当天'),
    (259200, '隔天到 3 天'),
    (None, '3 天以上'),
)

# 一组订单至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条样本
# 上几个百分点的差距是噪声。
MIN_MEASURED_ORDERS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 待核实清单最多列这么多条。清单是给"现在去核实"用的，不是给归档用的。
LOCKED_LIST_LIMIT = 20

# 反推重新生成次数用的通知标题。必须和 generate_delivery_code 里写的那一条一致，
# 改任何一边都要同时改这里，否则这一列会静默变成 0。
GENERATION_NOTICE_TITLE = '买家已生成交付确认码'


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
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


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _fetch_concluded_orders(*, created_after=None, created_before=None):
    """按显式排序取出已经离开当面交付阶段的订单。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何"第一名"都会随机漂移。
    """
    orders = Order.objects.select_related(
        'item', 'buyer', 'seller', 'meeting_location', 'delivery_confirmation',
    ).filter(
        status__in=CONCLUDED_STATUSES,
    ).order_by('created_at', 'id')
    if created_after is not None:
        orders = orders.filter(created_at__gte=created_after)
    if created_before is not None:
        orders = orders.filter(created_at__lte=created_before)
    return list(orders)


def _meeting_entered_at_by_order(orders):
    """每笔订单第一次进入"待当面交付"的时刻，没有就没有。

    事件表里混着一批 ``from_status == to_status`` 的记录——签到、约定交付时间、
    发起争议，都是真动作但不是阶段切换。拿它们当进入时刻，会把一笔一直在本阶段
    里打转的订单读成一笔刚进来的订单，这里和订单阶段流面板用的是同一条规则。
    """
    events = OrderEvent.objects.filter(
        order_id__in=[order.id for order in orders],
    ).values('order_id', 'from_status', 'to_status', 'created_at').order_by(
        'order_id', 'created_at', 'id',
    )
    entered_at = {}
    for event in events:
        if event['to_status'] != MEETING_STATUS:
            continue
        if event['from_status'] and event['from_status'] == event['to_status']:
            continue
        entered_at.setdefault(event['order_id'], event['created_at'])
    return entered_at


def _generation_count_by_order(orders):
    """每笔订单历史上生成过几次确认码，从站内通知反推。

    每次生成都会给卖家发一条"买家已生成交付确认码"的通知，而这条通知没有配聚合
    键——同一笔订单每生成一次就多一条记录。这是系统里唯一能数出"重新生成"的地方：
    ``handoff_code_issued_at`` 每生成一次就被覆盖，早期的生成时间在数据里已经不存在
    了，想从交付确认本身数出来是不可能的。

    这个口径有漏洞，而且必须说清楚：卖家关掉"订单状态更新"这类通知后，生成就不再
    留下这条记录。所以这里给出的是下界——真实的重新生成次数只会更多，不会更少。
    把它标成下界而不是当成准数，是因为运营要拿这个数字判断"现场是不是经常出问题"，
    一个被低估的下界会让这句话听起来比实际温和。
    """
    notices = Notification.objects.filter(
        order_id__in=[order.id for order in orders],
        kind='order_status',
        title=GENERATION_NOTICE_TITLE,
    ).values('order_id').annotate(total=Count('id'))
    return {row['order_id']: row['total'] for row in notices}


def _has_code(confirmation):
    """这笔订单到底有没有生成过确认码。

    只看 ``handoff_code_hash``：只要生成过就一定有哈希，即使后来又重新生成过、
    或者现在这笔订单已经被取消。一笔取消了但用过确认码的订单，仍然是"生成过"的
    样本——它在现场发生过，取消是另一回事。
    """
    return confirmation is not None and bool(confirmation.handoff_code_hash)


def _was_used(confirmation):
    return confirmation is not None and confirmation.handoff_code_used_at is not None


def _attempt_bucket_index(confirmation):
    """错误尝试落在哪一档。

    生成的码从没被核对过、和核对过且一次就对，是两件不同的事：前者说明现场没有走
    到核对那一步，后者说明码起了作用。分不清这两者，"启用确认码"就会和"确认码在
    现场被用上"混为一谈。
    """
    if confirmation is None or not confirmation.handoff_code_hash:
        return None
    if confirmation.handoff_code_used_at is not None:
        attempts = confirmation.handoff_code_attempts
        if attempts <= 0:
            return 0
        if attempts == 1:
            return 1
        if attempts == 2:
            return 2
        if attempts >= LOCK_THRESHOLD:
            return 5
        return 3
    # 没有使用时间，但错误次数已经到了上限：这一枚码在现场撞了五次，作废了。
    if confirmation.handoff_code_attempts >= LOCK_THRESHOLD:
        return 5
    return 4


def _issue_to_use_seconds(confirmation):
    """从生成到现场核对的间隔，没核对返回 None。"""
    if confirmation is None or not confirmation.handoff_code_used_at:
        return None
    if not confirmation.handoff_code_issued_at:
        return None
    return max(
        (confirmation.handoff_code_used_at - confirmation.handoff_code_issued_at).total_seconds(),
        0.0,
    )


def _bucket_index(seconds, buckets):
    for index, (limit, _label) in enumerate(buckets):
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _is_success(order):
    """这笔订单最终做成了吗。

    借用走的是"借用中 / 已归还"，交易走的是"交易完成"，两头都算做成。取消不算：
    一笔取消的订单可能只是没约成，把它算成失败会让"确认码没用"这句话凭空多出很多
    证据，而实际上双方根本没走到那一步。
    """
    return order.status in SUCCESS_STATUSES


def _build_adoption_stats(orders, confirmation_by_order, generation_by_order):
    """生成率：进入当面交付并且走完了的订单里，有多少真的生成了码。

    分母只收已经离开本阶段的订单。一个现在还挂在"待当面交付"的订单根本谈不上有
    没有生成码——见面还没发生。把它算进分母会让生成率凭空变小，这是右删失样本，
    和订单阶段流面板、"约好的那一次见面"面板是同一个道理；还没离开这个阶段的订单
    单独数出来，不丢也不混。
    """
    issued = [o for o in orders if _has_code(confirmation_by_order.get(o.id))]
    unissued = [o for o in orders if not _has_code(confirmation_by_order.get(o.id))]
    return {
        'total': len(orders),
        'issued_count': len(issued),
        'unissued_count': len(unissued),
        'issue_rate': _percent(len(issued), len(orders)),
        'used_count': sum(1 for o in issued if _was_used(confirmation_by_order.get(o.id))),
        'use_rate': _percent(
            sum(1 for o in issued if _was_used(confirmation_by_order.get(o.id))),
            len(issued),
        ),
        # 生成过但一次都没核对。这一行是"码发下去了，现场没走完"的量，它和"用了"
        # 加起来才等于"生成过"。
        'unused_count': sum(
            1 for o in issued if not _was_used(confirmation_by_order.get(o.id))
        ),
        'regenerated_count': sum(
            1 for o in orders if generation_by_order.get(o.id, 0) > 1
        ),
        'regenerated_rate': _percent(
            sum(1 for o in orders if generation_by_order.get(o.id, 0) > 1),
            len(orders),
        ),
    }


def _build_attempt_stats(orders, confirmation_by_order):
    """现场核对是怎么发生的：一次就对、错几次才对、以及撞到锁定。

    ``handoff_code_attempts`` 记的是最后一枚码的错误次数，不是这笔订单累计的错误
    次数——重新生成会把它归零。所以这一列必须和重新生成那一列合起来读：单看这一列，
    一个现场出过问题、后来换过码的订单一手滑就对的订单一模一样，而这是两回事。
    """
    buckets = [
        {'label': label, 'hint': hint, 'count': 0}
        for label, hint in ATTEMPT_BUCKETS
    ]
    issued_total = 0
    for order in orders:
        confirmation = confirmation_by_order.get(order.id)
        index = _attempt_bucket_index(confirmation)
        if index is None:
            continue
        issued_total += 1
        buckets[index]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], issued_total)

    locked = [
        order for order in orders
        if _attempt_bucket_index(confirmation_by_order.get(order.id)) == 5
    ]
    return {
        'issued_total': issued_total,
        'bucket_rows': buckets,
        'first_try_count': buckets[0]['count'],
        'first_try_rate': _percent(buckets[0]['count'], issued_total),
        'locked_count': len(locked),
        'locked_rate': _percent(len(locked), issued_total),
        'is_small_sample': issued_total < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_issue_gap_stats(orders, confirmation_by_order):
    """生成到现场核对隔了多久。

    一枚码的意义就在"现场那一下"。隔半小时生成和隔三天生成，说的是两件不同的事：
    前者是交付当天早上临出门生成，后者是提前很久生成好、码在中间那段时间里一直是
    有效状态。这个间隔本身不说明任何一方有问题——它只是运营该知道的形状。
    """
    seconds_list = [
        seconds for seconds in
        (_issue_to_use_seconds(confirmation_by_order.get(o.id)) for o in orders)
        if seconds is not None
    ]
    buckets = [{'label': label, 'count': 0} for _limit, label in ISSUE_TO_USE_BUCKETS]
    for seconds in seconds_list:
        buckets[_bucket_index(seconds, ISSUE_TO_USE_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(seconds_list))

    return {
        'measured_count': len(seconds_list),
        'bucket_rows': buckets,
        'average_seconds': _mean(seconds_list),
        'average_label': _format_duration(_mean(seconds_list)),
        'median_label': _format_duration(_percentile(seconds_list, 0.5)),
        'p90_label': _format_duration(_percentile(seconds_list, 0.9)),
        'fastest_label': _format_duration(min(seconds_list)) if seconds_list else '—',
        'slowest_label': _format_duration(max(seconds_list)) if seconds_list else '—',
    }


def _build_locked_rows(orders, confirmation_by_order, generation_by_order):
    """撞到五次锁定的订单，逐条列出来。

    这是本模块唯一一份清单，也是唯一一份"现在去核实"用得上的东西：错误次数已经达到
    上限，意味着现场发生过一次真实的沟通问题，而不是手滑打错一位。每一行都带上后来
    有没有重新生成、有没有最终做成——因为一笔锁定之后换了个码顺利交付的订单，和一
    笔锁定之后就僵在那里的订单，要做的动作完全不一样。
    """
    rows = []
    for order in orders:
        confirmation = confirmation_by_order.get(order.id)
        if _attempt_bucket_index(confirmation) != 5:
            continue
        generations = generation_by_order.get(order.id, 0)
        rows.append({
            'item_title': order.item.title,
            'buyer': order.buyer.username,
            'seller': order.seller.username,
            'status_label': order.get_status_display(),
            'is_success': _is_success(order),
            'attempts': confirmation.handoff_code_attempts,
            'regenerated': generations > 1,
            'generation_count': generations,
            'hint': confirmation.handoff_code_hint or '—',
        })
    return rows


def _build_outcome_contrast(orders, confirmation_by_order):
    """用过确认码的订单，是不是真的更可能做成。

    只做对照，不给因果，而且这个限定不是免责声明，是这条统计的全部边界。生成码的
    订单更可能完成，很可能只是因为愿意生成码的双方本来就是更认真的那批人——平台
    没有能力把"码起了作用"和"这对双方本来就靠谱"分开。把它讲成结论，运营就会去
    推一个所有人都生成码的功能，然后发现什么都没变。

    取消的订单两组都留着：取消不是失败，把它从对照组里剔掉会让两边的完成率一起变
    好看，而那正好是最该被看见的那批订单。两组各自要过门槛，差值落在噪声带内不给
    方向。
    """
    used = [o for o in orders if _was_used(confirmation_by_order.get(o.id))]
    unused = [
        o for o in orders
        if _has_code(confirmation_by_order.get(o.id))
        and not _was_used(confirmation_by_order.get(o.id))
    ]
    used_rate = _percent(sum(1 for o in used if _is_success(o)), len(used))
    unused_rate = _percent(sum(1 for o in unused if _is_success(o)), len(unused))
    gap = round(used_rate - unused_rate, 1) if used and unused else None
    enough = len(used) >= MIN_MEASURED_ORDERS and len(unused) >= MIN_MEASURED_ORDERS
    return {
        'used_count': len(used),
        'used_success_count': sum(1 for o in used if _is_success(o)),
        'used_success_rate': used_rate,
        'unused_count': len(unused),
        'unused_success_count': sum(1 for o in unused if _is_success(o)),
        'unused_success_rate': unused_rate,
        'gap_points': gap,
        # 差值落在噪声带内，就不给方向：两个方向相差几个百分点，在几十条样本上
        # 和一次抛硬币没有区别。
        'has_direction': bool(enough and gap is not None and abs(gap) >= NOISE_POINTS),
        'is_small_sample': not enough,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _summary(adoption, attempt, gap_stats, contrast, locked_rows):
    """一段话把这个模块的读数讲完，样本不足的地方明说。"""
    parts = []
    if not adoption['total']:
        parts.append('当前周期还没有走完当面交付阶段的订单，确认码统计暂无可核对的样本。')
        return ''.join(parts)

    parts.append(
        f"周期内 {adoption['total']} 笔订单走完了当面交付阶段，"
        f"{adoption['issued_count']} 笔生成了确认码（{adoption['issue_rate']}%）"
    )
    if adoption['issued_count']:
        parts.append(
            f"，其中 {adoption['used_count']} 笔在现场被核对使用"
            f"（{adoption['use_rate']}%）"
        )
        if adoption['unused_count']:
            parts.append(f"，{adoption['unused_count']} 笔生成后没有留下核对记录")
    if adoption['issued_count']:
        # 这个数永远是下界，不管它是几：卖家关掉订单状态通知后生成就不再留痕。
        # 报 0 的时候同样要说清楚，因为一个被低估的 0 比一个被高估的数字更危险。
        parts.append(
            f"，{adoption['regenerated_count']} 笔重新生成过（下界，"
            f"卖家关掉订单状态通知后生成不再留痕）"
        )
    if attempt['locked_count']:
        parts.append(f"，{attempt['locked_count']} 笔撞到 {LOCK_THRESHOLD} 次锁定")

    if contrast['has_direction']:
        direction = '更高' if contrast['gap_points'] > 0 else '更低'
        parts.append(
            f"。用过确认码的订单完成率比生成过但没用过的{direction} "
            f"{abs(contrast['gap_points'])} 个百分点——这是对照，不是结论"
        )
    elif contrast['is_small_sample']:
        parts.append(
            f"；两组各自不足 {contrast['min_measured_orders']} 笔，"
            f"完成率对照只报数字、不给方向"
        )
    if gap_stats['measured_count']:
        parts.append(f"，生成到使用的中位间隔 {gap_stats['median_label']}")
    return ''.join(parts)


def build_handoff_code_usage(days=30, now=None):
    '''统计交付确认码的生成、核对与锁定情况，只报告不改写任何字段。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    orders = _fetch_concluded_orders(created_after=start, created_before=now)

    confirmation_by_order = {
        order.id: getattr(order, 'delivery_confirmation', None) for order in orders
    }
    generation_by_order = _generation_count_by_order(orders)

    adoption = _build_adoption_stats(orders, confirmation_by_order, generation_by_order)
    attempt = _build_attempt_stats(orders, confirmation_by_order)
    gap_stats = _build_issue_gap_stats(orders, confirmation_by_order)
    contrast = _build_outcome_contrast(orders, confirmation_by_order)
    locked_rows = _build_locked_rows(orders, confirmation_by_order, generation_by_order)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(orders),
        'summary': _summary(adoption, attempt, gap_stats, contrast, locked_rows),
        'adoption_stats': adoption,
        'attempt_stats': attempt,
        'issue_gap_stats': gap_stats,
        'outcome_contrast': contrast,
        'locked_rows': locked_rows,
        'locked_row_limit': LOCKED_LIST_LIMIT,
        'lock_threshold': LOCK_THRESHOLD,
        'generation_notice_title': GENERATION_NOTICE_TITLE,
    }

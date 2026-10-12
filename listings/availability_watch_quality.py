# -*- coding: utf-8 -*-
"""等一个有货的人等了多久，第一次被运营读到。

`ItemAvailabilityWatch` 是站内唯一一条「用户主动表达想要、商品真的回来了、然后证据被
删掉」的队列。模型只有 `user`、`item`、`created_at` 三个字段；唯一的写路径是
`views.py` 的 `toggle_availability_watch`，唯一的读路径是商品详情页判断当前用户有没有
点过「有货时提醒我」，唯一的消费路径是 `availability.py` 的 `notify_item_available`——
在一个事务里捞出这件商品的全部关注，逐条发通知，**发成功就把这条 watch delete 掉**。

于是三个数从来没有任何一行代码算过：从点下关注到商品真的回来隔了多久；有多少关注最终
等到了；点进来的用户有多少。而 `item_available` 这一类通知虽然已经进了
`notification_response.py` 的时延统计，那统计的是「通知发了多久被读」，不是「用户点完
关注之后还回不回来」。

这个模块把它们读出来，只报告，不改写任何一条关注。几处口径上的取舍比数字本身更要紧，
它们都在下面的注释里。

消费即删除，所以「等了多久」只报存量。`notify_item_available` 删掉关注不是 bug，是一
次性提醒的正确业务行为——同一个用户不该为同一件商品被反复通知。但后果是：已经被消费
掉的那批关注，唯一的证据 `created_at` 在通知发出的那一刻就被抹掉了，事后无法回补。
所以「从点下关注到商品真的回来隔了多久」这个分布，对已消费的样本数不出来，本模块不报，
也不用一个近似值冒充它。分母只收当前仍挂着的关注，报的是它们的年龄分布——「现在还有
多少人在等，等了多久了」。那是存量不是流量，而存量才是运营现在能动作的那一批。

不改 delete()。把消费改成 update 会多一个「已通知」状态，那要改迁移、要改唯一的消费路
径、要处理「通知发失败之后 watch 怎么办」——为了一次统计去动一条正在跑的业务路径，不
划算，而且它现在没有出错。

死信和白等分开判，这是两条不同的判据。同样是「关注还挂在一件不可交易的商品上」，底
下是两件不同的事。死信是商品已经恢复在售、关注却还挂着：这些人点了提醒、商品回来了、
却因为关掉了这一类通知（`NotificationPreference.item_available = False`，此时
`create_notification` 返回 None，watch 不被删除），或者走了不走通知的重新上架路径
（`edit_item` 把过期商品重新上架、`confirm_return` 双方确认归还，两条路都不调
`notify_item_available`），而永远不会被通知。**这是唯一一条能确定「用户被漏掉了」的判
据**，逐条列出来。白等是商品已经售出或过期、关注还挂着：卖家还能重新上架，所以不判死
信，只报规模和等了多久，不给方向。

商品状态一律按当前状态回看。商品状态只记当前值，不记历史，所以「商品已经不可交易」这
一栏是系统性高估：一条关注点下时商品在售、之后才被卖掉的也算进来，一条发得很及时、
但商品在送达之后才卖掉的提醒也会被算进「送达时已不可交易」。宁可高估——运营看到高估
的数字会去核实，看到低估的数字会直接相信。

一件商品上有多少人在等，分母是商品数。一件商品被八个人关注，按关注数走会在分布里出
现八次，「竞争激烈」那一档被同一件商品撑起来，而它其实只是一件商品。和
`gift_application_queue` 的队列长度分布是同一个道理。

有货提醒的触达质量，只问「这条提醒发出去还有没有用」。`notification_response.py` 已经
报了这一类通知「发了多久被读」，本模块不重复它，只补它不报的那一列：提醒送达的那一刻
商品还可不可交易。一条三天前送到、商品第二天就卖掉的提醒是无效的——用户点进去看到
「已售出」，下一次他就不会点这类提醒了。

本模块不读任何自由文本：只读 `created_at`、`item.status`、`item.trade_mode`、
`item.category` 和 `notification.is_read`。商品标题会带出来（它是清单的第一格，运营要
认得出是哪一件），`description`、`buyer_note` 一个字符都不读。不自动清理任何一条关注、
不补发漏掉的通知、不给卖家看「有 N 个人在等」——那是平台在改变卖家和等他的人之间的力
量对比，要做也得单独设计。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import Item, ItemAvailabilityWatch, Notification

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一组样本至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条
# 样本上几个百分点的差距是噪声。
MIN_MEASURED_WATCHES = 12

# 一件商品上的关注数下限。只有一条关注的商品谈不上「集中度」，低于这个数就不给方向。
MIN_ITEM_WATCHES = 3

# 等待年龄的分档。互斥，左闭右开，按天切。等待的时间尺度是天：一个人点下关注，
# 之后每天都会回来看一眼这件商品，所以「等了 3 天」和「等了 3 小时」是两个世界。
WAIT_BUCKETS = (
    (86400, '1 天内', '刚点不久，还在每天回来看'),
    (3 * 86400, '1-3 天', '等几天是正常的，卖家还没重新上架'),
    (7 * 86400, '3-7 天', '等一周了，多数人已经忘了这个关注'),
    (14 * 86400, '7-14 天', '跨了两周，大概率已经去别处买了'),
    (None, '超过 14 天', '挂了半个月以上，多半是白等'),
)

# 死信清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
DEAD_LETTER_LIST_LIMIT = 20

# 等得最久的清单最多列这么多条，同上。
LONG_WAIT_LIST_LIMIT = 20

# 一件商品上的关注数分档。分母是商品数，不是关注数——理由见模块开头。
ITEM_WATCH_BUCKETS = (
    (2, '1 人在等', '只有一个人在等，谈不上竞争'),
    (4, '2-3 人在等', '两三个人，是长尾商品常见的形状'),
    (8, '4-7 人在等', '明显有人抢，商品一回来就不够分'),
    (None, '8 人及以上', '竞争激烈，该考虑分批或抽签'),
)

TRADE_MODE_LABELS = dict(Item.TRADE_MODE_CHOICES)

# 一条关注的完整轨迹，一次查完固定下来。年龄分档、商品状态交叉、死信判定各读一遍，
# 按记录现算会让每条关注打好几遍库，而看板是一次打开全算的。
Watch = namedtuple('Watch', [
    'row', 'wait_seconds', 'item_available', 'is_dead_letter', 'trade_mode',
    'category_name',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，不是
    ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_days(seconds):
    """等待时长一律按天说话，不足一天的说「当天」。

    这个模块报的全是天尺度的量。报「19 小时」会让运营以为是小时级的问题，而一个
    点下关注的人是每天回来看一眼，不是每小时来看一眼。
    """
    if seconds is None:
        return '—'
    if seconds < 86400:
        return '当天'
    return f'{int(seconds // 86400)} 天'


def _mean(values):
    return sum(values) / len(values) if values else None


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


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


def _fetch_watches(*, created_after=None, created_before=None):
    """按显式排序取出窗口内点下的关注，带上商品、分类和当前状态。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「等得最久的那一条」都会随机
    漂移。

    窗口按 `created_at` 取。要回答的是「这段时间里点下的关注，现在还在不在等、等了多
    久」，所以起点是用户点下关注的那一刻。

    一条已经不在等、或者商品已经删掉的关注不进来。商品删除会把关注级联删掉，所以
    `select_related('item')` 不会拿到 None；但关注可能在窗口内被取消或消费，那时它已经
    不在表里——被取消和被消费的关注是右删失样本，数不出来，也不编一个数冒充它们。
    """
    queryset = ItemAvailabilityWatch.objects.select_related(
        'item', 'item__category',
    )
    if created_after is not None:
        queryset = queryset.filter(created_at__gte=created_after)
    if created_before is not None:
        queryset = queryset.filter(created_at__lte=created_before)

    now = timezone.now()
    watches = []
    for row in queryset.order_by('created_at', 'pk'):
        wait_seconds = max((now - row.created_at).total_seconds(), 0.0)
        watches.append(Watch(
            row=row,
            wait_seconds=wait_seconds,
            item_available=row.item.is_available_now,
            # 商品已经恢复在售、关注却还挂着：这条关注永远不会被通知，也不会被清理。
            # 它是死信，也是本模块唯一能确定「用户被漏掉了」的判据。
            is_dead_letter=row.item.is_available_now,
            trade_mode=row.item.trade_mode,
            category_name=(
                row.item.category.name if row.item.category_id else '未分类'
            ),
        ))
    return watches


def _stats(watches):
    """全部在等的关注的等待年龄分布，以及商品当前状态的分层。"""
    waits = [w.wait_seconds for w in watches]
    rows = _bucket_rows(waits, WAIT_BUCKETS)
    available = sum(1 for w in watches if w.item_available)

    # 按档看商品状态，不按总数看。想知道的是「等越久是不是越可能是白等」，而这个
    # 问题只有逐档对照才答得出来：如果每一档的不可交易率都差不多，那等待本身就不
    # 是原因，是这批商品本来就容易卖掉。
    status_rows = []
    for index, row in enumerate(rows):
        in_bucket = [
            w for w in watches
            if _bucket_index(w.wait_seconds, WAIT_BUCKETS) == index
        ]
        unavailable = sum(1 for w in in_bucket if not w.item_available)
        # 空档也要给出完整的键。少了 unavailable_count / unavailable_rate 两个键，
        # 模板和导出就得为「这个档有没有样本」分两套写法，而漏掉一套就是一个 KeyError。
        status_rows.append({
            'label': row['label'],
            'count': len(in_bucket),
            'available_count': len(in_bucket) - unavailable,
            'unavailable_count': unavailable,
            'unavailable_rate': (
                _percent(unavailable, len(in_bucket)) if in_bucket else None
            ),
        })

    return {
        'measured_count': len(watches),
        'available_count': available,
        'unavailable_count': len(watches) - available,
        'unavailable_rate': _percent(len(watches) - available, len(watches)),
        'dead_letter_count': sum(1 for w in watches if w.is_dead_letter),
        'mean_label': _format_days(_mean(waits)),
        'median_label': _format_days(_percentile(waits, 0.5)),
        'p90_label': _format_days(_percentile(waits, 0.9)),
        'rows': rows,
        'status_rows': status_rows,
        'is_small_sample': len(watches) < MIN_MEASURED_WATCHES,
    }


def _item_watch_rows(watches):
    """一件商品上有多少人在等，分母是商品数。

    一件商品被八个人关注，按关注数走会在分布里出现八次，「竞争激烈」那一档被同一件
    商品撑起来，而它其实只是一件商品。所以分母收商品数，一件商品只算一次。
    """
    grouped = {}
    for watch in watches:
        entry = grouped.setdefault(watch.row.item_id, {
            'item_title': watch.row.item.title,
            'item_id': watch.row.item_id,
            'count': 0,
        })
        entry['count'] += 1

    rows = [
        {'label': label, 'hint': hint, 'count': 0}
        for _limit, label, hint in ITEM_WATCH_BUCKETS
    ]
    for entry in grouped.values():
        rows[_bucket_index(entry['count'], ITEM_WATCH_BUCKETS)]['count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(grouped))
    return rows


def _category_rows(watches):
    """按商品分类分组的在等量与不可交易率，在等量大的排在前面。"""
    grouped = {}
    for watch in watches:
        entry = grouped.setdefault(watch.category_name, {
            'category_name': watch.category_name, 'count': 0, 'unavailable': 0,
        })
        entry['count'] += 1
        if not watch.item_available:
            entry['unavailable'] += 1

    rows = []
    for entry in grouped.values():
        rows.append({
            'category_name': entry['category_name'],
            'count': entry['count'],
            'unavailable_count': entry['unavailable'],
            'unavailable_rate': _percent(entry['unavailable'], entry['count']),
        })
    rows.sort(key=lambda row: (-row['count'], row['category_name']))
    return rows


def _trade_mode_rows(watches):
    """按交易方式分组的在等量与不可交易率。

    三种交易方式等不到的原因完全不同：出售和借用会重新上架，等是值得的；免费赠送是
    一件东西给一个人，等不到就是真的没有了。混在一起报会把「可以再等等」和「已经不
    可能了」这两种处境稀释成同一个百分比。
    """
    grouped = {}
    for watch in watches:
        entry = grouped.setdefault(watch.trade_mode, {
            'trade_mode': watch.trade_mode, 'count': 0, 'unavailable': 0,
        })
        entry['count'] += 1
        if not watch.item_available:
            entry['unavailable'] += 1

    rows = []
    for trade_mode, entry in grouped.items():
        rows.append({
            'trade_mode': trade_mode,
            'trade_mode_label': TRADE_MODE_LABELS.get(trade_mode, trade_mode),
            'count': entry['count'],
            'unavailable_count': entry['unavailable'],
            'unavailable_rate': _percent(entry['unavailable'], entry['count']),
        })
    rows.sort(key=lambda row: (-row['count'], row['trade_mode']))
    return rows


def _dead_letter_rows(watches, limit=DEAD_LETTER_LIST_LIMIT):
    """商品已经恢复在售、关注却还挂着的人，按等待时长倒序。

    返回 (截断后的行, 全部命中)。行数有上限，因为清单是给「现在去核实」用的；但上限
    之下的总量要一起报出来，否则模板里「完整的在导出里」这句话没有依据。

    这是本模块唯一一条能确定「用户被漏掉了」的清单。它只有三个来源：关掉了这一类
    通知、`edit_item` 重新上架、`confirm_return` 双方确认归还。每行只带商品和等了多久，
    一个字符的正文都不读。
    """
    dead = [w for w in watches if w.is_dead_letter]
    dead.sort(key=lambda w: (-w.wait_seconds, w.row.pk))
    return dead, [
        {
            'item_title': w.row.item.title,
            'item_id': w.row.item_id,
            'wait_label': _format_days(w.wait_seconds),
            'wait_seconds': w.wait_seconds,
            'created_at': w.row.created_at,
        }
        for w in dead[:limit]
    ]


def _long_wait_rows(watches, limit=LONG_WAIT_LIST_LIMIT):
    """等得最久、且商品已经不可交易的关注，按等待时长倒序。

    返回 (截断后的行, 全部命中)。同 `_dead_letter_rows`：行数有上限，但总量一起报。

    死信是「商品回来了却没人知道」，这里是「商品还没回来，而人已经等了很久」。前者
    是平台的漏报，后者多半是白等——但卖家随时可能重新上架，所以平台没有能力替一个
    具体的人决定这条关注该不该继续挂着。只报规模和等了多久，不给方向。
    """
    stale = [w for w in watches if not w.item_available]
    stale.sort(key=lambda w: (-w.wait_seconds, w.row.pk))
    return stale, [
        {
            'item_title': w.row.item.title,
            'item_id': w.row.item_id,
            'category_name': w.category_name,
            'trade_mode_label': TRADE_MODE_LABELS.get(w.trade_mode, w.trade_mode),
            'wait_label': _format_days(w.wait_seconds),
            'wait_seconds': w.wait_seconds,
            'created_at': w.row.created_at,
        }
        for w in stale[:limit]
    ]


def _fetch_delivered(*, created_after=None, created_before=None):
    """按显式排序取出窗口内已经送达的有货提醒，带上商品当前状态。

    `notification_response.py` 已经报了这一类通知「发了多久被读」，这里不重复它，只补
    它不报的那一列：提醒送达的那一刻商品还可不可交易。

    送达时刻距关注点下的间隔算不出来——`created_at` 在通知发出的那一刻就被删掉了。
    所以这里只能按通知自己的 `created_at` 取窗口，报的是「这段时间里发出去的提醒，还
    有多少用」。这不是同一个问题的近似，这是另一个问题，报告里必须分开说。
    """
    queryset = Notification.objects.filter(
        kind='item_available', item__isnull=False,
    ).select_related('item')
    if created_after is not None:
        queryset = queryset.filter(created_at__gte=created_after)
    if created_before is not None:
        queryset = queryset.filter(created_at__lte=created_before)
    return list(queryset.order_by('created_at', 'pk'))


def _delivery_stats(delivered):
    """已经送达的有货提醒的触达质量。"""
    read_count = sum(1 for row in delivered if row.is_read)
    stale = [row for row in delivered if not row.item.is_available_now]
    return {
        'measured_count': len(delivered),
        'read_count': read_count,
        'read_rate': _percent(read_count, len(delivered)),
        'stale_count': len(stale),
        'stale_rate': _percent(len(stale), len(delivered)),
        'is_small_sample': len(delivered) < MIN_MEASURED_WATCHES,
    }


def _summary(stats, item_watch_rows, dead_letter_total, long_wait_total, delivery):
    parts = [
        f'周期内共有 {stats["measured_count"]} 条关注现在还挂着'
    ]
    if stats['measured_count']:
        parts.append(
            f'，中位等 {stats["median_label"]}、P90 等 {stats["p90_label"]}'
        )
    if stats['unavailable_count']:
        parts.append(
            f'；其中 {stats["unavailable_count"]} 条关注的商品已经不可交易'
            f'（{stats["unavailable_rate"]}%，按当前状态回看，偏高）'
        )
    if dead_letter_total:
        parts.append(
            f'。{dead_letter_total} 条关注挂在已经恢复在售的商品上，'
            f'这些人点了提醒、商品回来了、却永远不会被通知，已逐条列在下面'
        )
    if long_wait_total:
        parts.append(
            f'；另有 {long_wait_total} 条关注挂在已售出或已过期的商品上'
        )
    crowded = sum(1 for row in item_watch_rows if row['label'] == '8 人及以上')
    if crowded:
        parts.append(f'；{crowded} 件商品有八个人以上在等')
    if delivery['measured_count']:
        parts.append(
            f'。周期内送达 {delivery["measured_count"]} 条有货提醒，'
            f'{delivery["read_rate"]}% 已读，其中 {delivery["stale_count"]} 条'
            f'送达时商品已经不可交易（同样按当前状态回看，偏高）'
        )
    if stats['is_small_sample'] and stats['measured_count']:
        parts.append(
            f'。样本不足 {MIN_MEASURED_WATCHES} 条，占比只作参考'
        )
    return ''.join(parts)


def build_availability_watch_quality(days=30, now=None):
    """统计「有货时提醒我」现在还在等的人等了多久，只报告不改写任何一条关注。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    watches = _fetch_watches(created_after=start, created_before=now)
    stats = _stats(watches)
    item_watch_rows = _item_watch_rows(watches)
    category_rows = _category_rows(watches)
    trade_mode_rows = _trade_mode_rows(watches)
    dead_letter_all, dead_letter_rows = _dead_letter_rows(watches)
    long_wait_all, long_wait_rows = _long_wait_rows(watches)
    delivered = _fetch_delivered(created_after=start, created_before=now)
    delivery = _delivery_stats(delivered)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(watches),
        'summary': _summary(
            stats, item_watch_rows, len(dead_letter_all), len(long_wait_all),
            delivery,
        ),
        'stats': stats,
        'item_watch_rows': item_watch_rows,
        'category_rows': category_rows,
        'trade_mode_rows': trade_mode_rows,
        'dead_letter_rows': dead_letter_rows,
        'dead_letter_row_total': len(dead_letter_all),
        'dead_letter_row_limit': DEAD_LETTER_LIST_LIMIT,
        'long_wait_rows': long_wait_rows,
        'long_wait_row_total': len(long_wait_all),
        'long_wait_row_limit': LONG_WAIT_LIST_LIMIT,
        'delivery': delivery,
        'min_measured_watches': MIN_MEASURED_WATCHES,
        'min_item_watches': MIN_ITEM_WATCHES,
    }

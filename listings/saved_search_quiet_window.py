# -*- coding: utf-8 -*-
"""关注搜索的临时静默：被用户主动推迟的那批命中，第一次被运营读到。

``SavedSearch.quiet_until`` 是站内唯一一个「用户亲手给一条自动通知按下暂停键」的
字段。考试周不想被刷屏、假期不想被提醒，用户在关注搜索页填一个时间，这条关注的
提醒就停到那个时刻为止。写入路径有三处老老实实读它：``send_saved_search_digest``
在 ``is_quiet`` 为真时把这条关注整个跳过、``deliverable_now`` 让即时节奏在静默期内
一次都不发、``update_saved_search_cadence`` 让用户随时改也随时清空。

而 ``result['quiet']`` 这个数加完就没了。digest 的注释里写着它，管理后台把它列在
``list_display`` 里，模板把它渲染成一个月亮徽章「静默至 03-21 08:00」。
**三处都在显示它，没有一处统计过它。**

静默是用户对这条通知流的一次**主动评价**：没有人会平白无故关掉一个自己觉得有用的
提醒。而关掉它的代价又极低——取消关注要点删除，静默只是填一个时间。一个低成本的
暂停键，恰好是最该被看见的反馈信号。

这个模块把那几个字段读出来，只报告，不改写。几处口径上的取舍比数字本身更要紧。

**静默时长报不了，如实不报。** ``quiet_until`` 只存结束时刻，站内没有任何字段记下
用户是什么时候按下暂停键的——``updated_at`` 每次保存都被覆盖，而节奏表单每次都写
这个字段，改频率、改条数、清空静默都会刷新它，所以它连「最后一次改动」都不是一个
可靠的信号。为「正在静默」和「早已过期」两档反推一个时长，等于编一个数据出来。本
模块只报此刻的状态和缓冲区的年龄，不报这段沉默持续了多久。

**静默造成的等待，和平台造成的等待，必须分开。** ``saved_search_latency`` 报的是
汇总任务排在半夜、漏跑一天造成的等待；静默期的等待是用户自己说「现在不方便」。
两者成因相反，混进同一个分布，运营会把「用户主动暂停」读成「我们的任务又慢了」。
本模块只把静默期攒下的那部分拆出来，两套阈值不合成一个平均数，也不重复那个模块
的总时延结论。

**商品状态只能按当前状态回看。** 送达之后商品仍可能被下架或卖出，历史状态没有记录。
所以「送达时已不可交易」这一栏系统性偏高。宁可高估：运营看到高估的数字会去核实，
看到低估的数字会直接相信。

**不报静默和取消关注的因果。** 一个人暂停了关注、之后不再回来看，可能只是他已经
找到了想找的东西。本模块只报「静默结束后还有没有新的命中」这一个事实，不把它读成
「静默赶走了用户」，也不报取消率。

**不按用户排名，不报任何用户名、不列任何商品名。** 谁在什么时候暂停了哪条关注是
用户的私事，做成一份清单，设置页就变成了公开表态。关注名称可以报——那是用户给
自己条件起的名字，不是身份；商品名不报，因为那会牵出「谁在找什么」。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import SavedSearch, SavedSearchMatch

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一行关注，连同它此刻的静默状态和还压在缓冲区里的命中。用 namedtuple 而不是 dict，
# 是因为下面每一节都要同时读七八个字段，dict 的键名拼错要到模板里才炸。
Search = namedtuple('Search', 'row state buffered')

# 一条缓冲中的命中，连同它等了多久、商品现在还可不可交易。
BufferedMatch = namedtuple('BufferedMatch', 'row age_seconds item_available')

# 静默是全部节奏共有的能力：即时、日度、周度三条关注都能填 quiet_until。所以本节
# 不按节奏过滤，只按关注本身的状态过滤。
CADENCE_LABELS = dict(SavedSearch.FREQUENCY_CHOICES)

# 三档静默状态。「正在静默」和「早已过期」的区分是硬的：quiet_until 是一个绝对时刻，
# 过期之后写入路径就不再跳过它，但它还留在字段里——不清空、也不再生效。
QUIET_STATE_LABELS = {
    'quiet_now': '正在静默',
    'quiet_expired': '静默已过期',
    'never_quiet': '从没静默过',
}

# 缓冲区分档。按天切，和 saved_search_latency 同一套尺度：一次汇总等待的时间尺度
# 是天，报「19 小时」会让运营以为是小时级的问题，而去查一个按天运转的定时任务。
BUFFER_AGE_BUCKETS = (
    (86400, '当天', '命中当天就进了缓冲区，还没有到下一个汇总点'),
    (2 * 86400, '1 天内', '隔了一天，多半是任务排在半夜跑'),
    (7 * 86400, '1-7 天', '拖过了一天，需要看是不是任务漏跑'),
    (14 * 86400, '7-14 天', '已经跨过一个周度周期，这批商品多半已经不在售'),
    (None, '超过 14 天', '跨了两个周度周期，这一批基本失效'),
)

# 一档要给方向，至少这么多条关注。和其余模块同一个量级：几十条样本上几个百分点的
# 差距是噪声。
MIN_QUIET_SEARCHES = 12

# 缓冲区的年龄结构要给方向，至少这么多条命中。
MIN_BUFFERED_MATCHES = 12

# 待核实清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
EXPIRED_LIST_LIMIT = 20


def _percent(part, whole):
    """占比一律返回 None 表示空分母，不返回 0。

    0 会被读成「发了，占比很小」，而这里的事实是「分母不存在」。看板模板把 None
    显示成「—」，和 0 区分开。
    """
    return round(part / whole * 100, 1) if whole else None


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_days(seconds):
    """年龄一律按天说话，不足一天的说「当天」。"""
    if seconds is None:
        return '—'
    if seconds < 86400:
        return '当天'
    return f'{int(seconds // 86400)} 天'


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


def _quiet_state(saved_search, now):
    """一条关注此刻处在哪一档静默状态。

    取不到 ``quiet_until`` 的关注归「从没静默过」，不归「正在静默」——没有按下过
    暂停键的关注，和按下过又清空的关注，在运营眼里不是一回事。
    """
    if not saved_search.quiet_until:
        return 'never_quiet'
    return 'quiet_now' if saved_search.quiet_until > now else 'quiet_expired'


def _fetch_searches(*, now):
    """按显式排序取出全部关注，带上缓冲区里还没发出去的命中。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的分组，比一份错的分组更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一批」都会随机漂移。

    不按统计周期过滤。静默问的是「此刻有多少关注停在暂停键后面」，一个上个月就
    开始静默、这个月才过期的关注，恰恰是最不该被周期切掉的一批。
    """
    searches = []
    for row in SavedSearch.objects.select_related(
        'user', 'category', 'location',
    ).order_by('pk'):
        buffered = [
            BufferedMatch(
                row=match,
                age_seconds=max((now - match.matched_at).total_seconds(), 0.0),
                item_available=match.item.is_available_now,
            )
            for match in SavedSearchMatch.objects.filter(
                saved_search=row, notified_at__isnull=True,
            ).select_related('item').order_by('matched_at', 'pk')
        ]
        searches.append(Search(
            row=row,
            state=_quiet_state(row, now),
            buffered=buffered,
        ))
    return searches


def _state_rows(searches, now):
    """三档静默状态的关注数，以及每一档此刻压在缓冲区里的命中数。

    每一档的缓冲命中单独数，因为「正在静默」攒命中是暂停键的本意，而「早已过期」
    还压着一批没发，那是静默结束之后发送路径没接上——两件事在同一个数字里会互相
    掩盖，所以分成两列报，不相加。
    """
    rows = []
    for state in ('quiet_now', 'quiet_expired', 'never_quiet'):
        subset = [s for s in searches if s.state == state]
        buffered_total = sum(len(s.buffered) for s in subset)
        stale_buffered = sum(
            1 for s in subset for m in s.buffered if not m.item_available
        )
        rows.append({
            'state': state,
            'label': QUIET_STATE_LABELS[state],
            'search_count': len(subset),
            'buffered_count': buffered_total,
            'stale_buffered_count': stale_buffered,
            # 一档至少要 MIN_QUIET_SEARCHES 条关注，占比才给方向。分母是全部关注，
            # 不是这一档自己——拿一档的条数去除以一档的条数，永远得到 100%。
            'share': _percent(len(subset), len(searches)),
            'is_small_sample': len(subset) < MIN_QUIET_SEARCHES,
        })
    return rows


def _buffer_age_stats(searches, *, state):
    """某一档关注的缓冲区年龄结构。

    只统计缓冲命中数达到门槛的档：一条关注攒了 3 条命中就谈不到「年龄结构」，
    那只是刚攒了三天。
    """
    subset = [s for s in searches if s.state == state]
    ages = [m.age_seconds for s in subset for m in s.buffered]
    rows = _bucket_rows(ages, BUFFER_AGE_BUCKETS)
    stale = sum(
        1 for s in subset for m in s.buffered if not m.item_available
    )
    # 逐关注一行，只收缓冲区非空的：一条攒了 0 条命中的关注排进来只会占一行
    # 空白。按缓冲区规模降序，规模相同时按 pk，保证同一份数据每次给出同一张表。
    search_rows = sorted(
        (
            {
                'saved_search_id': s.row.pk,
                'saved_search_name': s.row.name,
                'cadence_label': CADENCE_LABELS.get(
                    s.row.notify_frequency, s.row.notify_frequency,
                ),
                'buffered_count': len(s.buffered),
                'oldest_label': _format_days(
                    max((m.age_seconds for m in s.buffered), default=None),
                ),
                'stale_count': sum(
                    1 for m in s.buffered if not m.item_available
                ),
                'quiet_until': s.row.quiet_until,
            }
            for s in subset if s.buffered
        ),
        key=lambda r: (-r['buffered_count'], r['saved_search_id']),
    )
    return {
        'state': state,
        'label': QUIET_STATE_LABELS[state],
        'measured_count': len(ages),
        'search_count': len(subset),
        'mean_label': _format_days(_mean(ages)),
        'oldest_label': _format_days(max(ages)) if ages else '—',
        'rows': rows,
        'search_rows': search_rows,
        'stale_count': stale,
        # 商品状态按当前状态回看，所以「已不可交易」系统性偏高，见模块开头。
        'stale_rate': _percent(stale, len(ages)),
        'is_small_sample': len(ages) < MIN_BUFFERED_MATCHES,
    }


def _delivered_after_quiet(searches, *, start, now, limit=EXPIRED_LIST_LIMIT):
    """静默过期之后，这批关注还有没有收到过新的命中。

    判据是静默过期之后有没有新的 ``SavedSearchMatch`` 落库——命中落库和通知送达
    是两件事，一条命中可能在静默期产生、静默结束才送到。这里问的是「用户暂停回来
    之后，这条关注还活着吗」，所以看命中有没有继续产生，那才是关注还活着的证据。

    不报取消率。取消关注可能只是用户已经找到了东西，和静默没有因果关系。
    """
    expired = [s for s in searches if s.state == 'quiet_expired']
    rows = []
    for search in expired:
        quiet_until = search.row.quiet_until
        if not quiet_until:
            continue
        later_hits = SavedSearchMatch.objects.filter(
            saved_search=search.row, matched_at__gt=quiet_until,
        ).count()
        rows.append({
            'saved_search_id': search.row.pk,
            'saved_search_name': search.row.name,
            'cadence_label': CADENCE_LABELS.get(
                search.row.notify_frequency, search.row.notify_frequency,
            ),
            'quiet_until': quiet_until,
            # 静默过期距今天数。这一个数可以算——quiet_until 是一个绝对时刻，现在也
            # 是一个绝对时刻。它回答「这批人暂停了多久没回来」，和「沉默了多久」不是
            # 一回事：起点是暂停的结束，不是暂停的开始。
            'expired_for_days': max((now - quiet_until).total_seconds() / 86400, 0),
            'later_hit_count': later_hits,
            'still_alive': later_hits > 0,
            'buffered_count': len(search.buffered),
        })
    rows.sort(key=lambda r: (-r['later_hit_count'], r['saved_search_id']))
    alive = [r for r in rows if r['still_alive']]
    return {
        'total': len(rows),
        'alive_count': len(alive),
        'silent_count': len(rows) - len(alive),
        'alive_rate': _percent(len(alive), len(rows)),
        'is_small_sample': len(rows) < MIN_QUIET_SEARCHES,
        'rows': rows[:limit],
        'row_limit': limit,
        'row_total': len(rows),
    }


def _summary(state_rows, buffer_stats, delivered):
    """一句话说清这个周期里静默的整体形状。"""
    quiet_now = next(r for r in state_rows if r['state'] == 'quiet_now')
    expired = next(r for r in state_rows if r['state'] == 'quiet_expired')
    if not quiet_now['search_count'] and not expired['search_count']:
        return '当前没有处在静默中的关注，也没有静默已过期的关注。'

    parts = [
        f"{quiet_now['search_count']} 条关注正在静默",
        f"缓冲区压着 {quiet_now['buffered_count']} 条命中",
    ]
    if expired['search_count']:
        parts.append(
            f"{expired['search_count']} 条关注静默已过期，其中 "
            f"{delivered['silent_count']} 条至今没有新的命中"
        )
    if buffer_stats['measured_count'] and buffer_stats['oldest_label'] != '—':
        parts.append(f"最早一条缓冲命中已等待 {buffer_stats['oldest_label']}")
    return '；'.join(parts) + '。'


def build_saved_search_quiet_window(days=30, now=None):
    """统计关注搜索临时静默的规模、缓冲区的年龄与静默结束后的存活，只报告不改写。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    searches = _fetch_searches(now=now)
    state_rows = _state_rows(searches, now)

    # 缓冲区年龄只对「正在静默」和「早已过期」两档有意义：「从没静默过」的关注压在
    # 缓冲区里的命中，成因是汇总节奏，那是 saved_search_latency 的题目，本模块不碰。
    buffer_now = _buffer_age_stats(searches, state='quiet_now')
    buffer_expired = _buffer_age_stats(searches, state='quiet_expired')
    delivered = _delivered_after_quiet(searches, start=start, now=now)

    # 两档缓冲区的年龄结构按同一套分档渲染，所以合成一个有序列表交给模板。写成两个
    # 顶层键，模板就得写 `for stats in a, b`——那不是 Django 模板的合法语法，
    # 渲染时会直接抛 TemplateSyntaxError。顺序固定：正在静默在前，早已过期在后。
    buffer_blocks = [buffer_now, buffer_expired]

    quiet_total = (
        state_rows[0]['search_count'] + state_rows[1]['search_count']
    )
    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(quiet_total),
        'summary': _summary(state_rows, buffer_now, delivered),
        'state_rows': state_rows,
        'quiet_now_count': state_rows[0]['search_count'],
        'quiet_expired_count': state_rows[1]['search_count'],
        'never_quiet_count': state_rows[2]['search_count'],
        'buffered_now_count': state_rows[0]['buffered_count'],
        'buffered_expired_count': state_rows[1]['buffered_count'],
        'buffer_now': buffer_now,
        'buffer_expired': buffer_expired,
        'buffer_blocks': buffer_blocks,
        'delivered': delivered,
        'min_quiet_searches': MIN_QUIET_SEARCHES,
        'min_buffered_matches': MIN_BUFFERED_MATCHES,
    }
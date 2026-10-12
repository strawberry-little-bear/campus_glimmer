# -*- coding: utf-8 -*-
"""失物招领的线索，有多少条走到了「已确认」。

`LostFoundPost` 是站内唯一一条「我丢了 / 我捡到了」的记录，`LostFoundLead` 是它的
线索表：一个人看到一条记录，私下提交一条说明，等发布者确认。这条链路是站内唯一一条
**不经过交易就完成的互助**——没有 `Order`、没有价格、没有交付确认，走的是「发布 →
有人私下提线索 → 发布者确认 → 双方自己联系」四步。它的成败标准很朴素：**发布者确认
的那一条线索，是不是真的把东西找回来了。**

而这四步每一步都在写库，**没有任何一处把它们连起来看过。**

具体到四个答不上来的问题：一条记录平均收到几条线索；线索被确认的概率有多大，按记录
类型拆；确认之后东西找回来了没有；没被确认的线索是被拒了还是被撤回了。

更麻烦的是第五件事，它是本模块存在的首要理由。`review_lost_found_lead` 的 `accept`
分支在确认一条线索的同时，会把**同一条记录下其余 `pending` 的线索全部批量改成
`rejected`**。这是正确的产品行为——一条失物确认匹配了，其余候选自然不再需要。但它
意味着「被驳回的线索」里有很大一部分**不是发布者一条条看过之后拒绝的，而是被一条
`accept` 顺带清掉的**。把这两类混在一个「发布者拒绝率」里报，会得出「发布者很不认可
大家提供的线索」这个完全错误的结论，而它会直接导致错误的动作：去提醒发布者多看看
线索，或者给发布者加确认引导。而真实情况可能只是这个功能运转得很好——确认得很快，
其余候选自然就没用了。

所以本模块的第一件事，就是把这两类分开。

本模块只报告，不改写任何一条线索、任何一条记录、任何一份反馈。几处口径上的取舍比
数字本身更要紧。

确认率按 `post_type` 拆，不按总数报。`lost` 和 `found` 是两个完全不同的处境：丢东西
的人发一条等着捡到的人来，捡到的人发一条等着丢东西的人来，两边的候选池大小和认领
意愿完全不同。合并成一个「失物招领整体确认率 42%」，这个数无论往哪边动都无法定位
问题。

「一条记录收到几条线索」的分布，**分母是记录数**。一条收到三条线索的记录在这个分布
里只出现一次。按线索数走会让「3 条线索」那一档被同一批记录撑三次，而它其实只是几条
记录的真实热度。这和交易评价覆盖率、免费领取队列、有货关注集中度是同一个道理。

被批量清掉的线索单独数，不算「发布者拒绝」。一条 `rejected` 的线索，去看它所属记录
有没有 `accepted` 的线索：**有，归「另一条被确认时顺带清掉」；没有，归「发布者看过
之后拒绝」。** 两类单独报，绝不合并。

线索提交间隔按小时分档，不按天。一条失物招领的线索，时间尺度是小时到天：一条
「丢失校园卡」的记录可能发布当天下午就收到三条线索，也可能一周后还有人看到。只报
发布者收到**第一条**线索的时刻——那是「这条记录有没有被看见」的第一个信号，后续几条
是热度，不是时效。

完成率只报失物招领自己的，不和求购响应合并。`MutualAidFeedback` 有两个来源，
`lost_found_lead` 和 `demand_response`。`mutual_aid_feedback_insights` 把它们并进一个
「已确认互助」总数报完成率，那是那个模块的口径，它看的是互助反馈这个环节本身的运转。
本模块看的是失物招领这条链路走通没有——求购响应是「我手里正好有你要的」，失物招领是
「我捡到的东西正好是你丢的」，难度天差地别，合并会让两边互相稀释。

没提交反馈的已确认线索单独数。`submit_lost_found_feedback` 的入口只对 `accepted` 的
线索开放，一条线索只能提交一次，**但提交反馈不是确认时的强制动作**——
`review_lost_found_lead` 的 `accept` 分支里没有任何东西引导发布者之后去填反馈。所以
「已确认但没提交反馈」是一个真实存在的中间状态：东西可能找回来了，发布者只是没回来
填；也可能没找回来，发布者觉得没必要填。本模块把它单独数出来，**不猜测是哪一种**。

`LostFoundLead.message` 是必填自由文本，`identifying_features` 是颜色品牌贴纸挂件。两个
字段都只报「写了没写」和长度，**一个字符的内容都不读**。前者可能写着电话、微信、
楼栋号；后者是为了让双方核对的，不是为了被统计的。`mutual_aid_feedback_notes.py` 已经
为互助反馈的 `note` 做了切词，那是另一个字段、另一套约束，本模块不把它复制过来。

不按用户聚合、不排名、不报任何人的名字。`respondent` 和 `reporter` 都只用来判断方向。
失物招领是站内信任浓度最高的一条链路，把它做成排行榜会直接毁掉它——「提供线索排行
榜」会让帮忙变成刷分，「没人认领的记录排行榜」会让丢东西的人难堪。清单第一格只带记录
标题和线索编号，那是运营要认出来的东西。

不改 `score_lost_found_posts` 的权重，不评价匹配质量。那个函数是给用户看的「为什么这条
可能相关」，本模块看的是「确认这一步走成了没有」。匹配得好不好和确认不确认是两件事，
混在一起会让两边都说不清。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import LostFoundLead, LostFoundPost, MutualAidFeedback


# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '近 7 天'),
    (30, '近 30 天'),
    (90, '近 90 天'),
)

# 给出方向前至少要有这么多条记录。失物招领比交易更低频，门槛定得比交易评价低，
# 但不能再低：低于这个数，一条记录的进出就是一个百分点的波动。
MIN_MEASURED_POSTS = 8

# 两组占比相差不满这么多个百分点，视为噪声。
NOISE_POINTS = 8

# 线索提交间隔的分档。按小时起步、按天封顶：失物招领的时间尺度是小时到天。
LEAD_LATENCY_BUCKETS = (
    (6 * 3600, '6 小时内'),
    (86400, '6-24 小时'),
    (259200, '1-3 天'),
    (604800, '3-7 天'),
    (None, '7 天以上'),
)

# 一条记录收到了几条线索。分母是记录数，理由见模块开头。
#
# 这一组桶和其余分档表不一样，用的是「闭区间上界」而不是 `_bucket_index` 的
# 左闭右开上限：线索数是一个小数量的计数，运营要看的是「0 条」「1 条」「2 到
# 3 条」「4 条以上」这四个形状，而不是「< 1」「< 3」这种切法。所以它在
# `_build_lead_count_stats` 里用自己的归类逻辑，不走 `_bucket_index`。
LEAD_COUNT_BUCKETS = (
    (0, '没有人提线索', '发布了，但没有人看见，或者看见了不敢认'),
    (1, '只有一条线索', '有人看见了，也可能是发布者自己到处问来的'),
    (3, '两三条线索', '记录被看见了几次，是最常见的形状'),
    (None, '四条以上', '记录热度高，但也说明候选池里能确认的不止一个'),
)

# 自由文本长度分档。线索说明必填，长度分档只用来兜数据质量。
MESSAGE_LENGTH_BUCKETS = (
    (20, '20 字以内'),
    (60, '21-60 字'),
    (150, '61-150 字'),
    (None, '150 字以上'),
)

# 记录类型。两条记录只有类型相反才可能匹配，`LostFoundPost.clean()` 直接挡掉了
# 同类型匹配，所以 lost 和 found 面对的候选池是完全不同的两件事。
POST_TYPE_LABELS = dict(LostFoundPost.TYPE_CHOICES)

# 线索状态。
LEAD_STATUS_LABELS = dict(LostFoundLead.STATUS_CHOICES)

# 清单上限。清单是给「现在去核实」用的，不是给归档用的。
LIST_LIMIT = 20


# 一条记录，连它的线索和反馈一起取回来。一次查完固定下来：线索分布、确认率、
# 时延、完成率各读一遍，按记录现算会让每条记录打好几遍库，而看板是一次打开全算的。
PostRecord = namedtuple('PostRecord', [
    'post', 'leads', 'feedback',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _format_duration(seconds):
    """把一个时长渲染成最粗但仍可读的单位。"""
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} 秒'
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


def _bucket_index(seconds, buckets):
    """分档下标。桶是 (上限, 标签[, 说明]) 的元组，互斥、左闭右开。

    上限为 None 的那一档是最后一档，兜住所有超过前一档的样本。
    """
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _fetch_posts(*, created_after=None, created_before=None):
    """按显式排序取出失物招领记录，连它们的线索和反馈一起取回来。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「最早一条」都会随机漂移。

    统计窗口按 `LostFoundPost.created_at` 切。线索和反馈都可以在窗口外才落库——
    那条记录的线索数算进来，但它的提交间隔不进时延分布（它不在窗口内被提交）。
    """
    posts = LostFoundPost.objects.select_related(
        'reporter', 'category', 'location', 'matched_post',
    ).order_by('created_at', 'id')
    if created_after is not None:
        posts = posts.filter(created_at__gte=created_after)
    if created_before is not None:
        posts = posts.filter(created_at__lte=created_before)
    posts = list(posts)

    if not posts:
        return []

    # 线索按记录分组，组内按时间排序，第一条就是发布者收到的最早那一条。
    leads = {}
    for lead in LostFoundLead.objects.filter(
        post__in=posts,
    ).select_related('post', 'respondent', 'related_post').order_by('created_at', 'id'):
        leads.setdefault(lead.post_id, []).append(lead)

    # 反馈按来源线索分组。失物招领只是它的两个来源之一，这里只认
    # lost_found_lead 那一半，求购响应的那一半不进来。
    #
    # 变量名叫 feedback_by_lead 而不是 feedback：下面构造 PostRecord 时同名的
    # 局部变量会把整张分组表塞进每一条记录的 feedback 字段里，那样所有记录都会
    # 共享同一份反馈表——一条线索的反馈会同时算进另一条记录的完成率。
    feedback_by_lead = {}
    for row in MutualAidFeedback.objects.filter(
        lost_found_lead__post__in=posts,
    ).select_related('submitted_by', 'lost_found_lead'):
        feedback_by_lead[row.lost_found_lead_id] = row

    # 一条记录带哪一份反馈：这条记录上被确认的那条线索的反馈。一条记录只会有一条
    # accepted（确认时其余 pending 被批量清掉），所以取 accepted 那一条即可；
    # 没有 accepted 的记录反馈为空。整张表按线索 id 索引，不能把表本身传给每条
    # 记录——那样所有记录会共享同一份反馈表，一条线索的反馈会同时算进另一条
    # 记录的完成率。
    records = []
    for post in posts:
        post_leads = leads.get(post.id, [])
        accepted = next(
            (lead for lead in post_leads if lead.status == 'accepted'), None,
        )
        records.append(PostRecord(
            post=post,
            leads=post_leads,
            feedback=feedback_by_lead.get(accepted.id) if accepted else None,
        ))
    return records


def _lead_outcome(lead):
    """一条线索的结局，把「被顺带清掉」和「被发布者拒绝」分开。

    这是本模块最要紧的一次拆分。`review_lost_found_lead` 的 `accept` 会把同一条
    记录下其余 `pending` 的线索批量改成 `rejected`，所以一条 `rejected` 的线索
    可能是发布者看过之后拒绝的，也可能是另一条线索被确认时顺带清掉的。前者是
    发布者的判断，后者是产品行为的副作用，两者的运营含义完全相反。

    靠 `status` 一个字段分不开，必须回到记录上：这条记录有没有 `accepted` 的线索。
    """
    if lead.status == 'rejected':
        return 'bulk_cleared' if lead.post_has_accepted else 'rejected_by_reporter'
    if lead.status == 'withdrawn':
        return 'withdrawn'
    return lead.status


def _annotate_records(records):
    """给每条线索补上「它所属记录有没有已确认的线索」这个标记。

    在 Python 里做，不写进数据库：这是一个只为统计存在的派生字段，落到模型上会让
    `LostFoundLead` 多一个没有任何写入路径会维护的字段。
    """
    accepted_post_ids = {
        record.post.id
        for record in records
        if any(lead.status == 'accepted' for lead in record.leads)
    }
    for record in records:
        for lead in record.leads:
            lead.post_has_accepted = record.post.id in accepted_post_ids
    return records


def _lead_count_index(count):
    """线索数归到哪一档。闭区间上界，和 `_bucket_index` 的左闭右开不一样。

    理由见 `LEAD_COUNT_BUCKETS` 上方的注释：线索数是小数量计数，运营要看的是
    「0 / 1 / 2-3 / 4+」这四个形状。
    """
    for index, bucket in enumerate(LEAD_COUNT_BUCKETS):
        limit = bucket[0]
        if limit is None or count <= limit:
            return index
    return len(LEAD_COUNT_BUCKETS) - 1


def _build_lead_count_stats(records):
    """一条记录收到了几条线索。分母是记录数，不是线索数。"""
    total = len(records)
    by_count = [0] * len(LEAD_COUNT_BUCKETS)
    for record in records:
        by_count[_lead_count_index(len(record.leads))] += 1

    rows = []
    for index, (limit, label, note) in enumerate(LEAD_COUNT_BUCKETS):
        rows.append({
            'label': label,
            'note': note,
            'count': by_count[index],
            'share': _percent(by_count[index], total),
        })

    lead_counts = [len(record.leads) for record in records]
    return {
        'total': total,
        'lead_count_rows': rows,
        'lead_count': sum(lead_counts),
        'leads_per_post': round(_mean(lead_counts), 2) if lead_counts else 0,
        'posts_without_lead_count': by_count[0],
        'posts_without_lead_rate': _percent(by_count[0], total),
        'posts_with_lead_count': total - by_count[0],
        'posts_with_lead_rate': _percent(total - by_count[0], total),
        'is_small_sample': total < MIN_MEASURED_POSTS,
        'min_measured_posts': MIN_MEASURED_POSTS,
    }


def _build_confirm_stats(records):
    """线索被确认的概率有多大，按 post_type 拆开报。

    `lost` 和 `found` 是两个完全不同的处境：丢东西的人发一条等着捡到的人来，
    捡到的人发一条等着丢东西的人来。两边的候选池大小和认领意愿完全不同，合并成
    一个数之后，无论它往哪边动都无法定位问题。

    分母是记录数。一条记录确认了多少条线索，就报多少——一条记录只会有一条
    `accepted`（确认时其余 `pending` 被批量清掉），所以这里的分子天然不超过 1。
    """
    rows = []
    for key, label in LostFoundPost.TYPE_CHOICES:
        group = [record for record in records if record.post.post_type == key]
        if not group:
            continue
        accepted = sum(
            1 for record in group
            if any(lead.status == 'accepted' for lead in record.leads)
        )
        matched = sum(1 for record in group if record.post.status == 'matched')
        rows.append({
            'key': key,
            'label': label,
            'post_count': len(group),
            'lead_count': sum(len(record.leads) for record in group),
            'accepted_count': accepted,
            'confirm_rate': _percent(accepted, len(group)),
            'matched_post_count': matched,
            'matched_rate': _percent(matched, len(group)),
        })
    rows.sort(key=lambda row: -row['post_count'])

    total_accepted = sum(row['accepted_count'] for row in rows)
    return {
        'rows': rows,
        'post_count': len(records),
        'accepted_count': total_accepted,
        'confirm_rate': _percent(total_accepted, len(records)),
        'is_small_sample': len(records) < MIN_MEASURED_POSTS,
        'min_measured_posts': MIN_MEASURED_POSTS,
    }


def _build_rejection_stats(records):
    """被驳回的线索里，哪些是发布者拒绝的，哪些是被顺带清掉的。

    这是本模块存在的首要理由。`review_lost_found_lead` 的 `accept` 会把同一条
    记录下其余 `pending` 的线索批量改成 `rejected`，所以「被驳回的线索」里有很大
    一部分不是发布者一条条看过之后拒绝的。把两类混在一个「发布者拒绝率」里报，
    会得出「发布者很不认可大家提供的线索」这个完全错误的结论。
    """
    leads = [lead for record in records for lead in record.leads]
    rejected = [lead for lead in leads if lead.status == 'rejected']
    bulk_cleared = [lead for lead in rejected if _lead_outcome(lead) == 'bulk_cleared']
    truly_rejected = [lead for lead in rejected if _lead_outcome(lead) == 'rejected_by_reporter']
    withdrawn = [lead for lead in leads if lead.status == 'withdrawn']

    # 发起过线索的记录数，作为「发布者拒绝率」的分母。一条记录被发布者拒绝了
    # 三条线索和拒绝了一条线索，在记录层面都只是「这个发布者拒绝过线索」，而
    # 发布者拒绝率是一个关于发布者行为的比率，不是关于线索的比率。
    posts_with_rejection = {
        lead.post_id for lead in truly_rejected
    }
    posts_with_leads = {
        record.post.id for record in records if record.leads
    }

    rows = [
        {
            'key': 'rejected_by_reporter',
            'label': '发布者看过之后拒绝',
            'count': len(truly_rejected),
            'share': _percent(len(truly_rejected), len(leads)),
            'note': '发布者一条条看过，判断暂不匹配',
        },
        {
            'key': 'bulk_cleared',
            'label': '另一条被确认时顺带清掉',
            'count': len(bulk_cleared),
            'share': _percent(len(bulk_cleared), len(leads)),
            'note': '不是发布者的判断，是确认匹配时的产品行为',
        },
        {
            'key': 'withdrawn',
            'label': '提供者自己撤回',
            'count': len(withdrawn),
            'share': _percent(len(withdrawn), len(leads)),
            'note': '有人本来想帮忙又改了主意',
        },
    ]
    return {
        'rows': rows,
        'lead_count': len(leads),
        'rejected_count': len(rejected),
        'bulk_cleared_count': len(bulk_cleared),
        'bulk_cleared_share_of_rejected': _percent(len(bulk_cleared), len(rejected)),
        'rejected_by_reporter_count': len(truly_rejected),
        'rejected_by_reporter_share': _percent(len(truly_rejected), len(leads)),
        'posts_with_rejection_count': len(posts_with_rejection),
        'posts_with_rejection_rate': _percent(len(posts_with_rejection), len(posts_with_leads)),
        'posts_with_leads_count': len(posts_with_leads),
        'withdrawn_count': len(withdrawn),
        'withdrawn_share': _percent(len(withdrawn), len(leads)),
    }


def _build_latency_stats(records):
    """发布者多久收到第一条线索。只报第一条，它是「有没有被看见」的第一个信号。"""
    latencies = []
    for record in records:
        if not record.leads:
            continue
        first = min(record.leads, key=lambda lead: lead.created_at)
        delta = first.created_at - record.post.created_at
        seconds = delta.total_seconds()
        if seconds < 0:
            # 线索早于记录：要么记录补记晚了，要么数据被改过。这种样本算不出
            # 真实的等待时长，单独数出来，不塞进任何一档。
            continue
        latencies.append(seconds)

    buckets = [
        {'label': label, 'count': 0} for _limit, label in LEAD_LATENCY_BUCKETS
    ]
    for seconds in latencies:
        buckets[_bucket_index(seconds, LEAD_LATENCY_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(latencies))

    # 24 小时内收到第一条线索的记录：失物招领的时效窗口就在一天里，一条记录
    # 发布当天没人理，之后被看见的概率会掉得很厉害。
    same_day = sum(1 for seconds in latencies if seconds < 86400)

    return {
        'measured_count': len(latencies),
        'bucket_rows': buckets,
        'median_label': _format_duration(_percentile(latencies, 0.5)),
        'p90_label': _format_duration(_percentile(latencies, 0.9)),
        'average_label': _format_duration(_mean(latencies)),
        'fastest_label': _format_duration(min(latencies)) if latencies else '—',
        'slowest_label': _format_duration(max(latencies)) if latencies else '—',
        'same_day_count': same_day,
        'same_day_rate': _percent(same_day, len(latencies)),
    }


def _build_feedback_stats(records):
    """确认之后，东西找回来了没有。只报失物招领自己的，不和求购响应合并。

    `MutualAidFeedback` 有两个来源，`lost_found_lead` 和 `demand_response`。
    `mutual_aid_feedback_insights` 把它们并进一个「已确认互助」总数报完成率，那是
    那个模块的口径。本模块看的是失物招领这条链路走通没有——求购响应是「我手里
    正好有你要的」，失物招领是「我捡到的东西正好是你丢的」，难度天差地别，合并
    会让两边互相稀释。

    「已确认但没提交反馈」单独数出来，不猜测是哪一种：`review_lost_found_lead`
    的 `accept` 分支里没有任何东西引导发布者之后去填反馈，所以这是一个真实存在
    的中间状态——东西可能找回来了，发布者只是没回来填；也可能没找回来，发布者
    觉得没必要填。
    """
    accepted = [
        record for record in records
        if any(lead.status == 'accepted' for lead in record.leads)
    ]
    with_feedback = [record for record in accepted if record.feedback]
    without_feedback = [record for record in accepted if not record.feedback]

    completed = [record for record in with_feedback if record.feedback.outcome == 'completed']
    unresolved = [record for record in with_feedback if record.feedback.outcome == 'unresolved']

    return {
        'accepted_count': len(accepted),
        'feedback_count': len(with_feedback),
        'feedback_rate': _percent(len(with_feedback), len(accepted)),
        'without_feedback_count': len(without_feedback),
        'without_feedback_note': (
            '已确认但没回来填反馈，不猜测是找回来了还是没找回来'
        ) if without_feedback else '',
        'completed_count': len(completed),
        'unresolved_count': len(unresolved),
        'completion_rate': _percent(len(completed), len(with_feedback)),
        'is_small_sample': len(with_feedback) < MIN_MEASURED_POSTS,
        'min_measured_posts': MIN_MEASURED_POSTS,
        'rows': [
            {
                'key': 'completed',
                'label': '东西找回来了',
                'count': len(completed),
                'share': _percent(len(completed), len(with_feedback)),
            },
            {
                'key': 'unresolved',
                'label': '暂未解决',
                'count': len(unresolved),
                'share': _percent(len(unresolved), len(with_feedback)),
            },
        ],
    }


def _build_message_stats(records):
    """线索说明只报「写了没写」和长度，一个字符的内容都不读。

    `message` 是必填自由文本，可能写着电话、微信、楼栋号。`identifying_features`
    是颜色品牌贴纸挂件，是为了让双方核对的。两个字段都只报长度分档，不切词、
    不聚类、不抽关键词——`mutual_aid_feedback_notes.py` 已经为互助反馈的 `note`
    做了切词，那是另一个字段、另一套约束，本模块不把它复制过来。
    """
    leads = [lead for record in records for lead in record.leads]
    filled = [lead for lead in leads if (lead.message or '').strip()]
    lengths = [len(lead.message.strip()) for lead in filled]

    buckets = [
        {'label': label, 'count': 0} for _limit, label in MESSAGE_LENGTH_BUCKETS
    ]
    for length in lengths:
        index = 0
        for position, (limit, _label) in enumerate(MESSAGE_LENGTH_BUCKETS):
            if limit is None or length <= limit:
                index = position
                break
        buckets[index]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(lengths))

    features = [
        record for record in records if (record.post.identifying_features or '').strip()
    ]
    return {
        'lead_count': len(leads),
        'filled_count': len(filled),
        'fill_rate': _percent(len(filled), len(leads)),
        'empty_count': len(leads) - len(filled),
        'average_length': round(_mean(lengths), 1) if lengths else 0,
        'max_length': max(lengths) if lengths else 0,
        'length_rows': buckets,
        'feature_filled_count': len(features),
        'feature_fill_rate': _percent(len(features), len(records)),
    }


def _build_pending_rows(records):
    """还挂着 pending 的线索，给「现在去核实」用。"""
    rows = []
    for record in records:
        for lead in record.leads:
            if lead.status != 'pending':
                continue
            rows.append({
                'lead': lead,
                'post': record.post,
                'days_label': _format_duration(
                    (timezone.now() - record.post.created_at).total_seconds()
                ),
            })
    rows.sort(key=lambda row: row['lead'].created_at)
    return {
        'rows': rows[:LIST_LIMIT],
        'row_total': len(rows),
        'row_limit': LIST_LIMIT,
    }


def _summary(lead_count, confirm, rejection, latency, feedback):
    """一段话把这个模块的读数讲完，样本不足的地方明说。"""
    parts = []
    if lead_count['total']:
        parts.append(
            f"周期内 {lead_count['total']} 条失物招领记录，"  # noqa: E501
            f"{lead_count['posts_with_lead_count']} 条收到了线索"
            f"（{lead_count['posts_with_lead_rate']}%），平均 "
            f"{lead_count['leads_per_post']} 条"
        )
    else:
        parts.append('当前周期还没有失物招领记录。')

    if confirm['rows']:
        shapes = '、'.join(
            f"{row['label']} {row['confirm_rate']}%"
            for row in confirm['rows']
        )
        parts.append(f"。按记录类型拆开，确认率是 {shapes}")
        if confirm['is_small_sample']:
            parts.append(f"，不足 {MIN_MEASURED_POSTS} 条，占比只作参考")

    if rejection['rejected_count']:
        parts.append(
            f"。{rejection['rejected_count']} 条线索被驳回，其中 "
            f"{rejection['bulk_cleared_count']} 条"
            f"（{rejection['bulk_cleared_share_of_rejected']}%）是另一条被确认时"
            f"顺带清掉的，不是发布者拒绝的"
        )

    if latency['measured_count']:
        parts.append(
            f"。发布者收到第一条线索的中位间隔 {latency['median_label']}，"
            f"{latency['same_day_rate']}% 在 24 小时内"
        )

    if feedback['accepted_count']:
        parts.append(
            f"。{feedback['accepted_count']} 条记录确认了线索，"
            f"{feedback['feedback_count']} 条提交了反馈"
            f"（{feedback['feedback_rate']}%）"
        )
        if feedback['feedback_count']:
            parts.append(
                f"，其中东西找回来了 {feedback['completed_count']} 条"
                f"（{feedback['completion_rate']}%）"
            )
        if feedback['without_feedback_count']:
            parts.append(
                f"，{feedback['without_feedback_count']} 条已确认但没回来填反馈"
            )
    return ''.join(parts)


def build_lost_found_lead_quality(days=30, now=None):
    """统计失物招领线索的分布、确认率、驳回构成与完成反馈，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    records = _annotate_records(
        _fetch_posts(created_after=start, created_before=now)
    )

    lead_count = _build_lead_count_stats(records)
    confirm = _build_confirm_stats(records)
    rejection = _build_rejection_stats(records)
    latency = _build_latency_stats(records)
    feedback = _build_feedback_stats(records)
    message = _build_message_stats(records)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(records),
        'summary': _summary(lead_count, confirm, rejection, latency, feedback),
        'lead_count': lead_count,
        'confirm': confirm,
        'rejection': rejection,
        'latency': latency,
        'feedback': feedback,
        'message': message,
        'pending_rows': _build_pending_rows(records),
        'min_measured_posts': MIN_MEASURED_POSTS,
        'noise_points': NOISE_POINTS,
    }

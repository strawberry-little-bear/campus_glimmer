# -*- coding: utf-8 -*-
"""失物招领确认匹配之后，记录停在哪一种终局里。

``review_lost_found_lead`` 的 ``accept`` 分支做四件事：把线索置为 ``accepted``、把
记录置为 ``matched``、把关联的那条记录也置为 ``matched`` 并互填 ``matched_post``、
把同一条记录下其余 ``pending`` 的线索批量清成 ``rejected``。做完这四件事，这条
记录的状态就再也没有被任何东西读过。

而 ``matched`` 在这个系统里是一个**单向门**。全站只有四处写
``LostFoundPost.status``，其中三处的前置条件都是 ``status == 'active'``：一条记录
一旦变成 ``matched``，发布者再也关不掉它（``close_lost_found`` 只放 ``active``），
过期任务也再也不碰它（``expire_lost_found_posts`` 只扫 ``active``）。于是一类记录
会这样留在库里：``expires_at`` 早就过了，``status`` 还是 ``matched``，它不出现在
公开列表里（``_lost_found_queryset`` 只放 ``active``），却一直挂在发布者的「我的
失物招领」里。

这不是理论上的边角，这是这条链路**唯一的出口被焊死**：确认匹配是失物招领的成功
结局，而成功结局没有自己的收尾路径。本模块把这个结局读出来。

``lost_found_lead_quality`` 已经报了这条链路的前四步（收到几条线索、确认率、东西
找回来了没有、驳回怎么分类），也报了一个 ``matched_rate``——「有多少条记录走到了
这里」。它不报「走到这里之后，这些记录现在是什么样子」。后者才是运营能动手的地方。

本模块只报告，不改写任何一条记录、任何一条线索、任何一份反馈。

几处口径上的取舍比数字本身更要紧。

**四种终局分开数，其中三档预期是空的。** ``closed`` / ``expired`` / ``active``
这三档在当前代码里都到不了——``close_lost_found``、``expire_lost_found_posts``、
``edit_lost_found`` 三处的前置条件把 ``matched`` 挡在外面。报出来，是为了让「单向
门」这件事在看板上看得见：一栏永远是 0 的状态，比一段注释更难被忽略。空分母报
``None`` 显示「—」，不报 0。

**「仍标记已匹配」不读成问题。** 发布者确认了匹配，然后什么都没做，这是最自然的
结局——东西找回来了，这件事在用户心里已经结了。真正要看的是下一节：截止时间过了
一个月，状态还挂着。

**截止时间已过的记录按过期多久分档，不报取消率。** 一条 ``matched`` 挂着没动，
可能只是发布者忘了，也可能是他觉得没必要收尾。观测数据里分不开这两者，所以只报
「挂着」这个事实，不报它后来有没有被重新打开。``expires_at`` 为空的记录不计入
这个分布，单独数出来——没有截止时间的记录谈不上过期，那是右删失样本。

**匹配是双向的，但只报「两边是不是同一件事」，不报另一边是谁。** ``accept`` 会把
关联记录也置为 ``matched`` 并互填 ``matched_post``。单边匹配不是 bug——用户可能
删掉了自己那条记录。但它是「这次匹配在两边眼里是不是同一件事」的直接证据。不报
另一条记录的标题、不报发布者是谁：标题是自由文本，可能写着证件号。

**反馈提交率按终局拆，但不重复 ``lost_found_lead_quality`` 的结论。** 那个模块报
反馈维度（提交率、完成率、标签），本模块报终局维度（哪种终局的记录更有人回来
填）。一条 ``matched`` 挂着没人收尾的记录，和一条已经收尾的记录，发布者回来填
反馈的动机完全不同；混在一起报，会把「没收尾的记录也没人填反馈」这个信号稀释掉，
而它正好指向同一件事——收尾路径缺失。

**不按用户排名、不报任何用户名、不列任何记录标题。** 谁在什么时候确认了哪条失物
招领是用户的私事。记录 ID 可以报，它是数字不是身份。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import LostFoundLead, LostFoundPost, MutualAidFeedback

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一档要给方向，至少这么多条记录。全站统一量级。
MIN_BUCKET_SAMPLE = 12

# 匹配记录总数低于此数时，整张表只报数字不给方向。
MIN_MATCHED_POSTS = 12

# 确认匹配之后的四种终局。前三种在当前代码里都到不了，理由见模块开头；第四种是
# 默认结局。分开报，不相加。
OUTCOME_LABELS = {
    'still_matched': '仍标记已匹配',
    'closed': '已被发布者关闭',
    'expired': '已过期',
    'reopened': '回到寻找中',
}

OUTCOME_ORDER = ('still_matched', 'closed', 'expired', 'reopened')

# 「仍标记已匹配」里，截止时间已过的记录按过期多久分档。按天：失物招领的时间尺度
# 是天，报「19 小时」会让运营以为是小时级的问题。
OVERDUE_BUCKETS = (
    (7 * 86400, '7 天以内'),
    (30 * 86400, '7-30 天'),
    (None, '30 天以上'),
)

# 清单上限。清单是给「现在去核实」用的，不是给归档用的。
LIST_LIMIT = 20


# 一条匹配过的记录，连同它此刻的终局、过期多久、关联的那条记录、它自己的已确认
# 线索，以及那条线索上的反馈。用 namedtuple 而不是 dict，是因为下面每一节都要同时
# 读七八个字段，dict 的键名拼错要到模板里才炸。
MatchedPost = namedtuple(
    'MatchedPost',
    'post outcome overdue_seconds counterpart accepted_lead feedback',
)


def _percent(part, whole):
    """占比一律返回 None 表示空分母，不返回 0。

    0 会被读成「有，占比很小」，而这里的事实是「分母不存在」。看板模板把 None
    显示成「—」，和 0 区分开。
    """
    return round(part / whole * 100, 1) if whole else None


def _format_days(seconds):
    """过期多久一律按天说话，不足一天的说「当天」。"""
    if seconds is None:
        return '—'
    if seconds < 86400:
        return '当天'
    return f'{int(seconds // 86400)} 天'


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _outcome_of(post):
    """一条匹配过的记录，此刻停在哪一种终局。

    ``matched`` 是单向门，所以 ``closed`` / ``expired`` / ``active`` 三档在当前
    代码里都到不了。这个函数照实判断，不预设哪一档一定有数据——代码会变，
    看板该在代码变的那天自己跟上。
    """
    if post.status == 'closed':
        return 'closed'
    if post.status == 'expired':
        return 'expired'
    if post.status == 'active':
        return 'reopened'
    return 'still_matched'


def _fetch_matched_posts(*, now):
    """按显式排序取出全部匹配过的记录，连它们的已确认线索和反馈一起取回来。

    匹配过的记录有两类：现在 ``status == 'matched'`` 的，和历史上被匹配过、后来
    状态变了的。第二类只能通过 ``matched_post`` 反查——一条记录的
    ``matched_post`` 被填过，就说明它参与过一次匹配，不管它现在是什么状态。

    还要补一次反向关联：``A.matched_post`` 指向 ``B``，那么 ``B`` 也参与过一次
    匹配。正向查询只覆盖「自己填了 matched_post」，漏掉「被别人指向、自己没填」
    的那半边。正常情况下两边互填不会漏，但记录可能被删、可能只填了一边。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的分组，比一份错的分组更难用。

    不按统计周期过滤。这里问的是「此刻有多少条记录停在 matched 上」，一条上个月
    就匹配、这个月才过期的记录，恰恰是最不该被周期切掉的一批。
    """
    posts = {}
    for post in LostFoundPost.objects.select_related(
        'reporter', 'category', 'location', 'matched_post',
    ).order_by('pk'):
        if post.status == 'matched' or post.matched_post_id:
            posts[post.pk] = post

    referenced = LostFoundPost.objects.filter(
        matched_by_posts__isnull=False,
    ).select_related(
        'reporter', 'category', 'location', 'matched_post',
    ).order_by('pk')
    for post in referenced:
        posts.setdefault(post.pk, post)

    ordered = [posts[pk] for pk in sorted(posts)]
    if not ordered:
        return []

    # 已确认线索按记录分组：一条记录只会有一条 accepted（确认时其余 pending 被批量
    # 清掉），所以一条记录最多挂一条。
    accepted = {}
    for lead in LostFoundLead.objects.filter(
        post__in=ordered, status='accepted',
    ).select_related('post').order_by('pk'):
        accepted.setdefault(lead.post_id, lead)

    # 反馈按线索取回。一条线索只能提交一次反馈（OneToOne），所以一条线索最多一条。
    feedback = {}
    for row in MutualAidFeedback.objects.filter(
        lost_found_lead__post__in=ordered,
    ).select_related('lost_found_lead', 'lost_found_lead__post'):
        if row.lost_found_lead_id:
            feedback.setdefault(row.lost_found_lead_id, row)

    enriched = []
    for post in ordered:
        lead = accepted.get(post.pk)
        if post.expires_at and post.expires_at < now:
            overdue_seconds = (now - post.expires_at).total_seconds()
        else:
            overdue_seconds = None
        enriched.append(MatchedPost(
            post=post,
            outcome=_outcome_of(post),
            overdue_seconds=overdue_seconds,
            counterpart=post.matched_post,
            accepted_lead=lead,
            feedback=feedback.get(lead.pk) if lead else None,
        ))
    return enriched


def _build_outcome_rows(posts, *, now):
    """四种终局各自多少条，以及每一档截止时间已过的有多少。

    「仍标记已匹配」和「已被发布者关闭」是两件事：前者是发布者确认之后什么都没做，
    后者是发布者特意回来收了个尾。混在一个数里，就看不出收尾这件事有没有人在做。
    """
    rows = []
    for key in OUTCOME_ORDER:
        subset = [p for p in posts if p.outcome == key]
        overdue = [p for p in subset if p.overdue_seconds is not None]
        rows.append({
            'key': key,
            'label': OUTCOME_LABELS[key],
            'post_count': len(subset),
            'overdue_count': len(overdue),
            'share': _percent(len(subset), len(posts)),
            'is_small_sample': len(subset) < MIN_BUCKET_SAMPLE,
        })
    return rows


def _build_overdue_stats(posts):
    """仍标记已匹配的记录里，截止时间已过的那些，过期了多久。

    分母是「仍标记已匹配」这一档，不是全部匹配记录——问的是「挂着的这些记录里有
    多少已经过了截止时间」，拿全部匹配记录当分母会把已经收尾的也算进去。

    ``expires_at`` 为空的记录不计入分布，单独数出来：没有截止时间的记录谈不上
    过期，那是右删失样本。
    """
    still = [p for p in posts if p.outcome == 'still_matched']
    with_deadline = [p for p in still if p.overdue_seconds is not None]
    no_deadline = [p for p in still if p.overdue_seconds is None]

    # 分档互斥、左闭右开，和其余模块的 _bucket_index 同一套规则：上限为 None 的
    # 最后一档兜住所有超过前一档的样本。不另写一套边界，否则两条规则的差集会
    # 悄悄漏掉或重复计数。
    rows = []
    lower = 0
    for limit, label in OVERDUE_BUCKETS:
        if limit is None:
            count = sum(1 for p in with_deadline if p.overdue_seconds >= lower)
        else:
            count = sum(
                1 for p in with_deadline
                if lower <= p.overdue_seconds < limit
            )
        rows.append({
            'label': label,
            'count': count,
            'share': _percent(count, len(with_deadline)),
        })
        if limit is not None:
            lower = limit

    return {
        'measured_count': len(with_deadline),
        'no_deadline_count': len(no_deadline),
        'mean_label': _format_days(
            sum(p.overdue_seconds for p in with_deadline) / len(with_deadline)
        ) if with_deadline else '—',
        'oldest_label': _format_days(
            max(p.overdue_seconds for p in with_deadline)
        ) if with_deadline else '—',
        'rows': rows,
        'is_small_sample': len(with_deadline) < MIN_BUCKET_SAMPLE,
    }


def _build_counterpart_stats(posts):
    """匹配是不是双向的：互为匹配，还是只有一边认这笔账。

    ``accept`` 会把关联记录也置为 ``matched`` 并互填 ``matched_post``。单边匹配
    不是 bug——用户可能删掉了自己那条记录。但它是「这次匹配在两边眼里是不是同一
    件事」的直接证据，而这件事此前没有任何读者。
    """
    with_counterpart = [p for p in posts if p.counterpart is not None]
    mutual = []
    one_sided = []
    for entry in with_counterpart:
        other = entry.counterpart
        if other is not None and other.matched_post_id == entry.post.pk:
            mutual.append(entry)
        else:
            one_sided.append(entry)

    return {
        'measured_count': len(with_counterpart),
        'mutual_count': len(mutual),
        'one_sided_count': len(one_sided),
        'mutual_rate': _percent(len(mutual), len(with_counterpart)),
        'no_counterpart_count': len(posts) - len(with_counterpart),
        'is_small_sample': len(with_counterpart) < MIN_BUCKET_SAMPLE,
    }


def _build_feedback_by_outcome(posts, *, now):
    """按终局拆反馈提交率，但只报终局维度，不重复反馈维度的结论。

    ``lost_found_lead_quality`` 已经报了 ``accepted`` 记录的反馈提交率、完成率和
    标签分布。这里按终局再拆一次：一条 ``matched`` 挂着没人收尾的记录，和一条已经
    收尾的记录，发布者回来填反馈的动机完全不同。混在一起报，会把「没收尾的记录
    也没人填反馈」这个信号稀释掉，而它正好指向同一件事——收尾路径缺失。
    """
    rows = []
    for key in OUTCOME_ORDER:
        subset = [p for p in posts if p.outcome == key]
        if not subset:
            continue
        accepted = [p for p in subset if p.accepted_lead is not None]
        with_feedback = [p for p in accepted if p.feedback is not None]
        rows.append({
            'key': key,
            'label': OUTCOME_LABELS[key],
            'accepted_count': len(accepted),
            'feedback_count': len(with_feedback),
            'feedback_rate': _percent(len(with_feedback), len(accepted)),
        })
    return {
        'rows': rows,
        'is_small_sample': len(posts) < MIN_MATCHED_POSTS,
    }


def _build_list(posts, *, now, limit=LIST_LIMIT):
    """截止时间已过最久的那些记录，按过期多久降序。

    清单是给「现在去核实」用的，不是给归档用的。只带记录 ID、过期多久、和它有没有
    关联记录——**不带标题、不带发布者**：标题是自由文本，可能写着证件号；发布者是
    身份，做成一份清单，失物招领就变成了公开表态。
    """
    overdue = sorted(
        (p for p in posts if p.overdue_seconds is not None),
        key=lambda p: (-p.overdue_seconds, p.post.pk),
    )
    rows = [
        {
            'post_id': p.post.pk,
            'post_type_label': p.post.get_post_type_display(),
            'overdue_label': _format_days(p.overdue_seconds),
            'has_counterpart': p.counterpart is not None,
            'has_feedback': p.feedback is not None,
        }
        for p in overdue[:limit]
    ]
    return {
        'rows': rows,
        'row_limit': limit,
        'row_total': len(overdue),
    }


def _summary(outcome_rows, overdue_stats, counterpart_stats):
    """一句话说清这批匹配记录现在的形状。"""
    still = next(r for r in outcome_rows if r['key'] == 'still_matched')
    parts = [f"{still['post_count']} 条记录仍标记已匹配"]
    if overdue_stats['measured_count']:
        parts.append(
            f"其中 {overdue_stats['measured_count']} 条截止时间已过，"
            f"最久的一条已挂 {overdue_stats['oldest_label']}"
        )
    if counterpart_stats['one_sided_count']:
        parts.append(
            f"{counterpart_stats['one_sided_count']} 条只有一边认这笔账"
        )
    return '；'.join(parts) + '。'


def build_lost_found_matched_outcome(days=30, now=None):
    """统计失物招领确认匹配之后的终局分布、过期时长与匹配双向性，只报告不改写。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    posts = _fetch_matched_posts(now=now)

    outcome_rows = _build_outcome_rows(posts, now=now)
    overdue_stats = _build_overdue_stats(posts)
    counterpart_stats = _build_counterpart_stats(posts)
    feedback = _build_feedback_by_outcome(posts, now=now)
    listing = _build_list(posts, now=now)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(posts),
        'summary': _summary(outcome_rows, overdue_stats, counterpart_stats),
        'matched_total': len(posts),
        'outcome_rows': outcome_rows,
        'overdue_stats': overdue_stats,
        'counterpart_stats': counterpart_stats,
        'feedback': feedback,
        'list': listing,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'min_matched_posts': MIN_MATCHED_POSTS,
    }
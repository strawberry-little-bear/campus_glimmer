# -*- coding: utf-8 -*-
"""校园身份认证的最后一步，用户到底花了多久才走完。

`accounts.CampusVerification` 表里躺着三个一直有人在写、却从来没有人读过它们
差值的字段：

- `token_issued_at`——每一次「重新发送验证邮件」都会刷新它；
- `verified_at`——用户点开邮件里的链接、令牌通过校验的那一刻写上它；
- `status`——`pending` → `verified`，另外两个取值从来没有被任何代码写过。

`token_issued_at` 唯一的读者是 `verify_campus_email` 里的 24 小时过期判断。
`verified_at` 唯一的读者是 `is_verified` 属性和两处 `verified_at__isnull=False`
的筛选。**两个字段的差值——用户从收到邮件到点开链接花了多久——从来没有任何
一行代码算过。**

校园身份认证是这套系统里唯一一个「平台主动发一封邮件、然后等用户自己回来点
链接」的闭环，而它是可信交易的入口：交易安全卡上「发布者已完成校园身份认证」
这条正面信号、公开资料页上的认证标识，全靠它。一个用户点了两次「重新发送验证
邮件」还是没有认证成功，运营在后台看到的是同一个 `pending`，和第一次发邮件的
用户一模一样——分不出他是没收到邮件、收到了没空点、还是压根没找到那封邮件。

这个模块把两个时间戳读出来，只报告，不改写任何一条认证记录。几处口径上的取舍
比数字本身更要紧，它们都在下面的注释里。

分母只收已经认证成功的记录。还挂在 `pending` 上的认证没有完成时刻，谈不上花了
多久——那是右删失样本，单独数出来，不丢也不混。`unverified` 状态的记录压根没有
发过邮件，`token_issued_at` 是空的，把它们算进去会让平均耗时凭空变长。

耗时按分钟和小时分档，不按秒。一次认证等待的时间尺度是分钟到小时，报「平均
3 分 12 秒」听起来很精确，但运营要动作的对象是「邮件到没到」「链接好不好找」，
不是毫秒级的优化。分档按「十分钟内 / 一小时内 / 一天内 / 超过一天」走，每一档
对应一个可动作的判断。

重发次数只当参照，不当成分母。`token_issued_at` 会被重发覆盖，记录里只留着最后
一封邮件的发出时间，前几次是什么时候发的没有留痕。所以本模块不单独数「重发过
几次」——那个数报不出来——只把「最后一封邮件发出到认证成功隔了多久」当参照写进
报告，并说明它为什么只是参照。

按域名分组报，不按用户分组。同一个学校的校园邮箱，收件速度、垃圾邮件策略、
学生查邮件的习惯都是同一个量级。按域名报，运营能看出「是某一类邮箱特别慢」还是
「所有邮箱都慢」——前者是邮件服务商的问题，后者是我们自己的文案或链接问题。
按用户报个体耗时是隐私面很大的东西，而运营要改的是邮件文案和链接位置，
不是某个用户点了多久。

模块不判定「为什么慢」。一个期末周的学生三天后才看到邮件，不是平台的错；
一个把验证邮件当垃圾邮件拦掉的邮箱，也不是用户的错。报事实，不裁断。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from accounts.models import CampusVerification

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一组样本至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条
# 样本上几个百分点的差距是噪声。
MIN_MEASURED_VERIFICATIONS = 12

# 域名清单最多列这么多条。域名是给「哪一类邮箱有问题」用的，长尾域名一两条
# 样本说明不了任何事，列出来只会让人以为它有问题。
DOMAIN_LIST_LIMIT = 12

# 待核实清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
SLOW_LIST_LIMIT = 20

# 耗时分档。互斥，左闭右开，按分钟和小时切。最后一档是尾巴：隔了一天以上才
# 点开链接的，多半是邮件被拦了或者用户早就忘了这回事，那正是要动作的一批。
ELAPSED_BUCKETS = (
    (600, '十分钟内', '邮件一到就点了，链接好找、邮件也没被拦'),
    (3600, '十分钟到一小时', '当天就看到并点了，是正常的收件节奏'),
    (86400, '一小时到一天', '隔了几个小时，可能是在忙，也可能邮件沉到了后面'),
    (None, '超过一天', '隔了一天以上，多半被当成垃圾邮件，或者用户已经忘了'),
)

# 域名维度上「慢」的判定门槛：超过一天才认证的比例高过这个百分点，才值得
# 运营去看这个域名的收件情况。阈值和其余模块的噪声线同一个量级。
SLOW_DOMAIN_POINTS = 8

# 一条认证的完整轨迹，一次查完固定下来。耗时分布和域名分组各读一遍，
# 按记录现算会让每条记录打两遍库，而看板是一次打开全算的。
Verification = namedtuple('Verification', [
    'row', 'elapsed_seconds', 'domain_name',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime``
    类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_elapsed(seconds):
    """耗时按分钟和小时说话，不足十分钟的说「十分钟内」。

    这个模块报的是分钟和小时尺度的量。报「平均 3 分 12 秒」会让运营以为
    平台该去优化毫秒级的东西，而真正要动作的是「邮件有没有到」「链接好不好找」。
    """
    if seconds is None:
        return '—'
    if seconds < 600:
        return '十分钟内'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分钟'
    if seconds < 86400:
        return f'{int(seconds // 3600)} 小时'
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


def _fetch_verifications(*, verified_after=None, verified_before=None):
    """按显式排序取出窗口内已经认证成功的记录，带上域名。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。

    窗口按 ``verified_at`` 取，不按 ``token_issued_at``。要回答的是「这段时间里
    完成的认证，从收到邮件到点开链接花了多久」，所以终点是认证成功；一条上周
    才发邮件、这周才认证成功的记录，等的是上周的那封邮件。

    `unverified` 和 `rejected` 两种状态直接被条件挡在外面：前者压根没有发过
    邮件，`token_issued_at` 是空的，谈不上耗时；后者没有任何写路径会产生它，
    把它算进来只会得到一个永远为零的分组。
    """
    queryset = CampusVerification.objects.filter(
        status='verified',
        verified_at__isnull=False,
        token_issued_at__isnull=False,
    )
    if verified_after is not None:
        queryset = queryset.filter(verified_at__gte=verified_after)
    if verified_before is not None:
        queryset = queryset.filter(verified_at__lte=verified_before)

    verifications = []
    for row in queryset.order_by('verified_at', 'pk'):
        elapsed = max(
            (row.verified_at - row.token_issued_at).total_seconds(), 0.0,
        )
        verifications.append(Verification(
            row=row,
            elapsed_seconds=elapsed,
            domain_name=row.domain_name or '未填写学校',
        ))
    return verifications


def _domain_rows(verifications):
    """按域名分组的耗时与慢认证占比，慢的排在前面。

    排序先按「超过一天的比例」，再按样本量，都相同时按域名名稳定。样本量进
    排序键是必须的：一两条样本的域名比例不是 0 就是 100，不按样本量压下去，
    榜单前几行会被只认证成功过一个人的域名占满，而那说明不了任何事。
    """
    grouped = {}
    for verification in verifications:
        grouped.setdefault(verification.domain_name, []).append(
            verification.elapsed_seconds,
        )

    rows = []
    for name, elapsed_list in grouped.items():
        slow = sum(1 for seconds in elapsed_list if seconds >= 86400)
        rows.append({
            'domain_name': name,
            'count': len(elapsed_list),
            'median_label': _format_elapsed(
                _percentile(elapsed_list, 0.5),
            ),
            'slow_count': slow,
            'slow_rate': _percent(slow, len(elapsed_list)),
        })
    rows.sort(key=lambda row: (-row['slow_rate'], -row['count'], row['domain_name']))
    return rows


def _slow_rows(verifications, limit=SLOW_LIST_LIMIT):
    """隔了超过一天才认证成功的记录，按耗时倒序。"""
    slow = [
        verification for verification in verifications
        if verification.elapsed_seconds >= 86400
    ]
    slow.sort(key=lambda v: (-v.elapsed_seconds, v.row.pk))
    return [
        {
            'username': v.row.user.username,
            'campus_email': v.row.campus_email,
            'domain_name': v.domain_name,
            'elapsed_label': _format_elapsed(v.elapsed_seconds),
            'elapsed_seconds': v.elapsed_seconds,
            'token_issued_at': v.row.token_issued_at,
            'verified_at': v.row.verified_at,
        }
        for v in slow[:limit]
    ]


def _summary(stats, domain_rows, verifications, slow_rows, pending_count):
    parts = [
        f'周期内共有 {len(verifications)} 次校园身份认证走完'
    ]
    parts.append(
        f'，中位 {stats["median_label"]}、P90 {stats["p90_label"]}'
    )
    if domain_rows:
        worst = domain_rows[0]
        parts.append(
            f'；{worst["domain_name"]} 最慢，{worst["count"]} 次认证里有 '
            f'{worst["slow_rate"]}% 隔了超过一天'
        )
    if slow_rows:
        parts.append(
            f'。{len(slow_rows)} 次隔了超过一天才点开链接，已逐条列在下面'
        )
    if pending_count:
        parts.append(
            f'；另有 {pending_count} 条认证还挂在「待验证邮箱」，未计入任何耗时分布'
        )
    if stats['is_small_sample']:
        parts.append(
            f'。样本不足 {MIN_MEASURED_VERIFICATIONS} 条，占比只作参考'
        )
    return ''.join(parts)


def build_campus_verification_friction(days=30, now=None):
    """统计一次校园身份认证从收到邮件到点开链接花了多久，只报告不改写。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    verifications = _fetch_verifications(verified_after=start, verified_before=now)
    elapsed_list = [v.elapsed_seconds for v in verifications]
    rows = _bucket_rows(elapsed_list, ELAPSED_BUCKETS)
    domain_rows = _domain_rows(verifications)
    slow_rows = _slow_rows(verifications)

    stats = {
        'measured_count': len(verifications),
        'mean_label': _format_elapsed(_mean(elapsed_list)),
        'median_label': _format_elapsed(_percentile(elapsed_list, 0.5)),
        'p90_label': _format_elapsed(_percentile(elapsed_list, 0.9)),
        'rows': rows,
        'is_small_sample': len(verifications) < MIN_MEASURED_VERIFICATIONS,
    }

    # 还挂在 pending 上的认证单独数，不计入任何耗时分布：它们没有认证成功时刻，
    # 是右删失样本。按 token_issued_at 取窗口，因为要数的是「这段时间里发出去的
    # 邮件，有多少还没有回音」。
    pending_count = CampusVerification.objects.filter(
        status='pending',
        token_issued_at__isnull=False,
        token_issued_at__gte=start,
    ).count()

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(verifications),
        'summary': _summary(
            stats, domain_rows, verifications, slow_rows, pending_count,
        ),
        'stats': stats,
        'domain_rows': domain_rows[:DOMAIN_LIST_LIMIT],
        'domain_row_total': len(domain_rows),
        'domain_row_limit': DOMAIN_LIST_LIMIT,
        'slow_rows': slow_rows,
        'slow_row_total': sum(1 for v in verifications if v.elapsed_seconds >= 86400),
        'slow_row_limit': SLOW_LIST_LIMIT,
        'slow_domain_points': SLOW_DOMAIN_POINTS,
        'pending_count': pending_count,
        'min_measured_verifications': MIN_MEASURED_VERIFICATIONS,
    }

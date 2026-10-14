# -*- coding: utf-8 -*-
"""积分等级与徽章的门槛形状，第一次被运营读到。

``CommunityContribution`` 是站内唯一一套完整的游戏化体系。它有六种贡献类型、一份积分
权重表（``CONTRIBUTION_DEFAULTS`` 加四个 ``record_*_contribution`` 里的分值）、五级
积分门槛（``CONTRIBUTION_LEVELS``）、七枚徽章（``CONTRIBUTION_BADGE_RULES``），以及
一条幂等写入（``source_key`` 唯一约束加 ``get_or_create``）。

运营侧唯一的读者是 ``analytics._build_contribution_insights``，它报周期内的积分总量、
事件数、人数、按类型的分布、每日趋势和前 8 名用户——全是流量口径。而 ``contributions.py``
里那三张表是这套体系的全部规则，它们从来没有被任何一个数字碰过。

于是一套上线运行的等级体系，运营看不到它的形状。本模块把它们读出来，只报告。

## 一、为什么按全量算，不按周期切

等级是累计量。``_level_for_points`` 吃的是一个人的总积分，而总积分是他从第一次贡献
至今的全部记录之和。按周期切会让一个三个月前就升到第三级的人在这个月「消失」——
他这个月没有新贡献，于是从等级分布里掉出去，而他的等级并没有掉。

所以本模块的分母是**全量**，和 ``analytics`` 的周期口径明确分工：它报流量，本模块报
结构。两处都会在看板上出现，读者必须能看出它们问的不是同一个问题。

## 二、分母是有过记录的用户，不是全体用户

``CONTRIBUTION_LEVELS`` 的第一级门槛是 0，标签「新加入校园社区」。一个从没有任何贡献
记录的用户，按 ``_level_for_points`` 也落在这一级——但他是默认值，不是爬上去的。

把全体用户当分母，会让第一级的占比被大量「从没参与过」的用户撑起来，而这个数说明的
是产品渗透率，不是等级门槛的形状。**本模块的分母只收有过至少一条贡献记录的用户。**

## 三、为什么复用 ``_level_for_points`` 和 ``build_contribution_badges``

等级和徽章的判定规则各只有一处实现。本模块 import 它们，不在本地复制一份。

复制一份的代价不是多写几行代码：门槛表将来改一个数，本地那份不会跟着动，看板就会报
出一套和产品页不一样的等级分布。**两处对不上的那一天，测试会红。**

代价是 ``contributions.py`` 被拖进依赖。它只 import 模型和 ``django.db.models``，没有
import 通知链路，这是一个可以接受的依赖。

## 四、幂等键只做间接体检

``source_key`` 是唯一约束，``record_contribution`` 用 ``get_or_create`` 保证同一个业务
事件只发一次分。本模块报同一用户同一类型的记录数分布，作为幂等性的间接信号。

**但它不判断「异常多的记录」是重复还是真实高频。** 一个卖家这个月完成了八笔交易，他
名下就有八条 ``order:*`` 记录，这是正常的。本模块没有能力区分这两种情况，只把分布
报出来，让运营去核实。给一个「疑似重复」的结论出来，比不报更糟。

## 五、不做什么

- 不加迁移、不加字段、不改模型。只读。
- 不改 ``CONTRIBUTION_LEVELS``、不改 ``CONTRIBUTION_BADGE_RULES``、不改任何分值。
  门槛和权重是产品决策，本模块只把现状读出来。
- 不自动调整任何人的等级、不发任何积分、不补发漏记的分。
- 不报「升级用了多久」——库里只有 ``occurred_at`` 和 ``created_at``，没有「何时跨过
  某一级」。而两个字段的差本身就是补记噪声（``occurred_at`` 是业务时刻，
  ``created_at`` 是落库时刻），拿它们算升级时长等于编数据。
- 不按用户排名、不输出用户名。等级和徽章是公开信息，但把「谁停在最低级」做成一张
  名单，平台就从激励系统变成了一个记录谁不活跃的小本子。
- 不把「停在低级」读成「不活跃」。积分是特定行为的加权和，一个人完全可能交易频繁但
  不碰免费赠送和失物招领。面板上必须写明这一点。
- 不读 ``description``。它是商品标题拼出来的自由文本，读它会牵出「谁送了什么」。
- 不重复 ``analytics`` 的周期口径。它报流量，本模块报结构。
"""

from collections import Counter, namedtuple

from django.db.models import Count, Sum
from django.utils import timezone

from .contributions import (
    CONTRIBUTION_BADGE_RULES,
    CONTRIBUTION_LEVELS,
    _level_for_points,
    build_contribution_badges,
)
from .models import CommunityContribution

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一级积分门槛。低于这个数的用户不进入本模块的分母，见模块开头第二节。
LEVEL_ZERO = CONTRIBUTION_LEVELS[0][0]

# 一档要给方向，至少这么多有记录的用户。全站统一量级：几十条样本上几个百分点的
# 差距是噪声。
MIN_RANKED_USERS = 12

# 一种贡献类型的积分占比要给方向，至少这么多条该类型记录。
MIN_TYPE_SAMPLE = 12

# 一个幂等键前缀的分布要给方向，至少这么多条该前缀记录。
MIN_KEY_PREFIX = 12

# 百分点差值不满这么多视为噪声，不给方向。全站同一套。
NOISE_POINTS = 8

# 一档积分等级，连同它此刻的规模。门槛取自 CONTRIBUTION_LEVELS，不另立一个数。
LevelRow = namedtuple('LevelRow', 'threshold label user_count share is_top')

# 一枚徽章，连同它此刻的获取规模。规则取自 CONTRIBUTION_BADGE_RULES。
BadgeRow = namedtuple('BadgeRow', 'code title metric threshold earned_count share')

# 一级到下一级的距离分布的一格。distance 是积分差，span 是这一级到下一级的跨度。
GapRow = namedtuple('GapRow', 'label distance_count mean_distance_label max_distance')

# 一种贡献类型在整套积分体系里的分量。
TypeRow = namedtuple(
    'TypeRow', 'kind label event_count points mean_points_label point_share '
    'is_small_sample',
)

# 一个幂等键前缀（业务域）的规模。
KeyPrefixRow = namedtuple('KeyPrefixRow', 'prefix event_count user_count share')

# 同一用户同一类型的记录数分布的一格。1 次是正常，2 次以上说明这个用户在这条路径上
# 反复被记——可能是真的高频，也可能是幂等键没盖住，本模块不判断是哪一种。
# pair_count 数的是「用户 × 类型」的组合数，不是用户数：一个用户可以在六种类型下
# 各出现一次，那是六个组合、一个用户。分母是有过记录的用户数，两个数必须分开报。
RepeatRow = namedtuple('RepeatRow', 'times label pair_count share')


def _percent(part, whole):
    """占比一律返回 None 表示空分母，不返回 0。

    0 会被读成「有，占比很小」，而这里的事实是「分母不存在」。看板模板把 None
    显示成「—」，和 0 区分开。
    """
    return round(part / whole * 100, 1) if whole else None


def _mean_label(values):
    """平均值一律带上「分」，空样本报「—」。"""
    return f'{sum(values) / len(values):.1f} 分' if values else '—'


def _median(values):
    """中位数。偶数个取中间两个的平均，和 statistics.median 同一个口径。"""
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _key_prefix(source_key):
    """幂等键的业务域，取第一个冒号前的部分。

    ``order:12:trade_completed:seller`` 的前缀是 ``order``，
    ``demand-response:7:accepted`` 的前缀是 ``demand-response``。同一条写入路径的
    键形状一致，所以前缀就是业务域。
    """
    return source_key.split(':', 1)[0] if source_key else '（空键）'


def _fetch_user_points():
    """每个有贡献记录的用户的总积分，按用户 id 升序。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的分组，比一份错的分组更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的。

    一条 SQL 拿全量，不按周期过滤——等级是累计量，见模块开头第一节。
    """
    rows = CommunityContribution.objects.values('user').annotate(
        total=Sum('points'),
    ).order_by('user')
    return [(row['user'], row['total'] or 0) for row in rows]


def _level_rows(user_points):
    """五级等级的全量分布。

    门槛直接取自 ``CONTRIBUTION_LEVELS``，本地不复制一份。占比的分母是有过记录的
    用户数，不是全体用户——没有记录的人停在第 0 级是默认值，见模块开头第二节。

    五级全列出来，包括一级都没有人的那一级：漏掉空级，读者会以为这一级也是齐的。
    """
    total_users = len(user_points)
    counts = Counter()
    for _, points in user_points:
        current, _ = _level_for_points(points)
        counts[current[0]] += 1

    rows = []
    last_index = len(CONTRIBUTION_LEVELS) - 1
    for index, (threshold, label) in enumerate(CONTRIBUTION_LEVELS):
        rows.append(LevelRow(
            threshold=threshold,
            label=label,
            user_count=counts[threshold],
            share=_percent(counts[threshold], total_users),
            is_top=(index == last_index),
        ))
    return rows


def _gap_rows(user_points, level_rows):
    """每一级到下一级的距离分布。

    距离 = 下一级门槛 − 这个人现在的积分。只对「停在第 N 级、且还有下一级」的人算；
    已经停在最高一级的人没有下一级，不进这一节。

    报距离的中位数和最大值，不报平均值：一个差 200 分的人会把平均值抬起来，而这一节
    要看的正是「普遍还差多少」。
    """
    rows = []
    for index, row in enumerate(level_rows):
        if row.is_top:
            continue
        next_row = level_rows[index + 1]
        # 只算「停在本级」的人。用 row.threshold 和门槛比，不比较 _level_for_points
        # 返回的下标：门槛表将来改成非等距（比如 0/10/50/160），下标和门槛就不再一一
        # 对应，而门槛始终是本模块自己列出来的那一列。
        distances = [
            next_row.threshold - points
            for _, points in user_points
            if _level_for_points(points)[0][0] == row.threshold
        ]
        median = _median(distances)
        # 四级跨度全列出来，包括一级都没有人停的那一跨——漏掉空跨，读者会以为剩下的
        # 每一跨都有人正在爬。distance_count 为 0 时中位数和最大值都是「—」而不是 0：
        # 0 会被读成「差 0 分就升级」，而这里的事实是「这一级一个人都没停」。
        rows.append(GapRow(
            label=f'{row.label} → {next_row.label}',
            distance_count=len(distances),
            mean_distance_label=(f'{median:.1f} 分' if median is not None else '—'),
            max_distance=max(distances, default=0) if distances else None,
        ))
    return rows


def _badge_rows(user_points):
    """七枚徽章各自的获取规模。

    判定规则直接调 ``build_contribution_badges``，本地不复制一份。它吃一个 queryset，
    所以这里按用户逐个人跑——用户量是校园规模，这个循环的代价可以接受，而复制一份
    徽章规则的代价是不可接受的。

    threshold 和获取数摆在同一行：门槛和结果分开看，读不出「这枚徽章是不是太难过」。
    """
    total_users = len(user_points)
    earned = Counter()
    for user_id, _ in user_points:
        queryset = CommunityContribution.objects.filter(user_id=user_id)
        for badge in build_contribution_badges(queryset):
            if badge['earned']:
                earned[badge['code']] += 1

    rows = []
    for rule in CONTRIBUTION_BADGE_RULES:
        count = earned[rule['code']]
        rows.append(BadgeRow(
            code=rule['code'],
            title=rule['title'],
            metric=rule['metric'],
            threshold=rule['threshold'],
            earned_count=count,
            share=_percent(count, total_users),
        ))
    return rows


def _type_rows():
    """每种贡献类型在整套积分体系里的分量。

    按类型报事件数、积分总量、单次平均分、积分占比。占比的分母是全部积分。

    单次平均分不是权重本身——权重是代码里的常数，这里报的是「这一条记录实际发了多少
    分」。两者在有补记或改过历史分值时可能不同，报实际值不报常数。
    """
    aggregate = {
        row['kind']: row
        for row in CommunityContribution.objects.values('kind').annotate(
            event_count=Count('id'),
            points=Sum('points'),
        )
    }
    total_points = sum(row['points'] or 0 for row in aggregate.values())
    kind_labels = dict(CommunityContribution.KIND_CHOICES)

    rows = []
    # 六种类型全列出来，包括一条记录都没有的类型——漏掉空类型，读者会以为这套体系里
    # 只有出现过的几种行为。一个从没被写过的类型，本身就是一个该被看见的信号。
    for kind, label in CommunityContribution.KIND_CHOICES:
        row = aggregate.get(kind, {})
        events = row.get('event_count', 0)
        points = row.get('points', 0) or 0
        rows.append(TypeRow(
            kind=kind,
            label=label,
            event_count=events,
            points=points,
            mean_points_label=f'{points / events:.1f} 分' if events else '—',
            point_share=_percent(points, total_points),
            is_small_sample=events < MIN_TYPE_SAMPLE,
        ))
    return rows


def _key_prefix_rows():
    """幂等键的业务域分布。

    按第一个冒号前的部分分组，报每个域的事件数、涉及的用户数和占比。占比的分母是
    全部记录数。

    这一节和 ``_type_rows`` 问的不是同一个问题：类型是「哪一种行为」，业务域是
    「哪一条写入路径」。一条写入路径可以写出多种类型（``record_order_contribution``
    按 phase 写三种），一种类型也可以来自多条路径。两者摆在一起，能看出某条路径是
    不是整条没在跑。
    """
    aggregate = list(CommunityContribution.objects.values('source_key', 'user'))
    total = len(aggregate)
    event_counts = Counter()
    prefix_users = {}
    for row in aggregate:
        prefix = _key_prefix(row['source_key'])
        event_counts[prefix] += 1
        prefix_users.setdefault(prefix, set()).add(row['user'])

    rows = []
    for prefix, count in event_counts.most_common():
        rows.append(KeyPrefixRow(
            prefix=prefix,
            event_count=count,
            user_count=len(prefix_users[prefix]),
            share=_percent(count, total),
        ))
    return rows


def _repeat_rows():
    """同一用户同一类型的记录数分布。

    1 次是正常形态。2 次以上说明这个用户在这条路径上被记了多次——可能是真的高频，
    也可能是幂等键没盖住，本模块不判断是哪一种，只报规模。

    分母是有过记录的用户数，不是全体用户。
    """
    aggregate = CommunityContribution.objects.values('user', 'kind').annotate(
        times=Count('id'),
    ).order_by('user', 'kind')
    total_users = CommunityContribution.objects.values('user').distinct().count()
    counts = Counter(row['times'] for row in aggregate)

    rows = []
    for times in sorted(counts):
        rows.append(RepeatRow(
            times=times,
            label=f'{times} 次',
            pair_count=counts[times],
            share=_percent(counts[times], total_users),
        ))
    return rows


def _summary(level_rows, badge_rows, type_rows, user_points):
    """一句话说清这套等级与徽章体系此刻的形状。"""
    if not user_points:
        return '当前没有任何贡献记录，等级与徽章都无从谈起。'

    top_row = level_rows[-1]
    zero_row = level_rows[0]
    parts = [
        f'{len(user_points)} 个有记录的用户里，'
        f'{zero_row.user_count} 人停在第 0 级（{zero_row.label}）'
    ]
    if top_row.user_count:
        parts.append(f'{top_row.user_count} 人到达最高一级（{top_row.label}）')
    else:
        parts.append(f'最高一级（{top_row.label}）一个人都还没到')

    unearned = [row for row in badge_rows if not row.earned_count]
    if unearned:
        parts.append(
            f'{"、".join(row.title for row in unearned)}'
            f'{len(unearned)} 枚徽章一个人都没拿到'
        )
    top_type = max(type_rows, key=lambda r: r.points, default=None)
    if top_type is not None and top_type.points:
        parts.append(
            f'{top_type.label}占了总积分的 {top_type.point_share}%'
        )
    small = [row for row in type_rows if row.is_small_sample]
    if small:
        parts.append(
            f'{"、".join(row.label for row in small)}'
            f'记录不足 {MIN_TYPE_SAMPLE} 条，占比只作参考'
        )
    return '；'.join(parts) + '。'


def build_contribution_tier_shape(days=30, now=None):
    """统计积分等级与徽章的门槛形状，只报告不改写。

    ``days`` 只为和看板其余面板的口径字段保持一致而收下，本模块不按周期过滤任何
    数据——等级是累计量，见模块开头第一节。
    """
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    user_points = _fetch_user_points()
    level_rows = _level_rows(user_points)
    gap_rows = _gap_rows(user_points, level_rows)
    badge_rows = _badge_rows(user_points)
    type_rows = _type_rows()
    key_prefix_rows = _key_prefix_rows()
    repeat_rows = _repeat_rows()

    return {
        'period_days': days,
        'period_end': now,
        'has_data': bool(user_points),
        'summary': _summary(level_rows, badge_rows, type_rows, user_points),
        'user_count': len(user_points),
        'level_rows': level_rows,
        'gap_rows': gap_rows,
        'badge_rows': badge_rows,
        'type_rows': type_rows,
        'key_prefix_rows': key_prefix_rows,
        'repeat_rows': repeat_rows,
        'total_records': sum(row.event_count for row in key_prefix_rows),
        'level_zero': LEVEL_ZERO,
        'min_ranked_users': MIN_RANKED_USERS,
        'min_type_sample': MIN_TYPE_SAMPLE,
        'min_key_prefix': MIN_KEY_PREFIX,
        'noise_points': NOISE_POINTS,
    }

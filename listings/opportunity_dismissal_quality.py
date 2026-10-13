# -*- coding: utf-8 -*-
"""用户按过「不再显示」的机会，第一次被运营读到。

`opportunity_feed.build_opportunity_feed` 是站内唯一一条个性化推荐流，面向卖家：把他
的在售商品和活跃的校园求购、失物招领记录做可解释匹配，给出一条带理由的机会流。用户
在机会流里点「暂不展示」，`views.dismiss_opportunity` 落一行
`OpportunityDismissal`，带 `kind`（`demand` / `lost_found`）、`demand` 或
`lost_found_post`、`expires_at` 和 `created_at`。

读路径只有一处，就是机会流自己——把这些行读回来做成 id 集合，`exclude` 掉。**用户每一
次「不再显示」都被完整记录下来，而平台从来不知道这些忽略到底意味着什么。** 后台能列出
这些忽略，看板上没有任何一个数字。

站内已有四个「量我们自己的打分 / 排序」的模块——`search_position_quality` 量搜索排序、
`demand_match_quality` 量求购匹配打分、`recommendation_quality` 量首页八张推荐卡——
机会流是唯一一个既带分数、又带理由、又带用户负反馈、却从未被量过的个性化排序。而机会
流背后正是 `demand_matching._match_demand` 和 `lost_found_matching.score_lost_found_posts`
这两个已经上过看板的匹配器：它们的分数已经分别被求购人的接受 / 拒绝和线索确认率量
过，但量的都是**被撮合的那一方**；机会流量的是**撮合的另一方**——卖家面对同一个分数、
同一句理由时的取舍。

本模块只报告，不改写任何一行 `OpportunityDismissal`、不改机会流的任何常数、不改两个匹
配器的任何权重、不删任何忽略记录、不给任何人排名。几处口径上的取舍比数字本身更要紧。

**忽略率的分母，从这一轮起真正落库了。** `OpportunityDismissal` 只记「忽略」，不记「看
过」：一条机会被渲染出来、用户什么也没点，库里一行都没有。机会流页面本身也不产生任何
可反推的记录——「打开过机会流页」不在任何表里，机会卡上的「去响应」按钮指向求购详情
页，那条浏览记录会被算成一次普通商品浏览，无法和机会流区分。

所以本模块的早期版本**不报忽略率**，只报四个只描述忽略本身的数：规模与结构、按分数档
拆的忽略分布、按理由拆的忽略分布、重复忽略。前两个切分的分子是忽略、分母也是忽略，那是
忽略自己的形状，不是率。当时的结论写得很硬：报一个「用页面构造当分母」的率（比如「进入
机会流页的次数 × 每页候选数」）是估算不是测量，而一个估算出来的率会让运营以为自己量过
——那比没有数字更糟。

本模块补上那件被明确判定为「要先想清楚值不值得」的事：`OpportunityImpression`。写入点在
`views.opportunity_feed`——一次请求把这一屏的机会渲染出去，就把这一屏的事实用一次
`bulk_create` 写进来。于是「窗口内被展示过的机会」这个数第一次拿得到，忽略率第一次是一
个真实的比例而不是一个形状。设计取舍见 `docs/opportunity-dismissal-quality-design.md` 和
模型自己的 docstring。

**忽略率仍然报下界，方向明说。** 它只低不高：一条机会在窗口外就被看见、窗口内又被看见、
用户在窗口内才按「不再显示」，按分子规则它进不了分子（见 `_build_dismissal_rate`），而分
母里那次窗口内展示仍然计。虚高的忽略率会让运营以为机会流在惹恼用户，虚低只会让人保守，
所以这个方向是可接受的。

**重复忽略是唯一能回答「忽略有没有用」的数。** `OpportunityDismissal` 上有两个无条件
唯一约束 `(user, demand)` 和 `(user, lost_found_post)`，而写入用的是 `update_or_create`
、`defaults` 里只更 `expires_at`。所以同一个人对同一条机会的第二次「不再显示」不会产生
新行，只会把旧行的 `expires_at` 往后推 30 天；`created_at` 是 `auto_now_add`，走 update
分支时不碰它，停在第一次忽略的时刻。于是判据只能取一个间接信号：`expires_at` 和
`created_at` 的距离。第一次按下时两者相差 30 天（两次时钟读取相差毫秒），第二次按下时
`expires_at` 被推到「第二次的时刻 + 30 天」，距离就明显超过 30 天。见
`REPEAT_WINDOW_TOLERANCE` 的说明和面板上的边界文字。

**分数在忽略时刻重算，走匹配器自己的函数。** `OpportunityDismissal` 不存分数也不存理
由，而分数是每次请求重算的——求购方改了预算、商品降了价、失物招领的 `occurred_at` 过
了 7 天窗口，分数都会变。复算取这个用户名下**当前在售**的商品、**活跃**的失物招领记录
里分数最高的那一次匹配，这正是 `build_opportunity_feed` 的 `best_demands` /
`best_lost_found` 逻辑。所以它还原的是「如果今天再进池，这条机会的分数是多少」，不是
忽略那一刻的分数——这个近似必须写在面板上。

**按理由聚合，不按用户、不按商品。** 按用户是排名（隐私，也不是运营射程），按商品让被
忽略一万次和一次不可比。理由是打分的可解释输出，也是运营唯一能动手改的东西（改权重、
改文案）。
"""

from datetime import datetime, time as dt_time, timedelta

from django.db.models import Q
from django.utils import timezone

from .demand_matching import _match_demand
from .lost_found_matching import score_lost_found_posts
from .models import (
    Item, LostFoundPost, OpportunityDismissal, OpportunityImpression,
)

# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 静默期的天数。`views.dismiss_opportunity` 写死 30 天，这里照抄一份用来算
# `expires_at` 和 `created_at` 的距离，不反向修改任何一行。
SILENCE_DAYS = 30

# `expires_at - created_at` 落在哪个区间，就算这一行被同一个人忽略过不止一次。第一次按
# 下「不再显示」时 `expires_at = created_at + 30 天`，两次时钟读取相差毫秒级；第二次按
# 下时 `expires_at` 被推到「第二次的时刻 + 30 天」，距离就严格大于 30 天。取 (30, 35]
# 而不是「大于 30 天」，是因为要给两次忽略之间的间隔留出余量：间隔五天之内的第二次忽
# 略一定落在这个区间里，而间隔更长的仍然算重复忽略（距离只会更大），只是第二次发生在
# 更早。容差取 5 天，让判据对时钟抖动完全免疫。
REPEAT_WINDOW_TOLERANCE = timedelta(days=5)

# 一个分数档要凑够这么多条忽略，才允许对它下「这一档的分数不解释行为」的判断。取 12
# 和其余「量自己」模块同一个量级：占比的分母只有个位数时，一条忽略的改判就能把占比推
# 动十几个百分点，那种数字拿去改权重只会把好的分数改坏。
MIN_BAND_SAMPLE = 12

# 一句理由要凑够这么多条忽略，才允许对它下「这句理由在骗人」的判断。同理。
MIN_REASON_SAMPLE = 12

# 忽略率的分母（窗口内第一次被看见的机会）不足这么多条时，率只报数字、不给方向。取 24
# 而不是 12：率比占比更敏感，一条忽略在 12 条分母上就是 8 个百分点，在 24 条上才 4 个。
# 上一轮这个档位上报的是「12 条忽略」，因为那时没有分母、只能数忽略；有分母之后门槛改挂
# 在展示上，因为要谈的是率。
MIN_IMPRESSIONS = 24

# 两个档、两句理由的占比相差不满这么多个百分点，视为噪声。同一个量级的切分之间的这
# 点差别，运营看不出来、也动不了手。
NOISE_POINTS = 10

# 求购侧的机会分数来自 `_match_demand`，分值域 2-11。固定分档而不是一个分数一档：单
# 个分数上的忽略太少读不出形状，而运营也没法对「分数 7 的机会被忽略更多」直接动手。
# 分档边界照 `demand_match_quality.SCORE_BANDS` 取同一套，两个模块在同一块看板上出现
# 时分档含义一致。
DEMAND_SCORE_BANDS = (
    (2, 4, '2-4 分'),
    (5, 6, '5-6 分'),
    (7, 8, '7-8 分'),
    (9, 11, '9-11 分'),
)

# 失物侧的机会分数来自 `score_lost_found_posts`，进入机会流的门槛是 30 分（低于它的机
# 会 `build_opportunity_feed` 直接丢掉），上界 100。分档同样取固定档。
LOST_FOUND_SCORE_BANDS = (
    (30, 44, '30-44 分'),
    (45, 59, '45-59 分'),
    (60, 74, '60-74 分'),
    (75, 100, '75-100 分'),
)

# 复算求购分数时，给每个用户最多看多少件在售商品。`build_opportunity_feed` 自己也取
# `[:80]`，这里同一个量级：一个挂了四百件商品的卖家，机会流只看得到最近的八十件，
# 复算也就只看这八十件。
DEMAND_ITEM_SCAN_LIMIT = 80

# 复算失物分数时，每个用户最多看多少条自己发布的活跃记录。机会流取 `[:40]`，同一个
# 量级。
LOST_FOUND_POST_SCAN_LIMIT = 40

# `kind` 到中文名的映射，面板、CSV、结论共用一份，不各写一套。
KIND_LABELS = {'demand': '校园求购', 'lost_found': '失物招领'}


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _is_repeated(dismissal):
    """这一行是不是被同一个人忽略过不止一次。

    判据是间接的，见 `REPEAT_WINDOW_TOLERANCE` 的说明：`expires_at - created_at` 落在
    (30, 35] 天里，说明 `expires_at` 被往后推过一次。它数不到「第二次忽略恰好也落在
    `created_at + 30 天` 附近」的情形，也分不清「用户主动又按了一次」和「别的东西更新
    了这一行」——目前后者的写入路径不存在，但这是一个需要守住的前提。
    """
    span = dismissal.expires_at - dismissal.created_at
    return timedelta(days=SILENCE_DAYS) < span <= (
        timedelta(days=SILENCE_DAYS) + REPEAT_WINDOW_TOLERANCE
    )


def _available_items_for(user_id, cache):
    """这个用户名下当前在售的商品，按用户缓存。

    机会流自己取 `[:80]`，复算也就只看这八十件：一个挂了四百件商品的卖家，机会流看
    不到更早的那三百二十件，复算看到了只会算出一个机会流从来没展示过的分数。
    """
    if user_id not in cache:
        cache[user_id] = list(
            Item.objects.available().filter(seller_id=user_id).select_related(
                'category', 'location',
            )[:DEMAND_ITEM_SCAN_LIMIT]
        )
    return cache[user_id]


def _active_posts_for(user_id, cache):
    """这个用户名下仍在窗口内的活跃失物招领记录，按用户缓存。"""
    if user_id not in cache:
        now = timezone.now()
        cache[user_id] = list(
            LostFoundPost.objects.filter(
                reporter_id=user_id, status='active',
            ).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=now),
            ).select_related('category', 'location')[:LOST_FOUND_POST_SCAN_LIMIT]
        )
    return cache[user_id]


def _recompute_demand_score(record, item_cache):
    """这条求购机会，今天再进池会拿多少分。忽略行和展示行共用。

    `OpportunityDismissal` 没存用户当时拿哪件商品匹配的，所以照
    `build_opportunity_feed` 的 `best_demands` 逻辑：这个用户名下**当前在售**的商品
    里分数最高的那一次匹配。复算只读当前在售的商品，所以它还原的是「如果今天再进池的
    分数」，不是忽略那一刻的分数——用户之后可能把商品下架、降价、改了分类。

    返回 `(score, reason_text)`；一件在售商品都匹配不上时返回 `(None, None)`，这一行进
    不了按分数档和按理由的切分，但仍然计入规模。
    """
    demand = record.demand
    if demand is None:
        return None, None
    best = None
    for item in _available_items_for(record.user_id, item_cache):
        match = _match_demand(demand, item)
        if not match:
            continue
        if best is None or match[0] > best[0]:
            best = match
    return best if best else (None, None)


def _recompute_lost_found_score(record, post_cache):
    """这条失物招领机会，今天再进池会拿多少分。忽略行和展示行共用。

    同理照 `build_opportunity_feed` 的 `best_lost_found` 逻辑：这个用户名下**活跃**的
    失物招领记录里分数最高的那一次匹配，且只认 30 分以上的（机会流自己的门槛）。

    失物招领的机会方向和求购相反：候选记录是「和你自己那条相反类型」的别人发的记录，
    所以这里扫的是这个用户名下由他发布的活跃记录，拿每一条去和 `dismissal` 指向的那
    条候选配。
    """
    target = record.lost_found_post
    if target is None:
        return None, None
    best = None
    for source in _active_posts_for(record.user_id, post_cache):
        score, reasons = score_lost_found_posts(source, target)
        if score < 30:
            continue
        if best is None or score > best[0]:
            best = (score, '、'.join(reasons))
    return best if best else (None, None)


def _load_dismissals(start, now):
    """窗口内的忽略记录，连目标记录一次取回。

    归期按 `created_at`：这是用户**第一次**按下「不再显示」的时刻，也是这一行进入静默
    期的时刻。按 `updated_at` 归期会把重复忽略整行挪到第二次忽略的周期里，而它第一次
    被忽略的证据还留在上一个周期。

    `expires_at` 也要取，因为重复忽略的判据就是它和 `created_at` 的距离。
    """
    return list(
        OpportunityDismissal.objects.filter(
            created_at__gte=start, created_at__lte=now,
        ).select_related('user', 'demand', 'lost_found_post')
    )


def _score_dismissals(dismissals):
    """给每条忽略重算出忽略时刻的分数和理由。

    商品和失物记录按用户缓存：一个人的忽略在窗口里会有好几条，逐条各查一遍库会让看板
    的一次打开多出几十次查询。分数为 None 的行（今天已经没有在售商品 / 活跃记录能匹配
    上）不计入 `scored`，但仍然计入规模——它们是「忽略之后把自己的商品下架了」的那一
    批，本身就是一个信号。
    """
    item_cache = {}
    post_cache = {}
    scored = []
    unscored = 0
    for dismissal in dismissals:
        if dismissal.kind == 'demand':
            match = _recompute_demand_score(dismissal, item_cache)
        elif dismissal.kind == 'lost_found':
            match = _recompute_lost_found_score(dismissal, post_cache)
        else:
            match = (None, None)
        if not match or match[0] is None:
            unscored += 1
            continue
        scored.append({
            'kind': dismissal.kind,
            'score': match[0],
            'reason_text': match[1],
            'repeated': _is_repeated(dismissal),
        })
    return scored, unscored


def _build_kind_rows(dismissals):
    """忽略的规模与结构：按 `kind` 拆，顺带按用户拆两个数。"""
    rows = []
    for kind in ('demand', 'lost_found'):
        scoped = [row for row in dismissals if row.kind == kind]
        user_ids = {row.user_id for row in scoped}
        rows.append({
            'key': kind,
            'label': KIND_LABELS[kind],
            'dismissal_count': len(scoped),
            'user_count': len(user_ids),
            # 平均一个人按了几条。一个用户都没有时这个数不存在，不是 0。
            'per_user': round(len(scoped) / len(user_ids), 1) if scoped else None,
            'repeated_count': sum(1 for row in scoped if _is_repeated(row)),
        })
    return rows


def _build_band_rows(scored):
    """按分数档拆的忽略分布——**忽略自己的形状，不是率**。

    这一层的分子是忽略、分母也是忽略（该 kind 下全部可打分忽略），所以它回答的是「忽略
    集中在哪一档」。它会被展示结构影响：高分档忽略少，可能只是因为高分档本来就少。要看
    「分数高是不是真的更安全」，用 `_build_rate_rows` 那一层的 `dismissal_rate`——那里的
    分母是展示。

    每一行报的是「这一档占该 kind 下全部可打分忽略的比例」，以及档内的平均分。空平台报
    0 会被读成「没人忽略这一档」，而真相是根本没有忽略，所以没有可打分样本时占比是
    None。
    """
    rows = []
    for kind, bands in (
        ('demand', DEMAND_SCORE_BANDS), ('lost_found', LOST_FOUND_SCORE_BANDS),
    ):
        scoped = [row for row in scored if row['kind'] == kind]
        for low, high, label in bands:
            in_band = [row for row in scoped if low <= row['score'] <= high]
            rows.append({
                'kind': kind,
                'kind_label': KIND_LABELS[kind],
                'label': label,
                'low': low,
                'high': high,
                'dismissal_count': len(in_band),
                'share_of_kind': (
                    _percent(len(in_band), len(scoped)) if scoped else None
                ),
                'average_score': (
                    round(sum(row['score'] for row in in_band) / len(in_band), 1)
                    if in_band else None
                ),
                'is_small_sample': len(in_band) < MIN_BAND_SAMPLE,
            })
    return rows


def _build_reason_rows(scored):
    """按理由拆的忽略分布。

    理由在忽略时刻重算，走两个匹配器自己的函数，不能在运营模块里另写一套。按理由聚
    合，不按用户、不按商品：按用户是排名，按商品让被忽略一万次和一次不可比，理由是打
    分的可解释输出，也是运营唯一能动手改的东西。
    """
    rows = []
    for kind in ('demand', 'lost_found'):
        scoped = [row for row in scored if row['kind'] == kind]
        grouped = {}
        for row in scoped:
            key = row['reason_text'] or '未匹配到具体依据'
            entry = grouped.setdefault(key, {
                'dismissal_count': 0, 'score_sum': 0, 'repeated_count': 0,
            })
            entry['dismissal_count'] += 1
            entry['score_sum'] += row['score']
            entry['repeated_count'] += 1 if row['repeated'] else 0
        for reason_text, entry in sorted(
            grouped.items(), key=lambda pair: (-pair[1]['dismissal_count'], pair[0]),
        ):
            count = entry['dismissal_count']
            rows.append({
                'kind': kind,
                'kind_label': KIND_LABELS[kind],
                'label': reason_text,
                'dismissal_count': count,
                'share_of_kind': _percent(count, len(scoped)),
                'average_score': round(entry['score_sum'] / count, 1) if count else None,
                'repeated_count': entry['repeated_count'],
                'is_small_sample': count < MIN_REASON_SAMPLE,
            })
    return rows


def _build_score_shape(band_rows):
    """按分数档读出来的形状：忽略是不是集中在某一档。

    只比较**占比**（该档忽略数占该 kind 下全部忽略的比例），不比任何率——率在
    `_build_rate_shape` 里比，那里分母是展示。占比会被展示结构影响，所以它只能回答「忽略
    集中在哪一档」，不能回答「哪一档更安全」。两个 kind 分开读：求购和失物的分值域不同
    （2-11 对 30-100），混在一张表里比占比等于比两个不同的刻度。

    样本门槛和噪声线与其余模块同一个量级。达不到就只说样本不足，不指方向。
    """
    shape = {}
    for kind in ('demand', 'lost_found'):
        measurable = [
            row for row in band_rows
            if row['kind'] == kind and row['dismissal_count'] >= MIN_BAND_SAMPLE
        ]
        if len(measurable) < 2:
            shape[kind] = {
                'has_comparison': False,
                'kind_label': KIND_LABELS[kind],
                'summary': (
                    f'{KIND_LABELS[kind]}侧达到 {MIN_BAND_SAMPLE} 条忽略的分数档不足两个，'
                    '读不出分数高低和忽略多少的关系。'
                ),
            }
            continue
        top = max(measurable, key=lambda row: row['share_of_kind'])
        bottom = min(measurable, key=lambda row: row['share_of_kind'])
        spread = round(top['share_of_kind'] - bottom['share_of_kind'], 1)
        entry = {
            'has_comparison': True,
            'kind_label': KIND_LABELS[kind],
            'spread_points': spread,
            'strongest_label': top['label'],
            'weakest_label': bottom['label'],
            'strongest_share': top['share_of_kind'],
            'weakest_share': bottom['share_of_kind'],
        }
        if spread < NOISE_POINTS:
            entry.update({
                'kind': 'flat',
                'summary': (
                    f'{KIND_LABELS[kind]}侧占比最高与最低的分数档只相差 {spread} 个百分点，'
                    '落在噪声范围内：分数高低和忽略多少暂时分不开。'
                ),
            })
        else:
            entry.update({
                'kind': 'skewed',
                'summary': (
                    f'{KIND_LABELS[kind]}侧 {top["label"]} 占了全部可打分忽略的 '
                    f'{top["share_of_kind"]}%，而 {bottom["label"]} 只有 '
                    f'{bottom["share_of_kind"]}%，相差 {spread} 个百分点：'
                    '忽略集中在这一档。'
                ),
            })
        shape[kind] = entry
    return shape


def _build_repeat_stats(dismissals):
    """重复忽略：同一条机会被同一个人忽略过不止一次的行数。

    这曾经是本模块唯一能回答「忽略有没有用」的数：如果一条被忽略的机会反复回到同一个人
    面前又被反复忽略，说明 30 天静默期对这个人无效。判据是间接的，见 `_is_repeated` 的说
    明。

    展示表落库之后多了一个正交读数——`rate_rows` 里的 `repeat_show_count`：一条机会被渲染
    过不止一次。两个读数互相校验：展示了 5 次只忽略 1 次，那是用户在忍；展示了 5 次忽略
    3 次，那是静默期对该用户无效。单靠时钟距离分不清这两者。
    """
    repeated = [row for row in dismissals if _is_repeated(row)]
    total = len(dismissals)
    by_kind = {}
    for kind in ('demand', 'lost_found'):
        scoped = [row for row in dismissals if row.kind == kind]
        scoped_repeated = [row for row in scoped if _is_repeated(row)]
        by_kind[kind] = {
            'label': KIND_LABELS[kind],
            'dismissal_count': len(scoped),
            'repeated_count': len(scoped_repeated),
            'repeat_share': (
                _percent(len(scoped_repeated), len(scoped)) if scoped else None
            ),
        }
    return {
        'dismissal_total': total,
        'repeated_count': len(repeated),
        # 重复忽略占全部忽略的比例。这个数有自己的分母（窗口内全部忽略），所以它是一
        # 个真正的比例，不是形状——这也是本模块唯一敢报出来的比例。
        'repeat_share': _percent(len(repeated), total) if total else None,
        'user_count': len({row.user_id for row in repeated}),
        'by_kind': by_kind,
        'is_small_sample': len(repeated) < MIN_REASON_SAMPLE,
    }


def _load_impressions(start, now):
    """窗口内被渲染出去的机会，连目标记录一次取回。

    归期按 `shown_at`：这是这条机会**被看见**的时刻，也正是它进入「可能被忽略」这个池子
    的时刻。展示表的写入点在 `views.opportunity_feed`，一次渲染一批。

    刻意不按 `(user, target)` 去重：同一条机会在不同时刻被渲染给同一个人是两次独立的展
    示，去重会让分母变小、忽略率虚高。去重只发生在算「第一次被看见」的那一刻，见
    `_first_seen_keys`。
    """
    return list(
        OpportunityImpression.objects.filter(
            shown_at__gte=start, shown_at__lte=now,
        ).select_related('user', 'demand', 'lost_found_post')
    )


def _first_seen_keys(impressions):
    """窗口内**第一次**被看见的那些 (用户, 机会) 对。

    为什么只数第一次：一条机会在窗口外就被看见过、窗口内又被看见，它的分母会被计两次以
    上，而分子（忽略行）只可能在窗口内产生一条。分母重复计、分子不重复计，率会虚低。所
    以分母只认「这一次是窗口内第一次看见它」。

    这里去重的是 (user, kind, target_id) 三元组而不是主键：一条机会被同一个人渲染三次，
    三次都算「看见过」，但只有第一次进分母。这也顺带回答了「一条机会被展示了多少次」——
    那个数单独报，见 `_build_rate_rows` 的 `shown_count`。
    """
    seen = set()
    first = []
    for impression in impressions:
        key = (
            impression.user_id, impression.kind,
            impression.demand_id or impression.lost_found_post_id,
        )
        if key in seen:
            continue
        seen.add(key)
        first.append(impression)
    return first, seen


def _score_impressions(first_seen, dismissed_keys, impressions, dismissals):
    """给窗口内第一次被看见的机会重算今天的分数。

    和 `_score_dismissals` 同一个道理：`OpportunityImpression` 也不存分数，分数每次请求重
    算。复算走两个匹配器自己的函数，不在运营模块里另写一套。商品和失物记录按用户缓存——
    一个人的展示在窗口里会有好几条，逐条各查一遍库会让看板的一次打开多出几十次查询。

    返回 (rows, unscored_rows)。`rows` 每条带 `score`、`kind`、`user_id`、`target_id`、
    `shown_count`、`is_dismissed`，供 `_build_rate_rows` 分档；`unscored_rows` 是分数算不出
    来的那些，同样带 `shown_count` 和 `is_dismissed`，单独报一行。

    分数算不出来的行（用户今天已经没有在售商品 / 活跃记录能匹配上）不进任何分数档，但仍
    然要保留下来：它确实被看见过，丢掉会让分母偏小、忽略率虚高。
    """
    item_cache = {}
    post_cache = {}
    shown_by_pair = {}
    for impression in impressions:
        key = (
            impression.user_id, impression.kind,
            impression.demand_id or impression.lost_found_post_id,
        )
        shown_by_pair[key] = shown_by_pair.get(key, 0) + 1

    rows = []
    unscored_rows = []
    for impression in first_seen:
        if impression.kind == 'demand':
            match = _recompute_demand_score(impression, item_cache)
        elif impression.kind == 'lost_found':
            match = _recompute_lost_found_score(impression, post_cache)
        else:
            match = (None, None)
        target_id = impression.demand_id or impression.lost_found_post_id
        key = (impression.user_id, impression.kind, target_id)
        if not match or match[0] is None:
            # 分数算不出来的行不进任何分数档，但仍然带着 shown_count / is_dismissed 单独
            # 报一行：它确实被看见过，从分母里整个消失会让忽略率虚高。
            unscored_rows.append({
                'kind': impression.kind,
                'user_id': impression.user_id,
                'target_id': target_id,
                'shown_count': shown_by_pair.get(key, 1),
                'is_dismissed': key in dismissed_keys,
            })
            continue
        rows.append({
            'kind': impression.kind,
            'user_id': impression.user_id,
            'target_id': target_id,
            'score': match[0],
            'reason_text': match[1],
            'shown_count': shown_by_pair.get(key, 1),
            'is_dismissed': key in dismissed_keys,
        })
    return rows, unscored_rows


def _dismissed_keys(dismissals):
    """窗口内被按过「不再显示」的那些 (用户, 机会) 对。

    分子要求「这条忽略所对应的机会在窗口内至少被展示过一次」：一条忽略若来自窗口外的展
    示（用户先看到、过了几天才按按钮），它没有窗口内的分母，分子算它就会让率虚高。
    """
    keys = set()
    for dismissal in dismissals:
        target_id = dismissal.demand_id or dismissal.lost_found_post_id
        if target_id:
            keys.add((dismissal.user_id, dismissal.kind, target_id))
    return keys


def _build_dismissal_rate(first_seen, dismissed_keys):
    """忽略率：在窗口内第一次被看见的机会里，有多大比例后来被按了不再显示。

    这个数回答的是一个精确的问题，两个方向都只数窗口内产生的行：

    - **分母** = 窗口内第一次被看见的 (用户, 机会) 对；
    - **分子** = 其中被按过「不再显示」的那些对。

    **它报出来的是下界，只低不高。** 一次忽略若发生在窗口内的展示之后、但用户是先在窗口
    外看到这条机会才按的按钮，按分子规则它进不了分子，而分母里那次窗口内展示仍然计。这
    种情形让分子偏小、率偏低。可接受，因为虚高的忽略率会让运营以为机会流在惹恼用户，而
    虚低只会让人保守。

    **空分母报 `None`，不报 0。** 一周一条都没展示过，忽略率不存在，不是零。
    """
    denominator = len(first_seen)
    numerator = 0
    for impression in first_seen:
        key = (
            impression.user_id, impression.kind,
            impression.demand_id or impression.lost_found_post_id,
        )
        if key in dismissed_keys:
            numerator += 1
    return {
        'denominator': denominator,
        'numerator': numerator,
        # 分母不足门槛时只报数字、不给方向，和其余模块同一个量级。
        'is_small_sample': denominator < MIN_IMPRESSIONS,
        'has_direction': denominator >= MIN_IMPRESSIONS and numerator > 0,
        'rate': _percent(numerator, denominator) if denominator else None,
    }


def _build_rate_rows(scored, unscored_rows):
    """按机会类型和分数档拆的忽略率。

    分数档这一层是本轮真正的新东西：上一轮只能报「这一档占全部可打分忽略的比例」，那完全
    可以由展示结构决定——如果整个平台一周只展示了 20 条机会、高分档本来就少，高分档忽略
    少可能只是因为高分档展示得少。有分母之后，「高分档忽略率 8%、低分档 41%」才是一句能
    拿去改权重的话。

    每一行报四个数：

    - `shown_count`：这一档在窗口内第一次被看见的次数（分母）；
    - `dismissal_count`：其中被忽略的次数（分子）；
    - `dismissal_rate`：两者之比，空分母是 `None` 不是 0；
    - `repeat_show_count`：这一档里被渲染过不止一次的次数。它和重复忽略是一对正交读数：
      一条机会被展示了 5 次只忽略 1 次，那是用户在忍；展示了 5 次忽略 3 次，那是静默期对
      该用户无效。上一轮判重复忽略只能靠 `expires_at - created_at` 的距离，这是第二个读
      数，两个互相校验。
    """
    rows = []
    for kind, bands in (
        ('demand', DEMAND_SCORE_BANDS), ('lost_found', LOST_FOUND_SCORE_BANDS),
    ):
        scoped = [row for row in scored if row['kind'] == kind]
        for low, high, label in bands:
            in_band = [row for row in scoped if low <= row['score'] <= high]
            dismissed_in_band = [row for row in in_band if row['is_dismissed']]
            repeat_show = [row for row in in_band if row['shown_count'] > 1]
            denominator = len(in_band)
            rows.append({
                'kind': kind,
                'kind_label': KIND_LABELS[kind],
                'label': label,
                'low': low,
                'high': high,
                'shown_count': denominator,
                'dismissal_count': len(dismissed_in_band),
                'repeat_show_count': len(repeat_show),
                'dismissal_rate': (
                    _percent(len(dismissed_in_band), denominator) if denominator else None
                ),
                'is_small_sample': denominator < MIN_BAND_SAMPLE,
            })
        # 算不出分数的展示单独一行，不计入任何分数档。少了这一行，「被看见了 5 次、但用户
        # 今天已经没有在售商品能匹配上」的展示会从分母里整个消失，而它确实被看见过——把它
        # 丢掉会让分母偏小、忽略率虚高。它报的是 `shown_count`，`dismissal_rate` 恒为
        # None（没有分数就无从分档，也无从和一个「应该」的率比较）。
        unscored = [row for row in unscored_rows if row['kind'] == kind]
        unscored_dismissed = [row for row in unscored if row['is_dismissed']]
        rows.append({
            'kind': kind,
            'kind_label': KIND_LABELS[kind],
            'label': '今天已匹配不上',
            'low': None,
            'high': None,
            'shown_count': len(unscored),
            'dismissal_count': len(unscored_dismissed),
            'repeat_show_count': sum(1 for row in unscored if row['shown_count'] > 1),
            'dismissal_rate': None,
            'is_small_sample': len(unscored) < MIN_BAND_SAMPLE,
            'is_unscored': True,
        })
    return rows


def _build_rate_shape(rate_rows):
    """按分数档读出来的率：分数高的机会是不是更不容易被忽略。

    上一轮比的是占比（该档忽略数占全部忽略的比例），那是忽略自己的形状。这一轮比的是率，
    分母是展示，所以它能真正回答「分数高是不是更安全」。两个 kind 分开读：求购和失物的
    分值域不同（2-11 对 30-100），混在一张表里比等于比两个不同的刻度。

    方向判据和上一轮同一个量级：两档忽略率相差不满 `NOISE_POINTS` 个百分点，视为噪声。
    """
    shape = {}
    for kind in ('demand', 'lost_found'):
        measurable = [
            row for row in rate_rows
            if row['kind'] == kind and not row['is_small_sample']
            and row['dismissal_rate'] is not None and not row.get('is_unscored')
        ]
        if len(measurable) < 2:
            shape[kind] = {
                'has_comparison': False,
                'kind_label': KIND_LABELS[kind],
                'summary': (
                    f'{KIND_LABELS[kind]}侧达到 {MIN_BAND_SAMPLE} 次展示的分数档不足两个，'
                    '读不出分数高低和忽略率的关系。'
                ),
            }
            continue
        top = max(measurable, key=lambda row: row['dismissal_rate'])
        bottom = min(measurable, key=lambda row: row['dismissal_rate'])
        spread = round(top['dismissal_rate'] - bottom['dismissal_rate'], 1)
        entry = {
            'has_comparison': True,
            'kind_label': KIND_LABELS[kind],
            'spread_points': spread,
            'strongest_label': top['label'],
            'weakest_label': bottom['label'],
            'strongest_rate': top['dismissal_rate'],
            'weakest_rate': bottom['dismissal_rate'],
        }
        if spread < NOISE_POINTS:
            entry.update({
                'kind': 'flat',
                'summary': (
                    f'{KIND_LABELS[kind]}侧忽略率最高与最低的分数档只相差 {spread} 个百分点，'
                    '落在噪声范围内：分数高低和忽略率暂时分不开。'
                ),
            })
        else:
            entry.update({
                'kind': 'skewed',
                'summary': (
                    f'{KIND_LABELS[kind]}侧 {top["label"]} 的忽略率是 '
                    f'{top["dismissal_rate"]}%，而 {bottom["label"]} 只有 '
                    f'{bottom["dismissal_rate"]}%，相差 {spread} 个百分点：'
                    '分数高的机会并没有更安全。'
                ),
            })
        shape[kind] = entry
    return shape


def _recommendations(score_shape, repeat_stats, dismissal_rate):
    """可以做什么，每一条都挂在替它说话的数字上。只报告，不改任何常数。"""
    notes = []
    for kind in ('demand', 'lost_found'):
        shape = score_shape.get(kind, {})
        if shape.get('has_comparison') and shape.get('kind') == 'skewed':
            notes.append(
                f'{shape["kind_label"]}侧的忽略集中在 {shape["strongest_label"]}'
                f'（占全部可打分忽略的 {shape["strongest_share"]}%），而 '
                f'{shape["weakest_label"]} 只有 {shape["weakest_share"]}%：'
                '这一档的分数在机会流这个场景下不解释用户行为。它指向一个具体的权重或'
                '分档，但本模块只报告，不改 _match_demand 与 score_lost_found_posts '
                '的任何常数。'
            )
    if not repeat_stats['is_small_sample'] and repeat_stats['repeated_count']:
        notes.append(
            f'有 {repeat_stats["repeated_count"]} 行忽略是被同一个人按过不止一次的'
            f'（占全部忽略的 {repeat_stats["repeat_share"]}%），涉及 '
            f'{repeat_stats["user_count"]} 个用户。这是 30 天静默期对一部分人无效最'
            '直接的证据，但本模块只报告，不改静默期、不动任何一行的 expires_at。'
        )
    rate = dismissal_rate.get('rate')
    if dismissal_rate.get('has_direction') and rate:
        notes.append(
            f'本周期内第一次被看见的机会里有 {rate}% 被按了「不再显示」'
            f'（{dismissal_rate["numerator"]}/{dismissal_rate["denominator"]}）。'
            '这是一个真实的比例：分母来自 `OpportunityImpression`，分子来自 '
            '`OpportunityDismissal`，两个方向都只数本周期产生的行。它仍然是下界，'
            '只低不高——先在周期外看到、周期内才按按钮的那些忽略进不了分子。'
        )
    if not notes:
        if not repeat_stats['dismissal_total']:
            notes.append('本周期没有任何忽略记录，用户还没有在机会流上按过「暂不展示」。')
        elif dismissal_rate.get('denominator'):
            notes.append(
                '忽略的分布没有明显背离，可继续观察更长的周期。忽略率的分母已经落库，'
                '当前样本还不足以下方向判断；这个数报出来的是下界，只低不高。'
            )
        else:
            notes.append(
                '忽略的分布没有明显背离，可继续观察更长的周期。忽略率的分母'
                '（`OpportunityImpression`）自本模块上线起才开始积累，此前没有展示数据。'
            )
    return notes


def build_opportunity_dismissal_quality(days=30, now=None):
    """量机会流的负反馈：用户按过「不再显示」的机会长什么样。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localtime(now).date())

    dismissals = _load_dismissals(start, now)
    impressions = _load_impressions(start, now)
    kind_rows = _build_kind_rows(dismissals)
    scored, unscored = _score_dismissals(dismissals)
    band_rows = _build_band_rows(scored)
    reason_rows = _build_reason_rows(scored)
    score_shape = _build_score_shape(band_rows)
    repeat_stats = _build_repeat_stats(dismissals)

    # 忽略率：分母来自展示表，分子来自忽略表，两个方向都只数窗口内产生的行。
    first_seen, _ = _first_seen_keys(impressions)
    dismissed_keys = _dismissed_keys(dismissals)
    dismissal_rate = _build_dismissal_rate(first_seen, dismissed_keys)
    shown_scored, shown_unscored = _score_impressions(
        first_seen, dismissed_keys, impressions, dismissals,
    )
    rate_rows = _build_rate_rows(shown_scored, shown_unscored)
    rate_shape = _build_rate_shape(rate_rows)
    recommendations = _recommendations(score_shape, repeat_stats, dismissal_rate)

    total = len(dismissals)
    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(dismissals or impressions),
        'summary': _summary(
            days, total, len(scored), unscored, dismissal_rate, impressions,
        ),
        'metrics': {
            'dismissal_total': total,
            'scored_total': len(scored),
            'unscored_total': unscored,
            'repeat_share': repeat_stats['repeat_share'],
            'repeated_count': repeat_stats['repeated_count'],
            'min_band_sample': MIN_BAND_SAMPLE,
            'min_reason_sample': MIN_REASON_SAMPLE,
            'min_impressions': MIN_IMPRESSIONS,
            'noise_points': NOISE_POINTS,
            'silence_days': SILENCE_DAYS,
            'impression_total': len(impressions),
            'first_seen_total': len(first_seen),
            'shown_unscored_total': len(shown_unscored),
        },
        'kind_rows': kind_rows,
        'band_rows': band_rows,
        'reason_rows': reason_rows,
        'score_shape': score_shape,
        'repeat_stats': repeat_stats,
        'dismissal_rate': dismissal_rate,
        'rate_rows': rate_rows,
        'rate_shape': rate_shape,
        'recommendations': recommendations,
    }


def _summary(days, total, scored_total, unscored, dismissal_rate, impressions):
    """面板顶上那一句。忽略率有了分母之后，它从「没有这个数」变成「有一个数」。"""
    if not total and not impressions:
        return f'最近 {days} 天没有任何忽略记录，机会流也没有产生过展示。'
    parts = [
        f'最近 {days} 天共有 {total} 条忽略，其中 {scored_total} 条今天仍能重算出'
        f'匹配分数，{unscored} 条已经匹配不上任何在售商品或活跃记录。'
    ]
    rate = dismissal_rate['rate']
    if rate is None:
        parts.append(
            f'同期机会流产生了 {len(impressions)} 次展示，但没有任何一条机会是在本周期'
            '内第一次被看见的，忽略率的分母为空。'
        )
    elif dismissal_rate['is_small_sample']:
        parts.append(
            f'同期有 {dismissal_rate["denominator"]} 条机会在本周期内第一次被看见，'
            f'其中 {dismissal_rate["numerator"]} 条被按了「不再显示」'
            f'（{rate}%）；分母不足 {MIN_IMPRESSIONS} 条，这个数只作参考、不给方向。'
        )
    else:
        parts.append(
            f'同期有 {dismissal_rate["denominator"]} 条机会在本周期内第一次被看见，'
            f'其中 {dismissal_rate["numerator"]} 条被按了「不再显示」，忽略率 {rate}%。'
        )
    return ''.join(parts)

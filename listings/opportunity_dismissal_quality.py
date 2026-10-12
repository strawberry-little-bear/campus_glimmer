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

**本模块不报忽略率，因为分母不可得。** `OpportunityDismissal` 只记「忽略」，不记「看
过」：一条机会被渲染出来、用户什么也没点，库里一行都没有。机会流页面本身也不产生任何
可反推的记录——「打开过机会流页」不在任何表里，机会卡上的「去响应」按钮指向求购详情
页，那条浏览记录会被算成一次普通商品浏览，无法和机会流区分。所以「窗口内被展示过的机
会」这个数拿不到，而它是忽略率的分母。

`recommendation_quality` 遇到过同样的问题，当时的解法是用 `BrowsingHistory` 反推分母，
并明确命名成**反馈率的下界**。机会流连这个近似都没有。报一个「用页面构造当分母」的率
（比如「进入机会流页的次数 × 每页候选数」）是估算不是测量，而一个估算出来的率会让运营
以为自己量过——那比没有数字更糟。所以这里报的四个数全部只描述忽略本身：规模与结构、
按分数档拆的忽略分布、按理由拆的忽略分布、重复忽略。前两个切分的分子是忽略，分母也是
忽略，那是忽略自己的形状，不是率。

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
from .models import Item, LostFoundPost, OpportunityDismissal

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


def _recompute_demand_score(dismissal, item_cache):
    """这条被忽略的求购机会，今天再进池会拿多少分。

    `OpportunityDismissal` 没存用户当时拿哪件商品匹配的，所以照
    `build_opportunity_feed` 的 `best_demands` 逻辑：这个用户名下**当前在售**的商品
    里分数最高的那一次匹配。复算只读当前在售的商品，所以它还原的是「如果今天再进池的
    分数」，不是忽略那一刻的分数——用户之后可能把商品下架、降价、改了分类。

    返回 `(score, reason_text)`；一件在售商品都匹配不上时返回 `(None, None)`，这一行进
    不了按分数档和按理由的切分，但仍然计入规模。
    """
    demand = dismissal.demand
    if demand is None:
        return None, None
    best = None
    for item in _available_items_for(dismissal.user_id, item_cache):
        match = _match_demand(demand, item)
        if not match:
            continue
        if best is None or match[0] > best[0]:
            best = match
    return best if best else (None, None)


def _recompute_lost_found_score(dismissal, post_cache):
    """这条被忽略的失物招领机会，今天再进池会拿多少分。

    同理照 `build_opportunity_feed` 的 `best_lost_found` 逻辑：这个用户名下**活跃**的
    失物招领记录里分数最高的那一次匹配，且只认 30 分以上的（机会流自己的门槛）。

    失物招领的机会方向和求购相反：候选记录是「和你自己那条相反类型」的别人发的记录，
    所以这里扫的是这个用户名下由他发布的活跃记录，拿每一条去和 `dismissal` 指向的那
    条候选配。
    """
    target = dismissal.lost_found_post
    if target is None:
        return None, None
    best = None
    for source in _active_posts_for(dismissal.user_id, post_cache):
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
    """按分数档拆的忽略分布。

    分子是忽略，分母也是忽略——这是忽略自己的形状，不是率。忽略率的分母不可得，见模块
    docstring。所以每一行报的是「这一档占该 kind 下全部可打分忽略的比例」，以及档内的
    平均分。空平台报 0 会被读成「没人忽略这一档」，而真相是根本没有忽略，所以没有可打
    分样本时占比是 None。
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

    只比较**占比**，不比较任何率。两个 kind 分开读：求购和失物的分值域不同（2-11 对
    30-100），混在一张表里比占比等于比两个不同的刻度。

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

    这是本模块唯一能回答「忽略有没有用」的数，也是唯一一个能穿透「分母不可得」的证
    据：如果一条被忽略的机会反复回到同一个人面前又被反复忽略，说明 30 天静默期对这个
    人无效。判据是间接的，见 `_is_repeated` 的说明。
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


def _recommendations(score_shape, repeat_stats):
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
    if not notes:
        if not repeat_stats['dismissal_total']:
            notes.append('本周期没有任何忽略记录，用户还没有在机会流上按过「暂不展示」。')
        else:
            notes.append(
                '忽略的分布没有明显背离，可继续观察更长的周期。本模块不报忽略率，'
                '因为分母（展示过的机会）不可得。'
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
    kind_rows = _build_kind_rows(dismissals)
    scored, unscored = _score_dismissals(dismissals)
    band_rows = _build_band_rows(scored)
    reason_rows = _build_reason_rows(scored)
    score_shape = _build_score_shape(band_rows)
    repeat_stats = _build_repeat_stats(dismissals)
    recommendations = _recommendations(score_shape, repeat_stats)

    total = len(dismissals)
    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(dismissals),
        'summary': (
            f'最近 {days} 天共有 {total} 条忽略，其中 {len(scored)} 条今天仍能重算出'
            f'匹配分数，{unscored} 条已经匹配不上任何在售商品或活跃记录。'
            if dismissals else f'最近 {days} 天没有任何忽略记录。'
        ),
        'metrics': {
            'dismissal_total': total,
            'scored_total': len(scored),
            'unscored_total': unscored,
            'repeat_share': repeat_stats['repeat_share'],
            'repeated_count': repeat_stats['repeated_count'],
            'min_band_sample': MIN_BAND_SAMPLE,
            'min_reason_sample': MIN_REASON_SAMPLE,
            'noise_points': NOISE_POINTS,
            'silence_days': SILENCE_DAYS,
        },
        'kind_rows': kind_rows,
        'band_rows': band_rows,
        'reason_rows': reason_rows,
        'score_shape': score_shape,
        'repeat_stats': repeat_stats,
        'recommendations': recommendations,
    }

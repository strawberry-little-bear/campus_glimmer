# -*- coding: utf-8 -*-
"""首页那八张推荐卡挑得准不准，第一次被运营读到。

`listings/recommendations.py` 的 `RecommendationService` 给每个登录用户算八件商品，按
一个可解释的分数排下来，首页「为你挑选」一栏渲染出来。分数由五部分组成：新鲜度（最多
4 分）、被收藏数（最多 8 分）、用户标过「想看看」（+10）、分类匹配（+8）、地点匹配
（+5）。每一件推荐都带一句人话理由，用户点卡上的「想看看」或「不感兴趣」，落在
`RecommendationFeedback` 里。

也就是说，**打分、证据、和证据所对应的那个分数，三样东西都在库里**，只是从没被放在
同一张表里。`RecommendationFeedback` 是全站唯一一个「用户对推荐结果本人表态、而平台
从来不知道这个表态准不准」的字段。后台能列出这些反馈，看板上没有任何一个数字。

站内已经有三个「量我们自己的排序/打分」的模块——`search_position_quality.py` 量搜索
排序，`demand_match_quality.py` 量匹配打分——推荐打分是唯一一个没被量过的，而它直接
决定全站流量最大的首页第一屏。一个新鲜度权重写错的推荐服务，会让每个用户的第一屏都
是刚上架四小时内的商品，运营从任何现有面板上都看不出这件事。

所以这个模块回答四个问题，四个分母：

1. **推荐卡收到了多少反馈。** 反馈率本身是第一个数字。分母是窗口内全部被点开过的
   (用户, 商品) 对，直接数 `BrowsingHistory`——不加曝光表，首页不该为一个运营面板
   多一次 insert。零反馈不是「用户满意」，是这套卡没有引起任何反应。这个分母比真实
   的展示次数只低不高：一件从搜索点进去的商品也占了分母，却没有推荐卡可点，所以反馈
   率报出来的是**下界**，只低不高。
2. **正负反馈的比例。** 「想看看」除以全部反馈。分母只收有过反馈的推荐，不收展示数：
   拿展示数当分母会把「没人看」混进「看了不喜欢」。
3. **按理由拆。** 五句理由各自的正负比例。这是唯一能指向具体改动的切分——「你标记过
   想看，优先为你保留」如果负反馈占多数，说明 +10 分把用户自己点过「想看看」的商品推
   得太频繁，用户当时想看、现在不想看了。
4. **反馈之后用户做了什么。** 「想看看」之后有没有真的去看这件商品、有没有真的下单；
   「不感兴趣」之后是不是还在被推。

四处的口径取舍比数字本身更要紧，面板上和导出里都要原样带到：

- **理由在反馈时刻重算。** `RecommendationFeedback` 不存理由，而理由由分数决定、每次
  请求重算。复算走 `recommendations.recommendation_reason_key`——同一个函数，不能在
  运营模块里另写一套。边界：重算的理由不等于推荐当时的理由，用户之后又收藏或浏览会
  改变它，所以每一行都带说明。复算里的 `feedback_match` 取「改过口」而不是「现在还
  想看」：改过口的行，第一次表态已经把商品写进了「想看看」集合，用户第二次点按钮时
  「你标记过想看」那句话是生效的；只表过一次态的行，点击本身就是入集合的那个动作，
  那句话还没生效。读「现在还想看」会让所有 interested 反馈都落进 `feedback_match`
  桶，正负比例退化成恒等式（interested 100%、dismiss 0%），第 3 问就永远问不出来。
- **改口是一次改口，不是两条记录。** `update_or_create` 保证 `(user, item)` 唯一，
  先「想看」后「不感兴趣」是一行 `action='dismiss'`，`created_at` 是首次表态、
  `updated_at` 是改口时刻。改口数单独统计——它是「推荐可能推得太频繁」最硬的证据。
  两列相差的判据带 60 秒容差，因为一次 `create()` 里 auto_now_add 与 auto_now 各取
  一次时钟，两列会差出毫秒级，直接 `!=` 会把每一条首次反馈都误判成改口。
- **不按用户聚合、不按商品聚合。** 按用户是排名（隐私，也不是运营射程），按商品让被推
  一万次和一次不可比。只按理由聚合，因为理由是打分的可解释输出。
- **只报告，不改权重。** 不调 +10、不调 0.8、不调 +8、不改理由文案、不加黑名单、不重
  算推荐。自己改自己权重会让看板量的不是推荐质量，而是它上周的输出。
"""

from datetime import datetime, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import BrowsingHistory, Favorite, Item, Order, RecommendationFeedback
from .recommendations import REASON_LABELS, REASONS, recommendation_reason_key

# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一个理由要凑够这么多条反馈，才允许对它下「这句理由在骗人」的判断。取 12 和其余
# 「量自己」模块同一个量级：正负比例的分母只有个位数时，一条反馈的改口就能把比例
# 推动十几个百分点，那种数字拿去改权重只会把好的理由改坏。
MIN_REASON_FEEDBACK = 12

# 两个理由的正负比例相差不满这么多个百分点，视为噪声。同一个量级的理由之间的差别，
# 运营是看不出来、也动不了手的。
REASON_NOISE_POINTS = 15

# 「想看看」之后，浏览或订单发生在这个天数之内才算「反馈之后真的动了」。超过这个
# 窗口的浏览无法归因，用户可能是在翻历史记录，不是在回应推荐卡。
FOLLOWUP_WINDOW_DAYS = 7

# 改口单独统计的门槛。改口数是「推得太频繁」最硬的证据，但它也是一个低频事件，样本
# 太少时报出来只是噪声，所以和理由门槛同一个量级。
MIN_SWITCH_SAMPLE = 12

# 按 action 读的理由标签顺序，与 `RecommendationFeedback.ACTION_CHOICES` 一致。
INTERESTED_ACTION = 'interested'
DISMISS_ACTION = 'dismiss'

# 判「改过口」的容差。`created_at` 是 auto_now_add、`updated_at` 是 auto_now，一次
# `create()` 里两列各取一次时钟，实测相差几微秒到一毫秒。所以不能用 `!=` 判改口——
# 那会把每一条首次反馈都算成改口，改口数恒等于反馈总数，第 4 问直接作废。真实用户
# 两次表态之间至少隔着一次页面往返，取 60 秒足够把「同一时刻写入的两列」和「后来又
# 点了一次」分开，又不会把真的改口漏掉。
SWITCH_TOLERANCE = timedelta(seconds=60)


def _switched(feedback):
    """这条反馈是不是改过口：先点了一个动作，后来又点了另一个。

    `update_or_create` 保证 `(user, item)` 只有一行，所以库里没有第二条记录可数，
    只能比两列的时刻。同一次 `create()` 写的两列相差在毫秒级，见 `SWITCH_TOLERANCE`
    的说明；跨过一次页面往返的表态才会差出分钟级。
    """
    delta = feedback.updated_at - feedback.created_at
    return delta > SWITCH_TOLERANCE


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, datetime.min.time()),
    )


def _recompute_reason(feedback, category_ids, location_ids, switched):
    """反馈时刻重算这条反馈的理由，走推荐服务同一个函数。

    复算只读五个输入：用户的分类偏好、地点偏好、这件商品的收藏数，以及这条反馈有没
    有改过口。偏好集合在调用方按用户缓存，不是每条反馈各查一遍。

    `feedback_match` 取 `switched` 而不是「这件商品现在还在不在想看看集合里」，这是
    本模块最关键的一个取舍：改过口的行，第一次表态已经把商品写进了「想看看」集合，
    用户第二次点按钮时「你标记过想看，优先为你保留」那句话是生效的，按钮就是被它推
    动的；只表过一次态的行，点击本身就是入集合的那个动作，那句话还没生效。若按「现
    在想看」读，所有 interested 反馈都会落进 `feedback_match` 桶、所有 dismiss 都不
    落，按理由拆开的正负比例就退化成恒等式，第 3 问永远问不出东西。

    边界明说：这不是推荐当时的理由——用户在点按钮之前又收藏、又浏览过的东西会改变
    它。但它是唯一不用改表、不用加曝光日志就能拿到的近似。
    """
    return recommendation_reason_key(
        feedback_match=switched,
        category_match=feedback.item.category_id in category_ids,
        location_match=(
            feedback.item.location_id is not None
            and feedback.item.location_id in location_ids
        ),
        favorite_count=Favorite.objects.filter(item=feedback.item).count(),
    )


def _preferred_category_ids(user):
    """用户表达过偏好的分类：收藏、浏览、成交三类都算。与推荐服务同一套口径。"""
    favorite_items = Favorite.objects.filter(
        user=user,
    ).values_list('item__category_id', flat=True)
    viewed_items = BrowsingHistory.objects.filter(
        user=user,
    ).values_list('item__category_id', flat=True)
    ordered_items = Order.objects.filter(
        buyer=user, status__in=['confirmed', 'meeting', 'completed'],
    ).values_list('item__category_id', flat=True)
    ids = set()
    for values in (favorite_items, viewed_items, ordered_items):
        ids.update(value for value in values if value is not None)
    return ids


def _preferred_location_ids(user):
    """用户表达过偏好的交易地点：收藏、浏览、成交三类都算，地点为空的商品不计。"""
    favorite_locations = Favorite.objects.filter(user=user).exclude(
        item__location_id=None,
    ).values_list('item__location_id', flat=True)
    viewed_locations = BrowsingHistory.objects.filter(user=user).exclude(
        item__location_id=None,
    ).values_list('item__location_id', flat=True)
    ordered_locations = Order.objects.filter(
        buyer=user, status__in=['confirmed', 'meeting', 'completed'],
    ).exclude(item__location_id=None).values_list('item__location_id', flat=True)
    ids = set()
    for values in (favorite_locations, viewed_locations, ordered_locations):
        ids.update(value for value in values if value is not None)
    return ids


def _reason_label(key):
    return REASON_LABELS[key]


def _build_reason_rows(feedbacks, now):
    """按理由聚合反馈：正负比例、改口数、反馈之后的动作。

    只按理由聚合。按用户聚合是排名，按商品聚合让被推一万次和一次不可比，而理由是打分
    的可解释输出，也是运营唯一能动手改的东西（改权重、改文案）。
    """
    rows = []
    for key in REASONS:
        scoped = [feedback for feedback in feedbacks if feedback['reason_key'] == key]
        feedback_count = len(scoped)
        interested = sum(1 for f in scoped if f['action'] == INTERESTED_ACTION)
        dismissed = sum(1 for f in scoped if f['action'] == DISMISS_ACTION)
        # 改口单独数：一行的 created_at 早于 updated_at，说明这个人先点了一个动作、
        # 后来又点了另一个。它是「推得太频繁」最直接的证据。
        switched = sum(1 for f in scoped if f['switched'])

        followed_up_view = 0
        followed_up_order = 0
        interested_rows = [f for f in scoped if f['action'] == INTERESTED_ACTION]
        if interested_rows:
            followed_up_view, followed_up_order = _followup_counts(interested_rows, now)

        interested_count = interested
        rows.append({
            'key': key,
            'label': _reason_label(key),
            'feedback_count': feedback_count,
            'interested_count': interested_count,
            'dismissed_count': dismissed,
            # 这一句理由一次都没被反馈过时，比例不存在而不是 0%——理由同上。
            'positive_rate': (
                _percent(interested_count, feedback_count) if feedback_count else None
            ),
            'switched_count': switched,
            'followed_view_count': followed_up_view,
            'followed_order_count': followed_up_order,
            'is_small_sample': feedback_count < MIN_REASON_FEEDBACK,
        })
    return rows


def _followup_counts(rows, now):
    """「想看看」之后有没有真的去看、有没有真的下单。

    两个条件都要满足：动作发生在表态**之后**，且落在 `FOLLOWUP_WINDOW_DAYS` 天窗口内。
    只看窗口不看表态时刻，会把「先看了才点按钮」算成「点了按钮才去看」——那是用户本来
    就在翻这件商品，不是推荐卡起了作用。超过窗口的同样不归因：用户可能只是在翻历史
    记录，不是在回应推荐卡。

    只报「表态之后有没有去看」，不报「表态之后有没有买」当成推荐质量：它和下单之间
    还隔着一次预约、一次见面。

    按 (用户, 商品) 批量取回来在内存里比，不逐条查库：窗口内的表态在校园规模下是几百
    行，而看板是一次打开全算的。
    """
    if not rows:
        return 0, 0
    window_start = now - timedelta(days=FOLLOWUP_WINDOW_DAYS)
    moments = {(row['user_id'], row['item_id']): row['updated_at'] for row in rows}
    user_ids = {row['user_id'] for row in rows}
    item_ids = {row['item_id'] for row in rows}

    def _count_after(rows_with_time):
        counted = 0
        for user_id, item_id, happened_at in rows_with_time:
            moment = moments.get((user_id, item_id))
            if moment is None or happened_at is None:
                continue
            if happened_at > moment and happened_at >= window_start:
                counted += 1
        return counted

    history = BrowsingHistory.objects.filter(
        user_id__in=user_ids, item_id__in=item_ids,
    ).values_list('user_id', 'item_id', 'last_viewed_at')
    orders = Order.objects.filter(
        buyer_id__in=user_ids, item_id__in=item_ids,
    ).values_list('buyer_id', 'item_id', 'created_at')
    return _count_after(history), _count_after(orders)


def _build_feedback_loop(reason_rows):
    """反馈率、正负比例、改口这三个整体数字，带各自的分母。"""
    feedback_total = sum(row['feedback_count'] for row in reason_rows)
    interested_total = sum(row['interested_count'] for row in reason_rows)
    dismissed_total = sum(row['dismissed_count'] for row in reason_rows)
    switched_total = sum(row['switched_count'] for row in reason_rows)
    # 一条反馈都没有的时候，正负比例不存在，不是 0%。空平台报 0% 会让运营以为用户在
    # 大量拒绝推荐，而真相是根本没人按过按钮。
    positive_rate = _percent(interested_total, feedback_total) if feedback_total else None
    return {
        'feedback_total': feedback_total,
        'interested_total': interested_total,
        'dismissed_total': dismissed_total,
        'positive_rate': positive_rate,
        'switched_total': switched_total,
        # 反馈率的分母是窗口内全部被点开过的商品，用 BrowsingHistory 数，不和反馈
        # 取交集。这个数字在 `build_recommendation_quality` 里填，因为它要读浏览表
        # 而不是反馈表。
        'viewed_recommendations': None,
        # 一件都没被点开过时，反馈率不存在——报 0% 会把「没人看」算成「看了不喜欢」。
        'feedback_rate': None,
    }


def _viewed_items_in_window(start, now):
    """窗口内被点开过的商品数，反馈率的分母。

    数 `BrowsingHistory` 在窗口里去重后的 (用户, 商品) 对数。**不**拿反馈的表单过
    滤：拿反馈过滤会让分母只剩「被推荐、被点开、还表过态」的那几件，反馈率恒为
    100%——正是设计文档明令禁止的「拿反馈数当反馈率的分母，这个数字就不存在了」。
    一件被点开过、用户什么按钮都没按的商品，也必须占一个分母。

    这个分母只低不高：一件从搜索点进去的商品也占了分母，却没有推荐卡可点。真正的分
    母要一张曝光表才数得出来，而本模块不加曝光表（首页不该为一个运营面板多一次
    insert）。所以反馈率是**反馈率的下界**，只低不高，面板和导出里都要这么写。
    """
    return BrowsingHistory.objects.filter(
        last_viewed_at__gte=start, last_viewed_at__lte=now,
    ).values('user_id', 'item_id').distinct().count()


def _build_measured_reasons(reason_rows):
    """达到样本门槛的理由，以及其中最值得看的那一句。

    一条理由的门槛比整体门槛更严：它单独切一刀，分母只有整体的一小块。
    """
    measured = [row for row in reason_rows if not row['is_small_sample']]
    if len(measured) < 2:
        return {
            'has_comparison': False,
            'summary': (
                f'达到 {MIN_REASON_FEEDBACK} 条反馈门槛的理由不足两条，'
                '读不出哪一句理由在骗人。'
            ),
        }
    strongest = max(measured, key=lambda row: row['positive_rate'])
    weakest = min(measured, key=lambda row: row['positive_rate'])
    spread = round(strongest['positive_rate'] - weakest['positive_rate'], 1)
    if spread < REASON_NOISE_POINTS:
        return {
            'has_comparison': True,
            'kind': 'flat',
            'summary': (
                f'最强与最弱理由的正反馈比例只相差 {spread} 个百分点，'
                '落在噪声范围内：五句话目前分不出高下。'
            ),
            'strongest_label': strongest['label'],
            'weakest_label': weakest['label'],
            'spread_points': spread,
        }
    kind = 'rising' if strongest['positive_rate'] > 50 else 'weak'
    return {
        'has_comparison': True,
        'kind': kind,
        'summary': (
            f'{strongest["label"]}的正反馈比例 {strongest["positive_rate"]}%，'
            f'{weakest["label"]}只有 {weakest["positive_rate"]}%，'
            f'相差 {spread} 个百分点。'
        ),
        'strongest_label': strongest['label'],
        'weakest_label': weakest['label'],
        'strongest_rate': strongest['positive_rate'],
        'weakest_rate': weakest['positive_rate'],
        'spread_points': spread,
    }


def _recommendations(reason_rows, measured, feedback_loop):
    """可以做什么，每一条都挂在替它说话的数字上。只报告，不改权重。"""
    notes = []
    # 最差的那句理由值得点名，不管是 rising 还是 weak：rising 是「有几句明显好、
    # 有一句明显差」，weak 是「最好的也就那样」。两种形状下最差那一句都指向一个
    # 具体的权重或文案，只是前者说明打分有区分度、后者说明整体都要重看。
    if measured.get('has_comparison') and measured.get('kind') in ('weak', 'rising'):
        shape = (
            '其余几句理由明显更好' if measured['kind'] == 'rising'
            else '没有哪一句理由明显更好'
        )
        notes.append(
            f'{measured["weakest_label"]}的正反馈比例只有 '
            f'{measured["weakest_rate"]}%，用户在这句话下面大量点「不感兴趣」，'
            f'而{shape}（{measured["strongest_label"]} '
            f'{measured["strongest_rate"]}%）。它指向一个具体的权重或文案，'
            '但本模块只报告，不改 recommendations.py 里的任何常数。'
        )
    if feedback_loop['switched_total'] >= MIN_SWITCH_SAMPLE:
        rate = _percent(feedback_loop['switched_total'], feedback_loop['feedback_total'])
        notes.append(
            f'有 {feedback_loop["switched_total"]} 条反馈改过口（{rate}%），'
            '用户当时想看、现在不想看了。这是「推荐可能推得太频繁」最直接的证据，'
            '比任何比例都硬。'
        )
    for row in reason_rows:
        if row['is_small_sample']:
            continue
        if row['switched_count'] >= MIN_SWITCH_SAMPLE:
            notes.append(
                f'{row["label"]}下有 {row["switched_count"]} 条改口，'
                '这句话推的东西用户看过了又会反悔。'
            )
    if not notes:
        notes.append('推荐反馈的比例没有明显背离，可继续观察更长的周期。')
    return notes


def _load_feedbacks(start, now):
    """窗口内的反馈，连理由复算需要的外键一次取回。

    归期按 `updated_at`：改口是最近一次表态，运营要看的是用户**现在**的态度。而
    `created_at` 也一起取，因为改口的判据就是两列的时刻差，见 `_switched`。
    """
    return list(
        RecommendationFeedback.objects.filter(
            updated_at__gte=start, updated_at__lte=now,
        ).select_related('user', 'item')
    )


def build_recommendation_quality(days=30, now=None):
    """量推荐打分：用户认不认这套推荐，不认的话是哪一句理由在骗人。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localdate(now))

    feedbacks = []
    # 理由复算要读用户的偏好集合，而一个人的反馈在窗口里会有好几条：偏好按用户缓存
    # 一次，不逐条反馈各查一遍库。看板是一次打开全算的，这里多一次查询就是多一次全表
    # 关联。
    preference_cache = {}
    for feedback in _load_feedbacks(start, now):
        cached = preference_cache.get(feedback.user_id)
        if cached is None:
            cached = (
                _preferred_category_ids(feedback.user),
                _preferred_location_ids(feedback.user),
            )
            preference_cache[feedback.user_id] = cached
        category_ids, location_ids = cached
        # 改口判据一次算清，理由复算和行上的 switched 用同一个值：分开算两次，
        # 将来改了判据就会出现「行上说改过口、理由却按没改过口归」的半份状态。
        switched = _switched(feedback)
        feedbacks.append({
            'user_id': feedback.user_id,
            'item_id': feedback.item_id,
            'action': feedback.action,
            'switched': switched,
            'reason_key': _recompute_reason(
                feedback, category_ids, location_ids, switched,
            ),
            # 表态时刻。反馈之后的动作要拿它和浏览、下单的时刻比。
            'updated_at': feedback.updated_at,
        })

    reason_rows = _build_reason_rows(feedbacks, now)
    feedback_loop = _build_feedback_loop(reason_rows)
    # 展示数用 BrowsingHistory 反推：不加曝光表，首页不该为一个运营面板多一次
    # insert，而这个 insert 要写在渲染八张卡的事务里。反推数不到「展示了没点开」
    # 的那部分，所以反馈率的分母是「被推荐且被点开过的」，不是「被推荐过的」。
    viewed_recommendations = _viewed_items_in_window(start, now)
    feedback_loop['viewed_recommendations'] = viewed_recommendations
    if viewed_recommendations:
        feedback_loop['feedback_rate'] = _percent(
            feedback_loop['feedback_total'], viewed_recommendations,
        )
    measured = _build_measured_reasons(reason_rows)
    recommendations = _recommendations(reason_rows, measured, feedback_loop)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(feedbacks),
        'summary': measured['summary'],
        'metrics': {
            'feedback_total': feedback_loop['feedback_total'],
            'positive_rate': feedback_loop['positive_rate'],
            'switched_total': feedback_loop['switched_total'],
            'min_reason_feedback': MIN_REASON_FEEDBACK,
            'reason_noise_points': REASON_NOISE_POINTS,
            'followup_window_days': FOLLOWUP_WINDOW_DAYS,
        },
        'reason_rows': reason_rows,
        'measured': measured,
        'feedback_loop': feedback_loop,
        'recommendations': recommendations,
    }

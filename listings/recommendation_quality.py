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

1. **推荐卡收到了多少反馈。** 反馈率本身是第一个数字。从本轮起分母真正落库了：
   `views.home` 每次渲染首页推荐卡都写一行 `RecommendationImpression`，「被看见」
   这件事本身第一次进了库，反馈率从一个下界变成一个测量（仍是下界，理由见下）。
   在此之前分母只能拿 `BrowsingHistory` 反推——一件从搜索点进去的商品也占了分母，
   却没有推荐卡可点。零反馈不是「用户满意」，是这套卡没有引起任何反应。
2. **正负反馈的比例。** 「想看看」除以全部反馈。分母只收有过反馈的推荐，不收展示数：
   拿展示数当分母会把「没人看」混进「看了不喜欢」。
3. **按理由拆。** 五句理由各自的正负比例。这是唯一能指向具体改动的切分——「你标记过
   想看，优先为你保留」如果负反馈占多数，说明 +10 分把用户自己点过「想看看」的商品推
   得太频繁，用户当时想看、现在不想看了。
4. **反馈之后用户做了什么。** 「想看看」之后有没有真的去看这件商品、有没有真的下单；
   「不感兴趣」之后是不是还在被推。

四处的口径取舍比数字本身更要紧，面板上和导出里都要原样带到：

- **曝光表记的是推荐当时的理由，复算记的是反馈时刻的理由，两个都要。**
  `RecommendationImpression` 落库时带着 `reason_key` 和 `score`，因为
  `Recommendation` 是 frozen dataclass、这两样东西在渲染那一刻就已经算出来了，跟着
  `item` 一起在内存里，落库是零成本的（机会流那边刻意不存，因为它的分数在忽略时刻
  要重算，存一份「当时的分数」只会得到一份永远对不齐的数据）。于是「当时挂这句话
  的卡，用户点了吗」有了推荐当时的分母；而复算回答的是另一问——「这句理由今天还
  成立吗」。两问不能混，面板和导出里都原样带到。
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

from .models import (
    BrowsingHistory, Favorite, Item, Order, RecommendationFeedback, RecommendationImpression,
)
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

# 反馈率的分母（窗口内第一次被看见的推荐卡）不足这么多张时，率只报数字、不给方向。
# 取 24 而不是 12：率比占比更敏感，一条反馈在 12 条分母上就是 8 个百分点，在 24 条上
# 才 4 个。与 `opportunity_dismissal_quality.MIN_IMPRESSIONS` 同一个量级、同一个理由
# ——那边上一轮刚为忽略率立过同样的门槛。与上面的 `MIN_REASON_FEEDBACK` 分开立：前者
# 是曝光门槛，后者是反馈门槛，复用一个会让「够不够样本」这件事在两张表上含义不同。
MIN_RECOMMENDATION_IMPRESSIONS = 24

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
        # 反馈率的分母是窗口内第一次被看见的 (用户, 商品) 对，从
        # `RecommendationImpression` 数，不和反馈取交集。这个数字在
        # `build_recommendation_quality` 里填，因为它要读曝光表而不是反馈表。
        'viewed_recommendations': None,
        # 一件都没被点开过时，反馈率不存在——报 0% 会把「没人看」算成「看了不喜欢」。
        'feedback_rate': None,
    }


# 推荐分数的分档边界，照三个可解释的加分取：+10（反馈匹配）、+8（分类匹配）、+5
# （地点匹配）。不按分数等分——运营能动手改的就是这三个常数，按它们分档才问得出
# 「+10 这一档把多少商品推上了首页」。上界一档是开区间兜底，见 `_build_score_bands`。
SCORE_BANDS = (
    (0, 5, '0-5 分（新鲜度与热度）'),
    (5, 8, '5-8 分（地点匹配）'),
    (8, 10, '8-10 分（分类匹配）'),
    (10, 13, '10-13 分（反馈匹配）'),
)


def _first_seen_recommendations(start, now):
    """窗口内第一次被看见的 (用户, 商品) 对，反馈率真正的分母。

    数 `RecommendationImpression` 在窗口内去重后的 (用户, 商品) 对数。这一轮之前分
    母只能拿 `BrowsingHistory` 反推——一件从搜索点进去的商品也占分母，却没有推荐卡
    可点，所以反馈率报出来的是下界；现在「被看见」这件事本身落了库，见
    `docs/recommendation-impression-design.md`。

    三条口径，逐条都是为了让分母和分子说的是同一件事：

    - **去重只发生在算「第一次被看见」的那一刻。** 一件商品在窗口内被渲染了五
      次，它只占一个分母。反过来不去重会让分母重复计、分子不重复计，率会虚低。
    - **不设 (用户, 商品) 唯一约束，所以这里必须自己去重。** `RecommendationFeedback`
      上有唯一约束因为表态该折叠；`RecommendationImpression` 上没有，因为「看见」不
      该折叠——同一个人在不同时刻看见同一件卡是两次独立的看见。
    - **只数登录用户的看见。** 推荐服务对匿名用户返回空列表，首页推荐卡整栏不渲染，
      所以库里本来就不存在匿名行。

    仍是下界，只低不高，三种偏低来源：

    1. `RecommendationFeedback.updated_at` 可能在窗口外（用户很早就表过态、窗口内又
       点了一次），按「反馈时刻归期」它进不了分子，分母照算；
    2. 一件商品在窗口内被看见、用户在窗口内才按按钮，但按钮的动机来自窗口外那次看
       见——这种情形分子算不到、分母照算；
    3. 曝光表自本轮起才开始积累，窗口跨过上线时刻的周期，前半段的分母是缺的。

    方向是可接受的：**虚高的反馈率会让运营以为推荐在惹恼用户，虚低只会让人保守。**
    空分母报 `None`，不报 0——一周一张卡都没渲染过，反馈率不存在，不是零。
    """
    return RecommendationImpression.objects.filter(
        created_at__gte=start, created_at__lte=now,
    ).values('user_id', 'item_id').distinct().count()


def _build_reason_impression_rows(start, now):
    """按推荐当时的理由拆曝光与反馈：五句理由各自被看见了多少张、其中多少张被点了。

    这是本轮真正的新读数。`reason_rows` 那一层报的是**反馈时刻重算**的理由，回答
    「这句理由今天还成立吗」；这一层报的是**曝光当时**的理由（`Recommendation`
    在渲染那一刻就带着 `reason_key`，落库是零成本的），回答「当时挂这句话的卡，用
    户口点了吗」。两问不能混：前者会因为用户之后又收藏、又浏览而漂移，后者不会。

    只报有曝光的理由：一句理由一次都没被渲染过，它在这一层不存在，不是零。

    分子的口径与整体一致——这条反馈对应的商品在窗口内至少被展示过一次，见
    `_first_seen_recommendations`。
    """
    impressions = list(
        RecommendationImpression.objects.filter(
            created_at__gte=start, created_at__lte=now,
        ).values('user_id', 'item_id', 'reason_key')
    )
    if not impressions:
        return []

    seen_pairs = {(row['user_id'], row['item_id']) for row in impressions}
    feedback_pairs = set(
        RecommendationFeedback.objects.filter(
            updated_at__gte=start, updated_at__lte=now,
        ).values_list('user_id', 'item_id')
    ) & seen_pairs

    by_reason = {}
    for row in impressions:
        by_reason.setdefault(row['reason_key'], set()).add((row['user_id'], row['item_id']))
    rows = []
    for key in REASONS:
        pairs = by_reason.get(key, set())
        feedback_count = len(pairs & feedback_pairs)
        rows.append({
            'key': key,
            'label': _reason_label(key),
            'shown_count': len(pairs),
            'feedback_count': feedback_count,
            # 空分母报 None 不报 0：一句理由一张卡都没渲染过，反馈率不存在。
            'feedback_rate': (
                _percent(feedback_count, len(pairs)) if pairs else None
            ),
            'is_small_sample': len(pairs) < MIN_RECOMMENDATION_IMPRESSIONS,
        })
    return rows


def _build_score_bands(start, now):
    """按推荐当时的分数档拆曝光规模：这套打分今天在把什么样的商品推上首页。

    分数五部分：新鲜度（最多 4）、被收藏数（最多 8）、「想看看」+10、分类匹配 +8、
    地点匹配 +5。档位边界照这三个可解释的加分取，不按分数等分——运营能动手改的就
    是这三个常数，按它们分档才问得出「+10 这一档推了多少东西上来」。

    只报规模不报反馈率：分数档不是理由，一句「8 分档的反馈率更高」说明不了任何可
    改的东西（分数是五部分相加的结果，两个同分的商品可能一个靠新鲜度一个靠分类）。
    要按分数问率，得先有「这条反馈当时的分数」，那是另一个模块的事。
    """
    scores = list(
        RecommendationImpression.objects.filter(
            created_at__gte=start, created_at__lte=now,
        ).values_list('score', flat=True)
    )
    rows = []
    for low, high, label in SCORE_BANDS:
        shown_count = sum(1 for score in scores if low <= score < high)
        rows.append({
            'label': label,
            'shown_count': shown_count,
        })
    # 兜底档：超过最高档上界的分数（`+10 +8 +5 +8 +4` 的理论上限是 35，真实数据不可
    # 能到，但分档要覆盖到，否则最后一段会凭空消失）。
    overflow = sum(1 for score in scores if score >= SCORE_BANDS[-1][1])
    rows.append({'label': f'{SCORE_BANDS[-1][1]} 分及以上', 'shown_count': overflow})
    return rows


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


def _recommendations(reason_rows, measured, feedback_loop, reason_impression_rows):
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
    # 按推荐当时的理由挑一句最值得看的：它比反馈时刻复算的理由更硬——分母是渲染那
    # 一刻真的挂在那张卡上的那句话，不随用户之后的收藏、浏览漂移。样本不足时不点名，
    # 见 `is_small_sample`。
    # 样本够的理由都进候选，**包括零反馈的**：一张卡都没被点开正是最弱的那种形状，
    # 把它筛掉只会留下「有几张被点开」的理由，最该被点名的那一句反而消失。样本不够的
    # 不进——24 条分母都凑不到时，0% 和 100% 都只是一两次渲染的偶然。
    impression_measured = [
        row for row in reason_impression_rows
        if not row['is_small_sample'] and row['shown_count']
    ]
    if impression_measured:
        # 挑最弱的那一句，文案按率的方向分开写：率低时说「只有几张被点开」，率高时
        # 说「几乎张张被点开」。共用一句「只有 N 张被点开（100%）」会在最好的一句上
        # 写出一个自相矛盾的数——读的人会以为自己看错了，转而怀疑整张表。
        weakest = min(impression_measured, key=lambda row: row['feedback_rate'])
        if weakest['feedback_rate'] < 50:
            shape = (
                f"当时的 {weakest['shown_count']} 张卡里只有 "
                f"{weakest['feedback_count']} 张被点开（{weakest['feedback_rate']}%）"
            )
        else:
            shape = (
                f"当时的 {weakest['shown_count']} 张卡里有 "
                f"{weakest['feedback_count']} 张被点开（{weakest['feedback_rate']}%）"
            )
        notes.append(
            f"「{weakest['label']}」{shape}，是五句理由里最弱的一句。"
            '这个分母是推荐那一刻的，不随用户之后的收藏、浏览漂移——'
            '按理由拆的那张表量的是反馈时刻重算的理由，两问不能混。'
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
    # 反馈率的分母自本轮起真正落库：首页每次渲染推荐卡都写一行
    # `RecommendationImpression`，「被看见」这件事本身进了库，不再拿 BrowsingHistory
    # 反推。反推那一版报的是下界（一件从搜索点进去的商品也占分母，却没有推荐卡可
    # 点）；落库之后仍是下界，但偏低的来源只剩三种，见 `_first_seen_recommendations`。
    viewed_recommendations = _first_seen_recommendations(start, now)
    feedback_loop['viewed_recommendations'] = viewed_recommendations
    if viewed_recommendations:
        feedback_loop['feedback_rate'] = _percent(
            feedback_loop['feedback_total'], viewed_recommendations,
        )
    measured = _build_measured_reasons(reason_rows)
    # 按推荐当时的理由拆曝光，以及按分数档拆曝光规模。两层都只读曝光表，一条反馈都
    # 不需要，所以周期里一张卡都没渲染过时它们是空的——空平台报空，不报 0。
    reason_impression_rows = _build_reason_impression_rows(start, now)
    score_bands = _build_score_bands(start, now)
    recommendations = _recommendations(
        reason_rows, measured, feedback_loop, reason_impression_rows,
    )

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        # 有反馈或有曝光都算有数据：曝光表先于反馈表积累（用户先看见、后才可能点
        # 按钮），一个刚上线、只渲染过首页还没人按过按钮的周期也要能在面板上读到
        # 「已经被看见多少张、其中零张被点」。
        'has_data': bool(feedbacks) or bool(reason_impression_rows),
        'summary': measured['summary'],
        'metrics': {
            'feedback_total': feedback_loop['feedback_total'],
            'positive_rate': feedback_loop['positive_rate'],
            'switched_total': feedback_loop['switched_total'],
            'min_reason_feedback': MIN_REASON_FEEDBACK,
            'reason_noise_points': REASON_NOISE_POINTS,
            'followup_window_days': FOLLOWUP_WINDOW_DAYS,
            # 曝光门槛与反馈门槛分开立，面板上两个数都要显示，否则「样本够不够」会
            # 被读成同一个意思。
            'min_recommendation_impressions': MIN_RECOMMENDATION_IMPRESSIONS,
        },
        'reason_rows': reason_rows,
        'reason_impression_rows': reason_impression_rows,
        'score_bands': score_bands,
        'measured': measured,
        'feedback_loop': feedback_loop,
        'recommendations': recommendations,
    }

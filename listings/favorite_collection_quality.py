# -*- coding: utf-8 -*-
"""用户建的心愿单分组，第一次被运营读到。

`views.toggle_favorite` 在商品卡上收藏或取消收藏，`views.create_favorite_collection` 建分
组，`views.update_favorite_collection` 把一条收藏移进或移出分组。分组的功能从很早起就是
建全的：心愿单页上能建、能改名之外的一切操作、能按分组筛选，`admin.py` 里两个模型都注册
了。而 `Favorite` 在别处只被顺带读过——`analytics.campus_pulse` 拿它算活跃用户、
`recommendations` 拿它取分类偏好、专题分析拿它算「浏览到收藏」的转化。

**也就是说，「分组」这个维度从头到尾没有一条统计：没人知道有多少人在用它，也不知道建了分
组之后收藏行为有没有变化。** 后台能按用户名列出分组，看板上没有任何一个数字。

站内已有二十多个「量我们自己的功能」的模块，量过搜索排序、求购匹配、首页推荐、机会流负
反馈、私信响应、评价质量、争议处理。本模块和它们的性质不一样：那些量的是「平台做了一个
判断，这个判断准不准」，本模块量的是「平台给了一个工具，这个工具好不好用」——功能采用的
度量，而不是打分器的自我检验。站内并不缺打分器的样板，缺的是后者。

本模块只报告，不新建分组、不合并分组、不改任何一条 `Favorite.collection`、不改任何分组
名、不动 `FavoriteCollectionForm` 的任何校验、不给任何用户排名、不预设任何分组名。几处口
径上的取舍比数字本身更要紧。

**「先收藏后归类」现在报得了，前提是 `Favorite.grouped_at`。** 在那一列加上之前，这个
数在库里不存在：`Favorite` 没有 `updated_at`，而 `views.update_favorite_collection` 走的
是 `favorite.save(update_fields=['collection'])`，模型里也没有任何 `auto_now` 字段，所以
这次保存不刷新任何时间列。一条 60 天前收藏的商品，昨天被移进分组，它在这个库里的全部痕
迹就是 `created_at = 60 天前`、`collection = 那个分组`——**它是什么时候被归类的，不知
道。**

于是「窗口内新建的收藏里有多少条直接就归了类」这个数读不出来：一条窗口内新建的收藏，
`collection` 是今天读到的值，不是它创建时的值。报出来会被读成「收藏时就归组率」，而它其
实混着大量后来才移进去的收藏。报一个会被误读成另一个意思的数，比没有数字更糟——和
`opportunity_dismissal_quality` 当初不报忽略率是同一条规矩。

`grouped_at` 补上之后那条规矩守住了，而这个数本身第一次可测。它记的是**这条收藏的分组
最后一次发生变化的时刻**，归进去和移出来都算，由 `Favorite.save()` 在 `collection_id`
变化时自己写。写在模型层而不是视图层，是因为 `collection` 有两条写入路径：这个视图和
Django admin 的 change 表单，只改视图层会让后台每改一条收藏就留一个假数据。

**同理不报「分组被改名 / 被整理的频率」。** `FavoriteCollection` 有 `updated_at`
（`auto_now`），但应用内没有改名路由：`create_favorite_collection` 走 `get_or_create`，同
名时不写；`update_favorite_collection` 改的是 `Favorite.collection`，不是分组名。唯一的写
入路径是 Django admin 的保存。所以 `updated_at != created_at` 只说明「有人在后台动过这条分
组」，数不到改名，也分不清改名和别的字段变更。

**同理不报「删分组之后收藏去哪了」。** `Favorite.collection` 是 `on_delete=SET_NULL`：删
分组不删收藏，只把它们变成未分组。而删分组在应用里没有路由、没有视图，只走 admin。所以
「删过分组」这件事本身不留痕，删完的收藏和从来没归过类的收藏长得一模一样。

**分母全都有，但四类分母各自独立，不能混。** 这是本模块和上一轮最大的区别：机会流那边
「被展示过的机会」拿不到，心愿单这边每一样东西的存量都是可数的。但「当前全部收藏」「当
前全部分组」「当前有收藏的用户」「窗口内新建的收藏或分组」是四个不同的分母，混用会造出
读不出来的数——比如拿「有收藏的用户」当分母去报分组渗透率，会把没收藏过任何东西的人也
算进分母，而他们根本看不到分组功能。

**「当前」是快照，不是窗口。** 分组可以被删（删完收藏变未分组），收藏可以被取消
（`toggle_favorite` 的 `delete` 分支），所以快照和「窗口内发生过什么」是两件事。本模块同
时报窗口内的新建规模（按 `created_at` 归期）和当前快照（渗透、分布），两者在面板上分开
摆，不放进同一张表。这一点必须写在面板上，否则「最近 7 天新建了 40 个分组」和「当前一共
有 40 个分组」会被读成同一件事。

**空分组要分「刚建」和「早就建了还空着」。** 空分组是「建了分组不会用」的信号，但它有一
个明显的良性解释：这个分组是几秒钟前刚建的，用户还没来得及往里放东西。所以按
`FavoriteCollection.created_at` 拆一刀，建于 7 天前仍然是空的才算真信号。7 天没有理论依
据，取的是「一个真心想用分组的人不会一周都想不起来往里放第一条」这个量级，见
`EMPTY_GROUP_GRACE_DAYS`。

**分组名按用户数聚合，天然就是用户数。** 分组名在同一个用户内唯一
（`UniqueConstraint(fields=['user', 'name'])`），所以一个名字的计数不需要另做去重。但按名
字聚合有一个隐私边界：一个只被一个人用过的名字，报出来就等于报了一个人的个人命名。所以
设 `MIN_NAME_SAMPLE`，只有被这么多个以上不同用户用过的名字才进榜单——这不是样本量问
题，是隐私问题，和「按用户聚合是排名」是同一条线。
"""

from datetime import datetime, time as dt_time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import Favorite, FavoriteCollection, Item

# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 建于多少天前仍然是空的分组，才算「建了不会用」。取 7 天：一个真心想用分组的人不会一
# 周都想不起来往里放第一条，而几秒钟前刚建的分组当然还是空的——后者是良性解释，不能和
# 前者混在一个数里。
EMPTY_GROUP_GRACE_DAYS = 7

# 收藏之后多久之内归类，算「当场归类」。取 60 分钟：一个真心想当场归组的用户，合
# 理的路径是「看到商品 → 收藏 → 去心愿单整理」，这条路径花掉几分钟到半小时都正
# 常，但不太可能花掉大半天还想得起刚才那条收藏该归哪。这个数没有理论依据，所以
# 它必须出现在面板上——运营读不出「当场」和「事后」的分界在哪，这两个数就没有意
# 义。和 `EMPTY_GROUP_GRACE_DAYS` 是同一条理由，但量的不是同一件事：那边是分组成
# 立多久还算「刚建」，这边是归类距收藏多久还算「当场」。
INSTANT_GROUPING_GRACE_MINUTES = 60

# 一个分组大小档要凑够这么多分组，才允许对它下「这一档是主流用法」的判断。占比的分母只有
# 个数时，一条收藏的改判就能把占比推动十几个百分点，那种数字拿去改引导只会把好的用法改坏。
# 注意这和 `MIN_NAME_SAMPLE` 是两个不同的门槛：那边是隐私线（低频名字不进榜），这边是样本
# 线（小样本档不给方向），取同一个数只是量级相同，含义必须分开写。
MIN_BAND_SAMPLE = 3

# 一个名字要被这么多个以上不同用户用过，才允许进命名收敛榜。这是隐私线不是样本线：一
# 个只被一个人用过的分组名，报出来等于把某个人的个人命名公开展示。分组名在同一个用户内
# 唯一，所以这个数天然就是不同用户数。
MIN_NAME_SAMPLE = 3

# 两个分组大小档的人数占比相差不满这么多个百分点，视为噪声。同一个量级的切分之间的这点
# 差别，运营看不出来、也动不了手。
NOISE_POINTS = 10

# 分组大小按「条收藏」分档。档位取运营能直接动手改引导的量级：一个装几十条的收纳盒和一
# 个装一两条的标签是两种完全不同的用法，而「3-10 条」这一档是这个平台上大多数分组落在
# 的地方。上限一档是 20 条以上而不是 50 条：能往一个分组里放二十条以上收藏的用户，收
# 藏夹本身就已经很大了，这一档再往上切分读不出形状。
SIZE_BANDS = (
    (1, 1, '1 条'),
    (2, 2, '2 条'),
    (3, 10, '3-10 条'),
    (11, 20, '11-20 条'),
    (21, None, '20 条以上'),
)


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _share_or_none(part, whole):
    """占比；分母为零时返回 None，不返回 0。

    空平台报 0% 会被读成「渗透率是零，一个都没有」，而真相是根本没有分母。和其余模块同
    一条规矩：没有分母时这个数不存在，不是 0。
    """
    return _percent(part, whole) if whole else None


def _load_collections():
    """当前全部分组，一条收藏都没有的也算进来，连收藏一起预取。

    这里不做时间过滤：分组大小和空分组都问的是「现在有多少条」，而删分组会让收藏变成未分
    组、取消收藏会让分组变小，所以问的必须是当前快照。窗口内的新建规模另走
    `_load_window_counts`，两个数在面板上分开摆。
    """
    return list(
        FavoriteCollection.objects.annotate(favorite_count=Count('favorites')),
    )


def _load_window_counts(start, now):
    """窗口内新建的分组数和收藏数，按 `created_at` 归期。

    收藏的 `created_at` 是收藏时刻，分组的 `created_at` 是建分组时刻，两列都是
    `auto_now_add`，写进去就不再动，所以窗口归属是干净的——这是库里唯一两个可以直接按
    时间切的列。
    """
    collection_count = FavoriteCollection.objects.filter(
        created_at__gte=start, created_at__lte=now,
    ).count()
    favorite_count = Favorite.objects.filter(
        created_at__gte=start, created_at__lte=now,
    ).count()
    return collection_count, favorite_count


def _load_current_totals():
    """当前快照的三个分母：全部分组、全部收藏、有收藏的用户数、建过分组的用户数。

    四个数各自独立，见模块 docstring 里「四类分母各自独立」那段。任何一个都不拿另一个
    当分母。
    """
    group_total = FavoriteCollection.objects.count()
    favorite_total = Favorite.objects.count()
    favoriter_ids = set(
        Favorite.objects.values_list('user_id', flat=True).distinct(),
    )
    collector_ids = set(
        FavoriteCollection.objects.values_list('user_id', flat=True).distinct(),
    )
    return group_total, favorite_total, favoriter_ids, collector_ids


def _build_scale_rows(collections):
    """分组的大小分布，按收藏条数分档。分子是分组，分母也是分组。

    这是分组自己的形状，不是率——「多少人把收藏分了组」是另一张表（渗透），这里只问「分
    出来一个分组的人往里面放了几条」。两件事分母不同，不能放进同一张表。

    占比在**分母为零**时是 None，不是 0：空平台报 0% 会被读成「这一档一个都没有」，而真相
    是根本没有分组、这个数不存在。反过来，分母存在而某一档确实是 0 个时，0.0 是对的——那
    是一个真正的零。

    空分组（0 条）不属于任何一档，所以档位行加不起来不等于分组总数。这是有意的：大小分布
    问的是「往分组里放了几条」，空分组没有「几条」可放，它的信号在空分组那张表里。
    """
    rows = []
    total = len(collections)
    for low, high, label in SIZE_BANDS:
        in_band = [
            collection for collection in collections
            if collection.favorite_count >= low
            and (high is None or collection.favorite_count <= high)
        ]
        rows.append({
            'label': label,
            'low': low,
            'high': high,
            'group_count': len(in_band),
            'share_of_groups': _share_or_none(len(in_band), total),
            'is_small_sample': len(in_band) < MIN_BAND_SAMPLE,
        })
    return rows


def _build_size_shape(scale_rows, group_total):
    """按大小档读出来的形状：分组被当成收纳盒还是标签。

    只比较**占比**，不比较任何率。两个档的占比相差不满噪声线，只说分布平坦，不指方向
    ——和其余模块同一条规矩。

    门槛取 `MIN_BAND_SAMPLE`：一个档要凑够这么多分组才允许对它下判断。占比的分母只有个
    位数时，一条收藏的改判就能把占比推动十几个百分点，那种数字拿去改引导只会把好的用法
    改坏。注意这是样本线，和名字榜那条隐私线（`MIN_NAME_SAMPLE`）是两件事。
    """
    measurable = [
        row for row in scale_rows if row['group_count'] >= MIN_BAND_SAMPLE
    ]
    if not group_total or len(measurable) < 2:
        return {
            'has_comparison': False,
            'summary': (
                f'分组数量还太少（当前 {group_total} 个），读不出分组被当成收纳盒还是'
                '标签。'
            ),
        }
    top = max(measurable, key=lambda row: row['share_of_groups'])
    bottom = min(measurable, key=lambda row: row['share_of_groups'])
    spread = round(top['share_of_groups'] - bottom['share_of_groups'], 1)
    entry = {
        'has_comparison': True,
        'spread_points': spread,
        'strongest_label': top['label'],
        'weakest_label': bottom['label'],
        'strongest_share': top['share_of_groups'],
        'weakest_share': bottom['share_of_groups'],
    }
    if spread < NOISE_POINTS:
        entry.update({
            'kind': 'flat',
            'summary': (
                f'占比最高与最低的分组大小档只相差 {spread} 个百分点，落在噪声范围内：'
                '分组大小暂时读不出倾向。'
            ),
        })
    else:
        entry.update({
            'kind': 'skewed',
            'summary': (
                f'{top["label"]} 占了全部分组的 {top["share_of_groups"]}%，而 '
                f'{bottom["label"]} 只有 {bottom["share_of_groups"]}%，相差 '
                f'{spread} 个百分点：分组主要被当成「{top["label"]}」来用。'
            ),
        })
    return entry


def _build_empty_rows(collections, now):
    """空分组按建组时间拆一刀：刚建的和早就建了还空着的。

    见 `EMPTY_GROUP_GRACE_DAYS` 的说明。这一档报的占比分母是**空分组自己**，不是全部分
    组——问的是「空分组里有多少是早就空着的」，不是「多少比例的分组早就空了」。后者用全
    部分组当分母同样能算，但它和「空分组率」是同一个数的两种说法，放进同一张表会让运营
    以为有两个独立的信号。
    """
    empty = [
        collection for collection in collections if collection.favorite_count == 0
    ]
    cutoff = now - timedelta(days=EMPTY_GROUP_GRACE_DAYS)
    stale = [
        collection for collection in empty
        if collection.created_at < cutoff
    ]
    return {
        'group_total': len(collections),
        'empty_total': len(empty),
        # 空分组占全部分组的比例。分母是全部分组，这是一个真正的比例。
        'empty_share': _share_or_none(len(empty), len(collections)),
        'stale_empty_total': len(stale),
        # 早就空着的占空分组的比例。分母是空分组，报的是空分组自己的形状。
        'stale_share': _share_or_none(len(stale), len(empty)),
        'stale_user_count': len({collection.user_id for collection in stale}),
        'grace_days': EMPTY_GROUP_GRACE_DAYS,
    }


def _build_penetration(favorite_total, favoriter_ids, collector_ids):
    """收藏的分组渗透：多少条收藏被归了类，多少有收藏的人用了分组。

    两个比例的分母不同，必须分开报：

    - 「已归组的收藏占全部收藏的比例」，分母是**全部收藏**。未分组的收藏就是一条普通收
      藏，和分组功能无关——这个比例问的是「已经放进分组收纳盒的收藏有多少」。
    - 「建过分组的人占有收藏用户的比例」，分母是**有收藏的用户**，分子是**有收藏且建过
      分组的人**。

    分子必须是「有收藏且建过分组的人」，不能是「建过分组的人」：`create_favorite_collection`
    只要求登录，不要求有收藏，所以一个人完全可以先建一个空分组、一条收藏都没有。拿全部
    建分组的人当分子、有收藏的人当分母，占比会超过 100%——那不是口径不严谨，是报了一个
    数学上不可能的数。所以这里取两个集合的交集。

    与此同时，「建了分组但一条收藏都没有」是另一批人，他们不该被渗透率吞掉：他们用了分
    组功能、但分组是空的。单独报成绝对量。
    """
    grouped_favorite_total = Favorite.objects.filter(
        collection__isnull=False,
    ).count()
    collectors_with_favorites = collector_ids & favoriter_ids
    return {
        'favorite_total': favorite_total,
        'grouped_favorite_total': grouped_favorite_total,
        # 已归组收藏占全部收藏的比例。分母是全部收藏。
        'grouped_favorite_share': _share_or_none(
            grouped_favorite_total, favorite_total,
        ),
        'favoriter_count': len(favoriter_ids),
        'collector_count': len(collector_ids),
        'collector_with_favorite_count': len(collectors_with_favorites),
        # 有收藏且建过分组的人，占有收藏用户的比例。分子是交集，见 docstring。
        'collector_share': _share_or_none(
            len(collectors_with_favorites), len(favoriter_ids),
        ),
        # 只收藏、从没建过分组的人数。绝对量，分母是有收藏的用户。
        'favoriter_only_count': len(favoriter_ids - collector_ids),
        # 建了分组但一条收藏都没有的人数。绝对量。这批人用了分组功能，但分组是空的——他们
        # 不该被渗透率吞掉，也不能算进「没收藏的人看不到分组功能」那一类。
        'collector_only_count': len(collector_ids - favoriter_ids),
    }


def _build_grouping_latency_rows(now):
    """收藏是当场归类的，还是囤了一阵才整理的。

    这一张表是本轮新增的，前提是 `Favorite.grouped_at` 那一列。在它之前，「先收藏
    后归类」这个数在库里不存在——`Favorite` 没有 `updated_at`，而归类走的是
    `save(update_fields=['collection'])`，不刷新任何时间列。所以窗口内新建的收藏读出
    来的 `collection` 是今天读到的值，报出去会被读成「收藏时就归组率」，其实混着大量
    后来才移进去的收藏。报一个会被误读成另一个意思的数，比没有数字更糟。

    四分，不是三分：

    - **当场归类**：`grouped_at` 与 `created_at` 的距离在宽限之内。用户收藏时心里
      已经想好了这东西属于哪一类。
    - **事后归类**：宽限之外才归的。先囤后理，分组是事后整理的工具。
    - **从未归类**：`collection` 为空且 `grouped_at` 为空。分组功能没覆盖到它。
    - **归过类又取消**：`collection` 为空但 `grouped_at` 不为空。`update_favorite_
      collection` 支持把 collection_id 传空，也就是把收藏移出分组——这批人试过分组
      又退回去，那是比「从未归类」更强的信号，说明分组对他们没有价值。

    分母只收**窗口内新建的收藏**，不收全部收藏。「先收藏后归类」问的是一次行为的
    时序，而全部收藏里混着迁移前归组的老数据（`grouped_at` 为 NULL，归类的时刻不
    可知），把它们算进分母，事后归类的占比会被一批不知道时刻的收藏稀释。

    迁移前归组的老数据单列成一类，不进任何分母。这不是样本不足——样本不足照实报数
    字但压淡，数据不可知必须明确说「这个数不知道」，两件事的处理方式不同。
    """
    grace = timedelta(minutes=INSTANT_GROUPING_GRACE_MINUTES)
    # 时间差在 Python 里算，不写 `extra()`：本项目支持 PostgreSQL，而两个时间戳相
    # 减的 SQL 写法在各家数据库上不一样（SQLite 是 julianday，PostgreSQL 是直接减
    # interval）。一次收藏的时序判断不值得为它绑死一个数据库。
    stamped = Favorite.objects.filter(
        created_at__isnull=False, grouped_at__isnull=False,
    ).values_list('created_at', 'grouped_at')
    instant = sum(1 for created_at, grouped_at in stamped
                  if grouped_at - created_at <= grace)
    grouped_ever = Favorite.objects.filter(grouped_at__isnull=False).count()
    ungrouped_now = Favorite.objects.filter(
        collection__isnull=True, grouped_at__isnull=True,
    ).count()
    released = Favorite.objects.filter(
        collection__isnull=True, grouped_at__isnull=False,
    ).count()
    return {
        'grace_minutes': INSTANT_GROUPING_GRACE_MINUTES,
        'instant_total': instant,
        'delayed_total': grouped_ever - instant,
        'ungrouped_total': ungrouped_now,
        'released_total': released,
        'grouped_ever_total': grouped_ever,
        'current_total': Favorite.objects.count(),
    }


def _build_name_rows(collections):
    """分组名的收敛形状：哪些名字被很多人用过。

    分组名在同一个用户内唯一（`UniqueConstraint(fields=['user', 'name'])`），所以一个名
    字的计数天然就是「用过这个名字的用户数」，不需要另做去重——这也是为什么这里不按
    `values('name').annotate(Count('id'))`：那数出来的是分组条数，一个用户同名只能有一
    条，两条数在这里恰好相等，但写成用户数才说得清这个数是什么。

    只收被 `MIN_NAME_SAMPLE` 个以上不同用户用过的名字，见模块 docstring 的隐私说明。低
    频的名字不报，报出来等于公开某个人的个人命名。
    """
    grouped = {}
    for collection in collections:
        entry = grouped.setdefault(collection.name, {
            'group_count': 0,
            'user_ids': set(),
            'favorite_count': 0,
        })
        entry['group_count'] += 1
        entry['user_ids'].add(collection.user_id)
        entry['favorite_count'] += collection.favorite_count
    rows = []
    for name, entry in grouped.items():
        if len(entry['user_ids']) < MIN_NAME_SAMPLE:
            continue
        rows.append({
            'label': name,
            'user_count': len(entry['user_ids']),
            'group_count': entry['group_count'],
            'favorite_count': entry['favorite_count'],
            # 这个名字下的分组平均装了几条收藏。问的是「拿它当收纳盒还是当标签」。
            'average_size': round(
                entry['favorite_count'] / entry['group_count'], 1,
            ) if entry['group_count'] else None,
        })
    rows.sort(key=lambda row: (-row['user_count'], -row['favorite_count'], row['label']))
    return rows


def _recommendations(scale_rows, size_shape, empty_rows, penetration, name_rows,
                     grouping_latency, group_total):
    """可以做什么，每一条都挂在替它说话的数字上。只报告，不改任何分组。"""
    notes = []
    if size_shape.get('has_comparison') and size_shape.get('kind') == 'skewed':
        notes.append(
            f'分组主要被当成「{size_shape["strongest_label"]}」来用：这一档占了全部分'
            f'组的 {size_shape["strongest_share"]}%，而 {size_shape["weakest_label"]} '
            f'只有 {size_shape["weakest_share"]}%。它指向一个具体的引导方式——如果多数'
            '分组都只装一两条，说明大家在拿它当临时标签而不是收纳盒，心愿单页上的分组建'
            '议和默认名该按这个用法改。但本模块只报告，不预设任何分组名、不改任何一条'
            ' Favorite.collection。'
        )
    if empty_rows['stale_empty_total'] and empty_rows['empty_total']:
        notes.append(
            f'有 {empty_rows["stale_empty_total"]} 个分组建于 '
            f'{empty_rows["grace_days"]} 天之前、至今一条收藏都没有，涉及 '
            f'{empty_rows["stale_user_count"]} 个用户，占全部空分组的 '
            f'{empty_rows["stale_share"]}%。这是「建了分组不会用」最直接的证据，但本模块'
            '只报告，不删除任何分组、不替用户合并分组。'
        )
    if penetration['collector_share'] is not None and not penetration['collector_share']:
        notes.append(
            '有收藏的用户里一个都没建过分组。分组功能的入口（心愿单页上的新建表单）可能'
            '不够显眼，但本模块只报告，不改 FavoriteCollectionForm 的任何校验、不动页面'
            '结构。'
        )
    if penetration['collector_only_count']:
        notes.append(
            f'有 {penetration["collector_only_count"]} 个人建了分组却一条收藏都没有。他们'
            '用了分组功能、但分组是空的——既不该被渗透率吞掉，也不算「看不到分组功能」。'
            '但本模块只报告，不替他们合并分组、不删空分组。'
        )
    if name_rows:
        top = name_rows[0]
        notes.append(
            f'「{top["label"]}」是被最多人用过的分组名（{top["user_count"]} 个用户）。'
            '如果几个名字反复出现，说明站内缺一份预设分组名，让每个人都自己打一遍。但本'
            '模块只报告，不预设任何分组名。'
        )
    latency = grouping_latency
    if latency['grouped_ever_total'] >= MIN_BAND_SAMPLE:
        # 分母是「归过类的收藏」而不是全部收藏：问的是「归了类的人是什么时候归
        # 的」，没归过类的收藏回答不了这个问题。样本门槛借 MIN_BAND_SAMPLE 的
        # 量级——几条收藏的形状读不出倾向，而这不是隐私线，是样本线。
        instant_share = _share_or_none(
            latency['instant_total'], latency['grouped_ever_total'],
        )
        if instant_share is not None:
            if instant_share >= 50:
                notes.append(
                    f'归过类的收藏里有 {instant_share}% 是在 {latency["grace_minutes"]} '
                    f'分钟内当场归的（{latency["instant_total"]}/'
                    f'{latency["grouped_ever_total"]} 条）。用户收藏时心里已经想好了'
                    '这东西属于哪一类——分组该出现在收藏动作的当场，而不是只留在心愿'
                    '单页面上。但本模块只报告，不改收藏表单、不动页面结构。'
                )
            else:
                notes.append(
                    f'归过类的收藏里只有 {instant_share}% 是在 {latency["grace_minutes"]} '
                    f'分钟内当场归的（{latency["instant_total"]}/'
                    f'{latency["grouped_ever_total"]} 条），其余都是囤了一阵才整理的。'
                    '分组对他们是事后整理的工具，不是收藏那一刻的分类——那么该补的是'
                    '批量整理的入口，而不是在收藏表单里多加一个下拉。但本模块只报告，'
                    '不改任何页面、不替任何人整理。'
                )
    if latency['released_total']:
        notes.append(
            f'有 {latency["released_total"]} 条收藏归过类又被移了出来。他们试过了分组'
            '又退回去——这比「从未归类」是更强的信号，说明分组对他们没有价值。但本模块'
            '只报告，不删任何分组、不改任何一条 Favorite.collection。'
        )
    if not notes:
        if not group_total:
            notes.append('站内还没有任何心愿单分组。第一个分组建出来之后，这里会开始统计。')
        else:
            notes.append(
                '分组的规模和形状没有明显背离，可继续观察更长的周期。'
            )
    return notes


def build_favorite_collection_quality(days=30, now=None):
    """量心愿单分组：多少人建了分组，分组被当成什么用。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localtime(now).date())

    collections = _load_collections()
    window_collection_count, window_favorite_count = _load_window_counts(start, now)
    group_total, favorite_total, favoriter_ids, collector_ids = _load_current_totals()
    scale_rows = _build_scale_rows(collections)
    size_shape = _build_size_shape(scale_rows, group_total)
    empty_rows = _build_empty_rows(collections, now)
    penetration = _build_penetration(favorite_total, favoriter_ids, collector_ids)
    name_rows = _build_name_rows(collections)
    grouping_latency = _build_grouping_latency_rows(now)
    recommendations = _recommendations(
        scale_rows, size_shape, empty_rows, penetration, name_rows,
        grouping_latency, group_total,
    )

    total = len(collections)
    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(collections),
        'summary': (
            f'最近 {days} 天新建了 {window_collection_count} 个分组、'
            f'{window_favorite_count} 条收藏；当前快照共有 {total} 个分组、'
            f'{favorite_total} 条收藏，其中 {penetration["grouped_favorite_total"]} 条已归'
            f'入分组。'
            if collections else f'最近 {days} 天站内没有任何心愿单分组。'
        ),
        'metrics': {
            'window_collection_count': window_collection_count,
            'window_favorite_count': window_favorite_count,
            'group_total': group_total,
            'favorite_total': favorite_total,
            'empty_group_total': empty_rows['empty_total'],
            'stale_empty_total': empty_rows['stale_empty_total'],
            'empty_grace_days': EMPTY_GROUP_GRACE_DAYS,
            'min_band_sample': MIN_BAND_SAMPLE,
            'min_name_sample': MIN_NAME_SAMPLE,
            'noise_points': NOISE_POINTS,
            'instant_grouping_grace_minutes': INSTANT_GROUPING_GRACE_MINUTES,
        },
        'scale_rows': scale_rows,
        'size_shape': size_shape,
        'empty_rows': empty_rows,
        'penetration': penetration,
        'name_rows': name_rows,
        'grouping_latency': grouping_latency,
        'recommendations': recommendations,
    }

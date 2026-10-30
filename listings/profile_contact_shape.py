# -*- coding: utf-8 -*-
"""联系方式的形状：四个手写字段，第一次被数字读到。

``Profile`` 上有四个由用户亲手填写的字段：``phone``（15 字）、``wechat``（50）、
``bio``（500）、``student_id``（20）。四个都有表单入口（``ProfileUpdateForm``），
四个都挂着 ``max_length``——也就是说四个都有规则。但站内读它们的地方屈指可数：

* ``phone`` 和 ``wechat`` 只有一处展示：会话侧栏的「联系方式」块
  （``templates/chat_messages/conversation.html``）。要双方已经谈起一件事，才看得见
  对方留没留电话。
* ``bio`` 出现在四个模板里，是全站曝光最多的一个自由文本字段。
* ``student_id`` 除了 ``accounts/profile.html`` 自己的编辑表单，**全站零展示**：
  它被采集，然后没有任何一个页面、任何一个数字再读它。

四个字段在业务模块里的读者数为零。于是一组带着长度上限的采集规则，运营看不到它的
形状：不知道有多少人真的填了、填出来的长度是贴着上限还是只写了一两个字、留下联系方式的
人和只逛不谈的人是不是同一批。

本模块补上这一刀。它报四件事：

1. 各字段填写率，分母是全部 Profile，并标注每个字段在站内的展示位置。
2. 站外可触达组合：电话与微信的「两者都有 / 只有电话 / 只有微信 / 都没有」。
3. 长度形状：按各字段自己的 ``max_length`` 分四档，另报每个字段的最大观测长度与
   贴上限占比。
4. 联系方式与交易参与的相关性：按「有没有站外联系方式」分两组，报组内有过对话、
   下过买入订单、发布过商品的人数。

## 一、为什么按全量算，不按周期切

联系方式是累计状态，不是流量。一个人三个月前留下的微信，这个月不会消失。按周期切
会让「本期新填」和「此刻留着的联系方式」混成同一个数，而运营问的是后者——此刻站内
有多大比例的会话是「看得见对方电话」的。

所以本模块的分母是**全量**，和 ``analytics`` 的周期口径明确分工：它报流量，本模块
报结构。``days`` 参数只为和看板其余面板的接口保持一致而收下，不做任何过滤。

## 二、分母是全部 Profile，不是有过交易的用户

按「有过交易的用户」当分母，会把「留不留联系方式」和「参不参与交易」焊死成一个
问题，而第四节正是要把它们拆开来看。所以前三节的分母一律是全部 Profile 行。

``create_user_profile`` 信号给每个新建用户建一行 Profile，但历史上直接
``User.objects.create`` 或 ``createsuperuser`` 造出来的用户可能没有。没有的那一行
不在分母里，也**不摊派进任何一档**——它单独数出来（``orphan_user_count``）。
一个数不上的分母比一个错的分母好。

## 三、空白算未填

``strip()`` 之后为空串的一律算未填。四个字段都允许 ``blank=True``，一个只打了几个
空格再保存的用户，事实是没有留联系方式。不 strip 会让「留了联系方式」的人数被空格
撑大，而空格在会话侧栏渲染出来是空的。

## 四、长度档位取模型上的 max_length，不取分位数

四档是各字段 ``max_length`` 的固定比例：不足一成、一成到五成、五成到九成、贴上限
（≥ 90%）。换算成字符数，``phone`` 的九成是 14 字，``wechat`` 是 45，``bio`` 是
450，``student_id`` 是 18。这些数字表单上不显示，但它们在模型里写着，是产品规则。

**不取分位数**：一个每月都会移动的分位边界什么也说明不了，而这个数要能回答「上限
是不是该调」「``student_id`` 的 20 字是不是太短」——只有固定档能回答这个问题。

档位互斥、左闭右开、全列，包括 0 样本档：「没有人的简介写到 450 字以上」本身是
结论，不是表格的缺口。

## 五、站外可触达只数 phone 和 wechat

「站外可触达」指的是交易双方可以绕开站内私信直接找到彼此。``bio`` 是给人看的自我
介绍，``student_id`` 站内没有任何地方读它——把这两个算进「可触达」，这一节回答的
就不是它想问的问题了。

## 六、只报相关性，不讲因果

第四节把「有没有站外联系方式」和「有没有对谈、下单、发布」放在同一张表上。这是两个
变量在这批用户里的共同变化，不是因果。

**「留了联系方式的人更可能交易」这句话本模块不说，也不暗示。** 可能是留了联系方式
的人本来就更认真，也可能是交易过的人更愿意留——两种都解释得通，本模块没有能力区分。
**反过来也不说**：没留联系方式的人可能只是不需要，不是不参与。

## 七、不做什么

- 不加迁移、不加字段、不改模型、不改任何一个 ``max_length``。只读。
- 不输出任何字段值。不列用户名、不列手机号、不列微信号、不列学号、不读 ``bio`` 的
  正文——它是自由文本，读它会牵出「谁写了什么」。本模块只报它的长度。
- 不按用户排名、不输出任何名单。
- 不判断「微信号是不是真的」「电话格式对不对」。``phone`` 是 ``CharField``，
  站内没有任何电话格式校验器，一个填到 15 字的用户只是刚好撞上 ``max_length``，
  不代表那串数字能打通。本模块只报长度形状，不报有效性。
- 不报「改过几次」。``Profile`` 上没有 ``updated_at``，也没有字段变更历史表，这个数
  不知道，不编。
- 不自动补全、不提醒任何人填写、不因为某个字段填写率低就去改表单或改默认值。
- 不读 ``avatar``。
"""

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Profile
from chat_messages.models import PrivateMessage

from .borrow_risk import _rate
from .models import Item, Order

# 本模块全量口径，days 只为接口一致收下，见模块开头第一节。取值集合与看板下拉一致，
# 是为了让面板上的「回看 N 天」字段读起来不自相矛盾。
DEFAULT_DAYS = 30
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一个字段的填写率要给方向，至少这么多份 Profile。低于这个数，一个百分点的波动
# 就是一个人。
MIN_FIELD_SAMPLE = 12

# 一组（留了 / 没留站外联系方式）的参与率要给方向，至少这么多用户。
MIN_GROUP_SAMPLE = 12

# 百分点差值不满此值视为噪声，不给方向。全站同一套。
NOISE_POINTS = 8

# 「贴上限」取上限的九成。
NEAR_LIMIT_RATIO = 0.9

# 长度档：max_length 的固定比例。左闭右开，最后一档兜住贴上限的。全列，含 0 样本档。
LENGTH_BANDS = (
    (0.0, 0.1, '不足一成'),
    (0.1, 0.5, '一成到五成'),
    (0.5, 0.9, '五成到九成'),
    (0.9, None, '贴上限（≥ 90%）'),
)

# 四个手写字段的采集上限与展示位置。上限取模型原值，展示位置按模板实测逐个核对。
# student_id 的展示位置是空事实：全站只有它自己的编辑表单出现它。
CONTACT_FIELD_META = (
    ('phone', '电话', 15, '会话侧栏「联系方式」块'),
    ('wechat', '微信', 50, '会话侧栏「联系方式」块'),
    ('bio', '个人简介', 500, '资料页、公开资料、会话侧栏、商品详情'),
    ('student_id', '学号', 20, '全站零展示，只有自己的编辑表单读它'),
)

# 站外可触达字段。bio 与 student_id 不算，见模块开头第五节。
CONTACT_FIELDS = ('phone', 'wechat')

# 站外可触达的四种组合。全列，包括 0 样本。
REACH_ROWS = (
    ('both', '电话和微信都留了'),
    ('phone_only', '只留了电话'),
    ('wechat_only', '只留了微信'),
    ('neither', '都没留'),
)

# 第四节的三项参与，全量口径、不限周期。三项互相独立，一个人可以同时占三项。
PARTICIPATION_ROWS = (
    ('messaged', '有过对话'),
    ('bought', '下过买入订单'),
    ('listed', '发布过商品'),
)


def _filled(value):
    """空白算未填。见模块开头第三节。"""
    return bool((value or '').strip())


def _near_limit_chars(max_length):
    """上限的九成换算成字符数。

    用整数算术，不用 ``math.ceil(max_length * 0.9)``：0.9 在二进制浮点里比自己大
    一点点，``50 * 0.9`` 会算出 45.000000000000004，ceil 之后变成 46，而长度为
    45 本来正好压线。用整数算，50 的九成就是 45。
    """
    return -(-9 * int(max_length) // 10)


def _band_label(ratio, bands):
    """分档标签。桶是 (下界比例, 上界比例, 标签) 的元组，互斥、左闭右开。

    上界为 None 的那一档是最后一档，兜住所有超过前一档的样本。
    """
    for lower, upper, label in bands:
        if ratio >= lower and (upper is None or ratio < upper):
            return label
    return bands[-1][2]


def _fetch_profiles():
    """全部 Profile，连 user_id 一起取回来。

    一次查完固定下来：四节各读一遍会让每个字段多打好几次库。``bio`` 的正文也
    一起取了，但只用它的长度，不输出内容。
    """
    return list(
        Profile.objects.values('user_id', 'phone', 'wechat', 'bio', 'student_id')
    )


def _field_rows(profiles):
    """各字段填写率。

    分母是全部 Profile，包括一个字段都没填的人：这一节问的是「填的人占几成」，
    不是「填了的人里有什么形状」。展示位置一并报出来——读者要知道一个填写率
    背后其实是几次曝光机会。
    """
    total = len(profiles)
    rows = []
    for name, label, max_length, shown_in in CONTACT_FIELD_META:
        count = sum(1 for profile in profiles if _filled(profile[name]))
        rows.append({
            'key': name,
            'label': label,
            'max_length': max_length,
            'filled_count': count,
            'share': _rate(count, total),
            'shown_in': shown_in,
            'is_contact_field': name in CONTACT_FIELDS,
            # 展示位置是不是空事实。student_id 的 shown_in 写的就是「零展示」，
            # 所以这个标记和那句说明同源，不会各说各话。
            'has_display_surface': '零展示' not in shown_in,
            'is_small_sample': count < MIN_FIELD_SAMPLE,
            'note': (
                '采集后全站零展示，只有自己的编辑表单读它'
                if name == 'student_id' else '展示位置：' + shown_in
            ),
        })
    return rows


def _reach_rows(profiles):
    """站外可触达的四种组合。全列，包括 0 样本。"""
    total = len(profiles)
    counts = {key: 0 for key, _label in REACH_ROWS}
    for profile in profiles:
        has_phone = _filled(profile['phone'])
        has_wechat = _filled(profile['wechat'])
        if has_phone and has_wechat:
            counts['both'] += 1
        elif has_phone:
            counts['phone_only'] += 1
        elif has_wechat:
            counts['wechat_only'] += 1
        else:
            counts['neither'] += 1

    rows = []
    for key, label in REACH_ROWS:
        count = counts[key]
        rows.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _rate(count, total),
            'is_reachable': key != 'neither',
            'is_small_sample': count < MIN_FIELD_SAMPLE,
        })
    return rows


def _length_rows(profiles):
    """每个字段的长度形状 vs 它自己的 max_length。

    分母是填了该字段的人数，不是全部 Profile：没填的人没有长度，摊派进任何一档
    都会把「不足一成」撑成一个假的多数。填写率在第一节，两节分母不同，面板上
    必须写明，否则读者会拿两个占比对账。
    """
    rows = []
    for name, label, max_length, _shown_in in CONTACT_FIELD_META:
        lengths = [
            len(profile[name].strip()) for profile in profiles if _filled(profile[name])
        ]
        total = len(lengths)
        counts = {band: 0 for _lower, _upper, band in LENGTH_BANDS}
        for length in lengths:
            counts[_band_label(length / max_length, LENGTH_BANDS)] += 1

        near_limit_count = counts[LENGTH_BANDS[-1][2]]
        rows.append({
            'key': name,
            'label': label,
            'max_length': max_length,
            'near_limit_chars': _near_limit_chars(max_length),
            'sample_count': total,
            'max_seen': max(lengths) if lengths else None,
            'near_limit_count': near_limit_count,
            'near_limit_share': _rate(near_limit_count, total),
            'is_small_sample': total < MIN_FIELD_SAMPLE,
            'band_rows': [{
                'label': band,
                'count': counts[band],
                'share': _rate(counts[band], total),
                'is_small_sample': counts[band] < MIN_FIELD_SAMPLE,
            } for _lower, _upper, band in LENGTH_BANDS],
        })
    return rows


def _participant_ids():
    """三项参与的用户 id 集合。

    三项各自独立取，不做并集也不做交集：第四节要把它们分开报，合起来就看不见
    「只逛不谈」和「只发不买」是不是同一批人。全量口径，不限周期。
    """
    return {
        'messaged': (
            set(PrivateMessage.objects.values_list('sender_id', flat=True))
            | set(PrivateMessage.objects.values_list('receiver_id', flat=True))
        ),
        'bought': set(Order.objects.values_list('buyer_id', flat=True)),
        'listed': set(Item.objects.values_list('seller_id', flat=True)),
    }


def _participation_rows(profiles, participant_ids):
    """按「有没有站外联系方式」分两组，报组内三项参与的规模。

    两组都报、档位全列，包括 0 样本组：一个平台完全可能此刻没有任何人留微信，
    那是一个结论，不是表格的缺口。
    """
    total = len(profiles)
    groups = (
        ('reachable', '留了站外联系方式',
         [p for p in profiles if _filled(p['phone']) or _filled(p['wechat'])]),
        ('unreachable', '没留站外联系方式',
         [p for p in profiles
          if not (_filled(p['phone']) or _filled(p['wechat']))]),
    )
    rows = []
    for key, label, group in groups:
        group_total = len(group)
        user_ids = {profile['user_id'] for profile in group}
        items = []
        for part_key, part_label in PARTICIPATION_ROWS:
            count = len(user_ids & participant_ids[part_key])
            items.append({
                'key': part_key,
                'label': part_label,
                'count': count,
                'share': _rate(count, group_total),
                'is_small_sample': count < MIN_GROUP_SAMPLE,
            })
        rows.append({
            'key': key,
            'label': label,
            'user_count': group_total,
            'share': _rate(group_total, total),
            'is_small_sample': group_total < MIN_GROUP_SAMPLE,
            'items': items,
        })
    return rows


def _summary(field_rows, reach_rows, length_rows, participation_rows, total, orphan_count):
    """一句话说清这组字段此刻的形状。"""
    if not total:
        return '站内还没有任何一份用户资料，联系方式的形状无从谈起。'

    parts = ['%d 份资料里' % total]
    filled = [row for row in field_rows if row['filled_count']]
    if filled:
        top = max(filled, key=lambda row: row['filled_count'])
        parts.append('%s填得最多（%s%%）' % (top['label'], top['share']))
    else:
        parts.append('四个字段一个都没人填')

    empty = [row for row in field_rows if not row['filled_count']]
    if empty:
        parts.append('%s一个人都没填' % '、'.join(row['label'] for row in empty))

    if participation_rows:
        parts.append('%s%% 留了站外联系方式' % participation_rows[0]['share'])

    near = [row for row in length_rows if row['near_limit_count']]
    if near:
        parts.append('%s有人写到贴上限' % '、'.join(row['label'] for row in near))

    small = [row for row in field_rows if row['is_small_sample']]
    if small:
        parts.append(
            '%s 的填写数不足 %d 份，占比只作参考'
            % ('、'.join(row['label'] for row in small), MIN_FIELD_SAMPLE)
        )
    if orphan_count:
        parts.append('另有 %d 个用户没有资料行，不在分母里' % orphan_count)
    return '；'.join(parts) + '。'


def build_profile_contact_shape(days=DEFAULT_DAYS, now=None):
    """统计联系方式字段的填写、组合、长度与参与相关性，只报告不改写。

    ``days`` 只为和看板其余面板的口径字段保持一致而收下，本模块不按周期过滤任何
    数据——联系方式是累计状态，见模块开头第一节。
    """
    allowed_days = {value for value, _label in PERIOD_CHOICES}
    if days not in allowed_days:
        days = DEFAULT_DAYS
    now = now or timezone.now()

    profiles = _fetch_profiles()
    field_rows = _field_rows(profiles)
    reach_rows = _reach_rows(profiles)
    length_rows = _length_rows(profiles)
    participant_ids = _participant_ids()
    participation_rows = _participation_rows(profiles, participant_ids)

    return {
        'days': days,
        'period_end': now,
        'profile_count': len(profiles),
        'orphan_user_count': User.objects.filter(profile__isnull=True).count(),
        'has_data': bool(profiles),
        'summary': _summary(
            field_rows, reach_rows, length_rows, participation_rows,
            len(profiles), User.objects.filter(profile__isnull=True).count(),
        ),
        'field_rows': field_rows,
        'reach_rows': reach_rows,
        'length_rows': length_rows,
        'participation_rows': participation_rows,
        # 面板首屏四个小卡片要用的标量。都在上面几张表里算过，这里只是取出来，
        # 不另立一套口径。
        'reachable_share': participation_rows[0]['share'] if participation_rows else None,
        'near_limit_field_count': sum(1 for row in length_rows if row['near_limit_count']),
        'zero_display_fields': [
            row['label'] for row in field_rows if not row['has_display_surface']
        ],
        'contact_fields': list(CONTACT_FIELDS),
        'length_bands': [label for _lower, _upper, label in LENGTH_BANDS],
        'min_field_sample': MIN_FIELD_SAMPLE,
        'min_group_sample': MIN_GROUP_SAMPLE,
        'noise_points': NOISE_POINTS,
    }

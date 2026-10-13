# 通知类型的两个窟窿，补上

## 起点

`notification-kind-coverage-design.md` 上一轮把 `Notification.KIND_CHOICES`、
`NotificationPreference` 的布尔开关、`NotificationPreferenceForm` 的字段列表
并排放，让两个对不上的窟窿浮了出来，并在文档里明确写下：那两个窟窿的修法各是
一行代码，但那是修复，不是度量，修不修、什么时候修，是另一个决定。

本轮就是那个决定。两个都修。

修完之后三个表对齐，`notification_kind_coverage` 的差集变成空。那个度量模块的
存在意义也因此从「报窟窿」变成「守住对齐」——这是本轮真正想达到的状态：一个
度量模块的价值不在它报出多少问题，而在它报的问题被修完。

## 窟窿一：`mutual_aid_feedback` 是一个正在被发送、却没有名字的类型

`NotificationPreference` 上有 `mutual_aid_feedback` 这个布尔开关，
`NotificationPreferenceForm.Meta.fields` 里也有那一行，用户在偏好页面上看得见
它、关得掉它。而 `Notification.KIND_CHOICES` 的 24 个值里没有它。

它在 `views.py` 的两处被真实发送：

- 求购侧的互助结果反馈；
- 失物招领侧的互助结果反馈。

两处都走 `create_notification`，而 `create_notification` 的偏好闸门是
`getattr(preference, kind, True)`——模型上确实有这个字段，所以**开关是真生效的**。
缺的只是 `choices` 里那一行，后果有三层：

1. `notification_list` 的类型筛选器拿 `KIND_CHOICES` 当白名单
   （`views.py` 的 `allowed_kinds`），所以这一类通知**在用户自己的通知中心里
   按类型筛不出来**。它照样进来、照样进角标、照样能被点开，但类型下拉里没有它。
2. 看板上凡是读 `KIND_CHOICES` 当标签表的地方（`notification_response`、
   `notification_snooze_return`）都会落进 `kind_labels.get(kind, kind)` 的
   fallback，把英文键名直接显示给运营。
3. 偏好页面上那个开关因此变成一个谜：用户关掉了一个在看板上叫
   `mutual_aid_feedback` 的东西，而页面上它的 label 是中文的。

## 窟窿二：`meeting_incident` 是一个用户关不掉的开关

反方向。`NotificationPreference` 上有 `meeting_incident`，模型上有、默认开着、
`views.py` 的两处真的在发。而 `NotificationPreferenceForm.Meta.fields` 的字段
列表里没有它。

模板 `notification_preferences.html` 是 `{% for field in form.visible_fields %}`，
表单里没有的字段，页面上就没有那一行。所以交付预约异常的提醒，用户收到了，但
偏好页面上没有任何一个开关能关掉它。

这不是「默认开着所以不用管」。旁边二十多个类型的开关全在页面上，用户形成的心智
是「这一页能管我的通知」。一个 Silent 的例外破坏的是这个心智本身——用户会去别的
地方找这个开关，找不到，然后认为这个产品管不了自己的通知。

## 为什么两个修法不一样

- 第一个是**数据层的名字**：补一行 `KIND_CHOICES` 就够了。`choices` 只参与校验
  和展示，不改表结构，所以不需要迁移。
- 第二个是**表单层的暴露**：补一行 `Meta.fields` 就够了。模型上那个布尔字段早就
  存在，只是从来没被摆出来，所以也不需要迁移。

两个都不动 `create_notification`、不动任何发送路径、不给任何人自动打开或关闭开关。

## 修完之后要守住的东西

`notification_kind_coverage` 的三个来源全部从代码现读，所以窟窿补上之后它的差集
自动变空，不需要改那个模块。但「自动变空」和「永远是空的」是两件事——下一次有人
新增一种通知类型，很可能只改一处。所以本轮给那个模块补一条测试：三个来源必须对齐，
不对齐就报出来。这条测试守住的是未来，不是本轮。

## 不做什么

- 不加迁移。两个修法都不改表结构。
- 不动 `create_notification` 的偏好闸门。`getattr(preference, kind, True)` 的
  回落行为是本模块观测的对象，改掉它观测对象就没了。
- 不新增通知类型。本轮只是给已经在发的类型补名字、给已经在模型上的开关补暴露。
- 不改 `notification_kind_coverage` 的任何口径。它的差集变空是修的自然结果，
  不是改口径改出来的。
